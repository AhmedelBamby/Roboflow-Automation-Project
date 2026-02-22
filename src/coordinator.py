"""
URL Coordinator — cross-process coordination for Phase 2 parallel strategies.

Principle 3 (Failure Memory): Records held/done/failed state per job URL.
Principle 1 (Layered Verification): Layer 2 ground truth for collection decisions.

Two classes:
  URLCoordinator  — real implementation backed by a JSON file + filelock
  NullCoordinator — no-op drop-in when enable_coordination: false

Usage:
    from src.coordinator import build_coordinator
    coordinator = build_coordinator(config)

    # Before processing a URL:
    if coordinator.is_available(url):
        coordinator.claim(url, holder="top_down")
        ... process ...
        coordinator.mark_done(url)
    else:
        skip  # already held / done / failed by another process
"""

import json
import hashlib
import logging
import os
import time
from typing import Optional
from urllib.parse import quote

import requests as _requests

from src.utils import get_worker_id

logger = logging.getLogger("roboflow_batch")

# ── Status constants ──────────────────────────────────────────────────────
STATUS_HELD   = "held"
STATUS_DONE   = "done"
STATUS_FAILED = "failed"


class URLCoordinator:
    """
    Thread/process-safe coordination via a shared JSON file + filelock.

    The coordination file has this structure:
    {
      "https://app.roboflow.com/.../job/abc123": {
        "status":     "held" | "done" | "failed",
        "holder":     "top_down" | "bottom_up",
        "claimed_at": <unix timestamp>,
        "updated_at": <unix timestamp>,
        "error":      "..." (only on failed)
      },
      ...
    }

    Stale-entry handling: A "held" entry older than stale_timeout seconds
    is reclaimed automatically by the next process that encounters it.
    This recovers cleanly from process crashes.
    """

    def __init__(self, filepath: str, stale_timeout: int = 1800):
        """
        Args:
            filepath: Absolute path to the coordination JSON file.
            stale_timeout: Seconds before a "held" claim is considered
                           abandoned (process crashed). Default: 1800 (30 min).
        """
        try:
            from filelock import FileLock
        except ImportError as e:
            raise ImportError(
                "filelock package is required for coordination. "
                "Install it with: pip install filelock>=3.0"
            ) from e

        self._filepath = filepath
        self._lockpath = filepath + ".lock"
        self._stale_timeout = stale_timeout
        self._lock = FileLock(self._lockpath, timeout=30)
        self.enabled = True

    # ── Public API ────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Wipe the coordination file (fresh session start)."""
        with self._lock:
            self._write({})
        logger.info(f"Coordination file reset: {self._filepath}")

    def claim(self, url: str, holder: str) -> bool:
        """
        Atomically claim a URL for processing.

        Returns True if claim was granted (URL was unclaimed / stale).
        Returns False if another process holds it (or it's already done/failed).

        This is the atomic check-and-set — no TOCTOU race is possible
        because the entire read-modify-write happens under the file lock.
        """
        with self._lock:
            data = self._read()
            entry = data.get(url)

            if entry is not None:
                status = entry.get("status")
                if status in (STATUS_DONE, STATUS_FAILED):
                    return False  # Already processed — skip
                if status == STATUS_HELD:
                    age = time.time() - entry.get("updated_at", 0)
                    if age < self._stale_timeout:
                        return False  # Actively held by another process
                    # Stale — reclaim
                    logger.info(
                        f"  [coord] Reclaiming stale claim on {url[-20:]} "
                        f"(held by '{entry.get('holder')}' for {age:.0f}s)"
                    )

            now = time.time()
            data[url] = {
                "status":     STATUS_HELD,
                "holder":     holder,
                "claimed_at": entry.get("claimed_at", now) if entry else now,
                "updated_at": now,
            }
            self._write(data)
            logger.debug(f"  [coord] Claimed {url[-30:]} for '{holder}'")
            return True

    def batch_claim(self, urls: list, holder: str) -> tuple:
        """Batch claim — processes all URLs under one lock acquisition."""
        granted, denied = [], []
        for url in urls:
            if self.claim(url, holder):
                granted.append(url)
            else:
                denied.append(url)
        return granted, denied

    def mark_done(self, url: str) -> None:
        """Mark a URL as successfully processed."""
        self._update_status(url, STATUS_DONE)
        logger.debug(f"  [coord] Done: {url[-30:]}")

    def mark_failed(self, url: str, error: str = "") -> None:
        """Mark a URL as permanently failed."""
        with self._lock:
            data = self._read()
            entry = data.get(url, {})
            data[url] = {
                **entry,
                "status":     STATUS_FAILED,
                "updated_at": time.time(),
                "error":      error[:200],  # truncate for readability
            }
            self._write(data)
        logger.debug(f"  [coord] Failed: {url[-30:]}  — {error[:60]}")

    def is_available(self, url: str) -> bool:
        """
        Return True if this URL is safe to process (unclaimed, stale, or failed
        and available for a fresh attempt by this process).

        Rules:
          - Not in file → available
          - status=done → NOT available (already processed)
          - status=held, age < stale_timeout → NOT available
          - status=held, age >= stale_timeout → available (reclaim on next claim())
          - status=failed → available (give it another try from a fresh direction)
        """
        with self._lock:
            data = self._read()
            entry = data.get(url)

        if entry is None:
            return True
        status = entry.get("status")
        if status == STATUS_DONE:
            return False
        if status == STATUS_HELD:
            age = time.time() - entry.get("updated_at", 0)
            return age >= self._stale_timeout  # stale → available
        if status == STATUS_FAILED:
            return True  # Failed entries get one more chance
        return True  # Unknown status → allow

    def get_status(self, url: str) -> Optional[dict]:
        """Return the full status entry for a URL, or None if not tracked."""
        with self._lock:
            data = self._read()
        return data.get(url)

    def count_by_status(self, status: str) -> int:
        """Return how many URLs have the given status."""
        with self._lock:
            data = self._read()
        return sum(1 for e in data.values() if e.get("status") == status)

    def get_summary(self) -> dict:
        """Return a {status: count} summary of all tracked URLs."""
        with self._lock:
            data = self._read()
        summary: dict[str, int] = {STATUS_HELD: 0, STATUS_DONE: 0, STATUS_FAILED: 0}
        for entry in data.values():
            s = entry.get("status", "unknown")
            summary[s] = summary.get(s, 0) + 1
        return summary
    def get_snapshot(self) -> dict:
        """Return all URLs grouped by status from the local coordination file."""
        with self._lock:
            data = self._read()
        groups: dict = {STATUS_HELD: {}, STATUS_DONE: {}, STATUS_FAILED: {}}
        for url, entry in data.items():
            s = entry.get("status", "unknown")
            if s in groups:
                groups[s][url] = {"holder": entry.get("holder", ""), "worker": ""}
        return groups
    # ── Private helpers ───────────────────────────────────────────────────

    def _read(self) -> dict:
        """Read and return the current coordination data. Caller holds lock."""
        if not os.path.exists(self._filepath):
            return {}
        try:
            with open(self._filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logger.warning(f"Coordination file corrupt or unreadable — starting fresh")
            return {}

    def _write(self, data: dict) -> None:
        """Write coordination data atomically. Caller holds lock."""
        # Write to temp file first, then rename (atomic on most OS)
        tmp = self._filepath + ".tmp"
        try:
            os.makedirs(os.path.dirname(self._filepath), exist_ok=True) if os.path.dirname(self._filepath) else None
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._filepath)
        except Exception as e:
            logger.warning(f"Failed to write coordination file: {e}")
            try:
                os.unlink(tmp)
            except Exception:
                pass

    def _update_status(self, url: str, new_status: str) -> None:
        """Update an existing entry's status. Caller should NOT hold lock."""
        with self._lock:
            data = self._read()
            entry = data.get(url, {})
            data[url] = {
                **entry,
                "status":     new_status,
                "updated_at": time.time(),
            }
            self._write(data)


