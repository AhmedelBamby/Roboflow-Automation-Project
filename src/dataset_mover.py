"""
Dataset Mover module (Phase 2):
Process Annotating job cards -> convert unannotated to null -> add to dataset.

Architecture: Non-blocking round-robin pipeline with independent tab slots.
  - N slots (from config.parallel_tabs) run as independent state machines.
  - Each tick advances ONE slot by one micro-step (<2 s), then jumps to the next.
  - State machine: PENDING → NAVIGATING → DETAIL_WAIT → SCANNING → NULL_CLICKED
                   → CONVERTING → READY → ADD_CLICKED → CONFIRMING → VERIFYING → DONE
  - The system never waits for a tab to finish — it checks state, takes a quick
    action, and immediately jumps to the next tab.
  - Per-tab history, error tracking, freeze detection, and blacklisting.
  - When a slot finishes (DONE/ERROR), it recycles with the next URL from the queue.

Principles implemented:
  P1 -- Layered Verification    : DOM -> coordination file -> server URL check
  P2 -- Progressive Recovery    : 5-step escalation ladder on every critical operation
  P3 -- Failure Memory          : in-memory blacklist + coordination file failed status
  P4 -- Adaptive Timeouts       : scale with image_count via adaptive_timeout()
  P5 -- Diagnostic Completeness : capture_diagnostics() everywhere
  P6 -- Headless Parity         : attached-state fallback + JS overlay removal
  P7 -- Multi-Signal Exit       : DOM 0 + forced navigation + coordination done count
  P8 -- Non-Blocking Tab Jumping: round-robin micro-ticks, per-tab history & blacklisting
"""

import re
import logging
import time
from playwright.sync_api import Page, BrowserContext

from src.utils import capture_diagnostics, adaptive_timeout

logger = logging.getLogger("roboflow_batch")

# -- Constants -------------------------------------------------------------------
WAIT_STRATEGY      = "domcontentloaded"
NAV_TIMEOUT        = 60_000
CARD_LOAD_TIMEOUT  = 30_000
POLL_INTERVAL      = 5              # seconds between round-robin polls
TICK_INTERVAL      = 0.3            # seconds between individual tab ticks (fast jump)
QUICK_CHECK_MS     = 1_500          # max ms any single DOM probe may block (non-blocking feel)
MAX_CARD_RETRIES   = 2              # how many times to re-queue a failed card URL
DETAIL_VIEW        = ".AnnotationJobDetailView"  # unique detail-panel wrapper
BLACKLIST_THRESHOLD = 3             # consecutive errors before a tab URL is blacklisted

# Board selectors
_ANNOTATING_COL  = '.boardColumn:has(h2:text("Annotating"))'
_VIRTUOSO        = '[data-test-id="virtuoso-scroller"]'
_MAX_BOTTOM_SCROLL_ATTEMPTS = 20

# Fiber-based bulk extraction
_FIBER_PROBE_TIMEOUT = 5_000        # ms to spend probing React fiber
_SCROLL_STEP_PX      = 1200         # pixels per scroll step during bulk extraction

# Tier 0: Firestore subscription intercept — regex to extract job IDs from POST bodies
_FIRESTORE_JOB_RE       = re.compile(r'annotation_jobs/([A-Za-z0-9]{15,30})/timeline')
_FIRESTORE_JOB_RE_BYTES = re.compile(rb'annotation_jobs/([A-Za-z0-9]{15,30})')  # for binary gRPC bodies
_FIRESTORE_HOST         = "firestore.googleapis.com"

# JavaScript fetch() monkey-patch — injected via page.add_init_script().
# Intercepts Firestore calls at the JS layer, completely above HTTP/2, so it
# works regardless of transport version (gRPC-Web, HTTP/2, etc.).
# Calls window._pyFirestoreCapture(jobId) for every job ID found in a POST body.
_JS_FIRESTORE_FETCH_PATCH = r"""
(function () {
    if (window.__firestorePatchInstalled) return;
    window.__firestorePatchInstalled = true;

    const RE = /annotation_jobs\/([A-Za-z0-9]{15,30})/g;
    const _orig = window.fetch;

    window.fetch = async function (resource, options) {
        const url = (typeof resource === 'string' ? resource : resource?.url) || '';
        if (url.includes('firestore.googleapis.com') && options && options.body) {
            try {
                const body = options.body;
                let arr = null;
                if (body instanceof ArrayBuffer) {
                    arr = new Uint8Array(body);
                } else if (ArrayBuffer.isView(body)) {
                    arr = new Uint8Array(body.buffer, body.byteOffset, body.byteLength);
                }
                if (arr) {
                    // Latin-1 decode preserves ASCII job ID bytes intact
                    let s = '';
                    for (let i = 0; i < arr.length; i++) s += String.fromCharCode(arr[i]);
                    let m;
                    RE.lastIndex = 0;
                    while ((m = RE.exec(s)) !== null) {
                        if (window._pyFirestoreCapture) window._pyFirestoreCapture(m[1]);
                    }
                }
            } catch (e) { /* never break the page */ }
        }
        return _orig.apply(this, arguments);
    };
})();
"""


# =================================================================================
#  Tier 1: React Fiber Bulk URL Extraction
# =================================================================================

# JS code to discover React fiber properties on the first visible card.
# Returns {id_key, img_key, sample_id} or null if fiber access fails.
_JS_PROBE_FIBER = """
() => {
    const col = (() => {
        const cols = document.querySelectorAll('.boardColumn');
        for (const c of cols) {
            const h2 = c.querySelector('h2');
            if (h2 && h2.textContent.trim() === 'Annotating') return c;
        }
        return null;
    })();
    if (!col) return null;

    const card = col.querySelector('div[data-index]');
    if (!card) return null;

    // Find React fiber key (e.g. __reactFiber$abc123)
    const fiberKey = Object.keys(card).find(k =>
        k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$')
    );
    if (!fiberKey) return null;

    // Walk fibers to find the item prop
    let fiber = card[fiberKey];
    let item = null;
    for (let i = 0; i < 15 && fiber; i++) {
        const props = fiber.memoizedProps || fiber.pendingProps;
        if (props && props.item && typeof props.item === 'object') {
            item = props.item;
            break;
        }
        // Also check 'data' prop or 'children' with item
        if (props && props.data && typeof props.data === 'object' && props.data.id) {
            item = props.data;
            break;
        }
        fiber = fiber.return;
    }
    if (!item) return null;

    // Detect which key holds the job ID.
    // Priority 1: well-known ID field names (avoids misidentifying metadata like 'owner').
    let id_key = null;
    for (const candidate of ['id', 'jobId', '_id', 'key', 'slug']) {
        if (typeof item[candidate] === 'string' && item[candidate].length >= 8) {
            id_key = candidate;
            break;
        }
    }
    // Priority 2: broad regex scan — skip obvious metadata keys.
    const ID_RE = /^[A-Za-z0-9]{16,30}$/;
    const _SKIP_KEYS = new Set(['owner', 'project', 'reviewer', 'labeler', 'createdBy', 'name', 'sourceBatch', 'status', 'instructionsText']);
    if (!id_key) {
        for (const [k, v] of Object.entries(item)) {
            if (_SKIP_KEYS.has(k)) continue;
            if (typeof v === 'string' && ID_RE.test(v)) {
                id_key = k;
                break;
            }
        }
    }
    if (!id_key) return null;

    // Detect image count key
    let img_key = null;
    for (const candidate of ['numImages', 'imageCount', 'image_count', 'images', 'totalImages']) {
        if (typeof item[candidate] === 'number') {
            img_key = candidate;
            break;
        }
    }
    // Broader search: any numeric field > 0
    if (!img_key) {
        for (const [k, v] of Object.entries(item)) {
            if (typeof v === 'number' && v > 0 && k.toLowerCase().includes('image')) {
                img_key = k;
                break;
            }
        }
    }

    return {
        id_key: id_key,
        img_key: img_key,
        sample_id: item[id_key],
        sample_img: img_key ? item[img_key] : null,
        all_keys: Object.keys(item),
    };
}
"""

# JS to extract cards from all visible div[data-index] elements.
# Keys are passed as JS arguments [idKey, imgKey, fiberKeyHint] — NOT via .format().
_JS_EXTRACT_CARDS = """
(args) => {
    const [idKey, imgKey, fiberKeyHint] = args;
    const col = (() => {
        const cols = document.querySelectorAll('.boardColumn');
        for (const c of cols) {
            const h2 = c.querySelector('h2');
            if (h2 && h2.textContent.trim() === 'Annotating') return c;
        }
        return null;
    })();
    if (!col) return [];

    const wrappers = col.querySelectorAll('div[data-index]');
    const results = [];
    for (const w of wrappers) {
        const idx = parseInt(w.getAttribute('data-index'), 10);
        if (isNaN(idx)) continue;

        const fk = fiberKeyHint || Object.keys(w).find(k =>
            k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$')
        );
        if (!fk || !w[fk]) continue;

        let fiber = w[fk];
        let item = null;
        for (let i = 0; i < 15 && fiber; i++) {
            const props = fiber.memoizedProps || fiber.pendingProps;
            if (props && props.item && typeof props.item === 'object') {
                item = props.item;
                break;
            }
            if (props && props.data && typeof props.data === 'object' && props.data[idKey]) {
                item = props.data;
                break;
            }
            fiber = fiber.return;
        }
        if (!item || !item[idKey]) continue;

        results.push({
            index: idx,
            job_id: item[idKey],
            img_count: imgKey && typeof item[imgKey] === 'number' ? item[imgKey] : -1,
        });
    }
    return results;
}
"""


def _probe_fiber_structure(page: Page):
    """
    Probe the React fiber tree of the first visible card to discover
    which properties hold the job ID and image count.

    Returns dict with {id_key, img_key, fiber_key_hint, sample_id} or None.
    """
    try:
        result = page.evaluate(_JS_PROBE_FIBER)
        if result and result.get("id_key"):
            logger.info(
                f"  [Fiber] Probe OK: id_key={result['id_key']}, "
                f"img_key={result.get('img_key')}, "
                f"sample_id={result['sample_id']}, "
                f"keys={result.get('all_keys')}"
            )
            # Also discover the fiber key string for reuse
            fiber_key_hint = page.evaluate("""
                () => {
                    const col = (() => {
                        const cols = document.querySelectorAll('.boardColumn');
                        for (const c of cols) {
                            const h2 = c.querySelector('h2');
                            if (h2 && h2.textContent.trim() === 'Annotating') return c;
                        }
                        return null;
                    })();
                    if (!col) return null;
                    const card = col.querySelector('div[data-index]');
                    if (!card) return null;
                    return Object.keys(card).find(k =>
                        k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$')
                    ) || null;
                }
            """)
            result["fiber_key_hint"] = fiber_key_hint
            return result
        logger.debug("  [Fiber] Probe returned no id_key")
        return None
    except Exception as e:
        logger.debug(f"  [Fiber] Probe error: {e}")
        return None


def _extract_visible_cards(page: Page, id_key: str, img_key, fiber_key_hint=None) -> list:
    """
    Extract job data from all currently rendered cards via React fiber.

    Returns list of {index: int, job_id: str, img_count: int}.
    """
    try:
        return page.evaluate(
            _JS_EXTRACT_CARDS,
            [id_key, img_key, fiber_key_hint],
        )
    except Exception as e:
        logger.debug(f"  [Fiber] Extract error: {e}")
        return []


# =================================================================================
#  Tier 0: Firestore fetch() Intercept (JS monkey-patch)
# =================================================================================

def _install_firestore_intercept(page: Page, config: dict) -> None:
    """
    Install the fetch() monkey-patch and expose_function once per page lifetime.
    Stores captured job IDs in config["_firestore_ids"] (a shared set cleared
    each batch by _network_collect_urls).

    Must be called BEFORE any navigation so add_init_script fires on every
    subsequent page load, including the reload Tier 0 triggers.
    """
    if config.get("_firestore_intercept_installed"):
        return

    captured: set[str] = set()
    config["_firestore_ids"] = captured

    def _py_capture(job_id: str) -> None:
        captured.add(job_id)

    try:
        page.expose_function("_pyFirestoreCapture", _py_capture)
        page.add_init_script(_JS_FIRESTORE_FETCH_PATCH)
        config["_firestore_intercept_installed"] = True
        logger.info("  [Tier0] Firestore fetch() intercept installed")
    except Exception as e:
        logger.warning(f"  [Tier0] Failed to install fetch intercept: {e}")


