"""Tests for ArchiveCapture adapter (Task 7A).

Verifies safe, durable event recording, attempt locking, dry-run isolation,
and exception propagation without side effects.
"""
from pathlib import Path
from unittest.mock import Mock
import pytest

from youdo_monitor.archive_outbox import Outbox, ArchiveBlocked
from youdo_monitor.filters import Decision


def test_begin_offer_is_durable_before_return():
    from youdo_monitor.archive_capture import ArchiveCapture

    q, sync = Mock(), Mock()
    q.has_unresolved_attempt.return_value = False
    q.usage.return_value = {"full": False, "total_bytes": 1024}
    c = ArchiveCapture(q, sync)

    pitch_text = "Добрый день! Готов выполнить задачу качественно и в срок. Опыт более 5 лет."
    attempt_id = c.begin_offer(101, 2500, pitch_text)

    assert attempt_id
    assert attempt_id.startswith("att_101_")

    # Verification: offer_attempt event enqueued
    assert q.enqueue.called
    ev = q.enqueue.call_args.args[0]
    assert ev["kind"] == "offer_attempt"
    assert ev["task_id"] == 101
    assert ev["completeness"] == "detail"
    assert ev["payload"]["attempt_id"] == attempt_id
    assert ev["payload"]["price"] == 2500
    assert ev["payload"]["pitch"] == pitch_text
    assert ev["payload"]["mode"] == "postpaid"

    # Verification: attempt recorded in outbox ledger before returning
    assert q.record_attempt.called
    q.record_attempt.assert_called_once_with(
        101, attempt_id, {"price": 2500, "mode": "postpaid"}
    )


def test_unresolved_attempt_blocks_can_offer():
    from youdo_monitor.archive_capture import ArchiveCapture

    q, sync = Mock(), Mock()
    q.has_unresolved_attempt.return_value = True
    q.usage.return_value = {"full": False, "total_bytes": 1024}
    c = ArchiveCapture(q, sync)

    assert c.can_offer(101) is False
    with pytest.raises(ArchiveBlocked) as exc_info:
        c.begin_offer(101, 1000, "pitch")
    assert "Cannot offer" in str(exc_info.value)

    q.enqueue.assert_not_called()
    q.record_attempt.assert_not_called()


def test_full_outbox_blocks_can_offer():
    from youdo_monitor.archive_capture import ArchiveCapture

    q, sync = Mock(), Mock()
    q.has_unresolved_attempt.return_value = False
    q.usage.return_value = {"full": True, "total_bytes": 67108864}
    c = ArchiveCapture(q, sync)

    assert c.can_offer(101) is False
    with pytest.raises(ArchiveBlocked) as exc_info:
        c.begin_offer(101, 1000, "pitch")
    assert "Cannot offer" in str(exc_info.value)

    q.enqueue.assert_not_called()
    q.record_attempt.assert_not_called()


def test_observe_feed_enqueues_summary_events():
    from youdo_monitor.archive_capture import ArchiveCapture

    q = Mock()
    c = ArchiveCapture(q, source="youdo")

    feed = [
        {"id": 201, "name": "Настроить сервер Nginx", "budget": "5000", "category": "infra"},
        {"id": 202, "title": "Сделать Telegram бота", "Price": "10000", "CategoryFlag": "bot"},
    ]
    c.observe_feed(feed)

    assert q.enqueue.call_count == 2
    ev1 = q.enqueue.call_args_list[0].args[0]
    assert ev1["kind"] == "task_observed"
    assert ev1["task_id"] == 201
    assert ev1["completeness"] == "summary"
    assert ev1["source"] == "youdo"
    assert ev1["payload"]["title"] == "Настроить сервер Nginx"
    assert ev1["payload"]["budget"] == "5000"

    ev2 = q.enqueue.call_args_list[1].args[0]
    assert ev2["kind"] == "task_observed"
    assert ev2["task_id"] == 202
    assert ev2["completeness"] == "summary"


