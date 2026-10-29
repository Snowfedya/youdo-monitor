"""Main orchestrator for the YouDo monitoring service.

Single entry point used by cron:
    python -m youdo_monitor.runner --once
"""
import argparse
import json
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

from youdo_monitor.client import YouDoClient
from youdo_monitor.config import CATEGORIES, DATA_DIR, LOG_FILE, ONLY_VIRTUAL, STATE_FILE
from youdo_monitor.filters import TaskClassifier
from youdo_monitor.generator import analyze_task_scope, calculate_offer_price, generate_pitch
from youdo_monitor.notifier import Notifier
from youdo_monitor.offers import OfferManager
from youdo_monitor.state import StateManager

logger = logging.getLogger("youdo_monitor")

ARCHIVE_CATALOG_FILE = DATA_DIR / "archive_catalog.json"
ARCHIVE_OUTBOX_FILE = DATA_DIR / "archive_outbox.sqlite3"

# A task whose detail endpoint keeps failing is retried this many cycles, then
# parked with an explicit status so it shows up in the report instead of
# disappearing. Parking is not rejection: the status says the data was never
# obtained.
MAX_DETAIL_ATTEMPTS = 3



def setup_logging(verbose: bool = False) -> None:
    """Configure rotating file log + stderr handler."""
    root = logging.getLogger("youdo_monitor")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.addHandler(sh)


def merge_task(summary: Dict[str, Any], detail: Dict[str, Any]) -> Dict[str, Any]:
    """Combine feed summary and detail payload into one task dict."""
    merged = dict(summary)
    if detail and "error" not in detail:
        merged.update({k: v for k, v in detail.items() if k != "id"})
    return merged


def detail_status(detail: Dict[str, Any]) -> str:
    """Classify a detail lookup as ``ok``, ``error`` or ``missing``.

    The feed summary carries no brief, so a task evaluated without detail is
    evaluated blind. These three states must stay distinct: only ``ok`` permits
    a verdict about the task itself.
    """
    if not detail:
        return "missing"
    if "error" in detail:
        return "error"
    if not (detail.get("description") or "").strip() and detail.get("offers_count") is None:
        return "missing"
    return "ok"



def _init_default_archive(dry_run: bool = False) -> tuple[Optional[Any], bool]:
    """Initialize default archive capture if archive_catalog.json exists.

    Returns:
        (archive_instance, init_failed_flag)
    """
    if not ARCHIVE_CATALOG_FILE.exists():
        return (None, False)

    try:
        content = ARCHIVE_CATALOG_FILE.read_text(encoding="utf-8").strip()
        if not content:
            return (None, False)
        cat_data = json.loads(content)
        if not isinstance(cat_data, dict):
            return (None, True)
    except Exception as e:
        logger.error("Failed to read archive catalog file %s: %s", ARCHIVE_CATALOG_FILE, e)
        return (None, True)

    if not cat_data.get("active", False):
        return (None, False)

    try:
        from youdo_monitor.archive_capture import ArchiveCapture
        from youdo_monitor.archive_google import GoogleArchive
        from youdo_monitor.archive_outbox import Outbox
        from youdo_monitor.archive_sheets import SheetsIndex
        from youdo_monitor.archive_sync import ArchiveSync

        outbox = Outbox(ARCHIVE_OUTBOX_FILE)
        google = GoogleArchive()
        spreadsheet_id = cat_data.get("spreadsheet_id")
        sheets = SheetsIndex(google, spreadsheet_id) if spreadsheet_id else None
        sync = ArchiveSync(
            outbox=outbox,
            google=google,
            sheets=sheets,
            catalog_path=ARCHIVE_CATALOG_FILE,
            root_folder_id=cat_data.get("root_folder_id"),
        )
        capture = ArchiveCapture(
            outbox=outbox,
            sync=sync,
            source="youdo",
            dry_run=dry_run,
        )
        return (capture, False)
    except Exception as e:
        logger.error("Failed to initialize default archive: %s", e)
        return (None, True)