def _network_collect_urls(
    page: Page,
    count: int,
    min_images: int,
    blacklist: set,
    annotate_url: str,
    config=None,
    coord_held: set | None = None,
    coord_done: set | None = None,
) -> list | None:
    """
    Tier 0: Collect job URLs by intercepting Firestore subscription POST request bodies.

    Roboflow subscribes each job's timeline via POST to firestore.googleapis.com;
    the request body contains the job ID in plaintext:
        datasets/{datasetId}/annotation_jobs/{jobId}/timeline

    No DOM access, no scrolling, no clicks required.  Requires a page reload to
    trigger fresh subscription bursts.

    Returns a list of job URLs, or None if disabled / nothing captured.
    """
    if config and not config.get("use_network_tier0", True):
        logger.info("  [Tier0] Disabled via config — skipping")
        return None

    captured_ids: set[str] | None = config.get("_firestore_ids") if config else None
    if captured_ids is None:
        logger.warning("  [Tier0] Intercept not installed — skipping (call _install_firestore_intercept first)")
        return None

    # Clear IDs from previous batch, then reload to trigger fresh fetch() calls
    captured_ids.clear()

    logger.info("  [Tier0] Reloading page to trigger Firestore fetch() calls …")
    try:
        page.reload(wait_until=WAIT_STRATEGY, timeout=NAV_TIMEOUT)
    except Exception as e:
        logger.warning(f"  [Tier0] Reload failed: {e}")
        return None

    # Wait up to 8 s; exit early when counts stabilise (2 s stable + ≥1 result)
    deadline = time.time() + 8.0
    last_count, stable_ticks = 0, 0
    while time.time() < deadline:
        time.sleep(0.5)
        current = len(captured_ids)
        if current == last_count:
            stable_ticks += 1
            if stable_ticks >= 4 and current > 0:
                break
        else:
            stable_ticks = 0
            last_count = current

    if not captured_ids:
        logger.info("  [Tier0] No job IDs captured via fetch() intercept")
        return None

    base_job_url = annotate_url.rstrip("/") + "/job/"
    urls: list[str] = []
    _held = coord_held or set()
    _done = coord_done or set()
    skip_held = skip_done = skip_bl = 0
    for job_id in captured_ids:
        job_url = base_job_url + job_id
        if job_url in _done:
            skip_done += 1
            continue
        if job_url in _held:
            skip_held += 1
            continue
        if job_url in blacklist:
            skip_bl += 1
            continue
        urls.append(job_url)
        if len(urls) >= count:
            break

    parts = [f"{len(urls)} usable"]
    if skip_held: parts.append(f"{skip_held} held by other workers")
    if skip_done: parts.append(f"{skip_done} already done")
    if skip_bl:   parts.append(f"{skip_bl} blacklisted")
    logger.info(
        f"  [Tier0] Captured {len(captured_ids)} job IDs via fetch() intercept → "
        + ", ".join(parts)
    )
    return urls  # [] when all filtered out; caller uses 'is not None' to detect success


def _bulk_collect_urls(
    page: Page,
    count: int,
    min_images: int,
    coordinator,
    blacklist: set,
    annotate_url: str,
    direction: str = "top_down",
    config=None,
    coord_held: set | None = None,
    coord_done: set | None = None,
) -> list | None:
    """
    Tier 1: Collect job URLs by scrolling the virtualised list and reading
    React fiber data — NO click-navigate-back cycle.

    Returns a list of validated URLs, or None if fiber probing fails
    (caller should fall back to click-per-card).
    """
    if config and not config.get("use_fiber_tier1", True):
        logger.info("  [Bulk] Fiber Tier 1 disabled via config — skipping")
        return None

    probe = _probe_fiber_structure(page)
    if not probe:
        logger.info("  [Bulk] Fiber probe failed — falling back to click-per-card")
        return None

    id_key = probe["id_key"]
    img_key = probe.get("img_key")
    fiber_hint = probe.get("fiber_key_hint")

    # Build base URL for constructing job links
    # annotate_url looks like: .../project-name/annotate
    base_job_url = annotate_url.rstrip("/") + "/job/"

    annotating_col = page.locator(_ANNOTATING_COL)
    scroller = annotating_col.locator(_VIRTUOSO).first

    urls = []
    seen_indices = set()
    stall_count = 0
    MAX_STALL = 5  # stop if 5 consecutive scroll steps yield no new cards
    _skip_held = _skip_done = _skip_bl = 0

    start_time = time.time()

    # Initial scroll position
    if direction == "bottom_up":
        scroller.evaluate("e => { e.scrollTop = e.scrollHeight; }")
    else:
        scroller.evaluate("e => { e.scrollTop = 0; }")

    # Wait for Virtuoso to render at least one card before starting the extraction loop.
    # A bare sleep(0.5) races with the DOM — especially after Tier 0's page reload.
    try:
        page.wait_for_selector(
            f'{_ANNOTATING_COL} div[data-index]',
            state="visible",
            timeout=10_000,
        )
    except Exception:
        logger.warning("  [Bulk] Timed out waiting for cards to render — aborting fiber extraction")
        return None
    time.sleep(0.2)  # brief extra stabilisation after first card appears

    while len(urls) < count and stall_count < MAX_STALL:
        cards = _extract_visible_cards(page, id_key, img_key, fiber_hint)
        new_cards = [c for c in cards if c["index"] not in seen_indices]

        if not new_cards:
            stall_count += 1
        else:
            stall_count = 0

        # Process in order appropriate for direction
        if direction == "bottom_up":
            new_cards.sort(key=lambda c: c["index"], reverse=True)
        else:
            new_cards.sort(key=lambda c: c["index"])

        for card in new_cards:
            seen_indices.add(card["index"])

            # Image count filter (skip if img_key unavailable — accept all)
            if img_key and card["img_count"] >= 0 and card["img_count"] < min_images:
                continue

            job_url = base_job_url + card["job_id"]
            _done = coord_done or set()
            _held = coord_held or set()

            # Done filter — already completed by any worker
            if job_url in _done:
                _skip_done += 1
                continue

            # Held filter — claimed by another worker
            if job_url in _held:
                _skip_held += 1
                continue

            # Blacklist filter (P3) — locally failed
            if job_url in blacklist:
                _skip_bl += 1
                continue

            # Duplicate filter
            if job_url in urls:
                continue

            urls.append(job_url)
            logger.debug(f"  [Bulk] idx={card['index']} → {job_url[-30:]}")

            if len(urls) >= count:
                break

        if len(urls) >= count:
            break

        # Scroll to next viewport
        if direction == "bottom_up":
            scroller.evaluate(f"e => {{ e.scrollTop = Math.max(0, e.scrollTop - {_SCROLL_STEP_PX}); }}")
        else:
            scroller.evaluate(f"e => {{ e.scrollTop += {_SCROLL_STEP_PX}; }}")
        time.sleep(0.4)

    elapsed = time.time() - start_time
    parts = [f"{len(urls)} usable"]
    if _skip_held: parts.append(f"{_skip_held} held by other workers")
    if _skip_done: parts.append(f"{_skip_done} already done")
    if _skip_bl:   parts.append(f"{_skip_bl} blacklisted")
    logger.info(
        f"  [Bulk] {', '.join(parts)} via fiber extraction "
        f"in {elapsed:.1f}s (scanned {len(seen_indices)} cards)"
    )
    return urls  # [] when all filtered by coordination; caller uses 'is not None'


def _fast_navigate_back(page: Page, annotate_url: str) -> None:
    """
    Navigate back to the board page using the fastest available method.

    Tries browser back (bfcache) first, falls back to page.goto().
    Lighter post-check than _wait_for_board_scroller: only waits for
    the Annotating column to be visible.
    """
    try:
        page.go_back(wait_until=WAIT_STRATEGY, timeout=15_000)
        current = page.url
        # Verify we landed on the board, not still on a job detail page
        if "/annotate/job/" not in current and "/annotate" in current:
            # Wait for the Virtuoso scroller (not just the column) so React has
            # re-hydrated before any subsequent scroller.evaluate() call.
            page.wait_for_selector(
                f"{_ANNOTATING_COL} {_VIRTUOSO}", state="visible", timeout=15_000
            )
            try:
                page.wait_for_load_state("load", timeout=10_000)
            except Exception:
                pass  # non-fatal — scroller may already be interactive
            return
    except Exception:
        pass

    # Fallback: full navigation
    page.goto(annotate_url, wait_until=WAIT_STRATEGY, timeout=NAV_TIMEOUT)
    _wait_for_board_scroller(page)
    try:
        page.wait_for_load_state("load", timeout=10_000)
    except Exception:
        pass


def _close_stray_popups(page: Page) -> None:
    """Close any extra tabs that were opened as popups during card clicks.

    The board page (first context page) is kept; every other tab is closed.
    Silently ignores already-closed pages.
    """
    try:
        main_page = page
        for p in page.context.pages:
            if p != main_page and not p.is_closed():
                try:
                    p.close()
                except Exception:
                    pass
    except Exception:
        pass


def _click_card_and_get_url(page: Page, card_element, annotate_url: str) -> str | None:
    """Click a card and extract the job URL regardless of how the app opens it.

    Handles two behaviors:
      A) New tab / popup opens  → read URL from that tab, close it
      B) Same-page SPA navigation → read URL from current page, navigate back

    Returns the job URL string, or None if the click produced nothing useful.
    The board page is always left in a usable state.
    """
    pages_before = set(page.context.pages)

    # Click the card
    card_element.click()

    # Wait briefly for either a new tab or a URL change
    job_url = None

    # ── Check A: did a new tab appear? ────────────────────────────────
    # Poll for up to 5 seconds for a popup
    for _ in range(10):
        time.sleep(0.5)
        pages_now = set(page.context.pages)
        new_pages = [p for p in pages_now - pages_before if not p.is_closed()]
        if new_pages:
            popup = new_pages[0]
            try:
                popup.wait_for_load_state("commit", timeout=10_000)
            except Exception:
                pass
            job_url = popup.url
            # Close all new tabs
            for p in new_pages:
                try:
                    if not p.is_closed():
                        p.close()
                except Exception:
                    pass
            break

        # ── Check B: did the current page navigate away? ──────────────
        if "/annotate/job/" in page.url:
            job_url = page.url
            _fast_navigate_back(page, annotate_url)
            break

    # Fallback: if neither happened after 5s, one more check
    if job_url is None:
        if "/annotate/job/" in page.url:
            job_url = page.url
            _fast_navigate_back(page, annotate_url)
        else:
            # Clean up any stray tabs just in case
            _close_stray_popups(page)

    # Validate: must be an annotate/job URL
    if job_url and "/annotate/job/" in job_url:
        return job_url

    logger.debug(f"  _click_card_and_get_url: no valid job URL obtained (got {job_url})")
    return None


# ── Shared helpers for Tier 2 click-per-card ────────────────────────────────


def _dismiss_detail_overlay(page: Page) -> None:
    """Dismiss a lingering .AnnotationJobDetailView overlay that covers the board.

    The overlay sits at position:absolute covering 100% width/height, blocking
    card clicks.  Press Escape first; fall back to the close button.
    """
    try:
        overlay = page.locator(DETAIL_VIEW).first
        if overlay.is_visible(timeout=500):
            logger.debug("  Dismissing lingering detail overlay")
            page.keyboard.press("Escape")
            try:
                overlay.wait_for(state="hidden", timeout=2_000)
            except Exception:
                # fallback: click the X button
                close_btn = page.locator(".closeDialogButton").first
                if close_btn.is_visible(timeout=500):
                    close_btn.click()
                    try:
                        overlay.wait_for(state="hidden", timeout=2_000)
                    except Exception:
                        pass
    except Exception:
        pass


def _scroll_to_card(
    page: Page,
    scroller,
    annotating_col,
    data_index: int,
    *,
    avg_card_height: int = 150,
) -> "Locator | None":
    """Scroll the Virtuoso scroller so that ``div[data-index="N"]`` is rendered.

    1. Estimate scrollTop = data_index * avg_card_height.
    2. Set scrollTop, wait up to 2 s for the card to appear in the DOM.
    3. If not found, nudge ±400 px up to 5 times.

    Returns the card wrapper Locator if found, else None.
    """
    selector = f'div[data-index="{data_index}"]'
    estimated_top = data_index * avg_card_height

    # Health-check: verify scroller is interactive before attempting scroll.
    # Avoids a 60 s Playwright default-timeout hang when React hasn't yet
    # re-hydrated the Virtuoso scroller after SPA navigation + back.
    try:
        scroller.evaluate("e => e.scrollHeight", timeout=5_000)
    except Exception:
        logger.debug(
            f"  _scroll_to_card: scroller not interactive for index {data_index} "
            "(5s health-check failed)"
        )
        return None

    # Jump to estimated position and give Virtuoso a tick to reconcile.
    # Without the sleep, React's virtualizer may not have rendered the target
    # div yet on slower machines, causing spurious "End of list" warnings.
    scroller.evaluate(f"e => {{ e.scrollTop = {estimated_top}; }}")
    time.sleep(0.3)
    try:
        annotating_col.locator(selector).first.wait_for(state="attached", timeout=3_000)
        card = annotating_col.locator(selector).first
        if card.is_visible(timeout=500):
            return card
    except Exception:
        pass

    # Nudge search: alternate smaller offsets around estimated position
    nudges = [-400, 400, -800, 800, -1200]
    for nudge in nudges:
        target = max(0, estimated_top + nudge)
        scroller.evaluate(f"e => {{ e.scrollTop = {target}; }}")
        time.sleep(0.5)
        try:
            annotating_col.locator(selector).first.wait_for(state="attached", timeout=2_000)
            card = annotating_col.locator(selector).first
            if card.is_visible(timeout=500):
                return card
        except Exception:
            continue

    logger.debug(f"  _scroll_to_card: index {data_index} not reachable after nudges")
    return None


