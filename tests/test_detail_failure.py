"""Detail-fetch failures must not consume a task.

A transient error from /api/tasks/task/{id}/ currently produces an empty detail
payload, the classifier then sees a task with no description and rejects it as
`description_too_short`, and the runner marks it seen — so a genuinely good
order silently disappears after one bad request. These tests pin the contract
that a failed lookup is retried instead.
"""
import json
import time

import pytest

from youdo_monitor import runner as runner_mod
from youdo_monitor.filters import TaskClassifier
from youdo_monitor.runner import detail_status, run_once

MAX_ATTEMPTS = runner_mod.MAX_DETAIL_ATTEMPTS

GOOD_TASK = {
    "id": 4242,
    "name": "Настроить nginx и SSL на VPS",
    "link": "https://youdo.com/t4242",
    "budget": "до 8 000 ₽",
    "category": "webdevelopment",
}

GOOD_DETAIL = {
    "id": 4242,
    "description": "Есть VPS на Ubuntu, нужно настроить nginx как reverse proxy "
                   "и выпустить SSL. Доступ по SSH дам. Нужен рабочий сайт.",
    "offers_count": 3,
    "is_in_progress": False,
    "status_flag": "opened",
    "is_virtual": True,
    "is_b2b": False,
    "category_name": "Компьютеры и IT",
    "subcategory_name": "Серверы",
}


class FakeNotifier:
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


class FakeClient:
    """Feed of one task; detail responses are scripted per run."""

    def __init__(self, feed=None, details=None):
        self._warmed_up = True
        self._feed = feed if feed is not None else [dict(GOOD_TASK)]
        self._details = details
        self.detail_calls = 0

    def start(self):
        pass

    def close(self):
        pass

    def check_auth_status(self):
        return {"authenticated": True}

    def fetch_account_counts(self):
        return {"notifications": 0, "messages": 0}

    def fetch_notifications(self, limit=15):
        return []

    def fetch_feed(self, **kw):
        return [dict(t) for t in self._feed]

    def fetch_task_details(self, ids):
        self.detail_calls += 1
        if callable(self._details):
            return self._details(self.detail_calls, ids)
        if self._details is not None:
            return self._details
        return [dict(GOOD_DETAIL)]

    def fetch_chat_messages(self, max_dialogs=5):
        return []

    def mark_all_notifications_read(self):
        return True


@pytest.fixture
def wired(monkeypatch, tmp_path):
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

    def make_notifier(dry_run=False):
        holder["notifier"] = FakeNotifier()
        return holder["notifier"]

    monkeypatch.setattr(runner_mod, "YouDoClient", make_client)
    monkeypatch.setattr(runner_mod, "Notifier", make_notifier)
    return holder


def read_state(holder):
    path = runner_mod.STATE_FILE
    return json.loads(path.read_text(encoding="utf-8"))


# --- detail_status ---------------------------------------------------------

def test_detail_status_ok_for_real_payload():
    assert detail_status(GOOD_DETAIL) == "ok"


def test_detail_status_error_when_client_reported_a_failure():
    assert detail_status({"id": 1, "error": "Timeout 45000ms exceeded"}) == "error"


def test_detail_status_missing_when_no_payload_was_returned():
    assert detail_status({}) == "missing"


# --- classifier ------------------------------------------------------------

def test_classifier_names_missing_detail_instead_of_short_description():
    """`description_too_short` is a verdict about the client's text;
    a failed lookup says nothing about the task and must not be reported as one."""
    task = dict(GOOD_TASK, detail_status="error")
    decision = TaskClassifier().evaluate(task)
    assert decision.passed is False
    assert decision.reason == "no_detail_data"


def test_classifier_still_rejects_genuinely_short_description():
    task = dict(GOOD_TASK, detail_status="ok", description="надо сайт")
    decision = TaskClassifier().evaluate(task)
    assert decision.reason == "description_too_short"


# --- runner ----------------------------------------------------------------

def test_failed_detail_does_not_consume_the_task(wired):
    wired["client_kw"] = {"details": [{"id": GOOD_TASK["id"], "error": "timeout"}]}
    stats = run_once(enable_auto_offer=False)

    state = read_state(wired)
    assert str(GOOD_TASK["id"]) not in state["seen_tasks"], "failed lookup must leave the task unprocessed"
    assert stats["detail_errors"] == 1
    assert stats["matched"] == 0


def test_task_is_parked_after_repeated_detail_failures(wired):
    wired["client_kw"] = {"details": [{"id": GOOD_TASK["id"], "error": "timeout"}]}
    for _ in range(MAX_ATTEMPTS):
        run_once(enable_auto_offer=False)

    entry = read_state(wired)["seen_tasks"][str(GOOD_TASK["id"])]
    assert entry["status"] == "detail_unavailable"


def test_successful_detail_after_one_failure_is_processed_normally(wired):
    # The client is rebuilt per run, so the counter must outlive it.
    calls = {"n": 0}

    def scripted(call_no, ids):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"id": GOOD_TASK["id"], "error": "timeout"}]
        return [dict(GOOD_DETAIL)]

    wired["client_kw"] = {"details": scripted}
    run_once(enable_auto_offer=False)
    stats = run_once(enable_auto_offer=False)

    entry = read_state(wired)["seen_tasks"][str(GOOD_TASK["id"])]
    assert entry["status"] == "matched"
    assert stats["matched"] == 1
    assert stats["detail_errors"] == 0
