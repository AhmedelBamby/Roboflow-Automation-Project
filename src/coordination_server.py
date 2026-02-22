"""
HTTP Coordination Server — cross-machine Phase 2 coordination.

A lightweight Flask server that replaces file-based coordination for
multi-machine setups. Both laptops talk to this server over HTTP,
eliminating filelock / SMB reliability issues.

Usage:
    python -m src.coordination_server [OPTIONS]

Options:
    --host TEXT          Bind address (default: 0.0.0.0)
    --port INT           Bind port (default: 8099)
    --stale-timeout INT  Seconds before held entries become stale (default: 1800)
    --save-interval INT  Seconds between auto-saves to disk (default: 30)
    --data-file TEXT     Path to persistence file (default: coordination.json)
    --reset              Wipe existing data file on startup
"""

import argparse
import collections
import hashlib
import json
import logging
import os
import queue
import threading
import time
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request as flask_request, send_file

# ── Logging ───────────────────────────────────────────────────────────────
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("coordination_server")
logger.setLevel(logging.DEBUG)

# Console handler
_ch = logging.StreamHandler()
_ch.setLevel(logging.INFO)
_ch.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-8s %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_ch)

# File handler
_fh = logging.FileHandler(os.path.join(LOG_DIR, "coordination_server.log"), encoding="utf-8")
_fh.setLevel(logging.DEBUG)
_fh.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-8s %(message)s"))
logger.addHandler(_fh)

# ── Status constants (same as coordinator.py) ─────────────────────────────
STATUS_HELD   = "held"
STATUS_DONE   = "done"
STATUS_FAILED = "failed"

# ── In-memory state ──────────────────────────────────────────────────────
_data: dict = {}           # URL coordination state
_lock = threading.Lock()   # guards _data, _log_store, _workers
_start_time = time.time()

# Log streaming (Section B)
_log_store: dict[str, collections.deque] = {}   # worker_id → deque(maxlen=5000)
_sse_subscribers: list[queue.Queue] = []         # per-subscriber event queues

# Worker heartbeats (Section F)
_workers: dict[str, dict] = {}                   # worker_id → {last_seen, status, ...}

# Code manifest cache (Section D)
_code_manifest: list[dict] = []                  # [{path, sha256, size}, ...]
_code_version: int = 0                           # incremented on each POST /code/manifest

# ── Config (set at startup via CLI args) ──────────────────────────────────
_stale_timeout: int = 1800
_data_file: str = "coordination.json"
_save_interval: int = 30

# ── Flask app ─────────────────────────────────────────────────────────────
app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates"),
)
# Suppress Flask's default request logging — we log manually
log = logging.getLogger("werkzeug")
log.setLevel(logging.WARNING)


# ═══════════════════════════════════════════════════════════════════════════
#  Persistence helpers
# ═══════════════════════════════════════════════════════════════════════════

def _load_from_disk() -> dict:
    """Load coordination data from disk for crash recovery."""
    if not os.path.exists(_data_file):
        return {}
    try:
        with open(_data_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not load {_data_file}: {e} — starting empty")
        return {}


def _save_to_disk() -> None:
    """Save current in-memory state to disk (atomic via temp + rename)."""
    tmp = _data_file + ".tmp"
    try:
        with _lock:
            snapshot = dict(_data)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)
        os.replace(tmp, _data_file)
        logger.debug(f"Auto-saved {len(snapshot)} entries to {_data_file}")
    except Exception as e:
        logger.warning(f"Auto-save failed: {e}")
        try:
            os.unlink(tmp)
        except Exception:
            pass


def _auto_save_loop() -> None:
    """Background thread: save to disk every _save_interval seconds."""
    while True:
        time.sleep(_save_interval)
        _save_to_disk()


# ═══════════════════════════════════════════════════════════════════════════
#  Stale-entry helper
# ═══════════════════════════════════════════════════════════════════════════

def _is_stale(entry: dict) -> bool:
    """Return True if a 'held' entry has exceeded the stale timeout."""
    if entry.get("status") != STATUS_HELD:
        return False
    age = time.time() - entry.get("updated_at", 0)
    return age >= _stale_timeout