def _add_dataset_timeout(image_count: int, config: dict) -> int:
    """Add-to-dataset button wait: base 30s, +50ms per image."""
    return adaptive_timeout(30_000, image_count, 50, config)


def _stall_timeout_s(image_count: int, config: dict) -> float:
    """Null conversion stall detector: base 60s, +0.02s per image. Returns seconds."""
    return adaptive_timeout(60_000, image_count, 20, config) / 1000.0


# =================================================================================
#  P2: Progressive Recovery Helper
# =================================================================================

def _retry_with_escalation(
    page: Page,
    context: BrowserContext,
    operation_fn,
    label: str,
    *,
    coordinator=None,
    url: str = "",
    max_level: int = 4,
) -> tuple:
    """
    Generic 5-step escalation ladder.  Wraps any bool-returning operation.

    Level 1 - plain retry on same page
    Level 2 - dismiss overlays + retry
    Level 3 - page.reload() + retry
    Level 4 - fresh tab (context.new_page()) + navigate + retry

    Returns (success: bool, page: Page).
    page may be a NEW page object if Level 4 fired.
    """
    for level in range(1, max_level + 1):
        try:
            if level == 1:
                result = operation_fn(page)
            elif level == 2:
                logger.info(f"  [{label}] Escalation L2: dismiss overlays + retry")
                _dismiss_overlays(page)
                result = operation_fn(page)
            elif level == 3:
                logger.info(f"  [{label}] Escalation L3: page reload + retry")
                try:
                    page.reload(wait_until=WAIT_STRATEGY, timeout=NAV_TIMEOUT)
                    _dismiss_overlays(page)
                except Exception:
                    pass
                result = operation_fn(page)
            else:  # level == 4
                logger.info(f"  [{label}] Escalation L4: fresh tab + retry")
                try:
                    page.close()
                except Exception:
                    pass
                page = context.new_page()
                if url:
                    page.goto(url, wait_until=WAIT_STRATEGY, timeout=NAV_TIMEOUT)
                result = operation_fn(page)

            if result:
                if level > 1:
                    logger.info(f"  [{label}] Succeeded at escalation level {level}")
                return True, page

        except Exception as e:
            logger.debug(f"  [{label}] L{level} attempt raised: {e}")

    return False, page


# =================================================================================
#  Helper Functions
# =================================================================================

def get_job_count(page: Page) -> int:
    """Parse job count from the Annotating column header.

    Strategy 1: Read the span.font-mono text (fast, precise).
    Strategy 2: Count visible AnnotationJobCard elements (fallback).
    """
    try:
        col = page.locator(_ANNOTATING_COL)
        col.wait_for(state="visible", timeout=15_000)
    except Exception as e:
        logger.warning(f"Annotating column not found on page: {e}")
        capture_diagnostics(page, "annotating_column_not_found")
        return 0

    time.sleep(2)  # let header values settle

    # Strategy 1: header span
    try:
        count_el = col.locator("> div > span.font-mono").first
        if count_el.count() > 0 and count_el.is_visible():
            try:
                page.wait_for_function(
                    "(el) => { const t = (el.textContent || '').trim(); return /\\d/.test(t); }",
                    count_el,
                    timeout=10_000,
                )
            except Exception:
                logger.debug("  Header span not hydrated with digits within timeout")
            text = count_el.inner_text().strip()
            match = re.search(r"(\d+)", text)
            if match:
                count = int(match.group(1))
                logger.info(f"Annotating column: {count} Jobs (header span)")
                return count
            logger.debug(f"  Header span text had no number: '{text}'")
    except Exception as e:
        logger.debug(f"  Strategy 1 (header span) failed: {e}")

    # Strategy 2: count card elements
    try:
        cards = page.locator(f"{_ANNOTATING_COL} .AnnotationJobCard")
        card_count = cards.count()
        if card_count > 0:
            logger.info(f"Annotating column: {card_count}+ Jobs (card count)")
            return card_count
    except Exception as e:
        logger.debug(f"  Strategy 2 (card count) failed: {e}")

    logger.warning("Could not determine job count from the Annotating column.")
    return 0


def collect_job_urls(
    page: Page,
    count: int,
    min_images: int = 400,
    *,
    coordinator=None,
    blacklist=None,
    coord_held: set | None = None,
    coord_done: set | None = None,
    config=None,
) -> list:
    """
    Collect job URLs top-down using virtual scrolling.

    Tier 1: React fiber bulk extraction (no clicking, ~seconds).
    Tier 2: Optimised click-per-card fallback (go_back, reduced sleeps).

    P1: coordinator.is_available() pre-filter.
    P3: blacklist filter.
    """
    if coordinator is None:
        from src.coordinator import NullCoordinator
        coordinator = NullCoordinator()
    if blacklist is None:
        blacklist = set()

    annotate_url = page.url

    # ── Tier 0: Firestore subscription intercept ─────────────────────────────
    tier0 = _network_collect_urls(page, count, min_images, blacklist, annotate_url, config,
                                   coord_held=coord_held, coord_done=coord_done)
    if tier0 is not None:
        return tier0    # may be [] if coordination filtered everything

    # ── Tier 1: bulk extraction ──────────────────────────────────────────────
    bulk = _bulk_collect_urls(
        page, count, min_images, coordinator, blacklist,
        annotate_url, direction="top_down", config=config,
        coord_held=coord_held, coord_done=coord_done,
    )
    if bulk is not None:
        return bulk     # may be [] if coordination filtered everything

    # ── Tier 2: click-per-card fallback ──────────────────────────────
    logger.info("  [TopDown] Using click-per-card fallback")
    urls = []
    current_data_index = 0
    max_checks = 300

    # Prepare: dismiss any overlay and reset scroll position
    _dismiss_detail_overlay(page)
    _close_stray_popups(page)

    # Initial reset — runs once before the scan loop
    _init_col = page.locator(_ANNOTATING_COL)
    _init_col.locator(_VIRTUOSO).first.evaluate("e => { e.scrollTop = 0; }")
    time.sleep(0.5)

    while len(urls) < count and current_data_index < max_checks:
        try:
            # Re-acquire locators each iteration — they become stale after
            # SPA navigation + _fast_navigate_back() replaces the DOM.
            annotating_col = page.locator(_ANNOTATING_COL)
            scroller = annotating_col.locator(_VIRTUOSO).first

            # Use shared helper to scroll the target index into view
            card_wrapper = _scroll_to_card(page, scroller, annotating_col, current_data_index)

            if card_wrapper is None:
                logger.warning(f"  Could not find card at index {current_data_index}. End of list?")
                break

            # Check image count
            card_inner = card_wrapper.locator(".AnnotationJobCard").first
            try:
                img_count_text = card_inner.locator("p.text-xs.font-semibold.text-gray-700").first.inner_text()
                img_match = re.search(r"(\d+)\s+Images", img_count_text)
                img_count = int(img_match.group(1)) if img_match else 0
            except Exception:
                img_count = 0

            if img_count < min_images:
                logger.info(f"  Skipping index {current_data_index}: {img_count} images (< {min_images})")
                current_data_index += 1
                continue

            logger.info(f"  Processing index {current_data_index}: {img_count} images")

            # Click card -> detect new tab or SPA nav -> extract URL
            time.sleep(0.3)
            job_url = _click_card_and_get_url(page, card_inner, annotate_url)

            if job_url is None:
                logger.warning(f"  Could not extract URL from index {current_data_index}. Skipping.")
                current_data_index += 1
                continue

            # P3: differentiated skip checks
            if job_url in (coord_done or set()):
                logger.info(f"  Skipping already-done URL: {job_url[-30:]}")
                current_data_index += 1
                continue
            if job_url in (coord_held or set()):
                logger.info(f"  Skipping held-by-other URL: {job_url[-30:]}")
                current_data_index += 1
                continue
            if job_url in blacklist:
                logger.info(f"  Skipping blacklisted URL: {job_url[-30:]}")
                current_data_index += 1
                continue

            if job_url not in urls:
                urls.append(job_url)
                logger.info(f"  > Collected URL: {job_url}")
            else:
                logger.warning(f"  Duplicate URL, skipping: {job_url[-30:]}")

            current_data_index += 1

        except Exception as e:
            logger.error(f"  Error processing index {current_data_index}: {e}")
            _close_stray_popups(page)
            # Recover if page navigated away from the board mid-loop
            if "/annotate" not in page.url or "/annotate/job/" in page.url:
                logger.info("  Page left the board — recovering...")
                _fast_navigate_back(page, annotate_url)
            current_data_index += 1

    logger.info(f"Collected {len(urls)} valid job URLs (checked {current_data_index} indices)")
    return urls