def test_observe_detail_links_observation_and_classification():
    from youdo_monitor.archive_capture import ArchiveCapture

    q = Mock()
    c = ArchiveCapture(q, source="youdo")

    summary = {"id": 301, "name": "Доработка интернет-магазина", "budget": "15000"}
    detail = {
        "TaskData": {
            "title": "Доработка интернет-магазина",
            "description": "Необходимо исправить верстку и настроить API интеграцию с 1C.",
            "Price": {"TotalPrice": 15000},
            "category_name": "web",
            "is_virtual": True,
        }
    }
    decision = Decision(passed=True, reason="ok", tags=["web", "dev", "bugfix"])

    c.observe_detail(summary, detail, decision)

    assert q.enqueue.call_count == 2

    # First event: task_observed
    obs_ev = q.enqueue.call_args_list[0].args[0]
    assert obs_ev["kind"] == "task_observed"
    assert obs_ev["task_id"] == 301
    assert obs_ev["completeness"] == "detail"
    assert obs_ev["payload"]["description"] == "Необходимо исправить верстку и настроить API интеграцию с 1C."
    assert obs_ev["payload"]["budget"] == "15000"

    # Second event: classification linked via observation_id
    cls_ev = q.enqueue.call_args_list[1].args[0]
    assert cls_ev["kind"] == "classification"
    assert cls_ev["task_id"] == 301
    assert cls_ev["completeness"] == "detail"
    assert cls_ev["payload"]["observation_id"] == obs_ev["event_id"]
    assert cls_ev["payload"]["passed"] is True
    assert cls_ev["payload"]["reason"] == "ok"
    assert cls_ev["payload"]["tags"] == ["web", "dev", "bugfix"]
    assert len(cls_ev["payload"]["rules_fingerprint"]) == 64


def test_observe_detail_error_does_not_become_complete():
    from youdo_monitor.archive_capture import ArchiveCapture

    q = Mock()
    c = ArchiveCapture(q)

    summary = {"id": 401, "name": "Заказ с ошибкой деталей"}
    detail = {"error": "request_timeout"}
    decision = Decision(passed=False, reason="description_too_short")

    c.observe_detail(summary, detail, decision)

    assert q.enqueue.call_count == 2
    obs_ev = q.enqueue.call_args_list[0].args[0]
    cls_ev = q.enqueue.call_args_list[1].args[0]

    assert obs_ev["completeness"] == "partial"
    assert cls_ev["completeness"] == "partial"
    assert cls_ev["payload"]["passed"] is False
    assert cls_ev["payload"]["observation_id"] == obs_ev["event_id"]


def test_finish_offer_sanitizes_failure_and_resolves_attempt():
    from youdo_monitor.archive_capture import ArchiveCapture

    q = Mock()
    c = ArchiveCapture(q)

    attempt_id = "att_501_1729000000_a1b2c3"
    raw_api_result = {
        "success": False,
        "task_id": 501,
        "status_code": 403,
        "error": "Недостаточно средств",
        "secret_token": "UNWANTED_SECRET",
        "user_profile": {"name": "Василий", "phone": "+79990001122"},
    }

    c.finish_offer(attempt_id, raw_api_result)

    assert q.enqueue.called
    ev = q.enqueue.call_args.args[0]
    assert ev["kind"] == "offer_result"
    assert ev["task_id"] == 501
    assert ev["completeness"] == "detail"
    assert ev["payload"]["attempt_id"] == attempt_id
    assert ev["payload"]["success"] is False
    assert ev["payload"]["status_code"] == 403
    assert ev["payload"]["message"] == "Недостаточно средств"

    # Leakage check: sensitive private fields stripped by allowlist
    assert "secret_token" not in ev["payload"]
    assert "user_profile" not in ev["payload"]

    # Resolution in outbox ledger
    q.resolve_attempt.assert_called_once_with(
        attempt_id, status="failed", result=ev["payload"]
    )