# ═══════════════════════════════════════════════════════════════════════════
#  Endpoints
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/health", methods=["GET"])
def health():
    """Health check — verifies the server is running."""
    uptime = int(time.time() - _start_time)
    return jsonify({"status": "ok", "uptime": uptime})


@app.route("/claim", methods=["POST"])
def claim():
    """
    Claim a URL for processing.

    Body: {"url": "...", "holder": "top_down" | "bottom_up"}
    Returns: {"ok": true} if claim granted, {"ok": false} otherwise.
    """
    body = flask_request.get_json(silent=True) or {}
    url = body.get("url", "")
    holder = body.get("holder", "unknown")
    worker = body.get("worker", "unknown")

    if not url:
        return jsonify({"ok": False, "error": "missing url"}), 400

    with _lock:
        entry = _data.get(url)

        if entry is not None:
            status = entry.get("status")
            if status in (STATUS_DONE, STATUS_FAILED):
                logger.debug(f"CLAIM DENIED  {url[-40:]}  (status={status})")
                return jsonify({"ok": False})
            if status == STATUS_HELD and not _is_stale(entry):
                logger.debug(f"CLAIM DENIED  {url[-40:]}  (held by '{entry.get('holder')}')")
                return jsonify({"ok": False})
            if status == STATUS_HELD and _is_stale(entry):
                age = time.time() - entry.get("updated_at", 0)
                logger.info(
                    f"RECLAIM stale  {url[-40:]}  "
                    f"(was '{entry.get('holder')}' for {age:.0f}s) → '{holder}'"
                )

        now = time.time()
        _data[url] = {
            "status":     STATUS_HELD,
            "holder":     holder,
            "worker":     worker,
            "claimed_at": entry.get("claimed_at", now) if entry else now,
            "updated_at": now,
        }

    logger.info(f"CLAIMED       {url[-40:]}  by '{holder}' on {worker}")
    return jsonify({"ok": True})


@app.route("/batch_claim", methods=["POST"])
def batch_claim():
    """
    Atomically claim multiple URLs in one request.

    Body: {"urls": ["..."], "holder": "top_down"|"bottom_up", "worker": "..."}
    Returns: {"granted": ["url1", ...], "denied": ["url2", ...]}

    Each URL is independently checked under the global lock — a single
    round-trip replaces N individual /claim calls.
    """
    body = flask_request.get_json(silent=True) or {}
    urls = body.get("urls", [])
    holder = body.get("holder", "unknown")
    worker = body.get("worker", "unknown")

    if not urls:
        return jsonify({"granted": [], "denied": []})

    granted = []
    denied = []
    now = time.time()

    with _lock:
        for url in urls:
            entry = _data.get(url)

            claimable = True
            if entry is not None:
                status = entry.get("status")
                if status in (STATUS_DONE, STATUS_FAILED):
                    claimable = False
                elif status == STATUS_HELD and not _is_stale(entry):
                    claimable = False

            if claimable:
                _data[url] = {
                    "status":     STATUS_HELD,
                    "holder":     holder,
                    "worker":     worker,
                    "claimed_at": entry.get("claimed_at", now) if entry else now,
                    "updated_at": now,
                }
                granted.append(url)
            else:
                denied.append(url)

    if granted:
        logger.info(f"BATCH_CLAIM   {len(granted)} granted, {len(denied)} denied  by '{holder}' on {worker}")
    if denied:
        logger.debug(f"  Denied URLs: {[u[-30:] for u in denied]}")
    return jsonify({"granted": granted, "denied": denied})


@app.route("/done", methods=["POST"])
def done():
    """Mark a URL as successfully processed. Body: {"url": "...", "worker": "..."}"""
    body = flask_request.get_json(silent=True) or {}
    url = body.get("url", "")
    worker = body.get("worker", "unknown")
    if not url:
        return jsonify({"ok": False, "error": "missing url"}), 400

    with _lock:
        entry = _data.get(url, {})
        _data[url] = {**entry, "status": STATUS_DONE, "worker": worker, "updated_at": time.time()}

    logger.info(f"DONE          {url[-40:]}  by {worker}")
    return jsonify({"ok": True})