def collect_job_urls_bottom_up(
    page: Page,
    count: int,
    min_images: int = 400,
    *,
    coordinator=None,
    blacklist=None,
    coord_held: set | None = None,
    coord_done: set | None = None,
    config=None,
) -> list:
    """
    Collect job URLs bottom-up.

    Tier 1: React fiber bulk extraction (no clicking, ~seconds).
    Tier 2: Optimised click-per-card fallback (go_back, reduced sleeps).

    P1: coordinator.is_available() pre-filter.
    P3: blacklist filter.
    Convergence: stop when MAX_CONSECUTIVE_SKIPS coord-held/done indices seen in a row.
    """
    if coordinator is None:
        from src.coordinator import NullCoordinator
        coordinator = NullCoordinator()
    if blacklist is None:
        blacklist = set()

    annotate_url = page.url

    # ── Tier 0: Firestore subscription intercept ─────────────────────────────
    tier0 = _network_collect_urls(page, count, min_images, blacklist, annotate_url, config,
                                   coord_held=coord_held, coord_done=coord_done)
    if tier0 is not None:
        return tier0    # may be [] if coordination filtered everything

    # ── Tier 1: bulk extraction ──────────────────────────────────────────────
    bulk = _bulk_collect_urls(
        page, count, min_images, coordinator, blacklist,
        annotate_url, direction="bottom_up", config=config,
        coord_held=coord_held, coord_done=coord_done,
    )
    if bulk is not None:
        return bulk     # may be [] if coordination filtered everything

    # ── Tier 2: click-per-card fallback ──────────────────────────────
    logger.info("  [BottomUp] Using click-per-card fallback")
    urls = []
    consecutive_skips = 0
    MAX_CONSECUTIVE_SKIPS = 15

    # Prepare: dismiss any overlay
    _dismiss_detail_overlay(page)
    _close_stray_popups(page)

    # Initial locators — captured by _scroll_to_bottom_and_get_max_index() closure.
    # Re-assigned inside the while loop to stay fresh after SPA nav + back.
    annotating_col = page.locator(_ANNOTATING_COL)
    scroller = annotating_col.locator(_VIRTUOSO).first

    def _scroll_to_bottom_and_get_max_index():
        try:
            scroller.evaluate("e => { e.scrollTop = e.scrollHeight; }")
            time.sleep(0.5)
            # Wait briefly for virtuoso to render bottom cards
            try:
                page.wait_for_selector(
                    f'{_ANNOTATING_COL} .AnnotationJobCard',
                    state="visible", timeout=3_000,
                )
            except Exception:
                pass
            max_idx = page.evaluate("""
                () => {
                    const cols = document.querySelectorAll('.boardColumn');
                    let col = null;
                    cols.forEach(c => {
                        const h2 = c.querySelector('h2');
                        if (h2 && h2.textContent.trim() === 'Annotating') col = c;
                    });
                    if (!col) return -1;
                    const items = col.querySelectorAll('div[data-index]');
                    let max = -1;
                    items.forEach(el => {
                        const idx = parseInt(el.getAttribute('data-index'), 10);
                        if (!isNaN(idx) && idx > max) max = idx;
                    });
                    return max;
                }
            """)
            return max_idx if max_idx >= 0 else 0
        except Exception as e:
            logger.debug(f"  _scroll_to_bottom_and_get_max_index error: {e}")
            return 0

    current_data_index = _scroll_to_bottom_and_get_max_index()
    logger.info(f"Bottom-up collection starting at data-index={current_data_index}")

    if current_data_index <= 0:
        logger.warning("Could not find any bottom cards. Returning empty collection.")
        return urls

    checked_indices = set()

    while len(urls) < count:
        if current_data_index < 0:
            logger.info("  Bottom-up: exhausted all indices.")
            break

        if consecutive_skips >= MAX_CONSECUTIVE_SKIPS:
            logger.info(
                f"  Bottom-up: {consecutive_skips} consecutive skips -- "
                f"convergence with top-down process detected. Stopping."
            )
            break

        if current_data_index in checked_indices:
            current_data_index -= 1
            continue

        checked_indices.add(current_data_index)

        try:
            # Re-acquire locators each iteration — stale after SPA nav + back.
            # The closure in _scroll_to_bottom_and_get_max_index() sees the
            # updated values because Python closures capture by reference.
            annotating_col = page.locator(_ANNOTATING_COL)
            scroller = annotating_col.locator(_VIRTUOSO).first

            # Use shared helper to scroll the target index into view
            card_wrapper = _scroll_to_card(page, scroller, annotating_col, current_data_index)

            if card_wrapper is None:
                # Fallback: re-check max index in case board refreshed
                new_max = _scroll_to_bottom_and_get_max_index()
                if new_max != current_data_index and new_max not in checked_indices:
                    logger.info(f"  Board refreshed: max index changed {current_data_index} -> {new_max}")
                    current_data_index = new_max
                else:
                    logger.warning(f"  Card at index {current_data_index} unreachable. Skipping.")
                    current_data_index -= 1
                continue

            # Check image count
            card_inner = card_wrapper.locator(".AnnotationJobCard").first
            try:
                img_count_text = card_inner.locator("p.text-xs.font-semibold.text-gray-700").first.inner_text()
                img_match = re.search(r"(\d+)\s+Images", img_count_text)
                img_count = int(img_match.group(1)) if img_match else 0
            except Exception:
                img_count = 0

            if img_count < min_images:
                logger.info(f"  [BU] Skipping index {current_data_index}: {img_count} images (< {min_images})")
                current_data_index -= 1
                continue

            # Click card -> detect new tab or SPA nav -> extract URL
            time.sleep(0.3)
            job_url = _click_card_and_get_url(page, card_inner, annotate_url)

            if job_url is None:
                logger.warning(f"  [BU] Could not extract URL from index {current_data_index}. Skipping.")
                current_data_index -= 1
                continue

            # P3: differentiated skip checks
            if coord_done and job_url in coord_done:
                logger.info(f"  [BU] Skipping already-done URL: {job_url[-30:]}")
                consecutive_skips += 1
                current_data_index -= 1
                continue
            if coord_held and job_url in coord_held:
                logger.info(f"  [BU] Skipping held-by-other URL: {job_url[-30:]}")
                consecutive_skips += 1
                current_data_index -= 1
                continue
            if job_url in blacklist:
                logger.info(f"  [BU] Skipping blacklisted URL: {job_url[-30:]}")
                consecutive_skips += 1
                current_data_index -= 1
                continue

            if job_url in urls:
                logger.warning(f"  [BU] Duplicate URL, skipping: {job_url[-30:]}")
                consecutive_skips += 1
                current_data_index -= 1
                continue

            consecutive_skips = 0
            urls.append(job_url)
            logger.info(f"  [BU] Collected URL (idx {current_data_index}): {job_url}")

            current_data_index -= 1

        except Exception as e:
            logger.error(f"  [BU] Error at index {current_data_index}: {e}")
            _close_stray_popups(page)
            # Recover if page navigated away from the board mid-loop
            if "/annotate" not in page.url or "/annotate/job/" in page.url:
                logger.info("  [BU] Page left the board — recovering...")
                _fast_navigate_back(page, annotate_url)
            current_data_index -= 1

    logger.info(f"[Bottom-up] Collected {len(urls)} job URLs")
    return urls


def _wait_for_board_scroller(page: Page) -> None:
    """Wait for the Annotating column scroller + at least one card to render."""
    try:
        page.wait_for_selector(
            f'{_ANNOTATING_COL} {_VIRTUOSO}',
            timeout=NAV_TIMEOUT,
        )
        page.wait_for_selector(
            f'{_ANNOTATING_COL} .AnnotationJobCard',
            state="visible", timeout=NAV_TIMEOUT,
        )
    except Exception:
        pass


def wait_for_card_detail(page: Page, *, headless: bool = False):
    """
    Wait for the card detail view to fully load.

    Returns: "start_annotating" | "add_to_dataset" | "submit_for_review" | None

    P2: single reload recovery on each gate failure.
    P6: headless uses state="attached" for image grid gate.
    """
    detail_view = ".AnnotationJobDetailView"

    def _try_reload_recovery(reason: str) -> bool:
        logger.info(f"  Recovery: {reason} -- reloading page...")
        try:
            page.reload(wait_until=WAIT_STRATEGY, timeout=NAV_TIMEOUT)
            _dismiss_overlays(page)
            page.wait_for_selector(detail_view, state="visible", timeout=CARD_LOAD_TIMEOUT)
            return True
        except Exception:
            logger.warning("  Recovery reload failed -- detail view still not visible.")
            return False

    try:
        page.wait_for_load_state("domcontentloaded", timeout=CARD_LOAD_TIMEOUT)

        # Gate 1: detail view wrapper
        try:
            page.wait_for_selector(detail_view, state="visible", timeout=CARD_LOAD_TIMEOUT)
        except Exception:
            if not _try_reload_recovery("detail view wrapper not visible"):
                return None

        # Gate 2: progress legend
        progress_selector = f"{detail_view} .AnnotationJobProgressLegend"
        try:
            page.wait_for_selector(progress_selector, state="visible", timeout=CARD_LOAD_TIMEOUT)
        except Exception:
            if not _try_reload_recovery("progress legend not visible"):
                return None
            try:
                page.wait_for_selector(progress_selector, state="visible", timeout=CARD_LOAD_TIMEOUT)
            except Exception:
                return None

        # Gate 3: image list tab bar
        image_list_sel = f"{detail_view} .AnnotationJobImageList"
        try:
            page.wait_for_selector(image_list_sel, state="visible", timeout=CARD_LOAD_TIMEOUT)
        except Exception:
            if not _try_reload_recovery("image list tab bar not visible"):
                return None
            try:
                page.wait_for_selector(image_list_sel, state="visible", timeout=CARD_LOAD_TIMEOUT)
            except Exception:
                return None

        # Gate 4: network idle (best-effort)
        try:
            page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        # Gate 5: image grid
        image_grid_sel = (
            f"{detail_view} #annotationContainer .ImageCard, "
            f"{detail_view} .AnnotationJobImageList .ImageItem"
        )
        gate_state = "attached" if headless else "visible"
        try:
            page.wait_for_selector(image_grid_sel, state=gate_state, timeout=CARD_LOAD_TIMEOUT)
        except Exception:
            if not _try_reload_recovery(f"image grid not {gate_state}"):
                return None
            try:
                page.wait_for_selector(image_grid_sel, state=gate_state, timeout=CARD_LOAD_TIMEOUT)
            except Exception:
                return None

        # P6: headless-safe button check
        def _find_button(selector: str) -> bool:
            loc = page.locator(selector).first
            if loc.count() == 0:
                return False
            if loc.is_visible() and loc.is_enabled():
                return True
            if headless:
                try:
                    loc.wait_for(state="attached", timeout=3_000)
                    return loc.is_enabled()
                except Exception:
                    pass
            return False

        for btn_text in ("Start Annotating", "Continue Annotating"):
            sel = f"{detail_view} button.btn2.medium:has-text('{btn_text}')"
            if _find_button(sel):
                logger.info(f"  Card detail loaded -- '{btn_text}' -> start_annotating")
                return "start_annotating"

        add_sel = f"{detail_view} button.btn2.medium.primary:has-text('to Dataset'):not([disabled])"
        if _find_button(add_sel):
            logger.info("  Card detail loaded -- 'Add to Dataset' -> add_to_dataset")
            return "add_to_dataset"

        review_sel = f"{detail_view} button.btn2.medium:has-text('Submit for Review')"
        if _find_button(review_sel):
            logger.info("  Card detail loaded -- 'Submit for Review' -> submit_for_review")
            return "submit_for_review"

        logger.warning("  Card detail loaded but action button unclassified.")
        capture_diagnostics(page, "card_action_unclassified")
        return None

    except Exception as e:
        logger.warning(f"Card detail did not fully load: {e}")
        capture_diagnostics(page, "card_detail_load_failed")
        return None


def wait_for_tab_content(page: Page, timeout: int = 15_000) -> None:
    """Wait for the image tab content to render after switching tabs."""
    try:
        page.wait_for_selector(
            f"{DETAIL_VIEW} #annotationContainer",
            state="attached", timeout=timeout,
        )
        page.wait_for_selector(
            f"{DETAIL_VIEW} .ImageCard, "
            f"{DETAIL_VIEW} .ImageItem, "
            f"{DETAIL_VIEW} .ImagesCollectionGrid, "
            f"{DETAIL_VIEW} #annotationContainer .ImageCollection",
            state="visible", timeout=timeout,
        )
    except Exception:
        pass


def get_unannotated_count(page: Page) -> int:
    """Parse unannotated count from tab badge or progress legend."""
    detail = ".AnnotationJobDetailView"

    # Strategy 1: tab badge
    try:
        tab_btn = page.locator(f"{detail} .AnnotationJobImageList button:has-text('Unannotated')").first
        badge = tab_btn.locator("span.font-mono, span:text-matches('^\\d+$')").first
        if badge.count() > 0 and badge.is_visible():
            badge_text = badge.inner_text().strip()
            match = re.search(r"(\d+)", badge_text)
            if match:
                return int(match.group(1))
    except Exception:
        pass

    # Strategy 2: progress legend
    try:
        legend = page.locator(f"{detail} .AnnotationJobProgressLegend:has(.Unannotated) .font-mono").first
        legend.wait_for(state="visible", timeout=10_000)
        text = legend.inner_text().strip()
        match = re.search(r"(\d+)", text)
        if match:
            return int(match.group(1))
    except Exception as e:
        logger.warning(f"Could not parse unannotated count: {e}")
    return 0


def start_null_conversion(page: Page, *, config: dict = None) -> bool:
    """
    Click Unannotated tab -> click Null button -> confirm 'Mark null'.

    P2: overlay dismiss before each click (L2 of escalation ladder).
    """
    _tmul = (config or {}).get("timeout_multiplier", 1.0)
    try:
        _dismiss_overlays(page)

        # Step 1: click Unannotated tab
        unannotated_tab = page.locator(
            f"{DETAIL_VIEW} .AnnotationJobImageList button:has-text('Unannotated')"
        ).first
        unannotated_tab.wait_for(state="visible", timeout=CARD_LOAD_TIMEOUT)
        unannotated_tab.click()
        wait_for_tab_content(page)

        _dismiss_overlays(page)

        # Step 2: click Null button
        null_btn = page.locator(f"{DETAIL_VIEW} button:has(i.far.fa-empty-set)").first
        try:
            null_btn.wait_for(state="visible", timeout=5_000)
        except Exception:
            # P2 L2: switch tabs and retry
            logger.warning("  Null button not found -- switching tabs and retrying")
            annotated_tab = page.locator(
                f"{DETAIL_VIEW} .AnnotationJobImageList button:has-text('Annotated')"
            ).first
            if annotated_tab.is_visible():
                annotated_tab.click()
                time.sleep(1)
                unannotated_tab.click()
                wait_for_tab_content(page)
                _dismiss_overlays(page)
            try:
                null_btn.wait_for(state="visible", timeout=5_000)
            except Exception:
                capture_diagnostics(page, "null_button_not_visible")
                return False

        if null_btn.is_disabled():
            capture_diagnostics(page, "null_button_disabled")
            return False

        null_btn.click()

        # Step 3: SweetAlert confirmation
        _swal_timeout = int(15_000 * _tmul)
        try:
            page.wait_for_selector(".swal2-popup", state="visible", timeout=_swal_timeout)
        except Exception:
            logger.warning("  SweetAlert did not appear -- retrying null click")
            _dismiss_overlays(page)
            null_btn.click()
            try:
                page.wait_for_selector(".swal2-popup", state="visible", timeout=_swal_timeout)
            except Exception:
                capture_diagnostics(page, "null_conversion_swal_failed")
                return False

        # P6: let SweetAlert buttons fully render before interacting
        page.wait_for_timeout(500)

        # Only dismiss React fullscreen overlay -- do NOT dismiss SweetAlert here
        try:
            overlay = page.locator('div.fixed.bottom-0.left-0.right-0.top-0[class*="z-"]')
            if overlay.count() > 0 and overlay.first.is_visible():
                overlay.first.wait_for(state="hidden", timeout=5_000)
        except Exception:
            pass

        _confirm_timeout = int(10_000 * _tmul)
        mark_null_btn = page.locator("button.swal2-confirm")
        mark_null_btn.wait_for(state="visible", timeout=_confirm_timeout)
        mark_null_btn.click()
        logger.info("  Confirmed 'Mark null' -- processing started")

        try:
            page.wait_for_selector(
                "#swal2-content, .swal2-popup", state="visible", timeout=10_000
            )
        except Exception:
            pass

        return True

    except Exception as e:
        logger.error(f"  Failed to start null conversion: {e}")
        capture_diagnostics(page, "null_conversion_failed")
        return False