def test_finish_offer_unknown_response_preserves_unresolved():
    from youdo_monitor.archive_capture import ArchiveCapture

    q = Mock()
    c = ArchiveCapture(q)

    attempt_id = "att_601_1729000000_123456"
    raw_result = {
        "status": "unknown",
        "task_id": 601,
        "message": "Connection reset by peer during offer post",
    }

    c.finish_offer(attempt_id, raw_result)

    q.resolve_attempt.assert_called_once()
    args, kwargs = q.resolve_attempt.call_args
    assert kwargs.get("status") == "unknown" or (len(args) > 1 and args[1] == "unknown")
    ev = q.enqueue.call_args.args[0]
    assert ev["payload"]["attempt_id"] == attempt_id


def test_dry_run_mode_has_separate_source_and_creates_no_real_offer_events():
    from youdo_monitor.archive_capture import ArchiveCapture

    q, sync = Mock(), Mock()
    q.has_unresolved_attempt.return_value = False
    q.usage.return_value = {"full": False, "total_bytes": 1024}

    c = ArchiveCapture(q, sync, dry_run=True)
    assert c.source == "dry-run"

    # Observations in dry-run tag source as dry-run
    c.observe_feed([{"id": 701, "name": "Тестовый заказ"}])
    assert q.enqueue.called
    assert q.enqueue.call_args.args[0]["source"] == "dry-run"

    q.enqueue.reset_mock()
    q.record_attempt.reset_mock()

    # begin_offer in dry-run generates an ID but records NO durable ledger attempt
    attempt_id = c.begin_offer(701, 1000, "Текст отклика в dry-run")
    assert attempt_id
    assert q.enqueue.called is False
    assert q.record_attempt.called is False

    # finish_offer in dry-run does NOT enqueue real offer_result event
    c.finish_offer(attempt_id, {"success": True, "dry_run": True})
    assert q.enqueue.called is False
    assert q.resolve_attempt.called is False


def test_finish_offer_simulated_success_not_saved_as_real_offer():
    from youdo_monitor.archive_capture import ArchiveCapture

    q = Mock()
    c = ArchiveCapture(q, dry_run=False)

    attempt_id = "att_801_1729000000_789abc"
    # Even if ArchiveCapture wasn't initialized as dry_run, a simulated result must not persist as real offer
    c.finish_offer(attempt_id, {"success": True, "task_id": 801, "dry_run": True})
    assert q.enqueue.called is False
    assert q.resolve_attempt.called is False


def test_last_seen_without_content_duplication(tmp_path: Path):
    from youdo_monitor.archive_capture import ArchiveCapture

    db_path = tmp_path / "outbox.db"
    outbox = Outbox(db_path)
    capture = ArchiveCapture(outbox, source="youdo")

    feed = [
        {"id": 901, "name": "Заказ 901", "budget": "3000", "category": "web"},
        {"id": 902, "name": "Заказ 902", "budget": "4000", "category": "dev"},
    ]

    capture.observe_feed(feed)
    assert len(outbox.pending()) == 2

    # Second pass with identical content must not create duplicate events
    capture.observe_feed(feed)
    assert len(outbox.pending()) == 2

    outbox.close()


def test_sync_now_delegation():
    from youdo_monitor.archive_capture import ArchiveCapture

    q = Mock()
    c_no_sync = ArchiveCapture(q, sync=None)
    assert c_no_sync.sync_now() == {"synced": False, "reason": "no_sync_configured"}

    mock_sync = Mock()
    mock_sync.run.return_value = {"synced": True, "batch_id": "b_123"}
    c_with_sync = ArchiveCapture(q, sync=mock_sync)
    assert c_with_sync.sync_now() == {"synced": True, "batch_id": "b_123"}