@app.route("/failed", methods=["POST"])
def failed():
    """Mark a URL as permanently failed. Body: {"url": "...", "error": "...", "worker": "..."}"""
    body = flask_request.get_json(silent=True) or {}
    url = body.get("url", "")
    error = body.get("error", "")[:200]
    worker = body.get("worker", "unknown")
    if not url:
        return jsonify({"ok": False, "error": "missing url"}), 400

    with _lock:
        entry = _data.get(url, {})
        _data[url] = {
            **entry,
            "status":     STATUS_FAILED,
            "worker":     worker,
            "updated_at": time.time(),
            "error":      error,
        }

    logger.info(f"FAILED        {url[-40:]}  by {worker} — {error[:60]}")
    return jsonify({"ok": True})


@app.route("/available", methods=["GET"])
def available():
    """
    Check if a URL is safe to process.

    Query: ?url=...
    Returns: {"available": true/false}

    Rules (same as URLCoordinator.is_available):
      - Not in data → available
      - status=done → NOT available
      - status=held, not stale → NOT available
      - status=held, stale → available (reclaim on next /claim)
      - status=failed → available (fresh attempt)
    """
    url = flask_request.args.get("url", "")
    if not url:
        return jsonify({"available": False, "error": "missing url"}), 400

    with _lock:
        entry = _data.get(url)

    if entry is None:
        return jsonify({"available": True})

    status = entry.get("status")
    if status == STATUS_DONE:
        return jsonify({"available": False})
    if status == STATUS_HELD:
        return jsonify({"available": _is_stale(entry)})
    if status == STATUS_FAILED:
        return jsonify({"available": True})

    return jsonify({"available": True})  # Unknown status → allow


@app.route("/status", methods=["GET"])
def status():
    """Get the full status entry for a URL. Query: ?url=..."""
    url = flask_request.args.get("url", "")
    if not url:
        return jsonify({"entry": None, "error": "missing url"}), 400

    with _lock:
        entry = _data.get(url)

    return jsonify({"entry": entry})


@app.route("/count", methods=["GET"])
def count():
    """Count URLs with a given status. Query: ?status=done"""
    target_status = flask_request.args.get("status", "")
    if not target_status:
        return jsonify({"count": 0, "error": "missing status"}), 400

    with _lock:
        n = sum(1 for e in _data.values() if e.get("status") == target_status)

    return jsonify({"count": n})


@app.route("/summary", methods=["GET"])
def summary():
    """Return a {status: count} summary of all tracked URLs."""
    with _lock:
        counts = {STATUS_HELD: 0, STATUS_DONE: 0, STATUS_FAILED: 0}
        for entry in _data.values():
            s = entry.get("status", "unknown")
            counts[s] = counts.get(s, 0) + 1

    return jsonify(counts)


@app.route("/snapshot", methods=["GET"])
def snapshot():
    """
    Return all tracked URLs grouped by status.

    Workers call this once per batch to pre-load the full coordination state
    into their local blacklist, replacing N per-card is_available() HTTP calls
    with a single round-trip.

    Response: {"held": {url: {holder, worker}, ...}, "done": {...}, "failed": {...}}
    """
    with _lock:
        groups: dict = {STATUS_HELD: {}, STATUS_DONE: {}, STATUS_FAILED: {}}
        for url, entry in _data.items():
            s = entry.get("status", "unknown")
            if s in groups:
                groups[s][url] = {
                    "holder": entry.get("holder", ""),
                    "worker": entry.get("worker", ""),
                }
    return jsonify(groups)


@app.route("/reset", methods=["POST"])
def reset():
    """Wipe all coordination state (fresh start)."""
    with _lock:
        _data.clear()

    logger.info("STATE RESET — all coordination data cleared")
    _save_to_disk()
    return jsonify({"ok": True})


@app.route("/reset_blacklist", methods=["POST"])
def reset_blacklist():
    """
    Remove all STATUS_FAILED entries from coordination state.

    Failed URLs become re-eligible for claiming on the next worker cycle.
    Done/held entries are completely untouched.
    """
    with _lock:
        failed_urls = [url for url, entry in _data.items()
                       if entry.get("status") == STATUS_FAILED]
        for url in failed_urls:
            del _data[url]
        count = len(failed_urls)

    logger.info(f"BLACKLIST RESET — {count} failed URL(s) cleared")
    _save_to_disk()
    return jsonify({"ok": True, "cleared": count})