# ═════════════════════════════════════════════════════════════════════════
#  NullCoordinator — no-op drop-in when coordination is disabled
# ═════════════════════════════════════════════════════════════════════════

class NullCoordinator:
    """
    Drop-in coordinator that does nothing.

    Used when enable_coordination: false in config.yaml.
    All methods return the "proceed normally" answer so all
    call sites can unconditionally call coordinator without
    if/else guards.
    """

    enabled = False

    def reset(self) -> None:
        pass

    def claim(self, url: str, holder: str) -> bool:
        return True  # Always grant claim

    def batch_claim(self, urls: list, holder: str) -> tuple:
        """Batch claim — always grant all in null mode."""
        return list(urls), []  # (granted, denied)

    def mark_done(self, url: str) -> None:
        pass

    def mark_failed(self, url: str, error: str = "") -> None:
        pass

    def is_available(self, url: str) -> bool:
        return True  # Always available

    def get_status(self, url: str) -> Optional[dict]:
        return None

    def count_by_status(self, status: str) -> int:
        return 0

    def get_summary(self) -> dict:
        return {}

    def get_snapshot(self) -> dict:
        return {"held": {}, "done": {}, "failed": {}}

    # ── Remote monitoring no-ops ──────────────────────────────────────

    def send_logs(self, entries: list) -> None:
        pass

    def upload_diagnostic(self, file_path: str, label: str = "") -> bool:
        return False

    def check_code_updates(self) -> list:
        return []

    def pull_code_update(self, path: str) -> bool:
        return False

    def send_heartbeat(self, status: str = "running", **extra) -> None:
        pass