def _sync_archive(archive: Any, stats: Dict[str, Any]) -> None:
    """Execute archive sync and populate telemetry metrics into stats."""
    if archive is None:
        return

    stats.setdefault("archive_pending", 0)
    stats.setdefault("archive_bytes", 0)
    stats.setdefault("archive_synced_events", 0)
    stats.setdefault("archive_warning", False)
    stats.setdefault("archive_last_verified_sync", None)

    try:
        sync_res = archive.sync_now()
        if isinstance(sync_res, dict):
            stats["archive_synced_events"] = sync_res.get("events", 0)
    except Exception as e:
        logger.error("Archive sync failed: %s", e)
        stats["archive_errors"] = stats.get("archive_errors", 0) + 1
        stats["archive_warning"] = f"sync_failed: {e}"

    outbox = getattr(archive, "outbox", None)
    if outbox is not None:
        try:
            if hasattr(outbox, "usage"):
                usage = outbox.usage()
                if isinstance(usage, dict):
                    stats["archive_bytes"] = usage.get("total_bytes", 0)
                    if usage.get("warning") and not isinstance(stats.get("archive_warning"), str):
                        stats["archive_warning"] = True
                elif isinstance(usage, (int, float)):
                    stats["archive_bytes"] = int(usage)
            elif hasattr(outbox, "total_bytes"):
                tb = outbox.total_bytes()
                if isinstance(tb, (int, float)):
                    stats["archive_bytes"] = int(tb)

            if hasattr(outbox, "pending"):
                pending = outbox.pending()
                if isinstance(pending, (list, tuple)):
                    stats["archive_pending"] = len(pending)
                elif isinstance(pending, int):
                    stats["archive_pending"] = pending
        except Exception as e:
            logger.error("Archive telemetry collection from outbox failed: %s", e)
            stats["archive_errors"] = stats.get("archive_errors", 0) + 1

    sync_obj = getattr(archive, "sync", None)
    if sync_obj is not None:
        last_sync = getattr(sync_obj, "last_verified_sync", None)
        if not last_sync and hasattr(sync_obj, "catalog_path"):
            try:
                cpath = Path(sync_obj.catalog_path)
                if cpath.exists():
                    cdata = json.loads(cpath.read_text(encoding="utf-8"))
                    last_sync = cdata.get("last_verified_sync")
            except Exception:
                pass
        stats["archive_last_verified_sync"] = last_sync