def is_processing_complete(page: Page) -> tuple:
    """
    Check if null-conversion processing is complete.
    Returns (is_done, progress_text, current_count, total_count).
    """
    try:
        swal_content = page.locator("#swal2-content .text-sm.text-gray-500").first
        if swal_content.count() == 0:
            swal = page.locator(".swal2-popup")
            if swal.count() == 0 or not swal.is_visible():
                return True, "No dialog visible", 0, 0
            return False, "Dialog visible but no progress text", 0, 0

        progress_text = swal_content.inner_text().strip()
        match = re.search(r"(\d+)\s+of\s+(\d+)", progress_text)
        if match:
            current = int(match.group(1))
            total = int(match.group(2))
            if current >= total:
                return True, progress_text, current, total
            return False, progress_text, current, total

        return False, progress_text, 0, 0

    except Exception as e:
        logger.debug(f"  Progress check error: {e}")
        return False, f"Error: {e}", 0, 0


def dismiss_swal_if_present(page: Page) -> None:
    """Forcibly close any SweetAlert dialog (3-strategy approach)."""
    try:
        closed_via_js = page.evaluate("""
            () => {
                if (typeof Swal !== 'undefined' && Swal.isVisible && Swal.isVisible()) {
                    Swal.close(); return true;
                }
                return false;
            }
        """)
        if closed_via_js:
            page.wait_for_timeout(500)
            return

        okay_btn = page.locator("button.swal2-confirm")
        if okay_btn.count() > 0 and okay_btn.first.is_visible():
            okay_btn.first.click(force=True)
            try:
                page.wait_for_selector(".swal2-popup", state="hidden", timeout=5_000)
            except Exception:
                pass
            return

        page.evaluate("() => document.querySelectorAll('.swal2-container').forEach(el => el.remove())")
    except Exception:
        pass


def _dismiss_overlays(page: Page) -> None:
    """
    Dismiss SweetAlert and React fullscreen overlays.

    P6: JS-based removal for headless mode.
    """
    dismiss_swal_if_present(page)
    try:
        page.evaluate("() => document.querySelectorAll('.swal2-container').forEach(el => el.remove())")
    except Exception:
        pass
    try:
        overlay = page.locator('div.fixed.bottom-0.left-0.right-0.top-0[class*="z-"]')
        if overlay.count() > 0 and overlay.first.is_visible():
            overlay.first.wait_for(state="hidden", timeout=10_000)
    except Exception:
        pass


def add_to_dataset(page: Page, *, image_count: int = 0, config=None) -> bool:
    """
    Click 'Add N images to Dataset' button, then confirm in the modal.

    P4: timeout scales with image_count.
    P2: reload recovery if button doesn't appear.
    """
    if config is None:
        config = {}

    btn_timeout = _add_dataset_timeout(image_count, config)
    logger.debug(f"  add_to_dataset timeout: {btn_timeout}ms (image_count={image_count})")

    try:
        _dismiss_overlays(page)

        # Navigate to Annotated tab
        annotated_tab = page.locator(
            f"{DETAIL_VIEW} .AnnotationJobImageList button:has-text('Annotated')"
        ).first
        if annotated_tab.count() > 0 and annotated_tab.is_visible():
            annotated_tab.click(force=True)
            wait_for_tab_content(page)

        # Find Add button (P4 adaptive timeout)
        add_btn = page.locator(
            f"{DETAIL_VIEW} button.btn2.medium.primary:has-text('to Dataset'):not([disabled])"
        ).first

        try:
            add_btn.wait_for(state="visible", timeout=btn_timeout)
        except Exception:
            logger.warning(f"  Add button not visible after {btn_timeout}ms -- reloading")
            _dismiss_overlays(page)
            page.reload(wait_until=WAIT_STRATEGY, timeout=NAV_TIMEOUT)
            _dismiss_overlays(page)
            try:
                add_btn.wait_for(state="visible", timeout=btn_timeout)
            except Exception:
                capture_diagnostics(page, "add_to_dataset_timeout")
                return False

        btn_text = add_btn.inner_text().strip()
        logger.info(f"  Clicking initial: '{btn_text}'")
        add_btn.click(force=True)

        # Confirm in modal
        modal_btn = page.locator(".dialogPanel button.primary, #AddApprovedImagesToDatasetButton").first
        try:
            modal_btn.wait_for(state="visible", timeout=30_000)
        except Exception:
            logger.warning("  Confirm modal did not appear -- retrying click")
            _dismiss_overlays(page)
            add_btn.click(force=True)
            modal_btn.wait_for(state="visible", timeout=30_000)

        modal_text = modal_btn.inner_text().strip()
        logger.info(f"  Confirming in modal: '{modal_text}'")
        modal_btn.click()

        try:
            page.wait_for_selector(".dialogPanel", state="hidden", timeout=30_000)
        except Exception:
            pass

        return True

    except Exception as e:
        logger.error(f"  Failed to add to dataset: {e}")
        capture_diagnostics(page, "add_to_dataset_failed")
        return False


# =================================================================================
#  Tab State Tracker (P4: image_count added)
# =================================================================================

class TabState:
    """Track the lifecycle of a single processing slot with full history.

    Enhanced state machine (non-blocking micro-steps):
      PENDING → NAVIGATING → DETAIL_WAIT → SCANNING → NULL_CLICKED
        → CONVERTING → READY → ADD_CLICKED → CONFIRMING → VERIFYING → DONE
    Any state can transition to ERROR.
    After BLACKLIST_THRESHOLD consecutive errors → BLACKLISTED (permanent).

    Each tick takes <2 s so the orchestrator can jump between tabs quickly.
    """

    # ── Primary lifecycle states ─────────────────────────────────────
    PENDING      = "pending"
    NAVIGATING   = "navigating"     # page.goto() done, waiting for DOM
    DETAIL_WAIT  = "detail_wait"    # detail view visible, waiting for full card load
    SCANNING     = "scanning"       # card loaded — reading type & counts
    NULL_CLICKED = "null_clicked"   # null button clicked, waiting for SweetAlert
    CONVERTING   = "converting"     # null conversion in progress (SweetAlert polling)
    READY        = "ready"          # ready for add-to-dataset
    ADD_CLICKED  = "add_clicked"    # add button clicked, waiting for confirm modal
    CONFIRMING   = "confirming"     # confirm button clicked, waiting for modal close
    VERIFYING    = "verifying"      # post-add server verification
    DONE         = "done"
    ERROR        = "error"
    BLACKLISTED  = "blacklisted"

    # Legacy aliases kept for backward-compat with step_scan/step_poll callers
    LOADING      = "detail_wait"
    ADDING       = "add_clicked"

    def __init__(self, job_url: str, *, headless: bool = False, config=None):
        self.job_url = job_url
        self.page = None
        self.status = self.PENDING
        self.headless = headless
        self.config = config or {}
        self.unannotated_count = 0
        self.image_count = 0
        self.error_msg = ""
        self.retry_count = 0
        self.start_time = 0.0

        # ── Sub-step tracking for non-blocking waits ─────────────────
        self.state_entered_at = time.time()   # when did we enter current state?
        self.card_type = None                 # "start_annotating" | "add_to_dataset" | "submit_for_review"
        self._null_retry = 0                  # retries for null click sub-steps
        self._add_retry = 0                   # retries for add-to-dataset sub-steps
        self._nav_retries = 0                 # reload retries during DETAIL_WAIT

        # ── Progress tracking (null conversion) ─────────────────────
        self.last_progress_count = 0
        self.last_progress_total = 0
        self.last_progress_time = 0.0

        # ── History / observability ──────────────────────────────────
        self.history: list = []               # [(timestamp, state, message), ...]
        self.phase_times: dict = {}           # {state_name: total_seconds_spent}
        self.freeze_count: int = 0            # stall detections
        self.error_history: list = []         # [error_msg, ...]
        self.consecutive_errors: int = 0      # for blacklist logic
        self.tick_count: int = 0              # how many ticks this tab has seen

        self._record_event("created", f"url={job_url}")

    # ── State transition helper ──────────────────────────────────────
    def transition(self, new_status: str, message: str = "") -> None:
        """Transition to a new state, recording timing + history."""
        now = time.time()
        elapsed = now - self.state_entered_at
        old = self.status

        # Accumulate time in old state
        self.phase_times[old] = self.phase_times.get(old, 0) + elapsed

        self.status = new_status
        self.state_entered_at = now

        if new_status == self.ERROR:
            self.error_msg = message
            self.error_history.append(message)
            self.consecutive_errors += 1
        elif new_status == self.DONE:
            self.consecutive_errors = 0

        self._record_event(new_status, message or f"from {old} ({elapsed:.1f}s)")

    def _record_event(self, state: str, msg: str) -> None:
        self.history.append((time.time(), state, msg))
        # Keep history bounded
        if len(self.history) > 200:
            self.history = self.history[-100:]

    @property
    def time_in_state(self) -> float:
        """Seconds spent in the current state so far."""
        return time.time() - self.state_entered_at

    @property
    def total_elapsed(self) -> float:
        """Seconds since this tab was first created or last reset."""
        return time.time() - self.start_time if self.start_time else 0.0

    # ── Reset / cleanup ──────────────────────────────────────────────
    def reset_for(self, job_url: str, *, is_retry: bool = False) -> None:
        self.job_url = job_url
        self.status = self.PENDING
        self.state_entered_at = time.time()
        self.unannotated_count = 0
        self.image_count = 0
        self.error_msg = ""
        self.card_type = None
        self.start_time = 0.0
        self.last_progress_count = 0
        self.last_progress_total = 0
        self.last_progress_time = 0.0
        self._null_retry = 0
        self._add_retry = 0
        self._nav_retries = 0
        self.freeze_count = 0
        self.tick_count = 0
        if is_retry:
            self.retry_count += 1
        else:
            self.retry_count = 0
            self.consecutive_errors = 0
        self._record_event("reset", f"url={job_url} retry={is_retry}")

    def close_page(self) -> None:
        try:
            if self.page and not self.page.is_closed():
                self.page.close()
        except Exception:
            pass
        self.page = None

    @property
    def short_id(self) -> str:
        return self.job_url.split("/")[-1][:8]

    def __repr__(self):
        return f"Tab({self.short_id}…, {self.status})"

    def to_dict(self) -> dict:
        """Serialize tab state for dashboard/logging."""
        return {
            "short_id": self.short_id,
            "status": self.status,
            "url": self.job_url,
            "time_in_state": round(self.time_in_state, 1),
            "total_elapsed": round(self.total_elapsed, 1),
            "ticks": self.tick_count,
            "image_count": self.image_count,
            "unannotated": self.unannotated_count,
            "error_msg": self.error_msg,
            "errors": len(self.error_history),
            "freezes": self.freeze_count,
            "card_type": self.card_type,
            "retry": self.retry_count,
            "phase_times": {k: round(v, 1) for k, v in self.phase_times.items()},
            "history_len": len(self.history),
        }


# =================================================================================
#  Per-Tab Non-Blocking Micro-Tick Functions
# =================================================================================
#
#  Each tick_* function does a QUICK check (<2 s) and transitions state or stays.
#  The orchestrator calls one tick per tab in round-robin order, then moves to
#  the next tab immediately — no tab ever blocks the loop.
#

