"""
Roboflow Annotation Batch Automation — Entry Point

Usage:
    python main.py
    python main.py --config path/to/config.yaml
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time

from playwright.sync_api import sync_playwright

from src.utils import setup_logging, load_config, get_session_path, optimize_context_for_headless, capture_diagnostics, RemoteLogHandler
from src.auth import authenticate
from src.navigator import navigate_to_annotate
from src.batch_creator import run_batch_loop
from src.coordinator import build_coordinator, HTTPCoordinator
from src.dataset_mover import run_dataset_mover


# ── Remote Monitoring Helpers ────────────────────────────────────────────

def _setup_remote_monitoring(logger, config: dict, coordinator) -> None:
    """
    Wire remote logging, heartbeat, and code auto-update for HTTPCoordinator.

    Does nothing when coordinator is not an HTTPCoordinator or the
    corresponding config flags are disabled.  All background threads
    are daemonic — they die automatically when the main process exits.
    """
    if not isinstance(coordinator, HTTPCoordinator):
        return

    # ── Remote log handler ───────────────────────────────────────────
    if config.get("remote_logging", True):
        handler = RemoteLogHandler(
            coordinator,
            flush_interval=config.get("log_flush_interval", 5),
            flush_threshold=config.get("log_flush_threshold", 50),
        )
        logging.getLogger("roboflow_batch").addHandler(handler)
        logger.info("Remote log handler attached")

    # ── Heartbeat timer ──────────────────────────────────────────────
    interval = config.get("heartbeat_interval", 30)

    # Shared state for live code update detection.
    # The heartbeat thread sets update_event when the server reports a new
    # code version; run_dataset_mover() checks it between batches.
    update_event = threading.Event()
    local_code_version = [0]  # mutable list so the closure can update it
    config["_update_event"] = update_event
    config["_local_code_version"] = local_code_version

    def _heartbeat_loop():
        while True:
            try:
                # Include live tab states if available
                extra = {"code_version": local_code_version[0]}
                tab_snapshot = config.get("_tab_states")
                if tab_snapshot:
                    extra["tabs"] = tab_snapshot
                strategy = config.get("collection_strategy", "")
                if strategy:
                    extra["strategy"] = strategy
                resp = coordinator.send_heartbeat("running", **extra)
                if resp.get("update_available"):
                    if not update_event.is_set():
                        logger.info(
                            "Server signals code update available — "
                            "will apply between batches"
                        )
                        update_event.set()
            except Exception:
                pass
            time.sleep(interval)

    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop, daemon=True, name="heartbeat"
    )
    heartbeat_thread.start()
    logger.info(f"Heartbeat timer started (every {interval}s)")

    # ── Code auto-update check ───────────────────────────────────────
    if config.get("auto_update", False):
        try:
            outdated = coordinator.check_code_updates()
            if outdated:
                logger.info(f"Code updates available for {len(outdated)} file(s): {outdated}")
                updated = 0
                for fpath in outdated:
                    if coordinator.pull_code_update(fpath):
                        updated += 1
                        logger.info(f"  Updated: {fpath}")
                if updated:
                    logger.warning(
                        f"Updated {updated} file(s) — restart recommended for changes to take effect"
                    )
            else:
                logger.info("All code files are up to date")
        except Exception as e:
            logger.debug(f"Code update check failed: {e}")


def main():
    # ── Parse arguments ──────────────────────────────────────────────
    parser = argparse.ArgumentParser(
        description="Automate Roboflow Annotation Batch creation"
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="Path to config.yaml (default: ./config.yaml)"
    )
    args = parser.parse_args()

    # ── Setup ────────────────────────────────────────────────────────
    logger = setup_logging()
    config = load_config(args.config)

    phase = config.get("phase", 1)

    logger.info("Configuration loaded:")
    logger.info(f"  Email:            {config['email']}")
    logger.info(f"  Workspace:        {config['workspace_url']}")
    logger.info(f"  Project:          {config['project_name']}")
    logger.info(f"  Phase:            {phase}")
    if phase in (1, "both"):
        logger.info(f"  Images/batch:     {config['images_per_batch']}")
        logger.info(f"  Iterations:       {config['total_iterations']}")
        logger.info(f"  Exclude:          {config['exclude_annotators']}")
    if phase in (2, "both"):
        logger.info(f"  Parallel tabs:    {config.get('parallel_tabs', 5)}")
    logger.info(f"  Headless:         {config['headless']}")
    if phase in (2, "both"):
        logger.info(f"  Strategy:         {config.get('collection_strategy', 'top_down')}")
        logger.info(f"  Coordination:     {'enabled' if config.get('enable_coordination') else 'disabled'}")

    # ── Launch browser ───────────────────────────────────────────────
    session_path = get_session_path()
    is_headless = config["headless"]

    with sync_playwright() as p:
        # slow_mo helps humans follow along in headed mode;
        # skip it entirely in headless for maximum speed.
        launch_args: list[str] = []
        if is_headless:
            # Prevent navigator.webdriver from returning true (bot detection)
            launch_args.append("--disable-blink-features=AutomationControlled")

        browser = p.chromium.launch(
            headless=True if is_headless else False,
            slow_mo=0 if is_headless else 500,
            args=launch_args or None,
        )

        try:
            # ── Build context options ─────────────────────────────────
            ctx_opts: dict = {}
            if os.path.exists(session_path):
                logger.info("Loading saved session...")
                ctx_opts["storage_state"] = session_path

            if is_headless:
                # Mimic a real desktop browser — avoid default 800×600
                # viewport and the "HeadlessChrome" user-agent string.
                ctx_opts["viewport"] = {"width": 1920, "height": 1080}
                ctx_opts["user_agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )

            context = browser.new_context(**ctx_opts)

            # ── Authenticate FIRST (no resource blocking yet) ────────
            # Route interception is deferred until after login so that
            # the Firebase Auth SPA, magic-link redirect chain, and any
            # CAPTCHAs can load every resource they need.
            page = authenticate(
                context,
                email=config["email"],
                workspace_url=config["workspace_url"],
            )

            # ── Headless optimisations (safe to apply after auth) ────
            if is_headless:
                optimize_context_for_headless(context)

            # ── Navigate to project ──────────────────────────────────────
            navigate_to_annotate(
                page,
                workspace_url=config["workspace_url"],
                project_name=config["project_name"],
            )

            # ── Phase 1: Assign batches ──────────────────────────────────
            if phase in (1, "both"):
                logger.info("\n" + "=" * 60)
                logger.info("STARTING PHASE 1: Assign Unassigned Images")
                logger.info("=" * 60)

                total_assigned = run_batch_loop(
                    page=page,
                    workspace_url=config["workspace_url"],
                    project_name=config["project_name"],
                    images_per_batch=config["images_per_batch"],
                    total_iterations=config["total_iterations"],
                    exclude_annotators=config["exclude_annotators"],
                )
                logger.info(f"Phase 1 complete — {total_assigned} images assigned")

                if phase == "both":
                    # Re-navigate to annotate page for Phase 2
                    logger.info("Re-navigating to Annotate page for Phase 2...")
                    navigate_to_annotate(
                        page,
                        workspace_url=config["workspace_url"],
                        project_name=config["project_name"],
                    )

            # ── Phase 2: Move annotating → dataset ───────────────────────
            if phase in (2, "both"):
                logger.info("\n" + "=" * 60)
                logger.info("STARTING PHASE 2: Move Annotating Jobs → Dataset")
                logger.info("=" * 60)

                coordinator = build_coordinator(config)

                # ── Remote monitoring wiring ─────────────────────────────
                _setup_remote_monitoring(logger, config, coordinator)

                total_moved = run_dataset_mover(
                    page=page,
                    context=context,
                    config=config,
                    coordinator=coordinator,
                )
                logger.info(f"Phase 2 complete — {total_moved} cards moved to dataset")

            # ── Cleanup ──────────────────────────────────────────────────
            logger.info("\n✅ All phases complete!")
            # Normal completion — close browser and exit
            completed_normally = True
        except Exception as phase_error:
            logger.error(f"Phase execution error: {phase_error}")
            try:
                capture_diagnostics(page, "phase_execution_error")
            except Exception:
                pass
            completed_normally = False
        finally:
            def _force_exit():
                """Force-close browser and terminate the process."""
                logger.info("Closing browser...")
                try:
                    browser.close()
                except Exception:
                    pass
                logger.info("Goodbye!")
                os._exit(0)

            def _run_phases():
                """Re-run the configured phases (used by retry)."""
                # A crashed Chromium renderer leaves the page permanently dead —
                # any method call on it re-throws "Page crashed".  Detect that
                # by probing with a lightweight call and, if unusable, open a
                # fresh page from the still-alive browser context.
                _page = None
                try:
                    _page = page
                    # Lightweight probe — throws immediately if renderer crashed
                    _page.title()
                except NameError:
                    pass  # page was never assigned (login failed earlier)
                except Exception as _probe_err:
                    _err_str = str(_probe_err).lower()
                    if "crash" in _err_str or "closed" in _err_str or "target" in _err_str:
                        logger.info(
                            f"Page is unusable ({_probe_err.__class__.__name__}: "
                            f"{_probe_err}) — will open a fresh page."
                        )
                        _page = None

                if _page is None:
                    logger.info("Opening a fresh page and re-authenticating...")
                    # Close any lingering crashed/stray pages silently
                    for _p in context.pages:
                        try:
                            if not _p.is_closed():
                                _p.close()
                        except Exception:
                            pass
                    _page = authenticate(
                        context,
                        email=config["email"],
                        workspace_url=config["workspace_url"],
                    )

                navigate_to_annotate(
                    _page,
                    workspace_url=config["workspace_url"],
                    project_name=config["project_name"],
                )

                if phase in (1, "both"):
                    logger.info("\n" + "=" * 60)
                    logger.info("RETRY — PHASE 1: Assign Unassigned Images")
                    logger.info("=" * 60)
                    run_batch_loop(
                        page=_page,
                        workspace_url=config["workspace_url"],
                        project_name=config["project_name"],
                        images_per_batch=config["images_per_batch"],
                        total_iterations=config["total_iterations"],
                        exclude_annotators=config["exclude_annotators"],
                    )

                    if phase == "both":
                        navigate_to_annotate(
                            _page,
                            workspace_url=config["workspace_url"],
                            project_name=config["project_name"],
                        )

                if phase in (2, "both"):
                    logger.info("\n" + "=" * 60)
                    logger.info("RETRY — PHASE 2: Move Annotating Jobs → Dataset")
                    logger.info("=" * 60)
                    run_dataset_mover(
                        page=_page,
                        context=context,
                        config=config,
                        coordinator=build_coordinator(config),
                    )

            # Only show the interactive prompt if something went wrong.
            # Normal completion → close browser and exit immediately.
            if completed_normally:
                _force_exit()

            # Something failed — keep the browser open for inspection
            try:
                while True:
                    print("\n" + "=" * 60)
                    print("🛑  The automation encountered a problem.")
                    print("    The browser is still open for inspection.")
                    print("=" * 60)
                    print("  [r] Retry — re-run the same phase(s)")
                    print("  [q] Quit  — close the browser and exit")
                    print("=" * 60)
                    choice = input("Your choice (r/q): ").strip().lower()

                    if choice == "q":
                        _force_exit()
                    elif choice == "r":
                        logger.info("User chose to retry. Re-running phases...")
                        try:
                            _run_phases()
                            logger.info("\n✅ Retry complete!")
                            _force_exit()
                        except Exception as e:
                            logger.error(f"Error during retry: {e}")
                        # Loop back to the prompt so user can retry again or quit
                    else:
                        print("  ⚠ Invalid choice. Please enter 'r' or 'q'.")

            except (KeyboardInterrupt, EOFError):
                logger.info("\nCtrl+C detected. Shutting down...")
                _force_exit()


# Handle Ctrl+C at the top level too (e.g. during phase execution)
signal.signal(signal.SIGINT, lambda *_: (print("\n⚠ Ctrl+C pressed. Exiting..."), os._exit(1)))

if __name__ == "__main__":
    main()
