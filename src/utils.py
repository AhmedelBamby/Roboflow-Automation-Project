"""
Utility functions: config loading, logging setup, and helpers.

Principles implemented:
  P4 — Adaptive Timeouts   : timeout_multiplier loaded from config
  P5 — Diagnostic Completeness : capture_diagnostics() replaces take_screenshot()
  P6 — Headless Parity     : fonts no longer blocked; "new" headless mode used
"""

import os
import re
import logging
import socket
import threading
import time as _time
import yaml
from datetime import datetime


LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
SCREENSHOT_DIR = os.path.join(LOG_DIR, "screenshots")
HTMLDUMP_DIR   = os.path.join(LOG_DIR, "htmldumps")


def get_worker_id() -> str:
    """Return a stable machine identifier (hostname) for worker identity."""
    return socket.gethostname()


def setup_logging() -> logging.Logger:
    """Configure and return the project logger."""
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(LOG_DIR, f"run_{timestamp}.log")

    logger = logging.getLogger("roboflow_batch")
    logger.setLevel(logging.DEBUG)

    # Prevent duplicate handlers on repeated calls
    if logger.handlers:
        return logger

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch_fmt = logging.Formatter("[%(asctime)s] %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
    ch.setFormatter(ch_fmt)

    # File handler
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh_fmt = logging.Formatter("[%(asctime)s] %(levelname)-8s %(name)s — %(message)s")
    fh.setFormatter(fh_fmt)

    logger.addHandler(ch)
    logger.addHandler(fh)

    logger.info(f"Log file: {log_file}")
    return logger


# ── Remote Log Handler ───────────────────────────────────────────────────

class RemoteLogHandler(logging.Handler):
    """
    Custom logging handler that buffers log records and flushes them
    to the coordination server periodically.

    Follows the existing handler pattern (FileHandler, StreamHandler).
    Fire-and-forget: flush failures are silently dropped — they never
    affect the automation pipeline or the local log file.

    Args:
        coordinator: An HTTPCoordinator instance (must have send_logs method).
        flush_interval: Seconds between automatic flushes (default from config).
        flush_threshold: Max buffered entries before early flush.
    """

    def __init__(self, coordinator, *, flush_interval: int = 5, flush_threshold: int = 50):
        super().__init__(level=logging.DEBUG)
        self._coordinator = coordinator
        self._buffer: list[dict] = []
        self._buffer_lock = threading.Lock()
        self._flush_interval = flush_interval
        self._flush_threshold = flush_threshold

        # Start periodic flush timer
        self._timer = threading.Thread(target=self._flush_loop, daemon=True, name="log-flusher")
        self._timer.start()

    def emit(self, record: logging.LogRecord) -> None:
        """Buffer a log record. Triggers early flush if threshold is reached."""
        entry = {
            "ts": self.format_time(record),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        with self._buffer_lock:
            self._buffer.append(entry)
            should_flush = len(self._buffer) >= self._flush_threshold

        if should_flush:
            self._do_flush()

    def _flush_loop(self) -> None:
        """Background thread: flush buffer every flush_interval seconds."""
        while True:
            _time.sleep(self._flush_interval)
            self._do_flush()

    def _do_flush(self) -> None:
        """Send buffered entries to the server. Never raises."""
        with self._buffer_lock:
            if not self._buffer:
                return
            batch = self._buffer[:]
            self._buffer.clear()
        try:
            self._coordinator.send_logs(batch)
        except Exception:
            # Fire-and-forget: re-add entries up to 2× threshold, then drop
            with self._buffer_lock:
                capacity = self._flush_threshold * 2 - len(self._buffer)
                if capacity > 0:
                    self._buffer = batch[:capacity] + self._buffer

    @staticmethod
    def format_time(record: logging.LogRecord) -> str:
        """Format a LogRecord timestamp to ISO-like string."""
        dt = datetime.fromtimestamp(record.created)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


def load_config(config_path: str = None) -> dict:
    """Load and validate config.yaml, applying safe defaults for all new keys."""
    if config_path is None:
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"
        )

    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # Validate required fields (always needed)
    always_required = ["email", "workspace_url", "project_name"]
    for key in always_required:
        if key not in config or config[key] is None:
            raise ValueError(f"Missing required config key: '{key}'")

    # Phase defaults
    config.setdefault("phase", 1)
    phase = config["phase"]

    # Phase 1 settings
    config.setdefault("images_per_batch", 2000)
    config.setdefault("total_iterations", 10)
    config.setdefault("max_pagination_pages", 50)
    config.setdefault("exclude_annotators", ["Roboflow Labeling"])

    # Phase 2 settings
    config.setdefault("parallel_tabs", 5)
    config.setdefault("min_images_per_job", 400)

    # Collection strategy
    strategy = config.setdefault("collection_strategy", "top_down")
    if strategy not in ("top_down", "bottom_up"):
        raise ValueError(
            f"Invalid collection_strategy '{strategy}'. Must be 'top_down' or 'bottom_up'."
        )

    # Coordination settings
    config.setdefault("enable_coordination", False)
    config.setdefault("coordination_file", "coordination.json")
    config.setdefault("coordination_stale_timeout", 1800)
    config.setdefault("coordination_reset_on_start", True)

    # Coordination mode (file-based or HTTP server)
    mode = config.setdefault("coordination_mode", "file")
    if mode not in ("file", "http"):
        raise ValueError(
            f"Invalid coordination_mode '{mode}'. Must be 'file' or 'http'."
        )
    config.setdefault("coordination_server_url", "http://localhost:8099")

    # Timeout multiplier (P4)
    multiplier = config.setdefault("timeout_multiplier", 1.0)
    if not isinstance(multiplier, (int, float)) or multiplier < 0.1:
        raise ValueError(
            f"timeout_multiplier must be a number >= 0.1, got: {multiplier!r}"
        )

    # Browser
    config.setdefault("headless", False)

    # Remote monitoring (only active when coordination_mode: "http")
    config.setdefault("auto_update", False)
    config.setdefault("remote_logging", True)
    config.setdefault("remote_diagnostics", True)

    hb = config.setdefault("heartbeat_interval", 30)
    if not isinstance(hb, int) or hb < 5:
        raise ValueError(f"heartbeat_interval must be int >= 5, got: {hb!r}")

    lf = config.setdefault("log_flush_interval", 5)
    if not isinstance(lf, int) or lf < 1:
        raise ValueError(f"log_flush_interval must be int >= 1, got: {lf!r}")

    lt = config.setdefault("log_flush_threshold", 50)
    if not isinstance(lt, int) or lt < 1:
        raise ValueError(f"log_flush_threshold must be int >= 1, got: {lt!r}")

    # Only validate batch settings if phase 1 or both is selected
    if phase in (1, "both"):
        required_keys = ["email", "workspace_url", "project_name", "images_per_batch", "total_iterations"]
        missing_keys = [k for k in required_keys if not config.get(k)]
        if missing_keys:
            raise ValueError(f"Missing required configuration keys: {', '.join(missing_keys)}")

    return config


def parse_image_count(button_text: str) -> int:
    """
    Parse the number from button text like 'Assign 200 Images'.
    Returns 0 if parsing fails.
    """
    match = re.search(r"(\d+)", button_text)
    if match:
        return int(match.group(1))
    return 0


# ── Principle 5: Diagnostic Completeness ─────────────────────────────────

def _upload_diagnostic_remote(coordinator, filepath: str, label: str, logger) -> None:
    """Fire-and-forget upload of a diagnostic file to the coordination server."""
    if coordinator is None:
        return
    upload_fn = getattr(coordinator, "upload_diagnostic", None)
    if upload_fn is None:
        return
    try:
        upload_fn(filepath, label)
    except Exception:
        logger.debug(f"Remote diagnostic upload skipped for {filepath}")


def capture_diagnostics(page, label: str = "error", coordinator=None) -> str | None:
    """
    Capture maximum diagnostic data even when the page is broken.

    Chain:
      1. page.screenshot()  with a hard 5s timeout
      2. On failure → page.content() → save as .html dump
      3. Always log page.url and page.title()
      4. If coordinator provided → log coordination status for this URL

    Returns the file path of the saved screenshot or HTML dump, or None.
    """
    logger = logging.getLogger("roboflow_batch")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^\w\-]", "_", label)[:80]

    # Step 3 — always log URL + title first (works even on stuck pages)
    try:
        current_url = page.url
    except Exception:
        current_url = "<unavailable>"
    try:
        current_title = page.title()
    except Exception:
        current_title = "<unavailable>"
    logger.debug(f"[diag] url={current_url}  title={current_title}")

    # Step 4 — coordination state for this URL
    if coordinator is not None:
        try:
            status = coordinator.get_status(current_url)
            logger.debug(f"[diag] coordinator status for url: {status}")
        except Exception:
            pass

    # Step 1 — screenshot (5 second timeout; don't hang on broken pages)
    try:
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        filename = f"{timestamp}_{safe_label}.png"
        filepath = os.path.join(SCREENSHOT_DIR, filename)
        page.screenshot(path=filepath, full_page=False, timeout=5_000)
        logger.info(f"📸 Screenshot saved: {filepath}")
        _upload_diagnostic_remote(coordinator, filepath, safe_label, logger)
        return filepath
    except Exception as ss_err:
        logger.debug(f"Screenshot failed ({ss_err}) — falling back to HTML dump")

    # Step 2 — HTML dump (always works even when the page is stuck)
    try:
        os.makedirs(HTMLDUMP_DIR, exist_ok=True)
        html_filename = f"{timestamp}_{safe_label}.html"
        html_filepath = os.path.join(HTMLDUMP_DIR, html_filename)
        html_content = page.content()
        with open(html_filepath, "w", encoding="utf-8") as f:
            f.write(html_content)
        logger.info(f"📄 HTML dump saved: {html_filepath}")
        _upload_diagnostic_remote(coordinator, html_filepath, safe_label, logger)
        return html_filepath
    except Exception as html_err:
        logger.warning(f"HTML dump also failed: {html_err}")
        return None