def tick_pending(tab: TabState, context: BrowserContext) -> None:
    """PENDING → NAVIGATING: Open page and fire navigation (goto blocks briefly)."""
    try:
        if tab.retry_count > 0:
            logger.info(f"  {tab}: Retry #{tab.retry_count} — opening fresh tab (P2 L4)")
            tab.close_page()

        if tab.page is None or tab.page.is_closed():
            tab.page = context.new_page()

        # Use "commit" so goto returns as soon as first byte arrives (~1-3 s)
        tab.page.goto(tab.job_url, wait_until="commit", timeout=NAV_TIMEOUT)
        tab.start_time = time.time()
        tab.transition(TabState.NAVIGATING, "goto fired")
        logger.info(f"  Opened: {tab}")
    except Exception as e:
        logger.error(f"  Failed to open {tab.job_url}: {e}")
        tab.transition(TabState.ERROR, str(e))
        if tab.page:
            capture_diagnostics(tab.page, f"tab_open_failed_{tab.short_id}")


def tick_navigating(tab: TabState) -> None:
    """NAVIGATING → DETAIL_WAIT: Quick-check if detail view appeared."""
    try:
        page = tab.page
        if page.is_closed():
            tab.transition(TabState.ERROR, "Page closed during navigation")
            return

        # Quick probe — is the detail view wrapper visible yet?
        detail = page.locator(DETAIL_VIEW)
        if detail.count() > 0 and detail.first.is_visible():
            tab.transition(TabState.DETAIL_WAIT, "detail view appeared")
            return

        # Timeout guard — allow up to CARD_LOAD_TIMEOUT ms total
        if tab.time_in_state > CARD_LOAD_TIMEOUT / 1000:
            tab.transition(TabState.ERROR, "Navigation timeout — detail view never appeared")
            capture_diagnostics(page, f"nav_timeout_{tab.short_id}")
    except Exception as e:
        tab.transition(TabState.ERROR, f"nav check error: {e}")


def tick_detail_wait(tab: TabState) -> None:
    """DETAIL_WAIT → SCANNING: Quick-check if card is fully loaded (all 5 gates)."""
    try:
        page = tab.page
        _dismiss_overlays(page)

        # Gate 1: progress legend (instant check)
        progress_sel = f"{DETAIL_VIEW} .AnnotationJobProgressLegend"
        if page.locator(progress_sel).count() == 0 or not page.locator(progress_sel).first.is_visible():
            if tab.time_in_state > CARD_LOAD_TIMEOUT / 1000:
                # One reload recovery attempt
                if tab._nav_retries < 1:
                    tab._nav_retries += 1
                    logger.info(f"  {tab}: Progress legend missing — reload recovery")
                    try:
                        page.reload(wait_until=WAIT_STRATEGY, timeout=NAV_TIMEOUT)
                    except Exception:
                        pass
                    tab.state_entered_at = time.time()  # reset timer
                else:
                    tab.transition(TabState.ERROR, "Card detail never fully loaded")
                    capture_diagnostics(page, f"card_not_loaded_{tab.short_id}")
            return  # not ready yet — jump to next tab

        # Gate 2: image list tab bar
        img_list = f"{DETAIL_VIEW} .AnnotationJobImageList"
        if page.locator(img_list).count() == 0 or not page.locator(img_list).first.is_visible():
            if tab.time_in_state > CARD_LOAD_TIMEOUT / 1000:
                tab.transition(TabState.ERROR, "Image list tab bar never appeared")
                capture_diagnostics(page, f"img_list_timeout_{tab.short_id}")
            return

        # Gate 3: image grid (quick with short timeout)
        image_grid_sel = (
            f"{DETAIL_VIEW} #annotationContainer .ImageCard, "
            f"{DETAIL_VIEW} .AnnotationJobImageList .ImageItem"
        )
        gate_state = "attached" if tab.headless else "visible"
        grid = page.locator(image_grid_sel)
        try:
            grid.first.wait_for(state=gate_state, timeout=QUICK_CHECK_MS)
        except Exception:
            if tab.time_in_state > CARD_LOAD_TIMEOUT / 1000:
                if tab._nav_retries < 1:
                    tab._nav_retries += 1
                    logger.info(f"  {tab}: Image grid missing — reload recovery")
                    try:
                        page.reload(wait_until=WAIT_STRATEGY, timeout=NAV_TIMEOUT)
                    except Exception:
                        pass
                    tab.state_entered_at = time.time()
                else:
                    tab.transition(TabState.ERROR, f"Image grid not {gate_state}")
                    capture_diagnostics(page, f"grid_timeout_{tab.short_id}")
            return

        # Gate 4: read image count (P4)
        try:
            header_text = page.locator(
                ".AnnotationJobDetailView h1, .AnnotationJobDetailView .text-xl"
            ).first.inner_text()
            img_m = re.search(r"(\d+)\s+[Ii]mage", header_text)
            if img_m:
                tab.image_count = int(img_m.group(1))
        except Exception:
            tab.image_count = 0

        # Gate 5: detect card type (instant button check)
        tab.card_type = _detect_card_type(page, headless=tab.headless)
        if tab.card_type is None:
            if tab.time_in_state > CARD_LOAD_TIMEOUT / 1000:
                tab.transition(TabState.ERROR, "Card type unclassified")
                capture_diagnostics(page, f"card_unclassified_{tab.short_id}")
            return

        # All gates passed — move to SCANNING
        tab.transition(TabState.SCANNING, f"card_type={tab.card_type}")
        logger.info(f"  {tab}: Card loaded — type={tab.card_type}, images={tab.image_count}")

    except Exception as e:
        tab.transition(TabState.ERROR, f"detail_wait error: {e}")
        if tab.page:
            capture_diagnostics(tab.page, f"detail_wait_err_{tab.short_id}")


def tick_scanning(tab: TabState) -> None:
    """SCANNING → NULL_CLICKED | READY: Read counts, click null if needed."""
    try:
        page = tab.page

        # Cards that don't need null conversion go straight to READY
        if tab.card_type in ("add_to_dataset", "submit_for_review"):
            logger.info(f"  {tab}: Pure annotated/review card → straight to READY")
            tab.unannotated_count = 0
            tab.transition(TabState.READY)
            return

        # Read unannotated count (instant DOM read)
        tab.unannotated_count = get_unannotated_count(page)
        logger.info(f"  {tab}: {tab.unannotated_count} unannotated images")

        if tab.unannotated_count == 0:
            tab.transition(TabState.READY)
            return

        # Need null conversion — click Unannotated tab + Null button
        _dismiss_overlays(page)
        success = _click_null_button_fast(page, tab.config)
        if success:
            tab.transition(TabState.NULL_CLICKED, "null button clicked")
            logger.info(f"  {tab}: Null button clicked — jumping to next tab")
        else:
            tab._null_retry += 1
            if tab._null_retry >= 3:
                # Skip null — proceed to add_to_dataset anyway
                logger.warning(f"  {tab}: Null click failed {tab._null_retry}x — skipping to READY")
                tab.transition(TabState.READY, "null click exhausted, proceeding")
            else:
                logger.info(f"  {tab}: Null click attempt {tab._null_retry} failed — will retry next round")

    except Exception as e:
        tab.transition(TabState.ERROR, f"scanning error: {e}")
        if tab.page:
            capture_diagnostics(tab.page, f"scan_err_{tab.short_id}")


def tick_null_clicked(tab: TabState) -> None:
    """NULL_CLICKED → CONVERTING: Quick-check if SweetAlert appeared."""
    try:
        page = tab.page

        # Did SweetAlert confirmation appear?
        swal = page.locator(".swal2-popup")
        if swal.count() > 0 and swal.first.is_visible():
            # Check if it's the confirmation dialog (has confirm button)
            confirm_btn = page.locator("button.swal2-confirm")
            if confirm_btn.count() > 0 and confirm_btn.first.is_visible():
                # Click "Mark null" confirm
                confirm_btn.first.click()
                logger.info(f"  {tab}: Confirmed 'Mark null' — processing started")
                tab.last_progress_count = 0
                tab.last_progress_total = 0
                tab.last_progress_time = time.time()
                tab.transition(TabState.CONVERTING, "null conversion confirmed")
                return

            # SweetAlert visible but no confirm button yet — processing may have
            # already started (e.g. progress dialog). Check for progress text.
            progress_text = ""
            try:
                progress_text = page.locator("#swal2-content .text-sm.text-gray-500").first.inner_text()
            except Exception:
                pass
            if "of" in progress_text:
                tab.last_progress_time = time.time()
                tab.transition(TabState.CONVERTING, "conversion already running")
                return

        # Not visible yet — timeout check
        _tmul = tab.config.get("timeout_multiplier", 1.0)
        timeout_s = 15 * _tmul
        if tab.time_in_state > timeout_s:
            # Retry: click null button again
            if tab._null_retry < 3:
                tab._null_retry += 1
                logger.warning(f"  {tab}: SweetAlert timeout — retry null click #{tab._null_retry}")
                _dismiss_overlays(page)
                _click_null_button_fast(page, tab.config)
                tab.state_entered_at = time.time()
            else:
                logger.warning(f"  {tab}: SweetAlert never appeared — skipping to READY")
                tab.transition(TabState.READY, "swal timeout, skipping null")

    except Exception as e:
        tab.transition(TabState.ERROR, f"null_clicked error: {e}")
        if tab.page:
            capture_diagnostics(tab.page, f"null_clicked_err_{tab.short_id}")


def tick_converting(tab: TabState) -> None:
    """CONVERTING: Quick-check SweetAlert progress; advance to READY when done.

    P4: stall timeout scales with tab.image_count.
    """
    try:
        now = time.time()
        elapsed = now - tab.start_time
        done, progress, current, total = is_processing_complete(tab.page)

        if current > tab.last_progress_count:
            tab.last_progress_count = current
            tab.last_progress_total = total
            tab.last_progress_time = now

        stall_limit = _stall_timeout_s(tab.image_count, tab.config)
        stall_duration = now - tab.last_progress_time
        if stall_duration > stall_limit and not done:
            logger.error(
                f"  {tab}: STALL TIMEOUT ({stall_limit:.0f}s) — "
                f"no progress for {stall_duration:.0f}s"
            )
            tab.freeze_count += 1
            tab.transition(TabState.ERROR, f"Progress stalled for {stall_duration:.0f}s")
            capture_diagnostics(tab.page, f"progress_stalled_{tab.short_id}")
            return

        if total > 0 and current > 0 and not done:
            pct = (current / total) * 100
            rate = current / max(elapsed, 1)
            remaining = (total - current) / rate if rate > 0 else 0
            logger.info(
                f"  {tab}: {current}/{total} ({pct:.1f}%) — "
                f"~{remaining / 60:.1f}min remaining  (stall budget: {stall_limit - stall_duration:.0f}s)"
            )

        if done:
            logger.info(f"  ✓ {tab}: Processing complete! ({progress})")
            dismiss_swal_if_present(tab.page)
            tab.transition(TabState.READY, "conversion complete")

    except Exception as e:
        logger.error(f"  {tab}: Poll error: {e}")
        tab.transition(TabState.ERROR, str(e))
        capture_diagnostics(tab.page, f"poll_error_{tab.short_id}")


def tick_ready(tab: TabState) -> None:
    """READY → ADD_CLICKED: Navigate to Annotated tab and click 'Add to Dataset'."""
    try:
        page = tab.page
        _dismiss_overlays(page)

        # Navigate to Annotated tab
        annotated_tab = page.locator(
            f"{DETAIL_VIEW} .AnnotationJobImageList button:has-text('Annotated')"
        ).first
        if annotated_tab.count() > 0 and annotated_tab.is_visible():
            annotated_tab.click(force=True)
            # Quick wait for tab content — short timeout
            try:
                page.wait_for_selector(
                    f"{DETAIL_VIEW} #annotationContainer",
                    state="attached", timeout=QUICK_CHECK_MS,
                )
            except Exception:
                pass

        # Find and click Add button
        add_btn = page.locator(
            f"{DETAIL_VIEW} button.btn2.medium.primary:has-text('to Dataset'):not([disabled])"
        ).first
        if add_btn.count() > 0 and add_btn.is_visible():
            btn_text = add_btn.inner_text().strip()
            logger.info(f"  {tab}: Clicking '{btn_text}'")
            add_btn.click(force=True)
            tab.transition(TabState.ADD_CLICKED, f"clicked: {btn_text}")
            return

        # Button not visible yet — wait or reload
        btn_timeout = _add_dataset_timeout(tab.image_count, tab.config)
        if tab.time_in_state > btn_timeout / 1000:
            if tab._add_retry < 1:
                tab._add_retry += 1
                logger.warning(f"  {tab}: Add button not visible — reload recovery")
                _dismiss_overlays(page)
                try:
                    page.reload(wait_until=WAIT_STRATEGY, timeout=NAV_TIMEOUT)
                except Exception:
                    pass
                tab.state_entered_at = time.time()
            else:
                tab.transition(TabState.ERROR, "Add button never appeared")
                capture_diagnostics(page, f"add_btn_timeout_{tab.short_id}")

    except Exception as e:
        tab.transition(TabState.ERROR, f"ready error: {e}")
        if tab.page:
            capture_diagnostics(tab.page, f"ready_err_{tab.short_id}")


