"""ArchiveCapture adapter for YouDo experience archive.

Safely records feed observations, task details, filter classifications,
and durable offer attempts into the local outbox without external side effects.
Guarantees that offer attempts are persisted before submission and
enforces unresolved attempt locking and storage budget guards.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any, Dict, List, Optional
import uuid

from youdo_monitor.archive_events import (
    make_event,
    offer_payload,
    rules_fingerprint,
    task_completeness,
    task_payload,
)
from youdo_monitor.archive_outbox import ArchiveBlocked

DEFAULT_RULES_PATHS: List[Path] = [
    Path(__file__).resolve().parent / "filters.py",
    Path(__file__).resolve().parent / "config.py",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ArchiveCapture:
    """Capture adapter mediating between YouDo monitor runner and archive outbox/sync."""

    def __init__(
        self,
        outbox: Any,
        sync: Any = None,
        source: str = "youdo",
        dry_run: bool = False,
        rules_paths: Optional[List[Path]] = None,
    ) -> None:
        self.outbox = outbox
        self.sync = sync
        self.dry_run = bool(dry_run or (source == "dry-run"))
        self.source = "dry-run" if self.dry_run and source == "youdo" else str(source)

        if rules_paths is not None:
            self.rules_paths = [Path(p) for p in rules_paths]
        else:
            self.rules_paths = [p for p in DEFAULT_RULES_PATHS if p.exists()]

    def _safe_enqueue(self, event: Dict[str, Any]) -> bool:
        """Enqueue event with translation of arbitrary errors into ArchiveBlocked."""
        try:
            return bool(self.outbox.enqueue(event))
        except ArchiveBlocked:
            raise
        except Exception as e:
            raise ArchiveBlocked(f"Archive error enqueuing event: {e}") from e

    def observe_feed(self, feed: List[Dict[str, Any]]) -> None:
        """Record batch of task summaries seen in the public task feed."""
        now_iso = _utc_now_iso()
        for item in feed:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id") or item.get("Id") or item.get("task_id")
            if raw_id is None:
                continue
            try:
                task_id = int(raw_id)
            except (ValueError, TypeError):
                continue

            payload = task_payload(item, None)
            event = make_event(
                kind="task_observed",
                task_id=task_id,
                payload=payload,
                observed_at=now_iso,
                source=self.source,
                completeness="summary",
            )
            self._safe_enqueue(event)

    def observe_detail(
        self,
        summary: Dict[str, Any],
        detail: Optional[Dict[str, Any]],
        decision: Any,
    ) -> None:
        """Record deep task details and associated classifier decision."""
        summary = summary or {}
        detail_obj = detail or {}

        raw_id = (
            summary.get("id")
            or summary.get("Id")
            or summary.get("task_id")
            or detail_obj.get("id")
            or (detail_obj.get("TaskData") or {}).get("id")
        )
        if raw_id is None:
            return
        try:
            task_id = int(raw_id)
        except (ValueError, TypeError):
            return

        payload = task_payload(summary, detail_obj)
        completeness = task_completeness(detail_obj)
        now_iso = _utc_now_iso()

        # 1. Enqueue task_observed event
        obs_event = make_event(
            kind="task_observed",
            task_id=task_id,
            payload=payload,
            observed_at=now_iso,
            source=self.source,
            completeness=completeness,
        )
        self._safe_enqueue(obs_event)

        # 2. Extract classification payload
        if isinstance(decision, dict):
            passed = bool(decision.get("passed", False))
            reason = str(decision.get("reason", ""))
            tags_raw = decision.get("tags")
            tags = list(tags_raw) if isinstance(tags_raw, (list, tuple, set)) else []
        else:
            passed = bool(getattr(decision, "passed", False))
            reason = str(getattr(decision, "reason", ""))
            tags_raw = getattr(decision, "tags", [])
            tags = list(tags_raw) if isinstance(tags_raw, (list, tuple, set)) else []

        rfp = rules_fingerprint(self.rules_paths) if self.rules_paths else ""

        cls_payload = {
            "observation_id": obs_event["event_id"],
            "passed": passed,
            "reason": reason,
            "rules_fingerprint": rfp,
            "tags": tags,
        }

        cls_event = make_event(
            kind="classification",
            task_id=task_id,
            payload=cls_payload,
            observed_at=now_iso,
            source=self.source,
            completeness=completeness,
        )
        self._safe_enqueue(cls_event)

    def can_offer(self, task_id: int) -> bool:
        """Check whether submitting an offer for task_id is permitted by archive safety guards."""
        try:
            if self.outbox.has_unresolved_attempt(task_id):
                return False
            usage = self.outbox.usage()
            if isinstance(usage, dict) and usage.get("full"):
                return False
            return True
        except ArchiveBlocked:
            raise
        except Exception as e:
            raise ArchiveBlocked(f"Archive error checking can_offer for {task_id}: {e}") from e

    def begin_offer(self, task_id: int, price: int, pitch: str) -> str:
        """Record an offer attempt durably BEFORE sending API request to YouDo.

        Returns unique attempt_id.
        Raises ArchiveBlocked if storage budget is full or unresolved attempt exists.
        """
        if not self.can_offer(task_id):
            raise ArchiveBlocked(f"Cannot offer for task {task_id}: unresolved attempt or storage full")

        now_ts = int(time.time())
        token = uuid.uuid4().hex[:6]
        attempt_id = f"att_{task_id}_{now_ts}_{token}"

        if self.dry_run:
            return attempt_id

        now_iso = _utc_now_iso()
        attempt_payload = {
            "attempt_id": attempt_id,
            "mode": "postpaid",
            "pitch": str(pitch),
            "price": int(price),
        }

        event = make_event(
            kind="offer_attempt",
            task_id=int(task_id),
            payload=attempt_payload,
            observed_at=now_iso,
            source=self.source,
            completeness="detail",
        )

        try:
            self.outbox.enqueue(event)
            self.outbox.record_attempt(
                int(task_id),
                attempt_id,
                {"price": int(price), "mode": "postpaid"},
            )
        except ArchiveBlocked:
            raise
        except Exception as e:
            raise ArchiveBlocked(f"Archive error beginning offer for {task_id}: {e}") from e

        return attempt_id

    def finish_offer(self, attempt_id: str, result: Dict[str, Any]) -> None:
        """Record offer submission outcome and resolve attempt in outbox ledger."""
        if self.dry_run or not isinstance(result, dict) or result.get("dry_run"):
            return

        safe = offer_payload(result)
        safe["attempt_id"] = str(attempt_id)

        # Status resolution
        if result.get("status") == "unknown" or result.get("unknown"):
            status = "unknown"
        elif result.get("success"):
            status = "success"
        else:
            status = "failed"

        if "status" in result:
            safe["status"] = result["status"]
        else:
            safe["status"] = status

        tid = safe.get("task_id")
        if tid is None:
            try:
                parts = str(attempt_id).split("_")
                if len(parts) >= 2:
                    tid = int(parts[1])
                    safe["task_id"] = tid
            except (ValueError, IndexError):
                tid = 0

        now_iso = _utc_now_iso()
        event = make_event(
            kind="offer_result",
            task_id=int(tid or 0),
            payload=safe,
            observed_at=now_iso,
            source=self.source,
            completeness="detail",
        )

        try:
            self.outbox.enqueue(event)
            self.outbox.resolve_attempt(str(attempt_id), status=status, result=safe)
        except ArchiveBlocked:
            raise
        except Exception as e:
            raise ArchiveBlocked(f"Archive error finishing offer {attempt_id}: {e}") from e

    def sync_now(self) -> Dict[str, Any]:
        """Trigger outbox synchronization pipeline if sync transport is configured."""
        if not self.sync:
            return {"synced": False, "reason": "no_sync_configured"}
        try:
            return self.sync.run()
        except ArchiveBlocked:
            raise
        except Exception as e:
            raise ArchiveBlocked(f"Archive sync error during sync_now: {e}") from e
