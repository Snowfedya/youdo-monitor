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
from typing import Any, Dict, List

from youdo_monitor.client import YouDoClient
from youdo_monitor.config import CATEGORIES, LOG_FILE, ONLY_VIRTUAL, STATE_FILE
from youdo_monitor.filters import TaskClassifier
from youdo_monitor.generator import analyze_task_scope, calculate_offer_price, generate_pitch
from youdo_monitor.notifier import Notifier
from youdo_monitor.offers import OfferManager
from youdo_monitor.state import StateManager

logger = logging.getLogger("youdo_monitor")


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


def run_once(dry_run: bool = False, max_notify: int = 25, enable_auto_offer: bool = True) -> Dict[str, Any]:
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
    }

    state = StateManager(state_file=STATE_FILE)
    classifier = TaskClassifier()
    notifier = Notifier(dry_run=dry_run)

    state.prune_expired()

    client = YouDoClient(headless=True)
    try:
        client.start()
    except Exception as e:
        logger.error("Failed to start browser session: %s", e)
        stats["errors"] += 1
        stats["fatal"] = f"browser_start_failed: {e}"
        return stats

    try:
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
            stats["duration_sec"] = round(time.time() - started, 1)
            logger.info("Run finished in %.1fs (no new tasks): %s", stats["duration_sec"], stats)
            return stats

        details = client.fetch_task_details([t["id"] for t in fresh])
        detail_by_id = {d["id"]: d for d in details}

        matched: List[Dict[str, Any]] = []
        for summary in fresh:
            merged = merge_task(summary, detail_by_id.get(summary["id"], {}))
            decision = classifier.evaluate(merged)
            merged["tags"] = decision.tags
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

        # Submit post-paid offers for matched tasks if authenticated
        offer_manager = OfferManager(dry_run=dry_run)
        for merged in matched:
            if enable_auto_offer and auth.get("authenticated") and not state.is_offer_sent(merged["id"]):
                try:
                    analysis = analyze_task_scope(merged)
                    price = calculate_offer_price(merged)
                    pitch = generate_pitch(merged, analysis, price)
                    offer_res = offer_manager.submit_postpaid_offer(
                        client=client,
                        task_id=merged["id"],
                        price=price,
                        description=pitch
                    )
                    merged["auto_offer"] = {
                        **offer_res,
                        "price": price,
                        "mode": analysis.mode,
                        "pitch": pitch
                    }
                    if offer_res.get("success"):
                        stats["offers_submitted"] += 1
                        state.mark_offer_sent(
                            merged["id"],
                            price=price,
                            pitch=pitch,
                            mode=analysis.mode,
                            result=offer_res
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
        client.close()

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
