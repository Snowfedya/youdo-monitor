"""Tests for the run-loop contract: notifications are marked read only after
a successful forward, and the state survives a failed send."""
import json
import time

import pytest

from youdo_monitor import runner as runner_mod
from youdo_monitor.runner import run_once


class FakeNotifier:
    """Records delivery outcome; `succeeds=False` simulates a telegram failure."""

    def __init__(self, succeeds=True):
        self.succeeds = succeeds
        self.sent = []
        self.failed_count = 0

    def send_raw(self, text):
        self.sent.append(text)
        return self.succeeds

    def send_status(self, text):
        self.sent.append(text)
        return True

    def send_tasks(self, tasks):
        return 0

    @property
    def format_card(self):  # not used on the notification path
        raise AssertionError("format_card must not be called here")


class FakeClient:
    """Minimal YouDoClient stand-in recording mark-as-read calls."""

    def __init__(self, notifications=0, items=None, mark_ok=True):
        self._warmed_up = True
        self._notifications = notifications
        self._items = items or []
        self._mark_ok = mark_ok
        self.mark_calls = []

    def start(self):
        pass

    def close(self):
        pass

    def check_auth_status(self):
        return {"authenticated": True}

    def fetch_account_counts(self):
        return {"notifications": self._notifications, "messages": 0}

    def fetch_notifications(self, limit=15):
        return self._items

    def fetch_feed(self, **kw):
        return []

    def fetch_task_details(self, ids):
        return []

    def mark_all_notifications_read(self):
        self.mark_calls.append(True)
        return self._mark_ok


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Patch module globals so run_once touches no browser, telegram or disk.

    The state file is pre-seeded with `_baselined` so the run models steady
    state: a fresh state makes the runner baseline the whole history instead of
    forwarding it, which is real behaviour but not what these tests exercise.
    """
    monkeypatch.setattr(runner_mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(runner_mod, "LOG_FILE", tmp_path / "run.log")
    (tmp_path / "state.json").write_text(
        json.dumps({"seen_notifications": {"_baselined": {"seen_at": time.time()}}}),
        encoding="utf-8",
    )

    holder = {}

    def make_client(headless=True):
        holder["client"] = FakeClient(**holder.get("client_kw", {}))
        return holder["client"]

    monkeypatch.setattr(runner_mod, "YouDoClient", make_client)

    def make_notifier(dry_run=False):
        holder["notifier"] = FakeNotifier(**holder.get("notifier_kw", {}))
        return holder["notifier"]

    monkeypatch.setattr(runner_mod, "Notifier", make_notifier)
    return holder


ITEMS = [
    {"when": "5 ч. 2 мин. назад", "text": "Отклик отправлен."},
    {"when": "5 ч. 7 мин. назад", "text": "Счет пополнен на 500 руб."},
]


def test_new_notifications_are_forwarded_then_marked_read(wired):
    """The user's request: after forwarding to Telegram, clear the badge."""
    wired["client_kw"] = {"notifications": 2, "items": ITEMS}
    stats = run_once()

    client = wired["client"]
    notifier = wired["notifier"]
    assert stats["new_notifications"] == 2
    assert len(client.mark_calls) == 1
    assert stats["notifications_marked_read"] is True
    assert "Отклик отправлен." in notifier.sent[0]


def test_not_marked_read_when_delivery_fails(wired):
    """Marking first would lose the event on a telegram outage — the badge is
    what makes a missed notification visible on the next run."""
    wired["client_kw"] = {"notifications": 2, "items": ITEMS}
    wired["notifier_kw"] = {"succeeds": False}

    stats = run_once()

    assert wired["client"].mark_calls == []
    assert stats.get("notifications_marked_read") is False


def test_not_marked_read_when_mark_call_itself_fails(wired):
    """A rejected POST must be visible in the run stats, not silently true."""
    wired["client_kw"] = {
        "notifications": 2, "items": ITEMS, "mark_ok": False,
    }
    stats = run_once()
    assert len(wired["client"].mark_calls) == 1
    assert stats.get("notifications_marked_read") is not True


def test_no_new_notifications_leaves_badge_untouched(wired):
    """Nothing new to forward, so nothing is marked — and it does not matter
    that the badge still shows unread: the watermark POST is what clears it,
    and sending it blindly would touch notifications the panel never listed."""
    wired["client_kw"] = {"notifications": 4, "items": []}
    stats = run_once()
    assert wired["client"].mark_calls == []
    assert stats["new_notifications"] == 0


def test_dry_run_never_mutates_read_state(wired):
    wired["client_kw"] = {"notifications": 2, "items": ITEMS}
    stats = run_once(dry_run=True)
    assert wired["client"].mark_calls == []
    assert "notifications_marked_read" not in stats


def test_guest_session_marks_nothing(wired, monkeypatch):
    """Account endpoints 404 for a guest session; marking must be skipped."""
    def guest(headless=True):
        client = FakeClient(notifications=2, items=ITEMS)
        client.check_auth_status = lambda: {"authenticated": False}
        return client

    monkeypatch.setattr(runner_mod, "YouDoClient", guest)
    stats = run_once()

    assert stats["authenticated"] is False
    # The whole account-notification block is skipped, so the key is absent
    # rather than zero — nothing is read and nothing is forwarded.
    assert "new_notifications" not in stats
    assert "notifications_marked_read" not in stats