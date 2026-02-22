"""
Batch Creator module: core automation loop.

Flow per iteration:
  1. Set "Images per page" dropdown to 200
  2. Click "Select All" on current page
  3. Navigate to next page → "Select All" again
  4. Repeat until selected count >= per-batch threshold
  5. Click "Assign {N} Images" button
  6. In dialog: select all labellers EXCEPT excluded ones
  7. Click "Assign to {N} Team Members"
  8. Wait for job creation loader to disappear
  9. Return to Annotate → View Unassigned Images
  10. Repeat until grand total >= total_threshold
"""

import re
import logging
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from src.navigator import click_annotate_button, click_view_unassigned
from src.utils import capture_diagnostics

logger = logging.getLogger("roboflow_batch")


# ---------------------------------------------------------------------------
#  Step helpers
# ---------------------------------------------------------------------------

def set_images_per_page(page: Page, target: int = 200) -> None:
    """
    Click the 'Images per page' dropdown and select the target value.
    The dropdown is a headlessui menu button near the "Images per page:" label.
    """
    logger.info(f"Setting images per page to {target}...")

    # First check if already set to target
    existing = page.locator(f"div[id^='headlessui-menu-button']:has-text('{target}')")
    if existing.count() > 0 and existing.is_visible():
        logger.info(f"Images per page already set to {target}.")
        return

    # Click the dropdown button next to "Images per page:"
    dropdown = page.locator("div[id^='headlessui-menu-button']").filter(
        has_text=re.compile(r"^\d+")
    ).last
    dropdown.wait_for(state="visible", timeout=10000)
    dropdown.click()
    logger.info("Dropdown clicked, waiting for menu to open...")

    # Wait for dropdown menu to open
    page.wait_for_timeout(1500)

    # Try multiple selector strategies for the menu item
    # Strategy 1: headlessui menu items
    menu_item = page.locator(f"[role='menuitem']:has-text('{target}')").first
    if menu_item.count() == 0:
        # Strategy 2: any clickable element with exact text in a dropdown area
        menu_item = page.locator(f"div[id^='headlessui-menu-items'] >> text='{target}'").first
    if menu_item.count() == 0:
        # Strategy 3: generic text match visible on page
        menu_item = page.get_by_text(str(target), exact=True).last

    try:
        menu_item.wait_for(state="visible", timeout=5000)
        menu_item.click()
        logger.info(f"Selected {target} from dropdown.")
    except PlaywrightTimeout:
        logger.error(f"Could not find '{target}' in dropdown menu. Taking screenshot.")
        page.screenshot(path="logs/dropdown_debug.png")
        raise

    page.wait_for_timeout(3000)  # let SPA render
    logger.info(f"Images per page set to {target}.")


def click_select_all(page: Page, retries: int = 3) -> None:
    """Click the 'Select All' button on the current page.

    Retries up to *retries* times with increasing wait between attempts
    to handle slow SPA hydration / transient DOM updates.
    """
    for attempt in range(1, retries + 1):
        try:
            # Wait for page content to stabilise before looking for the button
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
            select_all_btn = page.locator('button:has-text("Select All")')
            select_all_btn.wait_for(state="visible", timeout=15_000)
            select_all_btn.click()
            page.wait_for_timeout(500)  # let UI update
            logger.debug("Clicked 'Select All'.")
            return
        except PlaywrightTimeout:
            if attempt < retries:
                logger.warning(
                    f"Select All button not ready (attempt {attempt}/{retries}). "
                    f"Waiting 3s before retry..."
                )
                page.wait_for_timeout(3000)
            else:
                raise  # let caller handle after all retries exhausted