def run_once(
    dry_run: bool = False,
    max_notify: int = 25,
    enable_auto_offer: bool = True,
    archive: Any | None = None,
) -> Dict[str, Any]:
    """Execute a single monitoring cycle. Returns run statistics."""
    started = time.time()
    stats: Dict[str, Any] = {
        "scanned": 0,
        "new": 0,
        "matched": 0,
        "notified": 0,
        "offers_submitted": 0,
        "offers_skipped": 0,
        "errors": 0,
        "auto_offers_blocked_archive": 0,
        "archive_errors": 0,
        "detail_errors": 0,
        "detail_parked": 0,
    }

    archive_init_failed = False
    if archive is None:
        archive, archive_init_failed = _init_default_archive(dry_run=dry_run)
        if archive_init_failed:
            stats["archive_errors"] += 1
            stats["archive_warning"] = "archive_init_failed"

    if archive is not None:
        stats["archive_pending"] = 0
        stats["archive_bytes"] = 0
        stats["archive_synced_events"] = 0
        stats["archive_warning"] = False
        stats["archive_last_verified_sync"] = None

    state = StateManager(state_file=STATE_FILE)
    classifier = TaskClassifier()
    notifier = Notifier(dry_run=dry_run)

    state.prune_expired()

    client = YouDoClient(headless=True)
    try:
        try:
            client.start()
        except Exception as e:
            logger.error("Failed to start browser session: %s", e)
            stats["errors"] += 1
            stats["fatal"] = f"browser_start_failed: {e}"
            return stats

        if not client._warmed_up:
            logger.warning("Warmup failed — platform may be challenging this host.")
            stats["errors"] += 1
            stats["fatal"] = "warmup_failed"

        # Guest cookies support the task feed but not account endpoints. Report
        # the degraded mode once per run so a missing session is visible rather
        # than silently producing empty notifications.
        auth = client.check_auth_status()
        stats["authenticated"] = auth.get("authenticated", False)
        stats["notifications_available"] = auth.get("authenticated", False)
        if not auth.get("authenticated"):
            logger.warning("Guest session (AuthRequired): account notifications/chat unavailable.")
            if not dry_run:
                notifier.send_status(
                    "Сессия гостевая (AuthRequired) — мониторинг заказов работает, "
                    "уведомления и чаты недоступны. Нужны авторизованные куки."
                )

        feed = client.fetch_feed(categories=CATEGORIES, only_virtual=ONLY_VIRTUAL)
        stats["scanned"] = len(feed)
        logger.info("Feed fetched: %d unique tasks", len(feed))

        # 1. Archive observe_feed immediately after fetch_feed
        if archive is not None:
            try:
                archive.observe_feed(feed)
            except Exception as e:
                logger.error("Archive observe_feed failed: %s", e)
                stats["archive_errors"] += 1

        # Account-level events (offer changes, payment, moderator notices).
        # Deduped by a content key so a run only forwards genuinely new items.
        if auth.get("authenticated"):
            try:
                counts = client.fetch_account_counts()
                stats["unread_notifications"] = counts.get("notifications")
                stats["unread_messages"] = counts.get("messages")
                new_notes = []
                if counts.get("notifications"):
                    notes = client.fetch_notifications()
                    # First authenticated run just baselines existing history:
                    # forwarding stale items (some are years old) would spam.
                    first_run = "_baselined" not in state.state.get("seen_notifications", {})
                    for n in notes:
                        key = f"{n.get('when','')}|{n.get('text','')}"
                        if not n.get("text"):
                            continue
                        if not first_run and not state.is_notification_seen(key):
                            new_notes.append(n)
                        state.mark_notification_seen(key)
                    if notes:
                        state.mark_notification_seen("_baselined")
                    if first_run and notes:
                        logger.info("Baseline: %d existing notifications marked as seen.", len(notes))
                stats["new_notifications"] = len(new_notes)
                if new_notes:
                    logger.info("New account notifications: %d", len(new_notes))
                    if not dry_run:
                        body = "🔔 Уведомления YouDo:\n\n" + "\n\n".join(
                            f"⏰ {n['when']}\n{n['text']}" for n in new_notes)
                        if notifier.send_raw(body):
                            # Only after a successful send — marking first would
                            # drop the event on a delivery failure, and the
                            # unread badge is what re-surfaces a missed one.
                            if client.mark_all_notifications_read():
                                stats["notifications_marked_read"] = True
                            else:
                                logger.warning(
                                    "Forwarded notifications but the unread badge "
                                    "could not be cleared; it will retry next run.")
                        else:
                            stats["notifications_marked_read"] = False
            except Exception as e:
                logger.error("Account notification check failed: %s", e)
                stats["errors"] += 1

        # Determine which tasks are genuinely new
        fresh = [t for t in feed if not state.is_task_seen(t["id"])]
        stats["new"] = len(fresh)
        logger.info("New (unseen) tasks: %d", len(fresh))

        if not fresh:
            state.record_run({"scanned": stats["scanned"], "new": 0})
            state.save()
            return stats

        details = client.fetch_task_details([t["id"] for t in fresh])
        detail_by_id = {d["id"]: d for d in details}

        matched: List[Dict[str, Any]] = []
        for summary in fresh:
            detail = detail_by_id.get(summary["id"], {})
            dstate = detail_status(detail)

            # A lookup that failed is not a verdict about the task. Retry it on
            # the next cycle; after the budget is spent, park it with an
            # explicit status so it is visible instead of silently consumed.
            if dstate != "ok":
                attempts = state.record_detail_attempt(summary["id"])
                stats["detail_errors"] += 1
                if attempts >= MAX_DETAIL_ATTEMPTS:
                    state.mark_task_seen(
                        summary["id"],
                        status="detail_unavailable",
                        extra={
                            "title": (summary.get("name") or "")[:120],
                            "attempts": attempts,
                            "detail_status": dstate,
                        },
                    )
                    stats["detail_parked"] += 1
                    logger.warning(
                        "Task %s parked: detail %s after %d attempts",
                        summary.get("id"), dstate, attempts,
                    )
                else:
                    logger.info(
                        "Task %s detail %s; retry %d/%d next cycle",
                        summary.get("id"), dstate, attempts, MAX_DETAIL_ATTEMPTS,
                    )
                if archive is not None:
                    try:
                        archive.observe_detail(summary, detail, None)
                    except Exception as e:
                        logger.error("Archive observe_detail failed for task %s: %s", summary.get("id"), e)
                        stats["archive_errors"] += 1
                continue

            merged = merge_task(summary, detail)
            merged["detail_status"] = "ok"
            decision = classifier.evaluate(merged)
            merged["tags"] = decision.tags

            # 2. Archive observe_detail inside fresh evaluation loop
            if archive is not None:
                try:
                    archive.observe_detail(
                        summary,
                        detail,
                        decision,
                    )
                except Exception as e:
                    logger.error("Archive observe_detail failed for task %s: %s", summary.get("id"), e)
                    stats["archive_errors"] += 1

            # Every seen task is recorded, matched or not — prevents re-evaluation churn.
            state.mark_task_seen(
                merged["id"],
                status="matched" if decision.passed else f"rejected:{decision.reason}",
                extra={"title": merged.get("name", "")[:120]},
            )
            if decision.passed:
                matched.append(merged)
            else:
                logger.debug("Rejected t%s (%s): %s", merged["id"], decision.reason, merged.get("name", "")[:80])

        stats["matched"] = len(matched)
        logger.info("Matched after filtering: %d of %d new", len(matched), len(fresh))

        # 3. Submit post-paid offers for matched tasks if authenticated
        offer_manager = OfferManager(dry_run=dry_run)
        for merged in matched:
            if enable_auto_offer and auth.get("authenticated") and not state.is_offer_sent(merged["id"]):
                try:
                    analysis = analyze_task_scope(merged)
                    price = calculate_offer_price(merged)
                    pitch = generate_pitch(merged, analysis, price)

                    if archive_init_failed:
                        logger.warning("Archive initialization failed; blocking offer for task %s", merged["id"])
                        stats["offers_skipped"] += 1
                        stats["auto_offers_blocked_archive"] += 1
                        continue

                    if archive is not None:
                        if not archive.can_offer(merged["id"]):
                            logger.warning("Archive can_offer blocked task %s", merged["id"])
                            stats["offers_skipped"] += 1
                            stats["auto_offers_blocked_archive"] += 1
                            continue
                        try:
                            attempt_id = archive.begin_offer(merged["id"], price, pitch)
                        except Exception as e:
                            logger.error("Archive begin_offer failed: %s", e)
                            stats["offers_skipped"] += 1
                            stats["auto_offers_blocked_archive"] += 1
                            continue
                    else:
                        attempt_id = None

                    try:
                        offer_res = offer_manager.submit_postpaid_offer(
                            client=client,
                            task_id=merged["id"],
                            price=price,
                            description=pitch,
                        )
                    except Exception as e:
                        if archive is not None and attempt_id is not None:
                            try:
                                archive.finish_offer(
                                    attempt_id,
                                    {
                                        "success": False,
                                        "status": "unknown",
                                        "error": str(e),
                                        "task_id": merged["id"],
                                    },
                                )
                            except Exception as fe:
                                logger.error("Archive finish_offer failed after API exception: %s", fe)
                                stats["archive_errors"] += 1
                        raise

                    if archive is not None and attempt_id is not None:
                        try:
                            archive.finish_offer(attempt_id, offer_res)
                        except Exception as e:
                            logger.error("Archive finish_offer failed: %s", e)
                            stats["archive_errors"] += 1

                    merged["auto_offer"] = {
                        **offer_res,
                        "price": price,
                        "mode": analysis.mode,
                        "pitch": pitch,
                    }
                    if offer_res.get("success") and not dry_run and not offer_res.get("dry_run"):
                        stats["offers_submitted"] += 1
                        state.mark_offer_sent(
                            merged["id"],
                            price=price,
                            pitch=pitch,
                            mode=analysis.mode,
                            result=offer_res,
                        )
                    else:
                        stats["offers_skipped"] += 1
                except Exception as e:
                    logger.error("Auto-offer failed for task %s: %s", merged["id"], e)
                    merged["auto_offer"] = {"success": False, "error": str(e)}
                    stats["offers_skipped"] += 1

        # Notification cap guards against a flood on first run / after state loss.
        to_notify = matched[:max_notify]
        if len(matched) > max_notify:
            logger.warning("Notification cap hit: %d matched, sending first %d", len(matched), max_notify)

        stats["notified"] = notifier.send_tasks(to_notify)
        stats["send_failures"] = notifier.failed_count

        state.record_run({
            "total_scanned": stats["scanned"],
            "total_new": stats["new"],
            "total_matched": stats["matched"],
            "total_notified": stats["notified"],
            "total_offers_submitted": stats["offers_submitted"],
        })
        state.save()

    except Exception as e:
        logger.exception("Unhandled error during run: %s", e)
        stats["errors"] += 1
        stats["fatal"] = str(e)
    finally:
        try:
            client.close()
        except Exception as e:
            logger.error("Failed to close client: %s", e)
        _sync_archive(archive, stats)

    stats["duration_sec"] = round(time.time() - started, 1)
    logger.info("Run finished in %.1fs: %s", stats["duration_sec"], stats)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="YouDo monitoring service")
    parser.add_argument("--once", action="store_true", help="Run a single monitoring cycle (cron mode)")
    parser.add_argument("--dry-run", action="store_true", help="Do not send Telegram messages; print to stdout")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    parser.add_argument("--max-notify", type=int, default=25, help="Max notification messages per run")
    parser.add_argument("--no-auto-offer", action="store_true", help="Disable automatic post-paid offers")
    parser.add_argument("--json", action="store_true", help="Print run statistics as JSON to stdout")
    args = parser.parse_args()

    setup_logging(args.verbose)

    if not args.once:
        parser.error("Only --once mode is supported (long-running daemon is handled by cron).")

    stats = run_once(
        dry_run=args.dry_run,
        max_notify=args.max_notify,
        enable_auto_offer=not args.no_auto_offer
    )

    if args.json:
        print(json.dumps(stats, ensure_ascii=False))

    if stats.get("fatal"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
