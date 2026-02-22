"""
Navigator module: navigate to the project's Annotate page and Unassigned Images view.

All waits use DOM signals (selector visibility) — no fixed sleeps.
"""

import logging
import time
from playwright.sync_api import Page

from src.utils import capture_diagnostics

logger = logging.getLogger("roboflow_batch")

# Roboflow is a SPA — domcontentloaded is enough; networkidle is unreliable.
WAIT_STRATEGY = "domcontentloaded"
NAV_TIMEOUT = 60_000

# Board is ready once the column headers are rendered.
_BOARD_READY = ".boardColumn"
# Unassigned column — scoped to avoid matching Annotating/Dataset columns.
_UNASSIGNED_COL = '.boardColumn:has(h2:text("Unassigned"))'


def _wait_for_board(page: Page, timeout: int = NAV_TIMEOUT) -> None:
    """Wait for the annotate board columns to render."""
    page.wait_for_selector(_BOARD_READY, state="visible", timeout=timeout)
    logger.debug("  Board columns visible")


def navigate_to_annotate(page: Page, workspace_url: str, project_name: str) -> None:
    """
    Navigate to the project's Annotate page.
    URL pattern: {workspace_url}/{project_name}/annotate
    """
    annotate_url = f"{workspace_url.rstrip('/')}/{project_name}/annotate"
    logger.info(f"Navigating to: {annotate_url}")
    page.goto(annotate_url, wait_until=WAIT_STRATEGY, timeout=NAV_TIMEOUT)
    _wait_for_board(page)
    logger.info("Annotate page loaded.")


def click_annotate_button(page: Page) -> None:
    """Click the 'Annotate' navigation link in the sidebar.

    Uses the stable `id=annotate` attribute on the nav-link, falling back
    to an aria-label selector if the id is absent.
    """
    logger.info("Clicking 'Annotate' button...")
    annotate_link = page.locator(
        "a#annotate, a.nav-link[aria-label='Annotate']"
    ).first
    annotate_link.click()
    # Wait for the board to render after the SPA route change
    _wait_for_board(page)
    logger.info("Annotate section active.")


def click_view_unassigned(page: Page) -> bool:
    """Click 'View Unassigned Images' and verify the page redirects.

    The Roboflow UI sometimes freezes or responds slowly after the click,
    so the browser stays on the board instead of navigating to the
    unassigned-images page.  This function retries the full flow (wait
    for board → click button → verify redirect) until the page actually
    leaves the board.

    It does NOT click the button repeatedly in a tight loop — each
    retry waits a few seconds and re-checks before clicking again.

    Returns:
        True  — unassigned images page loaded with images present.
        False — button absent OR page loaded but no images remain.
    """
    MAX_RETRIES = 5
    RETRY_PAUSE = 5  # seconds between retries

    for attempt in range(1, MAX_RETRIES + 1):
        logger.info(f"Clicking 'View Unassigned Images' (attempt {attempt}/{MAX_RETRIES})...")

        # ── Make sure we're on the board ─────────────────────────────
        url_before = page.url
        unassigned_col = page.locator(_UNASSIGNED_COL)
        try:
            unassigned_col.wait_for(state="visible", timeout=NAV_TIMEOUT)
        except Exception:
            logger.warning("Unassigned column not visible — board may not be loaded.")
            capture_diagnostics(page, "unassigned_column_not_visible")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_PAUSE)
                continue
            return False

        # ── Try clicking the button ──────────────────────────────────
        view_btn = unassigned_col.locator(
            '.btn2:has-text("View Unassigned Images")'
        ).first
        try:
            view_btn.click(timeout=NAV_TIMEOUT)
        except Exception:
            # Button not rendered — could be a transient SPA issue (React
            # re-render, slow hydration) or truly no images left.
            # Reload the page and retry before giving up.
            logger.warning(
                f"  'View Unassigned Images' button not found (attempt {attempt}/{MAX_RETRIES}). "
                f"Reloading page..."
            )
            capture_diagnostics(page, f"view_unassigned_btn_missing_attempt{attempt}")
            if attempt < MAX_RETRIES:
                try:
                    page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                    page.wait_for_selector(_BOARD_READY, state="visible", timeout=NAV_TIMEOUT)
                except Exception:
                    pass
                time.sleep(RETRY_PAUSE)
                continue
            logger.info("'View Unassigned Images' button not found after all retries. No images to assign.")
            return False

        # ── Verify the page actually redirected ──────────────────────
        # The unassigned-images page has a different URL and renders
        # specific signals.  If the URL hasn't changed, the UI froze.
        try:
            # Wait up to 10s for the URL to change away from the board
            page.wait_for_function(
                f"() => window.location.href !== '{url_before}'",
                timeout=10_000,
            )
        except Exception:
            # URL didn't change — UI froze / slow response
            logger.warning(
                f"  Page did not redirect after click (attempt {attempt}/{MAX_RETRIES}). "
                f"UI may be frozen. Waiting {RETRY_PAUSE}s before retrying..."
            )
            capture_diagnostics(page, f"page_redirect_failed_attempt{attempt}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_PAUSE)
                continue
            logger.error("Page never redirected after all retries.")
            return False

        # ── Page redirected — now verify images are present ──────────
        logger.debug("  URL changed — checking for image presence signals...")

        # Signal A: "Images per page" dropdown (only rendered when images exist)
        images_per_page = page.locator("text='Images per page:'")
        # Signal B: Filter bar filename input (only rendered when images exist)
        filter_bar = page.locator('input[data-test="filenameFilterInput"]')

        # Wait a moment for React to hydrate the images page
        try:
            page.wait_for_selector(
                "text='Images per page:', input[data-test='filenameFilterInput']",
                state="visible", timeout=10_000,
            )
        except Exception:
            pass

        if images_per_page.is_visible() or filter_bar.is_visible():
            logger.info("Viewing unassigned images.")
            return True

        # Neither signal appeared — no unassigned images remain
        logger.info("Unassigned images page loaded but no images found. All images assigned!")
        return False

    return False