def get_assign_button_count(page: Page, last_known_count: int = 0) -> int:
    """
    Parse the image count from the 'Assign {N} Images' button.

    Retries up to 3 times if the button transiently disappears (common
    after deep-page Select All when the SPA re-renders).  Falls back to
    *last_known_count* only when the button is genuinely absent after all
    retries — this prevents the running total from resetting to 0.
    """
    MAX_RETRIES = 3
    RETRY_WAIT = 2000  # ms between retries

    assign_btn = page.locator('button.primary:has-text("Assign")')

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            assign_btn.wait_for(state="visible", timeout=8_000)
            text = assign_btn.inner_text()
            match = re.search(r"(\d+)", text)
            count = int(match.group(1)) if match else 0
            logger.debug(f"Assign button text: '{text}' → count = {count}")
            # Sanity: the count should never decrease while selecting more
            if count > 0:
                return count
            # count == 0 but button is visible — SPA glitch, retry
            if attempt < MAX_RETRIES:
                logger.warning(
                    f"Assign button shows 0 (attempt {attempt}/{MAX_RETRIES}). Retrying..."
                )
                page.wait_for_timeout(RETRY_WAIT)
                continue
        except PlaywrightTimeout:
            if attempt < MAX_RETRIES:
                logger.warning(
                    f"Assign button not visible (attempt {attempt}/{MAX_RETRIES}). "
                    f"Waiting {RETRY_WAIT}ms before retry..."
                )
                page.wait_for_timeout(RETRY_WAIT)
            else:
                logger.warning("Assign button not found after all retries.")
                capture_diagnostics(page, "assign_button_not_found")

    # All retries exhausted — fall back to last known good count so the
    # selection loop doesn't reset to 0 and falsely stop.
    if last_known_count > 0:
        logger.info(
            f"Using last known count ({last_known_count}) instead of 0 "
            f"to avoid false stop."
        )
        return last_known_count
    return 0


def click_next_page(page: Page) -> bool:
    """
    Click the next-page chevron button.
    Returns True if clicked successfully, False if no next page.
    """
    next_btn = page.locator(
        'div.btn2.secondary.xxsmall >> i.fa-chevron-right'
    ).first
    try:
        next_btn.wait_for(state="visible", timeout=3000)
        # Check if the button is actually clickable (not disabled)
        parent = next_btn.locator("..")
        parent.click()
        page.wait_for_timeout(2000)  # let page content load
        logger.debug("Navigated to next page.")
        return True
    except PlaywrightTimeout:
        logger.info("No next page available.")
        return False


def select_images_until_threshold(page: Page, threshold: int) -> int:
    """
    Select images across pages until the selected count >= threshold.
    Returns the actual number of selected images.

    Resilient to transient SPA glitches:
      - Retries Select All if the button times out (once per page).
      - Preserves the last known count so a flicker doesn't reset to 0.
    """
    logger.info(f"Selecting images until count >= {threshold}...")

    current_count = 0

    # First page — select all
    try:
        click_select_all(page)
    except PlaywrightTimeout:
        logger.warning("Select All failed on first page. Reloading and retrying...")
        capture_diagnostics(page, "select_all_first_page_timeout")
        page.reload(wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3000)
        click_select_all(page)

    current_count = get_assign_button_count(page, last_known_count=current_count)
    logger.info(f"After first Select All: {current_count} images selected.")

    # Plateau detection (R7.2): track pages where count doesn't increase
    prev_plateau_count = current_count
    consecutive_plateau_pages = 0
    MAX_PLATEAU_PAGES = 3
    page_num = 1

    # Safety ceiling: respect max_pagination_pages config
    # (config is not passed into this function, so we use a generous default)
    max_pages = 200

    # Continue to next pages if needed
    while current_count < threshold and page_num < max_pages:
        has_next = click_next_page(page)
        if not has_next:
            logger.info("No more pages available. Using what we have.")
            break

        page_num += 1

        try:
            click_select_all(page)
        except PlaywrightTimeout:
            # The page may have loaded slowly -- try once more after a brief wait
            logger.warning("Select All timed out on a subsequent page. Retrying...")
            capture_diagnostics(page, "select_all_page_timeout")
            page.wait_for_timeout(3000)
            try:
                click_select_all(page)
            except PlaywrightTimeout:
                logger.warning(
                    "Select All still failing -- skipping this page and continuing."
                )
                consecutive_plateau_pages += 1
                if consecutive_plateau_pages >= MAX_PLATEAU_PAGES:
                    logger.warning(
                        f"R7.2 PLATEAU: count stuck at {current_count} for "
                        f"{consecutive_plateau_pages} pages. Breaking pagination loop."
                    )
                    break
                continue

        new_count = get_assign_button_count(page, last_known_count=current_count)
        # Only update if the new count is higher (monotonic increase)
        if new_count >= current_count:
            current_count = new_count
        else:
            logger.warning(
                f"Count went from {current_count} -> {new_count} (unexpected). "
                f"Keeping {current_count}."
            )
        logger.info(f"Running total selected: {current_count} (page {page_num})")

        # R7.2: Plateau detection
        if current_count == prev_plateau_count:
            consecutive_plateau_pages += 1
            logger.debug(
                f"  Plateau: count unchanged at {current_count} "
                f"({consecutive_plateau_pages}/{MAX_PLATEAU_PAGES} pages)"
            )
            if consecutive_plateau_pages >= MAX_PLATEAU_PAGES:
                logger.warning(
                    f"R7.2 PLATEAU: count stuck at {current_count} for "
                    f"{consecutive_plateau_pages} pages. Breaking pagination loop."
                )
                break
        else:
            consecutive_plateau_pages = 0
            prev_plateau_count = current_count

    logger.info(f"Final selected count: {current_count}")
    return current_count