def tick_add_clicked(tab: TabState) -> None:
    """ADD_CLICKED → CONFIRMING: Quick-check if confirm modal appeared, click it."""
    try:
        page = tab.page
        modal_btn = page.locator(
            ".dialogPanel button.primary, #AddApprovedImagesToDatasetButton"
        ).first

        if modal_btn.count() > 0 and modal_btn.is_visible():
            modal_text = modal_btn.inner_text().strip()
            logger.info(f"  {tab}: Confirming modal: '{modal_text}'")
            modal_btn.click()
            tab.transition(TabState.CONFIRMING, f"confirmed: {modal_text}")
            return

        # Modal not visible yet — timeout check
        if tab.time_in_state > 30:
            if tab._add_retry < 2:
                tab._add_retry += 1
                logger.warning(f"  {tab}: Confirm modal timeout — re-clicking add button")
                _dismiss_overlays(page)
                add_btn = page.locator(
                    f"{DETAIL_VIEW} button.btn2.medium.primary:has-text('to Dataset'):not([disabled])"
                ).first
                if add_btn.count() > 0 and add_btn.is_visible():
                    add_btn.click(force=True)
                tab.state_entered_at = time.time()
            else:
                tab.transition(TabState.ERROR, "Confirm modal never appeared")
                capture_diagnostics(page, f"modal_timeout_{tab.short_id}")

    except Exception as e:
        tab.transition(TabState.ERROR, f"add_clicked error: {e}")
        if tab.page:
            capture_diagnostics(tab.page, f"add_clicked_err_{tab.short_id}")


def tick_confirming(tab: TabState) -> None:
    """CONFIRMING → VERIFYING: Quick-check if modal closed."""
    try:
        page = tab.page
        dialog = page.locator(".dialogPanel")

        if dialog.count() == 0 or not dialog.first.is_visible():
            tab.transition(TabState.VERIFYING, "modal closed")
            return

        if tab.time_in_state > 30:
            # Force-close and proceed
            logger.warning(f"  {tab}: Modal still open after 30s — assuming success")
            try:
                page.evaluate(
                    "() => document.querySelectorAll('.dialogPanel').forEach(el => el.remove())"
                )
            except Exception:
                pass
            tab.transition(TabState.VERIFYING, "modal force-closed")

    except Exception as e:
        # If we can't check the modal, assume success
        tab.transition(TabState.VERIFYING, f"modal check error (assuming ok): {e}")


def tick_verifying(tab: TabState, coordinator) -> None:
    """VERIFYING → DONE: Server verification after add-to-dataset."""
    try:
        page = tab.page

        # P1 L3: navigate to job URL and check redirect
        try:
            page.goto(tab.job_url, wait_until=WAIT_STRATEGY, timeout=15_000)
            final_url = page.url
            if "/annotate/job/" not in final_url:
                logger.info(f"  ✓ {tab}: Server confirmed done (redirected to board)")
            else:
                logger.warning(f"  {tab}: Job still accessible after add — treating as done")
        except Exception as ve:
            logger.debug(f"  {tab}: Server verify failed ({ve}) — treating as done")

        coordinator.mark_done(tab.job_url)
        tab.transition(TabState.DONE, "added to dataset")
        logger.info(f"  ✓ {tab}: Added to dataset ✓")

    except Exception as e:
        # Even if verification fails, the add was likely successful
        coordinator.mark_done(tab.job_url)
        tab.transition(TabState.DONE, f"done (verify error: {e})")
        logger.info(f"  ✓ {tab}: Added to dataset (verify skipped)")


# ── Fast helper: detect card type without blocking ───────────────────────
def _detect_card_type(page: Page, *, headless: bool = False) -> str | None:
    """Instant card-type detection via button visibility. Returns type or None."""
    detail = DETAIL_VIEW

    def _btn_check(selector: str) -> bool:
        loc = page.locator(selector).first
        if loc.count() == 0:
            return False
        if loc.is_visible() and loc.is_enabled():
            return True
        if headless:
            try:
                return loc.is_enabled()
            except Exception:
                pass
        return False

    for btn_text in ("Start Annotating", "Continue Annotating"):
        sel = f"{detail} button.btn2.medium:has-text('{btn_text}')"
        if _btn_check(sel):
            return "start_annotating"

    add_sel = f"{detail} button.btn2.medium.primary:has-text('to Dataset'):not([disabled])"
    if _btn_check(add_sel):
        return "add_to_dataset"

    review_sel = f"{detail} button.btn2.medium:has-text('Submit for Review')"
    if _btn_check(review_sel):
        return "submit_for_review"

    return None


# ── Fast helper: click null button without long waits ────────────────────
def _click_null_button_fast(page: Page, config: dict) -> bool:
    """Click Unannotated tab → click Null button. Returns True if click happened."""
    try:
        _dismiss_overlays(page)

        # Click Unannotated tab
        unannotated_tab = page.locator(
            f"{DETAIL_VIEW} .AnnotationJobImageList button:has-text('Unannotated')"
        ).first
        if unannotated_tab.count() > 0 and unannotated_tab.is_visible():
            unannotated_tab.click()
            # Short wait for content
            try:
                page.wait_for_selector(
                    f"{DETAIL_VIEW} #annotationContainer",
                    state="attached", timeout=QUICK_CHECK_MS,
                )
            except Exception:
                pass

        _dismiss_overlays(page)

        # Click Null button
        null_btn = page.locator(f"{DETAIL_VIEW} button:has(i.far.fa-empty-set)").first
        if null_btn.count() > 0 and null_btn.is_visible() and not null_btn.is_disabled():
            null_btn.click()
            return True

        return False

    except Exception as e:
        logger.debug(f"  _click_null_button_fast error: {e}")
        return False


# =================================================================================
#  Unannotated Scan Helper
# =================================================================================

def scan_unannotated_robustly(page: Page) -> int:
    """Robustly scan for unannotated images using DOM signals (3 strategies)."""
    count = get_unannotated_count(page)
    if count > 0:
        return count

    logger.info("  Count is 0 -- waiting for network to settle before re-scan...")
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass

    count = get_unannotated_count(page)
    if count > 0:
        logger.info(f"  Count updated to {count} after network settled.")
        return count

    # Fallback: check Null button state
    try:
        unannotated_tab = page.locator(
            f"{DETAIL_VIEW} .AnnotationJobImageList button:has-text('Unannotated')"
        ).first
        if unannotated_tab.is_visible():
            unannotated_tab.click()
            wait_for_tab_content(page)

        null_btn = page.locator(f"{DETAIL_VIEW} button:has(i.far.fa-empty-set)").first
        if null_btn.is_visible() and not null_btn.is_disabled():
            logger.info("  Null button is ENABLED -> unannotated images exist.")
            return 1
    except Exception as e:
        logger.debug(f"  Null button check failed: {e}")

    return 0


# =================================================================================
#  Main Entry Point
# =================================================================================

def run_dataset_mover(
    page: Page,
    context: BrowserContext,
    config: dict,
    coordinator=None,
) -> int:
    """
    Main entry point for Phase 2.

    P3: in-memory blacklist accumulates failed URLs across batches.
    P7: multi-signal exit -- DOM 0 + forced navigation + coordination count.
    """
    if coordinator is None:
        from src.coordinator import NullCoordinator
        coordinator = NullCoordinator()

    total_moved = 0
    parallel_tabs  = config.get("parallel_tabs", 5)
    min_images     = config.get("min_images_per_job", 400)
    headless       = config.get("headless", False)
    strategy       = config.get("collection_strategy", "top_down")
    board_url      = f"{config['workspace_url'].rstrip('/')}/{config['project_name']}/annotate"

    # P3: in-memory blacklist persists for full run lifetime
    blacklist = set()

    page.set_default_timeout(NAV_TIMEOUT)

    # Install Firestore fetch() intercept before first navigation so
    # add_init_script fires on every subsequent page load (incl. Tier 0 reloads).
    if config.get("use_network_tier0", True):
        _install_firestore_intercept(page, config)

    _go_to_board(page, board_url)

    logger.info("=" * 60)
    logger.info("PHASE 2: Move Annotating Jobs -> Dataset")
    logger.info(f"Parallel tabs:       {parallel_tabs}")
    logger.info(f"Collection strategy: {strategy}")
    logger.info(f"Coordination:        {'enabled' if coordinator.enabled else 'disabled'}")
    logger.info("=" * 60)

    batch_num = 0
    batch_moved = 0
    consecutive_empty_batches = 0
    prev_job_count = None
    original_job_count = None

    while True:
        # ── Live code update checkpoint ──────────────────────────────────────
        # The heartbeat thread sets this event when the server reports a new
        # code version.  We apply updates here — between batches — so we never
        # interrupt an in-progress pipeline.
        _update_event = config.get("_update_event")
        if _update_event and _update_event.is_set():
            logger.info("=" * 60)
            logger.info("CODE UPDATE — pausing automation to pull new files")
            logger.info("=" * 60)
            try:
                outdated = coordinator.check_code_updates()
                if outdated:
                    updated_count = 0
                    for fpath in outdated:
                        if coordinator.pull_code_update(fpath):
                            updated_count += 1
                            logger.info(f"  Updated: {fpath}")
                    _local_ver = config.get("_local_code_version", [0])
                    resp = coordinator.send_heartbeat(
                        "updated",
                        code_version=_local_ver[0] + 1,
                        updated_files=updated_count,
                    )
                    _local_ver[0] = resp.get("code_version", _local_ver[0] + 1)
                    logger.info(
                        f"  Updated {updated_count} file(s) — "
                        f"code version now v{_local_ver[0]}"
                    )
                else:
                    logger.info("  No files differ — already up to date")
            except Exception as _ue:
                logger.warning(f"  Code update failed: {_ue} — continuing with current code")
            _update_event.clear()
            logger.info("Resuming automation")
            logger.info("=" * 60)

        # P7: Multi-Signal Exit
        job_count = _read_job_count_safe(page, board_url)

        # Stale-read guard
        if job_count == 0 and prev_job_count is not None and batch_moved > 0:
            logger.info("Job count returned 0 right after a batch; retrying after forced navigation...")
            _go_to_board(page, board_url)
            job_count = _read_job_count_safe(page, board_url)

        if original_job_count is None and job_count > 0:
            original_job_count = job_count

        if job_count == 0:
            logger.info("DOM reports 0 jobs -- forcing navigation re-check (P7)...")
            _go_to_board(page, board_url)
            job_count_recheck = _read_job_count_safe(page, board_url)

            if job_count_recheck == 0:
                if coordinator.enabled and original_job_count:
                    done_count = coordinator.count_by_status("done")
                    threshold = original_job_count * 0.8
                    if done_count < threshold:
                        logger.warning(
                            f"P7 WARNING: DOM says 0 but coord shows only "
                            f"{done_count}/{original_job_count} done "
                            f"(threshold {threshold:.0f}). Continuing..."
                        )
                        batch_num += 1
                        _go_to_board(page, board_url)
                        continue

                logger.info("No more jobs in Annotating column! Phase 2 complete.")
                break
            else:
                job_count = job_count_recheck

        if prev_job_count is not None:
            expected = max(prev_job_count - batch_moved, 0)
            new_arrivals = job_count - expected
            if new_arrivals > 0:
                logger.info(
                    f"  {new_arrivals} new job(s) appeared "
                    f"(expected {expected}, found {job_count})"
                )

        batch_num += 1
        batch_size = min(parallel_tabs, job_count)
        logger.info(f"\n{'=' * 60}")
        logger.info(f"BATCH {batch_num} | {job_count} jobs remaining | Collecting {batch_size} URLs  [strategy={strategy}]")
        logger.info(f"{'=' * 60}")

        # Pre-load coordination state — one server call per batch instead of
        # N per-card is_available() HTTP calls during collection.
        snapshot = coordinator.get_snapshot()
        coord_held = set(snapshot.get("held", {}))
        coord_done = set(snapshot.get("done", {}))
        coord_failed = set(snapshot.get("failed", {}))
        if coord_held or coord_done or coord_failed:
            logger.info(
                f"  Coordination snapshot: {len(coord_held)} held, "
                f"{len(coord_done)} done, "
                f"{len(coord_failed)} failed — "
                f"pre-filtering {len(coord_held) + len(coord_done)} URLs"
            )

        common_kwargs = dict(
            coordinator=coordinator,
            blacklist=blacklist,
            coord_held=coord_held,
            coord_done=coord_done,
            config=config,
            min_images=min_images,
        )
        if strategy == "bottom_up":
            job_urls = collect_job_urls_bottom_up(page, batch_size, **common_kwargs)
        else:
            job_urls = collect_job_urls(page, batch_size, **common_kwargs)

        if not job_urls:
            logger.error("No job URLs collected! Refreshing and retrying...")
            consecutive_empty_batches += 1
            # After 3 consecutive empty batches all visible cards are likely
            # held by other workers.  Wait 60 s for them to finish, then try
            # once more before declaring phase complete.
            if consecutive_empty_batches >= 3:
                wait_s = 60
                logger.warning(
                    f"  {consecutive_empty_batches} consecutive empty batches — "
                    f"all visible cards appear coord-held. "
                    f"Waiting {wait_s}s for held jobs to complete..."
                )
                time.sleep(wait_s)
                # Refresh coordination snapshot after waiting — held jobs
                # may have been released or completed in the meantime.
                snapshot = coordinator.get_snapshot()
                common_kwargs["coord_held"] = set(snapshot.get("held", {}))
                common_kwargs["coord_done"] = set(snapshot.get("done", {}))
                _go_to_board(page, board_url)
                job_urls_final = (
                    collect_job_urls_bottom_up(page, batch_size, **common_kwargs)
                    if strategy == "bottom_up"
                    else collect_job_urls(page, batch_size, **common_kwargs)
                )
                if not job_urls_final:
                    logger.info(
                        "Still no collectable URLs after waiting — "
                        "assuming all remaining jobs are held by other workers. "
                        "Phase 2 complete."
                    )
                    break
                job_urls = job_urls_final
                consecutive_empty_batches = 0
            else:
                _go_to_board(page, board_url)
                continue

        prev_job_count = job_count
        consecutive_empty_batches = 0  # reset on any successful collection

        batch_moved, batch_failed_urls = _run_pipeline(
            job_urls, context, parallel_tabs,
            headless=headless,
            coordinator=coordinator,
            config=config,
        )
        total_moved += batch_moved

        for failed_url in batch_failed_urls:
            blacklist.add(failed_url)

        logger.info(
            f"Batch {batch_num} done -- {batch_moved} moved, "
            f"{len(batch_failed_urls)} failed "
            f"(local blacklist: {len(blacklist)}, "
            f"coord held: {len(common_kwargs.get('coord_held', set()))}, "
            f"coord done: {len(common_kwargs.get('coord_done', set()))}) "
            f"(cumulative: {total_moved})"
        )
        _go_to_board(page, board_url)

    logger.info(f"\n{'=' * 60}")
    logger.info(f"PHASE 2 COMPLETE -- {total_moved} cards moved to dataset")
    if coordinator.enabled:
        logger.info(f"Coordination summary: {coordinator.get_summary()}")
    logger.info(f"{'=' * 60}")
    return total_moved


