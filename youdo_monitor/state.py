"""State management, deduplication, and atomic persistence."""
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class StateManager:
    """Manages persistent state for seen tasks, notifications, and run metrics."""

    def __init__(self, state_file: Path | str, ttl_days: int = 14):
        self.state_file = Path(state_file)
        self.ttl_seconds = ttl_days * 86400
        self.state: Dict[str, Any] = {
            "seen_tasks": {},
            "seen_notifications": {},
            "seen_chat_messages": {},
            "detail_attempts": {},
            "last_run": None,
            "metrics": {
                "total_scanned": 0,
                "total_matched": 0,
                "total_notified": 0
            }
        }
        self.load()

    def load(self) -> None:
        """Load state from disk if available."""
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    self.state.update(data)
        except Exception as e:
            logger.warning("Failed to load state from %s: %s. Using fresh state.", self.state_file, e)

    def save(self) -> None:
        """Atomically persist state to disk using a temporary file."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = self.state_file.with_suffix(".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, self.state_file)
        except Exception as e:
            logger.error("Failed to save state to %s: %s", self.state_file, e)
            if tmp_file.exists():
                try:
                    tmp_file.unlink()
                except OSError:
                    pass
            raise

    def is_task_seen(self, task_id: int | str) -> bool:
        """Check if task has already been processed or alerted."""
        return str(task_id) in self.state["seen_tasks"]

    def is_offer_sent(self, task_id: int | str) -> bool:
        """Check if an offer was already submitted for this task."""
        item = self.state["seen_tasks"].get(str(task_id)) or {}
        status = item.get("status", "")
        return status.startswith("offered")

    def mark_offer_sent(
        self,
        task_id: int | str,
        price: int,
        pitch: str,
        mode: str = "green",
        result: Optional[Dict[str, Any]] = None
    ) -> None:
        """Record a submitted offer in state."""
        self.mark_task_seen(
            task_id,
            status="offered:postpaid",
            extra={
                "price": price,
                "mode": mode,
                "pitch": pitch[:400],
                "result": result or {}
            }
        )

    def mark_task_seen(self, task_id: int | str, status: str = "seen", extra: Optional[Dict[str, Any]] = None) -> None:
        """Mark task as processed with current timestamp."""
        payload: Dict[str, Any] = {
            "seen_at": time.time(),
            "status": status
        }
        if extra:
            payload["extra"] = extra
        self.state["seen_tasks"][str(task_id)] = payload
        # A recorded verdict supersedes any retry budget for the lookup.
        self.clear_detail_attempts(task_id)

    def record_detail_attempt(self, task_id: int | str) -> int:
        """Count a failed detail lookup and return the running total.

        The counter lives outside ``seen_tasks`` on purpose: an unprocessed task
        is not a seen task, and parking it must remain distinguishable from
        having judged it.
        """
        attempts = self.state.setdefault("detail_attempts", {})
        entry = attempts.get(str(task_id)) or {"count": 0, "first_at": time.time()}
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["last_at"] = time.time()
        attempts[str(task_id)] = entry
        return entry["count"]

    def detail_attempts(self, task_id: int | str) -> int:
        """Return how many detail lookups have failed for this task."""
        entry = self.state.get("detail_attempts", {}).get(str(task_id)) or {}
        return int(entry.get("count", 0))

    def clear_detail_attempts(self, task_id: int | str) -> None:
        """Drop the retry budget for a task."""
        self.state.get("detail_attempts", {}).pop(str(task_id), None)

    def is_notification_seen(self, key: str) -> bool:
        """Check whether an account notification was already forwarded."""
        return key in self.state.get("seen_notifications", {})

    def mark_notification_seen(self, key: str) -> None:
        """Record a forwarded account notification with current timestamp."""
        self.state.setdefault("seen_notifications", {})[key] = {
            "seen_at": time.time(),
            "status": "notified",
        }

    def is_chat_message_seen(self, message_id: int | str) -> bool:
        """Check whether a chat message was already forwarded.

        Messages carry a permanent integer ``Id``, so the dedup key never
        drifts the way a relative timestamp ("26 мин. назад") does.
        """
        return str(message_id) in self.state.get("seen_chat_messages", {})

    def mark_chat_message_seen(self, message_id: int | str, extra: Optional[Dict[str, Any]] = None) -> None:
        """Record a forwarded chat message with current timestamp."""
        payload: Dict[str, Any] = {
            "seen_at": time.time(),
            "status": "notified",
        }
        if extra:
            payload["extra"] = extra
        self.state.setdefault("seen_chat_messages", {})[str(message_id)] = payload

    def prune_expired(self) -> int:
        """Remove tasks older than TTL."""
        now = time.time()
        cutoff = now - self.ttl_seconds
        initial_count = len(self.state["seen_tasks"])
        self.state["seen_tasks"] = {
            tid: info for tid, info in self.state["seen_tasks"].items()
            if info.get("seen_at", 0) >= cutoff
        }
        pruned = initial_count - len(self.state["seen_tasks"])
        notes = self.state.setdefault("seen_notifications", {})
        # '_baselined' is an infrastructure marker, not history: if TTL prunes
        # it, the next run treats years-old notifications as fresh and spams.
        for key in [k for k, v in notes.items()
                    if k != "_baselined" and v.get("seen_at", 0) < cutoff]:
            del notes[key]
        msgs = self.state.setdefault("seen_chat_messages", {})
        for key in [k for k, v in msgs.items() if v.get("seen_at", 0) < cutoff]:
            del msgs[key]
        return pruned

    def get_seen_count(self) -> int:
        """Return total number of active seen tasks."""
        return len(self.state.get("seen_tasks", {}))

    def record_run(self, stats: Dict[str, Any]) -> None:
        """Record run timestamp and merge metrics."""
        self.state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
        metrics = self.state.setdefault("metrics", {})
        for k, v in stats.items():
            if isinstance(v, (int, float)):
                metrics[k] = metrics.get(k, 0) + v
            else:
                metrics[k] = v