# ---------------------------------------------------------------------------
#  Assignment dialog
# ---------------------------------------------------------------------------

def open_assign_dialog(page: Page) -> None:
    """Click the 'Assign {N} Images' button to open the assignment dialog."""
    assign_btn = page.locator('button.primary:has-text("Assign")')
    assign_btn.wait_for(state="visible", timeout=10000)
    assign_btn.click()
    logger.info("Opened assignment dialog.")

    # Wait for dialog to appear
    page.locator("div.dialogContainer").wait_for(state="visible", timeout=10000)
    logger.debug("Dialog is visible.")


def select_labellers(page: Page, exclude: list[str]) -> int:
    """
    In the assignment dialog, click each labeller EXCEPT the excluded ones.
    Returns the number of labellers selected.
    """
    logger.info(f"Selecting labellers (excluding: {exclude})...")

    # Get the teammates container (may need scrolling)
    teammates_container = page.locator("div.teammates")
    teammates_container.wait_for(state="visible", timeout=10000)

    # Get all labeller option elements
    labeller_options = page.locator("div.labelerAssignmentOption")
    count = labeller_options.count()
    logger.info(f"Found {count} labeller(s) in dialog.")

    selected = 0
    for i in range(count):
        option = labeller_options.nth(i)

        # Scroll the option into view
        option.scroll_into_view_if_needed()

        # Get the display name
        display_name_el = option.locator("div.displayName")
        display_name = display_name_el.inner_text().strip() if display_name_el.count() > 0 else ""

        # Skip excluded annotators
        if display_name in exclude:
            logger.info(f"  Skipping excluded: '{display_name}'")
            # If already selected, click to deselect
            if "selected" in (option.get_attribute("class") or ""):
                option.click()
                page.wait_for_timeout(300)
                logger.debug(f"  Deselected '{display_name}'")
            continue

        # Click to select (if not already selected)
        if "selected" not in (option.get_attribute("class") or ""):
            option.click()
            page.wait_for_timeout(300)
            logger.info(f"  Selected: '{display_name}'")
        else:
            logger.info(f"  Already selected: '{display_name}'")

        selected += 1

    logger.info(f"Total labellers selected: {selected}")
    return selected