# ═════════════════════════════════════════════════════════════════════════

class HTTPCoordinator:
    """
    Coordination client that talks to the HTTP coordination server.

    Drop-in replacement for URLCoordinator — same interface, HTTP transport.
    All state lives on the server; this client is stateless.

    Retry policy: 1 automatic retry on ConnectionError/Timeout with 2s backoff.
    Graceful degradation: if server is unreachable after retry, return "safe"
    defaults (claim=False, is_available=True) so automation doesn't crash.
    """

    enabled = True

    _TIMEOUT = 10   # seconds per HTTP request (generous for LAN)
    _RETRY_BACKOFF = 2  # seconds to wait before retry

    def __init__(self, server_url: str, stale_timeout: int = 1800):
        self._base = server_url.rstrip("/")
        self._stale_timeout = stale_timeout
        self._worker_id = get_worker_id()

    # ── Internal helpers ──────────────────────────────────────────────────

    def _post(self, path: str, body: dict, *, default=None):
        """POST JSON to the server with retry. Returns parsed JSON or default."""
        url = f"{self._base}{path}"
        for attempt in range(2):
            try:
                r = _requests.post(url, json=body, timeout=self._TIMEOUT)
                r.raise_for_status()
                return r.json()
            except (_requests.ConnectionError, _requests.Timeout) as exc:
                if attempt == 0:
                    logger.warning(f"  [coord-http] POST {path} failed ({exc}), retrying in {self._RETRY_BACKOFF}s…")
                    time.sleep(self._RETRY_BACKOFF)
                else:
                    logger.warning(f"  [coord-http] POST {path} failed after retry — returning default")
                    return default
            except _requests.RequestException as exc:
                logger.warning(f"  [coord-http] POST {path} error: {exc}")
                return default

    def _get(self, path: str, params: dict = None, *, default=None):
        """GET from the server with retry. Returns parsed JSON or default."""
        url = f"{self._base}{path}"
        for attempt in range(2):
            try:
                r = _requests.get(url, params=params, timeout=self._TIMEOUT)
                r.raise_for_status()
                return r.json()
            except (_requests.ConnectionError, _requests.Timeout) as exc:
                if attempt == 0:
                    logger.warning(f"  [coord-http] GET {path} failed ({exc}), retrying in {self._RETRY_BACKOFF}s…")
                    time.sleep(self._RETRY_BACKOFF)
                else:
                    logger.warning(f"  [coord-http] GET {path} failed after retry — returning default")
                    return default
            except _requests.RequestException as exc:
                logger.warning(f"  [coord-http] GET {path} error: {exc}")
                return default

    # ── Public API (identical to URLCoordinator / NullCoordinator) ────────

    def reset(self) -> None:
        """Wipe all coordination state on the server."""
        resp = self._post("/reset", {}, default={"ok": False})
        if resp and resp.get("ok"):
            logger.info(f"  [coord-http] Server state reset — {self._base}")

    def claim(self, url: str, holder: str) -> bool:
        """
        Claim a URL for processing via the server.

        Returns True if granted, False if already held/done/failed.
        On server error → returns False (safe: don't process if unsure).
        """
        resp = self._post("/claim", {"url": url, "holder": holder, "worker": self._worker_id}, default={"ok": False})
        granted = bool(resp and resp.get("ok"))
        if granted:
            logger.debug(f"  [coord-http] Claimed {url[-30:]} for '{holder}'")
        return granted

    def batch_claim(self, urls: list, holder: str) -> tuple:
        """
        Claim multiple URLs in one server round-trip.

        Returns (granted_list, denied_list).
        On server error → returns ([], urls) — deny all to be safe.
        """
        if not urls:
            return [], []
        resp = self._post(
            "/batch_claim",
            {"urls": urls, "holder": holder, "worker": self._worker_id},
            default=None,
        )
        if resp is None:
            # Server unreachable — fall back to individual claims
            logger.warning("  [coord-http] batch_claim failed — falling back to individual claims")
            granted, denied = [], []
            for url in urls:
                if self.claim(url, holder):
                    granted.append(url)
                else:
                    denied.append(url)
            return granted, denied
        return resp.get("granted", []), resp.get("denied", [])

    def mark_done(self, url: str) -> None:
        """Mark a URL as successfully processed."""
        self._post("/done", {"url": url, "worker": self._worker_id}, default={"ok": False})
        logger.debug(f"  [coord-http] Done: {url[-30:]}")

    def mark_failed(self, url: str, error: str = "") -> None:
        """Mark a URL as permanently failed."""
        self._post("/failed", {"url": url, "error": error, "worker": self._worker_id}, default={"ok": False})
        logger.debug(f"  [coord-http] Failed: {url[-30:]}  — {error[:60]}")

    def is_available(self, url: str) -> bool:
        """
        Check if a URL is safe to process.

        On server error → returns True (safe: allow processing if unsure,
        worst case is a duplicate which is better than skipping work).
        """
        resp = self._get("/available", {"url": url}, default={"available": True})
        return bool(resp and resp.get("available", True))

    def get_status(self, url: str) -> Optional[dict]:
        """Return the full status entry for a URL, or None if not tracked."""
        resp = self._get("/status", {"url": url}, default={"entry": None})
        return resp.get("entry") if resp else None

    def count_by_status(self, status: str) -> int:
        """Return how many URLs have the given status."""
        resp = self._get("/count", {"status": status}, default={"count": 0})
        return resp.get("count", 0) if resp else 0

    def get_summary(self) -> dict:
        """Return a {status: count} summary of all tracked URLs."""
        resp = self._get("/summary", default={})
        return resp if resp else {}

    def get_snapshot(self) -> dict:
        """Return all URLs grouped by status from the server (one HTTP call)."""
        empty: dict = {STATUS_HELD: {}, STATUS_DONE: {}, STATUS_FAILED: {}}
        resp = self._get("/snapshot", default=empty)
        return resp if resp else empty

    # ── Remote monitoring methods ─────────────────────────────────────

    def send_logs(self, entries: list) -> None:
        """Push a batch of log entries to the server. Fire-and-forget."""
        if not entries:
            return
        self._post("/logs", {"worker": self._worker_id, "entries": entries}, default={"ok": False})

    def upload_diagnostic(self, file_path: str, label: str = "") -> bool:
        """
        Upload a diagnostic file (screenshot/HTML dump) to the server.

        Uses multipart form data. Fire-and-forget: returns False on failure.
        """
        url = f"{self._base}/diagnostics"
        try:
            with open(file_path, "rb") as f:
                resp = _requests.post(
                    url,
                    files={"file": (os.path.basename(file_path), f)},
                    data={"worker": self._worker_id, "label": label},
                    timeout=self._TIMEOUT,
                )
                resp.raise_for_status()
                return True
        except Exception as exc:
            logger.debug(f"  [coord-http] Diagnostic upload failed: {exc}")
            return False

    def check_code_updates(self) -> list:
        """
        Compare local file hashes against the server's manifest.

        Returns a list of file paths that differ (outdated locally).
        On server error → returns [] (don't block startup).
        """
        resp = self._get("/code/manifest", default={})
        if not resp:
            return []
        # Server returns {"files": [...], "version": N} since code-version was
        # added.  Fall back to treating the response as a raw list for backward
        # compatibility with older server versions.
        if isinstance(resp, dict):
            manifest = resp.get("files", [])
        else:
            manifest = resp  # legacy: server returned a bare list
        if not manifest:
            return []

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        outdated = []
        for entry in manifest:
            remote_path = entry.get("path", "")
            remote_hash = entry.get("sha256", "")
            local_path = os.path.join(root, remote_path)
            if not os.path.exists(local_path):
                outdated.append(remote_path)
                continue
            try:
                with open(local_path, "rb") as f:
                    local_hash = hashlib.sha256(f.read()).hexdigest()
                if local_hash != remote_hash:
                    outdated.append(remote_path)
            except Exception:
                outdated.append(remote_path)

        return outdated

    def pull_code_update(self, path: str) -> bool:
        """
        Download a single file from the server and write it locally.

        Uses atomic write (temp + os.replace) to avoid partial content.
        Returns True on success, False on failure (logged, never raises).
        """
        url = f"{self._base}/code/file"
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        local_path = os.path.join(root, path)
        tmp_path = local_path + ".tmp"
        try:
            resp = _requests.get(url, params={"path": path}, timeout=self._TIMEOUT)
            resp.raise_for_status()
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            with open(tmp_path, "wb") as f:
                f.write(resp.content)
            os.replace(tmp_path, local_path)
            logger.info(f"  [coord-http] Updated: {path}")
            return True
        except Exception as exc:
            logger.warning(f"  [coord-http] Code update failed for {path}: {exc}")
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            return False

    def send_heartbeat(self, status: str = "running", **extra) -> dict:
        """Send a heartbeat ping to the server. Returns the server response."""
        body = {"worker": self._worker_id, "status": status, **extra}
        resp = self._post("/heartbeat", body, default={"ok": False})
        return resp if resp else {"ok": False}
