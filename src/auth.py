"""
Authentication module: magic-link login and session persistence.
"""

import os
import logging
from playwright.sync_api import Page, BrowserContext

from src.utils import get_session_path, capture_diagnostics

logger = logging.getLogger("roboflow_batch")

# Roboflow is a SPA — never use "networkidle", use "domcontentloaded" instead
WAIT_STRATEGY = "domcontentloaded"
NAV_TIMEOUT = 60000  # 60 seconds


def is_session_valid(context: BrowserContext, workspace_url: str) -> bool:
    """
    Check if a saved session is still valid by navigating to the workspace
    and polling the URL until the SPA finishes routing.
    """
    session_path = get_session_path()
    if not os.path.exists(session_path):
        logger.info("No saved session found.")
        return False

    page = context.pages[0] if context.pages else context.new_page()
    logger.info("Checking if saved session is still valid...")

    try:
        page.goto(workspace_url, wait_until=WAIT_STRATEGY, timeout=NAV_TIMEOUT)

        # Poll URL for up to 15 seconds — the SPA may briefly route through /login
        for i in range(15):
            page.wait_for_timeout(1000)
            current_url = page.url.lower()
            logger.debug(f"  Session check {i+1}s: {current_url}")

            # If URL is on the workspace (not login/auth), session is valid
            if ("login" not in current_url
                    and "sign" not in current_url
                    and "__/auth" not in current_url):
                logger.info(f"Session is valid — landed on: {page.url}")
                return True

        # After 15s still on login → expired
        logger.info(f"Session expired — final URL: {page.url}")
        return False
    except Exception as e:
        logger.warning(f"Session check failed: {e}")
        return False


def login(page: Page, email: str) -> None:
    """
    Perform magic-link login:
    1. Ask user if they already have a magic link
    2. If yes, navigate directly to it
    3. If no, open login page, enter email, wait for user to get the link
    """
    logger.info("Starting login flow...")

    print("\n" + "=" * 60)
    print("  ROBOFLOW LOGIN")
    print("=" * 60)
    print("\n  Do you already have a magic link from your email?")
    print("  [1] Yes — I have a magic link ready")
    print("  [2] No  — Open login page and send me one")
    choice = input("\n  Choice (1/2): ").strip()

    if choice == "1":
        # User already has the magic link
        magic_link = input("\n  Paste the magic link URL: ").strip()
    else:
        # Navigate to login page and enter email
        logger.info("Loading login page...")
        page.goto("https://app.roboflow.com/login", wait_until=WAIT_STRATEGY, timeout=NAV_TIMEOUT)
        page.wait_for_timeout(3000)  # let SPA render
        logger.info("Login page loaded.")

        # Click "Sign in with email" button if present
        try:
            email_btn = page.locator("button[data-provider-id='password']")
            if email_btn.is_visible(timeout=5000):
                logger.info("Clicking 'Sign in with email' button...")
                email_btn.click()
                page.wait_for_timeout(1000)
        except Exception as e:
            logger.debug(f"Did not find/click email sign-in button (might be already on form): {e}")

        # Enter email
        email_input = page.locator('input[type="email"]')
        email_input.wait_for(state="visible", timeout=15000)
        email_input.fill(email)
        logger.info(f"Email entered: {email}")

        # Click the submit/continue button
        submit_button = page.locator('button[type="submit"]')
        submit_button.click()
        logger.info("Login form submitted. Check your email for the magic link.")

        print("\n" + "=" * 60)
        print("  CHECK YOUR EMAIL (Outlook) FOR THE MAGIC LINK")
        print("  Paste the full URL below and press Enter:")
        print("=" * 60)
        magic_link = input("\n  Magic Link URL: ").strip()

    if not magic_link:
        raise ValueError("No magic link provided. Aborting.")

    logger.info("Navigating to magic link...")
    page.goto(magic_link, wait_until=WAIT_STRATEGY, timeout=NAV_TIMEOUT)

    # Wait for the auth redirect chain to fully complete.
    logger.info("Waiting for authentication redirect to complete...")
    max_wait_seconds = 30
    for i in range(max_wait_seconds):
        page.wait_for_timeout(1000)
        current_url = page.url.lower()
        logger.debug(f"  Redirect check {i+1}s: {current_url}")

        # 1. Handle "Confirm Email" if it appears again
        try:
            email_input = page.locator('input[type="email"]')
            if email_input.is_visible(timeout=200):
                logger.info("  Detected email confirmation prompt. Filling email...")
                email_input.fill(email)
                page.locator('button[type="submit"]').click()
                page.wait_for_timeout(1000)
                continue
        except Exception:
            pass

        # 2. Check for Success
        # If URL is on the workspace dashboard (not login/auth)
        if ("__/auth" not in current_url
                and "/login" not in current_url
                and "/sign" not in current_url):
            
            # If we are on a valid non-login page, we assume success.
            # We wait a bit for cookies to set.
            logger.info(f"Login successful! Landed on: {page.url}")
            page.wait_for_timeout(3000) 
            return

    # If we're still on a login page after max wait, fail
    current_url = page.url.lower()
    logger.error(f"Final URL after {max_wait_seconds}s: {current_url}")
    capture_diagnostics(page, "login_failed")
    raise RuntimeError(
        f"Login failed — still on auth/login page after {max_wait_seconds}s. "
        f"The magic link may have expired. Request a new one."
    )


def save_session(context: BrowserContext) -> None:
    """Save browser session (cookies + localStorage) to session.json."""
    session_path = get_session_path()
    context.storage_state(path=session_path)
    logger.info(f"Session saved to: {session_path}")


def authenticate(context: BrowserContext, email: str, workspace_url: str) -> Page:
    """
    Full auth flow:
    - Try to restore saved session
    - If expired, perform magic-link login
    - Save session for future runs
    Returns the authenticated page.
    """
    if is_session_valid(context, workspace_url):
        return context.pages[0]

    # Need fresh login — close old pages and create a new one
    for p in context.pages:
        p.close()
    page = context.new_page()
    login(page, email)
    save_session(context)
    return page