def confirm_assignment(page: Page) -> None:
    """
    Click the 'Assign to {N} Team Members' button to confirm assignment.
    Then wait for the job creation loader to finish.
    """
    # The confirm button is #assignImagesButton — its text changes to "Assign to N Team Members"
    confirm_btn = page.locator("button#assignImagesButton")
    confirm_btn.wait_for(state="visible", timeout=10000)

    btn_text = confirm_btn.inner_text()
    logger.info(f"Clicking confirm: '{btn_text}'")
    confirm_btn.click()

    # Wait for the job creation loader to appear
    logger.info("Waiting for job creation to complete...")
    loader = page.locator('h2:has-text("creating job")')
    try:
        loader.wait_for(state="visible", timeout=10000)
        logger.info("Job creation in progress...")
        # Now wait for it to disappear (jobs created)
        loader.wait_for(state="hidden", timeout=120000)  # up to 2 minutes
        logger.info("Job creation completed!")
    except PlaywrightTimeout:
        # Loader might have appeared and disappeared very fast, or never appeared
        logger.info("Loader not detected — checking if dialog closed.")

    # Wait for page to stabilize
    page.wait_for_timeout(5000)  # let SPA render


# ---------------------------------------------------------------------------
#  Main batch creation loop
# ---------------------------------------------------------------------------

def run_batch_loop(
    page: Page,
    workspace_url: str,
    project_name: str,
    images_per_batch: int,
    total_iterations: int,
    exclude_annotators: list[str],
) -> int:
    """
    Main automation loop:
    1. Navigate to Annotate → View Unassigned Images
    2. Set images per page to 200
    3. Select images across pages until per-batch threshold
    4. Open Assign dialog → select labellers → confirm
    5. Wait for jobs to be created
    6. Repeat for total_iterations loops

    Returns total number of images assigned.
    """
    grand_total = 0

    logger.info("=" * 60)
    logger.info(f"STARTING BATCH AUTOMATION")
    logger.info(f"  Images per batch: {images_per_batch}")
    logger.info(f"  Total iterations: {total_iterations}")
    logger.info(f"  Excluded annotators: {exclude_annotators}")
    logger.info("=" * 60)

    for iteration in range(1, total_iterations + 1):
        logger.info(f"\n{'─' * 50}")
        logger.info(f"ITERATION {iteration}/{total_iterations} | Total assigned so far: {grand_total}")
        logger.info(f"{'─' * 50}")

        # Step 1: Navigate to Annotate → View Unassigned
        try:
            click_annotate_button(page)
        except Exception:
            from src.navigator import navigate_to_annotate
            navigate_to_annotate(page, workspace_url, project_name)

        # Check if any unassigned batches remain
        has_batches = click_view_unassigned(page)
        if not has_batches:
            logger.info("=" * 60)
            logger.info("🎉 All unassigned images have been assigned!")
            logger.info(f"Total assigned across all iterations: {grand_total}")
            logger.info("=" * 60)
            break

        # Step 2: Set images per page to 200
        set_images_per_page(page, target=200)

        # Step 3: Select images across pages
        selected = select_images_until_threshold(page, images_per_batch)

        if selected == 0:
            logger.warning("No images available to assign. Stopping.")
            capture_diagnostics(page, "no_images_to_assign")
            break

        # Step 4: Open assignment dialog
        open_assign_dialog(page)

        # Step 5: Select labellers (exclude auto-labeller)
        num_labellers = select_labellers(page, exclude_annotators)

        if num_labellers == 0:
            logger.error("No labellers selected! Aborting this iteration.")
            capture_diagnostics(page, "no_labellers_selected")
            close_btn = page.locator("div.closeDialogButton")
            if close_btn.count() > 0:
                close_btn.click()
            continue

        # Step 6: Confirm assignment
        confirm_assignment(page)

        # Update total
        grand_total += selected
        logger.info(f"Iteration {iteration}/{total_iterations} done. Assigned {selected} images. Grand total: {grand_total}")

    logger.info("\n" + "=" * 60)
    logger.info(f"AUTOMATION COMPLETE")
    logger.info(f"  Total iterations completed: {iteration if 'iteration' in dir() else 0}/{total_iterations}")
    logger.info(f"  Total images assigned: {grand_total}")
    logger.info("=" * 60)

    return grand_total

