"""Funnel ledger: separate classes for refusals, errors and success.

The question this answers is the only one that decides whether buying contact
packages makes sense: of the tasks seen, how many passed the filter, how many
offers the platform accepted, and how many refusals were about money. A single
`offers_skipped` counter cannot answer it.
"""
import json
import time

import pytest

from youdo_monitor import runner as runner_mod
from youdo_monitor.funnel import Funnel, classify_offer_result, load, summarize


class FakeNotifier:
    def __init__(self, succeeds=True):
        self.succeeds = succeeds
        self.sent = []
        self.failed_count = 0

    def send_raw(self, text):
        self.sent.append(text)
        return self.succeeds

    def send_status(self, text):
        return True

    def send_tasks(self, tasks):
        return len(tasks)


class FakeClient:
    _warmed_up = True

    def __init__(self, tasks=None, detail=None):
        self._tasks = tasks or []
        self._detail = detail or {}

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
        return [dict(t) for t in self._tasks]

    def fetch_task_details(self, ids):
        return [dict(self._detail) for _ in ids]

    def fetch_chat_messages(self, max_dialogs=5):
        return []

    def mark_all_notifications_read(self):
        return True


GOOD_TASK = {
    "id": 777,
    "name": "Настроить nginx и SSL на VPS",
    "link": "https://youdo.com/t777",
    "budget": "до 9 000 ₽",
    "category": "webdevelopment",
}

BAD_TASK = {
    "id": 888,
    "name": "Сделать логотип",
    "link": "https://youdo.com/t888",
    "budget": "до 1 000 ₽",
    "category": "webdevelopment",
}

GOOD_DETAIL = {
    "id": 777,
    "description": "VPS Ubuntu, нужен nginx reverse proxy и SSL. SSH дам.",
    "offers_count": 2,
    "is_in_progress": False,
    "is_virtual": True,
    "is_b2b": False,
}

BAD_DETAIL = {
    "id": 888,
    "description": "Нарисовать логотип для кофейни, три варианта.",
    "offers_count": 2,
    "is_in_progress": False,
    "is_virtual": True,
    "is_b2b": False,
}