# ═════════════════════════════════════════════════════════════════════════

def build_coordinator(config: dict) -> "URLCoordinator | NullCoordinator | HTTPCoordinator":
    """
    Build and return the appropriate coordinator from config.

    When enable_coordination is false → returns NullCoordinator (zero overhead).
    When enable_coordination is true:
      coordination_mode == "file" → URLCoordinator (file-based, original)
      coordination_mode == "http" → HTTPCoordinator (HTTP client)
    """
    if not config.get("enable_coordination", False):
        logger.info("Coordination: disabled (NullCoordinator)")
        return NullCoordinator()

    mode = config.get("coordination_mode", "file")

    if mode == "http":
        server_url = config.get("coordination_server_url", "http://localhost:8099")
        stale_timeout = config.get("coordination_stale_timeout", 1800)
        coordinator = HTTPCoordinator(server_url, stale_timeout=stale_timeout)

        # SAFETY: HTTP-mode workers must NEVER call reset() — that would
        # wipe the shared server state for ALL connected machines.
        # Use the server CLI flags (--reset / --reset-blacklist) or the
        # dashboard button instead.
        if config.get("coordination_reset_on_start", False):
            logger.warning(
                "coordination_reset_on_start is IGNORED in HTTP mode — "
                "workers must not wipe the shared server.  Use the server "
                "CLI (--reset / --reset-blacklist) or the dashboard button."
            )
        summary = coordinator.get_summary()
        logger.info(
            f"Coordination: HTTP mode, resuming — {server_url}  "
            f"(held={summary.get('held', 0)}, done={summary.get('done', 0)}, "
            f"failed={summary.get('failed', 0)})"
        )
        return coordinator

    # mode == "file" (default, backward-compatible)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    coord_file_rel = config.get("coordination_file", "coordination.json")
    coord_file = os.path.join(root, coord_file_rel)

    stale_timeout = config.get("coordination_stale_timeout", 1800)
    coordinator = URLCoordinator(coord_file, stale_timeout=stale_timeout)

    if config.get("coordination_reset_on_start", True):
        coordinator.reset()
        logger.info(f"Coordination: file mode, fresh start — {coord_file}")
    else:
        summary = coordinator.get_summary()
        logger.info(
            f"Coordination: file mode, resuming — {coord_file}  "
            f"(held={summary.get('held', 0)}, done={summary.get('done', 0)}, "
            f"failed={summary.get('failed', 0)})"
        )

    return coordinator