def test_archive_exceptions_surface_as_archive_blocked():
    from youdo_monitor.archive_capture import ArchiveCapture

    q = Mock()
    q.has_unresolved_attempt.return_value = False
    q.usage.return_value = {"full": False, "total_bytes": 1024}
    q.enqueue.side_effect = RuntimeError("disk I/O failure")

    c = ArchiveCapture(q)

    with pytest.raises(ArchiveBlocked) as exc_info:
        c.observe_feed([{"id": 999, "name": "Crash task"}])
    assert "disk I/O failure" in str(exc_info.value)


def test_capture_has_no_side_effects_on_offermanager_or_telegram():
    import sys
    from youdo_monitor import archive_capture

    # Verify archive_capture module does not import offers or notifier
    imported_modules = dir(archive_capture)
    assert "OfferManager" not in imported_modules
    assert "Notifier" not in imported_modules


def test_full_pitch_preservation_without_truncation():
    from youdo_monitor.archive_capture import ArchiveCapture

    q = Mock()
    q.has_unresolved_attempt.return_value = False
    q.usage.return_value = {"full": False, "total_bytes": 1024}
    c = ArchiveCapture(q)

    long_pitch = "Очень длинный оффер с подробным планом работ...\n" * 200
    assert len(long_pitch) > 5000

    attempt_id = c.begin_offer(1001, 50000, long_pitch)
    ev = q.enqueue.call_args.args[0]
    assert ev["payload"]["pitch"] == long_pitch
    assert len(ev["payload"]["pitch"]) == len(long_pitch)


def test_finish_offer_parses_task_id_from_attempt_id_fallback():
    from youdo_monitor.archive_capture import ArchiveCapture

    q = Mock()
    c = ArchiveCapture(q)

    # Result dictionary lacks task_id
    attempt_id = "att_12345_1729000000_ffffff"
    c.finish_offer(attempt_id, {"success": True, "price": 5000, "status_code": 200})

    ev = q.enqueue.call_args.args[0]
    assert ev["task_id"] == 12345
    assert ev["payload"]["task_id"] == 12345
    assert ev["payload"]["attempt_id"] == attempt_id
    q.resolve_attempt.assert_called_once_with(attempt_id, status="success", result=ev["payload"])


def test_custom_rules_paths_fingerprinting(tmp_path: Path):
    from youdo_monitor.archive_capture import ArchiveCapture

    q = Mock()
    rule_file = tmp_path / "custom_rules.txt"
    rule_file.write_text("CLASSIFIER_V2_ACTIVE = TRUE\n", encoding="utf-8")

    c = ArchiveCapture(q, rules_paths=[rule_file])
    assert c.rules_paths == [rule_file]

    summary = {"id": 2222, "title": "Custom rule test"}
    detail = {"TaskData": {"description": "Detailed desc for rule fp test"}}
    decision = Decision(passed=True, reason="ok")

    c.observe_detail(summary, detail, decision)
    cls_ev = q.enqueue.call_args_list[1].args[0]
    assert len(cls_ev["payload"]["rules_fingerprint"]) == 64


def test_observe_feed_skips_malformed_items():
    from youdo_monitor.archive_capture import ArchiveCapture

    q = Mock()
    c = ArchiveCapture(q)

    feed = [
        "not_a_dict",
        {"no_id": "item"},
        {"id": "not_an_int"},
        {"id": 3333, "title": "Valid task"},
    ]
    c.observe_feed(feed)

    assert q.enqueue.call_count == 1
    ev = q.enqueue.call_args.args[0]
    assert ev["task_id"] == 3333


def test_sync_now_exception_raises_archive_blocked():
    from youdo_monitor.archive_capture import ArchiveCapture

    mock_sync = Mock()
    mock_sync.run.side_effect = RuntimeError("network connection dropped")

    c = ArchiveCapture(Mock(), sync=mock_sync)
    with pytest.raises(ArchiveBlocked) as exc_info:
        c.sync_now()
    assert "network connection dropped" in str(exc_info.value)