def take_screenshot(page, label: str = "error", timeout: int = 10_000) -> str | None:
    """
    Backward-compatible wrapper around capture_diagnostics().
    All legacy call sites that pass a `timeout` parameter continue to work.
    New code should call capture_diagnostics() directly.
    """
    return capture_diagnostics(page, label)


def get_session_path() -> str:
    """Return the path to the session storage file."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session.json"
    )


# ── Principle 4: Adaptive Timeouts ───────────────────────────────────────

def adaptive_timeout(base_ms: int, image_count: int, per_image_ms: int, config: dict) -> int:
    """
    Calculate a scaled timeout:
        max(base_ms, image_count * per_image_ms) * timeout_multiplier

    Args:
        base_ms: Minimum timeout in milliseconds (floor).
        image_count: Number of images in the current job card.
        per_image_ms: Extra milliseconds to add per image.
        config: Loaded config dict (reads timeout_multiplier).

    Returns:
        int — timeout in milliseconds, rounded to nearest 100ms.

    Examples:
        adaptive_timeout(30_000, 50,  50, cfg)  → 30_000ms  (50*50=2500 < 30k)
        adaptive_timeout(30_000, 800, 50, cfg)  → 40_000ms  (800*50=40k > 30k)
        adaptive_timeout(30_000, 800, 50, cfg with multiplier=2.0) → 80_000ms
    """
    multiplier = config.get("timeout_multiplier", 1.0)
    raw = max(base_ms, image_count * per_image_ms)
    scaled = int(raw * multiplier)
    # Round up to nearest 100ms for cleaner log messages
    return ((scaled + 99) // 100) * 100


# ── Principle 6: Headless Parity ─────────────────────────────────────────

# Resource types that are safe to block in headless mode.
# NOTE: "font" intentionally removed (P6 — font blocking causes zero-dimension
# button layout failures in headless mode, making buttons "not visible").
_BLOCKED_RESOURCE_TYPES = {"image", "media"}

# URL patterns for heavy third-party assets that aren't needed for automation.
_BLOCKED_URL_PATTERNS = [
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "www.googletagmanager.com",
    "www.google-analytics.com",
    "analytics.",
    "hotjar.com",
    "sentry.io",
    "intercom.io",
    "cdn.segment.com",
]


def _should_block(url: str) -> bool:
    """Return True if the URL matches a blocked third-party pattern."""
    lower = url.lower()
    return any(pattern in lower for pattern in _BLOCKED_URL_PATTERNS)


def optimize_context_for_headless(context) -> None:
    """
    Apply performance optimizations to a BrowserContext for headless mode.

    Uses Playwright route interception to abort requests for images, media,
    and known analytics/tracking scripts.

    NOTE: Fonts are intentionally NOT blocked (Principle 6 — Headless Parity).
    Blocking fonts disrupts CSS text layout and causes buttons to have
    zero dimensions, making them "not visible" to Playwright wait_for().

    Call this ONCE on the context right after creating it.  Every page
    opened from this context inherits the routes.
    """
    logger = logging.getLogger("roboflow_batch")

    def _route_handler(route):
        request = route.request
        # 1. Block heavy resource types (NOT fonts — see docstring)
        if request.resource_type in _BLOCKED_RESOURCE_TYPES:
            route.abort()
            return
        # 2. Block known third-party tracking / analytics URLs
        if _should_block(request.url):
            route.abort()
            return
        # 3. Allow everything else
        route.continue_()

    context.route("**/*", _route_handler)
    logger.info("Headless optimisation applied — blocking images, media & analytics (fonts allowed)")