@pytest.fixture
def wired(monkeypatch, tmp_path):
    monkeypatch.setattr(runner_mod, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(runner_mod, "LOG_FILE", tmp_path / "run.log")
    monkeypatch.setattr(runner_mod, "FUNNEL_FILE", tmp_path / "funnel.jsonl")
    (tmp_path / "state.json").write_text(
        json.dumps({"seen_notifications": {"_baselined": {"seen_at": time.time()},
                                           "_key_scheme": {"seen_at": time.time()}}}),
        encoding="utf-8",
    )
    holder = {"tmp": tmp_path}

    def make_client(headless=True):
        return FakeClient(**holder.get("client_kw", {}))

    def make_notifier(dry_run=False):
        holder["notifier"] = FakeNotifier(**holder.get("notifier_kw", {}))
        return holder["notifier"]

    monkeypatch.setattr(runner_mod, "YouDoClient", make_client)
    monkeypatch.setattr(runner_mod, "Notifier", make_notifier)
    return holder


# --- classification --------------------------------------------------------

def test_success_is_only_an_explicit_platform_confirmation():
    assert classify_offer_result({"success": True, "status": "success"}) == "confirmed_success"


def test_balance_refusal_is_its_own_class():
    result = {"success": False, "status": "failed",
              "error": "Пополните баланс для оплаты контакта"}
    assert classify_offer_result(result) == "refused_balance"


def test_escrow_only_refusal_is_not_a_balance_refusal():
    result = {"success": False, "status": "failed",
              "error": "Оплата задания возможна только через Сделку без риска"}
    assert classify_offer_result(result) == "refused_escrow_only"


def test_ambiguous_reply_stays_unknown():
    assert classify_offer_result({"success": False, "status": "unknown"}) == "unknown"


def test_explicit_failure_without_money_text_is_an_error():
    assert classify_offer_result({"success": False, "status": "failed",
                                  "error": "Task already closed"}) == "error"


def test_dry_run_is_not_counted_as_a_platform_result():
    assert classify_offer_result({"success": True, "dry_run": True}) == "dry_run"


# --- ledger ----------------------------------------------------------------

def test_ledger_appends_one_json_line_per_record(tmp_path):
    funnel = Funnel(tmp_path / "funnel.jsonl")
    funnel.record("task_seen", task_id=1)
    funnel.record("filter_decision", task_id=1, decision="passed")

    rows = load(tmp_path / "funnel.jsonl")
    assert [r["event"] for r in rows] == ["task_seen", "filter_decision"]
    assert all("ts" in r for r in rows)


def test_torn_line_does_not_break_summary(tmp_path):
    path = tmp_path / "funnel.jsonl"
    path.write_text('{"event": "task_seen"}\n{"event": "task_', encoding="utf-8")
    assert summarize(path)["task_seen"]["total"] == 1


def test_disabled_funnel_records_nothing(tmp_path):
    assert Funnel(None).record("task_seen", task_id=1) is None
    assert not (tmp_path / "funnel.jsonl").exists()


# --- runner integration ----------------------------------------------------

def test_run_writes_events_for_the_whole_path(wired):
    wired["client_kw"] = {"tasks": [GOOD_TASK], "detail": GOOD_DETAIL}
    runner_mod.run_once(enable_auto_offer=False)

    summary = summarize(wired["tmp"] / "funnel.jsonl")
    assert summary["task_seen"]["total"] == 1
    assert summary["detail_status"]["ok"] == 1
    assert summary["filter_decision"]["passed"] == 1


def test_rejection_reason_is_recorded(wired):
    wired["client_kw"] = {"tasks": [BAD_TASK], "detail": BAD_DETAIL}
    runner_mod.run_once(enable_auto_offer=False)

    rows = [r for r in load(wired["tmp"] / "funnel.jsonl")
            if r["event"] == "filter_decision"]
    assert rows and rows[0]["decision"] == "rejected"
    assert rows[0]["reason"]


def test_counts_reconcile_with_run_stats(wired):
    wired["client_kw"] = {"tasks": [GOOD_TASK, BAD_TASK], "detail": GOOD_DETAIL}
    stats = runner_mod.run_once(enable_auto_offer=False)

    summary = summarize(wired["tmp"] / "funnel.jsonl")
    assert summary["task_seen"]["total"] == stats["new"]
    accepted = summary["filter_decision"].get("passed", 0)
    assert accepted == stats["matched"]


# --- backfill --------------------------------------------------------------

def test_backfill_reads_logged_responses_and_keeps_their_timestamps(tmp_path):
    from youdo_monitor.backfill import backfill

    log = tmp_path / "run.log"
    log.write_text(
        "2026-10-18 12:00:22,279 [INFO] youdo_monitor.offers: Offer API response for "
        "task 15330465: {'status': 200, 'ok': True, 'data': {'ResultObject': None, "
        "'IsSuccess': False, 'Code': 1, 'Message': 'Оплата задания возможна только "
        "через Сделку без риска', 'Time': 1}}\n"
        "2026-10-19 00:15:23,573 [INFO] youdo_monitor.offers: Offer API response for "
        "task 15334908: {'status': 200, 'ok': True, 'data': {'ResultObject': None, "
        "'IsSuccess': False, 'Code': 1, 'Message': 'Пополните баланс для отправки "
        "отклика', 'Time': 1}}\n",
        encoding="utf-8",
    )
    ledger = tmp_path / "funnel.jsonl"
    counts = backfill(log, ledger)

    assert counts == {"refused_escrow_only": 1, "refused_balance": 1}
    rows = load(ledger)
    assert [r["ts"] for r in rows] == ["2026-10-18 12:00:22", "2026-10-19 00:15:23"]
    assert all(r["source"] == "backfill:run.log" for r in rows)