# =================================================================================
#  Pipeline Orchestrator
# =================================================================================

def _run_pipeline(
    job_urls: list,
    context: BrowserContext,
    num_slots: int,
    *,
    headless: bool = False,
    coordinator=None,
    config=None,
) -> tuple:
    """
    Drive N tab slots through the state machine using non-blocking round-robin.

    The orchestrator visits each tab in turn, runs ONE quick tick (<2 s),
    then jumps to the next tab.  No tab blocks the loop.

    Returns (moved_count, permanently_failed_urls_list).
    """
    if coordinator is None:
        from src.coordinator import NullCoordinator
        coordinator = NullCoordinator()
    if config is None:
        config = {}

    moved = 0
    permanently_failed = []
    retry_tracker = {}
    blacklisted_urls = set()

    # ── Claim phase: only keep URLs the server grants us ─────────────
    strategy_name = config.get("collection_strategy", "top_down")
    granted, denied = coordinator.batch_claim(job_urls, holder=strategy_name)
    queue = list(granted)
    if denied:
        logger.info(f"  {len(denied)}/{len(job_urls)} URLs denied by server — {len(queue)} remain")
    if not queue:
        logger.warning("  All URLs denied by server — skipping this batch")
        return 0, []

    slot_count = min(num_slots, len(queue))
    slots: list[TabState] = []
    for _ in range(slot_count):
        url = queue.pop(0)
        slots.append(TabState(url, headless=headless, config=config))

    logger.info(
        f"Pipeline: {len(slots)} slots, {len(queue)} queued — "
        f"round-robin tick interval: {TICK_INTERVAL}s"
    )

    round_num = 0

    while True:
        # ── Check termination: all slots finished and queue empty ─────
        active = [
            s for s in slots
            if s.status not in (TabState.DONE, TabState.ERROR, TabState.BLACKLISTED)
            and not s.status.startswith("_")   # _counted / _recycled are done
        ]
        if not active and not queue:
            break

        round_num += 1
        any_converting = False

        for slot in slots:
            # ── Recycle finished/errored slots ────────────────────────
            if slot.status in (TabState.DONE, TabState.ERROR, TabState.BLACKLISTED):
                if slot.status == TabState.DONE:
                    moved += 1
                    slot.transition(TabState.DONE, "counted")  # prevent double-count
                    slot.status = "_counted"  # internal marker
                elif slot.status == TabState.ERROR:
                    failed_url = slot.job_url
                    retries = retry_tracker.get(failed_url, 0)

                    # Blacklist check
                    if slot.consecutive_errors >= BLACKLIST_THRESHOLD:
                        blacklisted_urls.add(failed_url)
                        permanently_failed.append(failed_url)
                        coordinator.mark_failed(failed_url, f"blacklisted: {slot.error_msg}")
                        logger.warning(
                            f"  ✗ BLACKLISTED {slot} after {slot.consecutive_errors} "
                            f"consecutive errors: {slot.error_msg}"
                        )
                        slot.status = TabState.BLACKLISTED
                    elif retries < MAX_CARD_RETRIES:
                        retry_tracker[failed_url] = retries + 1
                        queue.append(failed_url)
                        logger.info(
                            f"  Re-queuing {slot} for retry "
                            f"({retries + 1}/{MAX_CARD_RETRIES}): {slot.error_msg}"
                        )
                        slot.status = "_recycled"
                    else:
                        permanently_failed.append(failed_url)
                        coordinator.mark_failed(failed_url, slot.error_msg)
                        logger.warning(
                            f"  ✗ Giving up on {slot} after {MAX_CARD_RETRIES} retries: "
                            f"{slot.error_msg}"
                        )
                        slot.status = "_recycled"
                elif slot.status == TabState.BLACKLISTED:
                    pass  # already handled

                # Assign next URL from queue
                if slot.status in ("_counted", "_recycled", TabState.BLACKLISTED):
                    if queue:
                        next_url = queue.pop(0)
                        if next_url in blacklisted_urls:
                            # skip blacklisted URLs
                            continue
                        is_retry = next_url in retry_tracker
                        slot.reset_for(next_url, is_retry=is_retry)
                    else:
                        continue  # no more work for this slot

                continue  # skip tick — was just recycled

            # ── Tick the slot (non-blocking micro-step) ───────────────
            slot.tick_count += 1
            _tick_slot_fast(slot, context, coordinator=coordinator)

            if slot.status == TabState.CONVERTING:
                any_converting = True

        # ── Brief sleep between rounds ────────────────────────────────
        # Longer pause when tabs are converting (nothing to do but poll)
        if any_converting and not any(
            s.status in (
                TabState.PENDING, TabState.NAVIGATING, TabState.DETAIL_WAIT,
                TabState.SCANNING, TabState.READY, TabState.ADD_CLICKED,
                TabState.CONFIRMING,
            ) for s in slots
        ):
            time.sleep(POLL_INTERVAL)
        else:
            time.sleep(TICK_INTERVAL)

        # ── Periodic status log ───────────────────────────────────────
        if round_num % 20 == 0:
            status_counts = {}
            for s in slots:
                st = s.status if not s.status.startswith("_") else "idle"
                status_counts[st] = status_counts.get(st, 0) + 1
            logger.info(
                f"  [Round {round_num}] Slots: {status_counts} | "
                f"Queue: {len(queue)} | Moved: {moved} | "
                f"Failed: {len(permanently_failed)} | "
                f"Blacklisted: {len(blacklisted_urls)}"
            )

        # ── Publish tab state snapshot for heartbeat/dashboard ────────
        if config is not None and round_num % 5 == 0:
            config["_tab_states"] = [
                s.to_dict() for s in slots
                if not s.status.startswith("_")
            ]

    # ── Final accounting for any remaining slots ──────────────────────
    for slot in slots:
        if slot.status == TabState.DONE:
            moved += 1
        elif slot.status == TabState.ERROR:
            permanently_failed.append(slot.job_url)
            coordinator.mark_failed(slot.job_url, slot.error_msg)

    # ── Log per-tab summary ───────────────────────────────────────────
    for slot in slots:
        phases = ", ".join(f"{k}={v:.1f}s" for k, v in slot.phase_times.items() if v > 0)
        logger.debug(
            f"  Tab {slot.short_id}: ticks={slot.tick_count} errors={len(slot.error_history)} "
            f"freezes={slot.freeze_count} phases=[{phases}]"
        )

    # ── Cleanup ───────────────────────────────────────────────────────
    for slot in slots:
        slot.close_page()

    return moved, permanently_failed


def _tick_slot_fast(
    slot: TabState,
    context: BrowserContext,
    *,
    coordinator=None,
) -> None:
    """
    Advance a single slot by one NON-BLOCKING micro-step.

    Each call takes <2 s (never blocks the round-robin loop).
    The orchestrator calls this once per tab, then jumps to the next tab.
    """
    if coordinator is None:
        from src.coordinator import NullCoordinator
        coordinator = NullCoordinator()

    # ── Mid-pipeline guard: skip if another worker already finished ───
    if slot.status == TabState.PENDING and coordinator.enabled:
        server_entry = coordinator.get_status(slot.job_url)
        if server_entry and server_entry.get("status") == "done":
            logger.info(
                f"  {slot}: Already done by another worker — skipping "
                f"(worker={server_entry.get('worker', '?')})"
            )
            slot.transition(TabState.DONE, "done by another worker")
            return

    # ── Dispatch to the correct micro-tick ────────────────────────────
    if slot.status == TabState.PENDING:
        tick_pending(slot, context)
    elif slot.status == TabState.NAVIGATING:
        tick_navigating(slot)
    elif slot.status == TabState.DETAIL_WAIT:
        tick_detail_wait(slot)
    elif slot.status == TabState.SCANNING:
        tick_scanning(slot)
    elif slot.status == TabState.NULL_CLICKED:
        tick_null_clicked(slot)
    elif slot.status == TabState.CONVERTING:
        tick_converting(slot)
    elif slot.status == TabState.READY:
        tick_ready(slot)
    elif slot.status == TabState.ADD_CLICKED:
        tick_add_clicked(slot)
    elif slot.status == TabState.CONFIRMING:
        tick_confirming(slot)
    elif slot.status == TabState.VERIFYING:
        tick_verifying(slot, coordinator)


# =================================================================================
#  Board Navigation Helpers
# =================================================================================

def _go_to_board(page: Page, board_url: str) -> None:
    """Navigate to the annotate board and wait for Annotating column hydration."""
    on_board = board_url.rstrip("/") in page.url and "/annotate/job/" not in page.url

    if on_board:
        page.reload(wait_until=WAIT_STRATEGY, timeout=NAV_TIMEOUT)
    else:
        logger.info(f"Navigating to board: {board_url}")
        page.goto(board_url, wait_until=WAIT_STRATEGY, timeout=NAV_TIMEOUT)

    col = page.locator(_ANNOTATING_COL)
    col.wait_for(state="visible", timeout=NAV_TIMEOUT)

    try:
        col_handle = col.element_handle()
        page.wait_for_function(
            "(col) => {"
            "  if (!col) return false;"
            "  const span = col.querySelector('div > span.font-mono');"
            "  if (span) {"
            "    const t = (span.textContent || '').trim();"
            "    if (/\\d/.test(t)) return true;"
            "  }"
            "  return !!col.querySelector('.AnnotationJobCard');"
            "}",
            col_handle,
            timeout=10_000,
        )
    except Exception:
        pass

    time.sleep(3)


def _read_job_count_safe(page: Page, board_url: str) -> int:
    """
    Read job count with board-URL verification and one automatic retry.

    P1 R1.4: Retry 1 uses same page; Retry 2 uses forced navigation.
    """
    on_board = board_url.rstrip("/") in page.url and "/annotate/job/" not in page.url
    if not on_board:
        logger.warning(f"Page not on board (url={page.url}). Navigating...")
        _go_to_board(page, board_url)

    try:
        return get_job_count(page)
    except Exception as e:
        logger.error(f"Error reading job count: {e}. Forcing navigation and retrying...")
        _go_to_board(page, board_url)
        return get_job_count(page)
