"""Notification dedup must key on stable identity and mark only after delivery.

The panel renders `when` as a relative label ("26 мин. назад"), so a key of
`when|text` mints a fresh key every cycle for the same event. And marking the
key before the Telegram send means a delivery failure loses the event forever.
"""
import json
import time

import pytest

from youdo_monitor import runner as runner_mod
from youdo_monitor.runner import run_once


class FakeNotifier:
    def __init__(self, succeeds=True):
        self.succeeds = succeeds
        self.sent = []
        self.failed_count = 0

    def send_raw(self, text):
        self.sent.append(text)
        if not self.succeeds:
            self.failed_count += 1
        return self.succeeds

    def send_status(self, text):
        self.sent.append(text)
        return True

    def send_tasks(self, tasks):
        return 0


class FakeClient:
    def __init__(self, items=None):
        self._warmed_up = True
        self._items = items or []

    def start(self):
        pass

    def close(self):
        pass

    def check_auth_status(self):
        return {"authenticated": True}

    def fetch_account_counts(self):
        return {"notifications": len(self._items), "messages": 0}

    def fetch_notifications(self, limit=15):
        return list(self._items)

    def fetch_feed(self, **kw):
        return []

    def fetch_task_details(self, ids):
        return []

    def fetch_chat_messages(self, max_dialogs=5):
        return []

    def mark_all_notifications_read(self):
        return True


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setattr(runner_mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(runner_mod, "LOG_FILE", tmp_path / "run.log")
    holder = {}

    def make_client(headless=True):
        holder["client"] = FakeClient(**holder.get("client_kw", {}))
        return holder["client"]

    def make_notifier(dry_run=False):
        holder["notifier"] = FakeNotifier(**holder.get("notifier_kw", {}))
        return holder["notifier"]

    monkeypatch.setattr(runner_mod, "YouDoClient", make_client)
    monkeypatch.setattr(runner_mod, "Notifier", make_notifier)

    def baseline():
        (tmp_path / "state.json").write_text(
            json.dumps({"seen_notifications": {"_baselined": {"seen_at": time.time()}}}),
            encoding="utf-8",
        )

    holder["baseline"] = baseline
    return holder


def state_of(wired):
    return json.loads(runner_mod.STATE_FILE.read_text(encoding="utf-8"))


def test_same_event_with_shifted_relative_stamp_is_not_forwarded_twice(wired):
    """The panel re-renders "5 мин. назад" as "20 мин. назад" every cycle."""
    wired["baseline"]()
    wired["client_kw"] = {"items": [{"when": "5 мин. назад", "text": "Отклик отправлен."}]}
    first = run_once(enable_auto_offer=False)

    wired["client_kw"] = {"items": [{"when": "20 мин. назад", "text": "Отклик отправлен."}]}
    second = run_once(enable_auto_offer=False)

    assert first["new_notifications"] == 1
    assert second["new_notifications"] == 0, "a shifted relative stamp is the same event"
    # One key for one event, regardless of how many times the stamp re-rendered.
    assert "text:Отклик отправлен." in state_of(wired)["seen_notifications"]


def test_failed_delivery_leaves_the_event_for_the_next_run(wired):
    wired["baseline"]()
    wired["client_kw"] = {"items": [{"when": "5 мин. назад", "text": "Заказчик написал."}]}
    wired["notifier_kw"] = {"succeeds": False}

    run_once(enable_auto_offer=False)
    keys_after_failure = state_of(wired)["seen_notifications"]
    assert "text:Заказчик написал." not in keys_after_failure

    wired["notifier_kw"] = {"succeeds": True}
    second = run_once(enable_auto_offer=False)

    assert second["new_notifications"] == 1, "the event must survive a delivery failure"


def test_successful_delivery_records_the_key(wired):
    wired["baseline"]()
    wired["client_kw"] = {"items": [{"when": "5 мин. назад", "text": "Счет пополнен."}]}
    run_once(enable_auto_offer=False)

    assert "text:Счет пополнен." in state_of(wired)["seen_notifications"]


def test_baseline_run_marks_history_without_sending(wired):
    """First authenticated run establishes a watermark; forwarding years of
    history is a defect, not a feature."""
    wired["client_kw"] = {"items": [{"when": "12 янв.", "text": "Старое событие."}]}
    stats = run_once(enable_auto_offer=False)

    assert stats["new_notifications"] == 0
    assert wired["notifier"].sent == []
    assert "text:Старое событие." in state_of(wired)["seen_notifications"]


def test_distinct_texts_are_both_forwarded(wired):
    wired["baseline"]()
    wired["client_kw"] = {"items": [
        {"when": "5 мин. назад", "text": "Отклик отправлен."},
        {"when": "6 мин. назад", "text": "Счет пополнен."},
    ]}
    stats = run_once(enable_auto_offer=False)
    assert stats["new_notifications"] == 2


def test_legacy_keys_do_not_trigger_a_one_off_flood(wired):
    """State written before the key change holds `when|text` entries. Without a
    migration step every currently visible notification looks new once, and the
    user gets a batch of already-seen events."""
    path = runner_mod.STATE_FILE
    path.write_text(json.dumps({"seen_notifications": {
        "_baselined": {"seen_at": time.time()},
        "5 мин. назад|Отклик отправлен.": {"seen_at": time.time(), "status": "notified"},
    }}), encoding="utf-8")

    wired["client_kw"] = {"items": [{"when": "20 мин. назад", "text": "Отклик отправлен."}]}
    stats = run_once(enable_auto_offer=False)

    assert stats["new_notifications"] == 0, "legacy state must be migrated, not re-forwarded"
    assert "text:Отклик отправлен." in state_of(wired)["seen_notifications"]