# ═══════════════════════════════════════════════════════════════════════════
#  Section B: Log Streaming Endpoints
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/logs", methods=["POST"])
def receive_logs():
    """
    Receive a batch of log entries from a worker.

    Body: {"worker": "...", "entries": [{"ts": "...", "level": "...", "message": "..."}, ...]}
    """
    body = flask_request.get_json(silent=True) or {}
    worker = body.get("worker", "unknown")
    entries = body.get("entries", [])
    if not entries:
        return jsonify({"ok": True, "stored": 0})

    with _lock:
        if worker not in _log_store:
            _log_store[worker] = collections.deque(maxlen=5000)
        dq = _log_store[worker]
        for entry in entries:
            entry["worker"] = worker
            dq.append(entry)
            # Push to all SSE subscribers
            for sub_q in _sse_subscribers:
                try:
                    sub_q.put_nowait(entry)
                except queue.Full:
                    pass  # Subscriber too slow — drop entry

    return jsonify({"ok": True, "stored": len(entries)})


@app.route("/logs/stream")
def log_stream():
    """
    SSE endpoint: stream log entries to the browser in real time.

    Query: ?worker=HOSTNAME (optional filter)
    Returns: text/event-stream with JSON log entries.
    """
    worker_filter = flask_request.args.get("worker", "")

    def _generate():
        sub_q = queue.Queue(maxsize=500)
        with _lock:
            _sse_subscribers.append(sub_q)
        try:
            while True:
                try:
                    entry = sub_q.get(timeout=30)
                    if worker_filter and entry.get("worker") != worker_filter:
                        continue
                    yield f"event: log\ndata: {json.dumps(entry)}\n\n"
                except queue.Empty:
                    # Send keepalive comment to prevent timeout
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with _lock:
                if sub_q in _sse_subscribers:
                    _sse_subscribers.remove(sub_q)

    return Response(_generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/logs/history")
def log_history():
    """
    Return the last N log entries for initial dashboard load.

    Query: ?n=200&worker=HOSTNAME (both optional)
    """
    n = int(flask_request.args.get("n", 200))
    worker_filter = flask_request.args.get("worker", "")

    with _lock:
        if worker_filter:
            dq = _log_store.get(worker_filter, collections.deque())
            entries = list(dq)[-n:]
        else:
            # Merge all workers, sort by timestamp
            all_entries = []
            for dq in _log_store.values():
                all_entries.extend(dq)
            all_entries.sort(key=lambda e: e.get("ts", ""))
            entries = all_entries[-n:]

    return jsonify({"entries": entries})


# ═══════════════════════════════════════════════════════════════════════════
#  Section C: Diagnostic File Upload Endpoints
# ═══════════════════════════════════════════════════════════════════════════

_REMOTE_DIAG_DIR = os.path.join(LOG_DIR, "remote_diagnostics")


@app.route("/diagnostics", methods=["GET", "POST"])
def diagnostics():
    """
    POST: Upload a diagnostic file (screenshot or HTML dump).
          multipart/form-data: fields 'worker', 'label'; file 'file'.
    GET:  List all uploaded diagnostic files per worker.
    """
    if flask_request.method == "POST":
        worker = flask_request.form.get("worker", "unknown")
        label = flask_request.form.get("label", "")
        file = flask_request.files.get("file")
        if not file:
            return jsonify({"ok": False, "error": "missing file"}), 400

        worker_dir = os.path.join(_REMOTE_DIAG_DIR, worker)
        os.makedirs(worker_dir, exist_ok=True)
        filename = file.filename or f"{label}_{int(time.time())}"
        filepath = os.path.join(worker_dir, filename)
        file.save(filepath)

        logger.info(f"DIAGNOSTIC    {filename}  from {worker}")
        return jsonify({"ok": True, "path": filepath})

    # GET — list all files per worker
    result = {}
    if os.path.isdir(_REMOTE_DIAG_DIR):
        for worker_name in sorted(os.listdir(_REMOTE_DIAG_DIR)):
            worker_path = os.path.join(_REMOTE_DIAG_DIR, worker_name)
            if os.path.isdir(worker_path):
                files = sorted(os.listdir(worker_path), reverse=True)
                result[worker_name] = files
    return jsonify(result)


@app.route("/diagnostics/<worker>/<filename>")
def serve_diagnostic(worker, filename):
    """Serve a diagnostic file for viewing in the dashboard."""
    filepath = os.path.join(_REMOTE_DIAG_DIR, worker, filename)
    if not os.path.isfile(filepath):
        return jsonify({"error": "not found"}), 404
    return send_file(filepath)


# ═══════════════════════════════════════════════════════════════════════════
#  Section D: Code Push Endpoints
# ═══════════════════════════════════════════════════════════════════════════

# Deployable files (relative to project root). Excludes config.yaml, session.json,
# coordination.json, logs, __pycache__ — each machine has its own.
_DEPLOYABLE_FILES = [
    "main.py",
    "requirements.txt",
    "src/__init__.py",
    "src/auth.py",
    "src/batch_creator.py",
    "src/coordination_server.py",
    "src/coordinator.py",
    "src/dataset_mover.py",
    "src/navigator.py",
    "src/utils.py",
]

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _build_code_manifest() -> list[dict]:
    """Compute SHA256 hash and size for each deployable file."""
    manifest = []
    for rel_path in _DEPLOYABLE_FILES:
        abs_path = os.path.join(_PROJECT_ROOT, rel_path)
        if not os.path.isfile(abs_path):
            continue
        try:
            with open(abs_path, "rb") as f:
                content = f.read()
            manifest.append({
                "path": rel_path,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            })
        except Exception:
            pass
    return manifest


@app.route("/code/manifest", methods=["GET", "POST"])
def code_manifest():
    """
    GET:  Return the code manifest (list of {path, sha256, size}).
    POST: Force re-scan of file hashes (after editing a file on the server).
    """
    global _code_manifest, _code_version
    if flask_request.method == "POST":
        _code_manifest = _build_code_manifest()
        _code_version += 1
        logger.info(f"Code manifest refreshed — {len(_code_manifest)} files (v{_code_version})")
        return jsonify({"ok": True, "files": len(_code_manifest), "version": _code_version})

    # GET — return cached manifest (rebuilt on POST or startup)
    return jsonify({"files": _code_manifest, "version": _code_version})


@app.route("/code/file")
def code_file():
    """
    Serve raw file content for download.

    Query: ?path=src/dataset_mover.py
    Only serves files in the deployable set (security).
    """
    rel_path = flask_request.args.get("path", "")
    if rel_path not in _DEPLOYABLE_FILES:
        return jsonify({"error": "path not in deployable set"}), 403

    abs_path = os.path.join(_PROJECT_ROOT, rel_path)
    if not os.path.isfile(abs_path):
        return jsonify({"error": "file not found"}), 404

    return send_file(abs_path, mimetype="text/plain")


# ═══════════════════════════════════════════════════════════════════════════
#  Section F: Worker Heartbeat Endpoints
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/heartbeat", methods=["POST"])
def heartbeat():
    """
    Receive a heartbeat ping from a worker.

    Body: {"worker": "...", "status": "running", "batch": N, "tabs": {...}, ...}
    """
    body = flask_request.get_json(silent=True) or {}
    worker = body.get("worker", "unknown")

    with _lock:
        _workers[worker] = {
            **body,
            "last_seen": time.time(),
        }

    worker_version = body.get("code_version", 0)
    update_available = _code_version > 0 and worker_version < _code_version
    return jsonify({
        "ok": True,
        "code_version": _code_version,
        "update_available": update_available,
    })


@app.route("/workers")
def workers_list():
    """Return all registered workers with heartbeat data and online/stale/offline status."""
    now = time.time()
    with _lock:
        result = {}
        for wid, info in _workers.items():
            age = now - info.get("last_seen", 0)
            if age < 60:
                conn_status = "online"
            elif age < 300:
                conn_status = "stale"
            else:
                conn_status = "offline"
            result[wid] = {**info, "connection": conn_status, "last_seen_ago": int(age)}

    return jsonify(result)


# ═══════════════════════════════════════════════════════════════════════════
#  Section E: Dashboard
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/dashboard")
def dashboard():
    """Serve the Jinja2 dashboard with initial data."""
    uptime = int(time.time() - _start_time)
    with _lock:
        coord_summary = {STATUS_HELD: 0, STATUS_DONE: 0, STATUS_FAILED: 0}
        for entry in _data.values():
            s = entry.get("status", "unknown")
            coord_summary[s] = coord_summary.get(s, 0) + 1

        # Recent logs (last 200 across all workers)
        all_entries = []
        for dq in _log_store.values():
            all_entries.extend(dq)
        all_entries.sort(key=lambda e: e.get("ts", ""))
        recent_logs = all_entries[-200:]

        workers_snapshot = {}
        now = time.time()
        for wid, info in _workers.items():
            age = now - info.get("last_seen", 0)
            if age < 60:
                conn = "online"
            elif age < 300:
                conn = "stale"
            else:
                conn = "offline"
            workers_snapshot[wid] = {**info, "connection": conn, "last_seen_ago": int(age)}

    return render_template(
        "dashboard.html",
        uptime=uptime,
        summary=coord_summary,
        workers=workers_snapshot,
        recent_logs=recent_logs,
        manifest=_code_manifest,
        code_version=_code_version,
        total_urls=len(_data),
    )


# ═══════════════════════════════════════════════════════════════════════════
#  CLI entry point
# ═══════════════════════════════════════════════════════════════════════════

def main():
    global _stale_timeout, _data_file, _save_interval, _data, _start_time, _code_manifest

    parser = argparse.ArgumentParser(
        description="HTTP Coordination Server for cross-machine Phase 2 automation"
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8099, help="Bind port (default: 8099)")
    parser.add_argument("--stale-timeout", type=int, default=1800,
                        help="Seconds before held entries become stale (default: 1800)")
    parser.add_argument("--save-interval", type=int, default=30,
                        help="Seconds between auto-saves to disk (default: 30)")
    parser.add_argument("--data-file", default="coordination.json",
                        help="Path to persistence file (default: coordination.json)")
    parser.add_argument("--reset", action="store_true",
                        help="Wipe existing data file on startup")
    parser.add_argument("--reset-blacklist", action="store_true",
                        help="Clear only failed/blacklisted URLs on startup; preserve done/held state")
    args = parser.parse_args()

    _stale_timeout = args.stale_timeout
    _data_file = args.data_file
    _save_interval = args.save_interval
    _start_time = time.time()

    # Load or reset
    if args.reset or not os.path.exists(_data_file):
        _data = {}
        logger.info(f"Starting with empty state" + (" (--reset)" if args.reset else ""))
    elif args.reset_blacklist:
        _data = _load_from_disk()
        failed_urls = [u for u, e in _data.items() if e.get("status") == STATUS_FAILED]
        for u in failed_urls:
            del _data[u]
        held = sum(1 for e in _data.values() if e.get("status") == STATUS_HELD)
        done = sum(1 for e in _data.values() if e.get("status") == STATUS_DONE)
        logger.info(
            f"Resumed {len(_data)} entries from {_data_file} with blacklist cleared "
            f"({len(failed_urls)} failed URL(s) removed, held={held}, done={done})"
        )
        _save_to_disk()
    else:
        _data = _load_from_disk()
        held = sum(1 for e in _data.values() if e.get("status") == STATUS_HELD)
        done = sum(1 for e in _data.values() if e.get("status") == STATUS_DONE)
        fail = sum(1 for e in _data.values() if e.get("status") == STATUS_FAILED)
        logger.info(f"Resumed {len(_data)} entries from {_data_file}  (held={held}, done={done}, failed={fail})")

    # Start auto-save background thread
    saver = threading.Thread(target=_auto_save_loop, daemon=True, name="auto-saver")
    saver.start()
    logger.info(f"Auto-save every {_save_interval}s to {_data_file}")

    # Build code manifest for code-push feature
    _code_manifest = _build_code_manifest()
    logger.info(f"Code manifest: {len(_code_manifest)} deployable files")

    # Startup banner
    logger.info("=" * 60)
    logger.info(f"  Coordination server running on {args.host}:{args.port}")
    logger.info(f"  Stale timeout:  {_stale_timeout}s")
    logger.info(f"  Save interval:  {_save_interval}s")
    logger.info(f"  Data file:      {_data_file}")
    logger.info("=" * 60)

    # Run Flask (threaded=True for concurrent requests from 2 laptops)
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
