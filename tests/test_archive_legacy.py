"""Tests for historical state import into archive outbox without profile leakage."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import pytest

from youdo_monitor.archive_legacy import import_state
from youdo_monitor.archive_outbox import Outbox


@pytest.fixture
def legacy_state():
    """Synthetic legacy state containing tasks with private profile markers."""
    return {
        "seen_tasks": {
            "1001": {
                "seen_at": 1712345678.0,
                "status": "rejected:price_too_low",
                "extra": {
                    "title": "Сайт на Тильде под ключ",
                    "CreatorInfo": {
                        "phone": "PRIVATE_PHONE_SECRET_1",
                        "email": "PRIVATE_EMAIL_SECRET_1@example.com",
                        "avatar": "https://img.youdo.com/private_avatar_1.jpg",
                    },
                },
            },
            "1002": {
                "seen_at": 1712345680.0,
                "status": "matched",
                "extra": {
                    "title": "Настройка Nginx на VPS сервере",
                    "UserProfile": {
                        "phone": "PRIVATE_PHONE_SECRET_2",
                        "email": "PRIVATE_EMAIL_SECRET_2@example.com",
                    },
                },
            },
            "1003": {
                "seen_at": 1712345690.0,
                "status": "offered:postpaid",
                "extra": {
                    "title": "Разработка Python бота для Telegram",
                    "pitch": "short truncated pitch...",
                    "price": 5000,
                    "mode": "postpaid",
                    "result": {
                        "success": True,
                        "task_id": 1003,
                        "price": 5000,
                        "CreatorInfo": {
                            "phone": "PRIVATE_PHONE_SECRET_3",
                            "email": "PRIVATE_EMAIL_SECRET_3@example.com",
                            "avatar": "https://img.youdo.com/private_avatar_3.jpg",
                        },
                        "profiles": [
                            {"secret": "PRIVATE_PROFILE_SECRET_4", "name": "Secret User"}
                        ],
                        "response": {
                            "ResultObject": {
                                "Offer": {
                                    "Id": 88001,
                                    "Description": "full unclipped pitch for python bot from response",
                                    "Price": 5000,
                                }
                            }
                        },
                    },
                },
            },
            "1004": {
                "seen_at": 1712345700.0,
                "status": "offered:postpaid",
                "extra": {
                    "title": "Неподтвержденный автоотклик",
                    "pitch": "pitch without confirmed API result",
                    "price": 3000,
                    "mode": "postpaid",
                    "result": {},
                },
            },
        },
        "seen_notifications": {},
        "seen_chat_messages": {},
    }


@pytest.fixture
def outbox(tmp_path):
    return Outbox(tmp_path / "outbox.sqlite3")


def test_legacy_import_keeps_source_unchanged(tmp_path, legacy_state, outbox):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(legacy_state), encoding="utf-8")
    before = hashlib.sha256(p.read_bytes()).hexdigest()
    res1 = import_state(p, outbox)
    assert hashlib.sha256(p.read_bytes()).hexdigest() == before
    assert res1["inserted"] > 0
    res2 = import_state(p, outbox)
    assert res2["inserted"] == 0
    assert hashlib.sha256(p.read_bytes()).hexdigest() == before


def test_private_profile_data_not_leaked(tmp_path, legacy_state, outbox):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(legacy_state), encoding="utf-8")

    import_state(p, outbox)

    # Inspect all enqueued events in SQLite outbox
    conn = outbox._get_conn()
    cur = conn.execute("SELECT payload FROM events;")
    all_payloads = [row["payload"] for row in cur.fetchall()]

    assert len(all_payloads) > 0
    full_dump = " ".join(all_payloads)

    assert "PRIVATE_PHONE_SECRET" not in full_dump
    assert "PRIVATE_EMAIL_SECRET" not in full_dump
    assert "PRIVATE_PROFILE_SECRET" not in full_dump
    assert "private_avatar" not in full_dump
    assert "CreatorInfo" not in full_dump
    assert "UserProfile" not in full_dump


def test_pitch_priority_full_description(tmp_path, legacy_state, outbox):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(legacy_state), encoding="utf-8")

    import_state(p, outbox)

    conn = outbox._get_conn()
    cur = conn.execute(
        "SELECT payload FROM events WHERE task_id = 1003 AND kind = 'offer_attempt';"
    )
    row = cur.fetchone()
    assert row is not None
    payload = json.loads(row["payload"])

    # Full pitch from ResultObject.Offer.Description takes precedence over short pitch
    assert payload["pitch"] == "full unclipped pitch for python bot from response"
    assert payload["pitch"] != "short truncated pitch..."


def test_pitch_fallback_to_extra_pitch_when_no_full_description(tmp_path, outbox):
    state = {
        "seen_tasks": {
            "2001": {
                "seen_at": 1712345678.0,
                "status": "offered:postpaid",
                "extra": {
                    "title": "Заказ без полного отклика",
                    "pitch": "fallback pitch text",
                    "price": 4000,
                    "result": {"success": True},
                },
            }
        }
    }
    p = tmp_path / "state.json"
    p.write_text(json.dumps(state), encoding="utf-8")

    import_state(p, outbox)

    conn = outbox._get_conn()
    cur = conn.execute(
        "SELECT payload FROM events WHERE task_id = 2001 AND kind = 'offer_attempt';"
    )
    row = cur.fetchone()
    assert row is not None
    payload = json.loads(row["payload"])
    assert payload["pitch"] == "fallback pitch text"


def test_unconfirmed_offer_not_marked_as_api_success(tmp_path, legacy_state, outbox):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(legacy_state), encoding="utf-8")

    import_state(p, outbox)

    conn = outbox._get_conn()
    cur = conn.execute(
        "SELECT payload FROM events WHERE task_id = 1004 AND kind = 'offer_result';"
    )
    row = cur.fetchone()
    # Task 1004 had status 'offered:postpaid' but empty result ({});
    # it must NOT have an API-confirmed offer_result event
    assert row is None


def test_corrupted_input_raises_explicit_error(tmp_path, outbox):
    # Corrupt JSON syntax
    p_corrupt = tmp_path / "corrupt.json"
    p_corrupt.write_text("{ this is not valid json", encoding="utf-8")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        import_state(p_corrupt, outbox)

    # Missing file
    p_missing = tmp_path / "nonexistent.json"
    with pytest.raises((FileNotFoundError, ValueError)):
        import_state(p_missing, outbox)

    # JSON that is not an object/dict
    p_array = tmp_path / "array.json"
    p_array.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        import_state(p_array, outbox)


def test_timestamp_utc_conversion(tmp_path, legacy_state, outbox):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(legacy_state), encoding="utf-8")

    import_state(p, outbox)

    conn = outbox._get_conn()
    cur = conn.execute("SELECT observed_at FROM events WHERE task_id = 1001;")
    row = cur.fetchone()
    assert row is not None
    observed_at_str = row["observed_at"]

    # Must be valid ISO 8601 with UTC timezone (+00:00 or Z)
    dt = datetime.fromisoformat(observed_at_str)
    assert dt.tzinfo is not None
    offset = dt.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0
    expected = datetime.fromtimestamp(1712345678.0, tz=timezone.utc).isoformat()
    assert observed_at_str == expected


def test_stable_attempt_identity(tmp_path, legacy_state, outbox):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(legacy_state), encoding="utf-8")

    import_state(p, outbox)

    conn = outbox._get_conn()
    # Task 1003 has offer_id 88001
    cur1003 = conn.execute(
        "SELECT identity FROM events WHERE task_id = 1003 AND kind = 'offer_attempt';"
    )
    row1003 = cur1003.fetchone()
    assert row1003 is not None
    assert row1003["identity"] == "legacy:1003:88001"

    # Task 1004 has no offer_id -> seen_at is used
    cur1004 = conn.execute(
        "SELECT identity FROM events WHERE task_id = 1004 AND kind = 'offer_attempt';"
    )
    row1004 = cur1004.fetchone()
    assert row1004 is not None
    assert row1004["identity"] == "legacy:1004:1712345700.0"


def test_return_shape_and_deduplication(tmp_path, legacy_state, outbox):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(legacy_state), encoding="utf-8")

    res1 = import_state(p, outbox)
    assert set(res1.keys()) == {
        "tasks_scanned",
        "events_generated",
        "inserted",
        "skipped_duplicates",
    }
    assert res1["tasks_scanned"] == 4
    assert res1["events_generated"] > 0
    assert res1["inserted"] == res1["events_generated"]
    assert res1["skipped_duplicates"] == 0

    # Second run should skip all duplicates
    res2 = import_state(p, outbox)
    assert res2["tasks_scanned"] == 4
    assert res2["events_generated"] == res1["events_generated"]
    assert res2["inserted"] == 0
    assert res2["skipped_duplicates"] == res1["events_generated"]


def test_all_events_have_legacy_source_and_partial_completeness(tmp_path, legacy_state, outbox):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(legacy_state), encoding="utf-8")

    import_state(p, outbox)

    conn = outbox._get_conn()
    cur = conn.execute("SELECT kind, source, completeness, payload FROM events;")
    rows = cur.fetchall()

    assert len(rows) > 0
    for r in rows:
        assert r["source"] == "legacy_state"
        assert r["completeness"] == "partial"
        if r["kind"] == "task_observed":
            payload = json.loads(r["payload"])
            assert payload["description"] is None
            assert payload["budget"] is None


def test_classification_events_for_matched_and_rejected(tmp_path, legacy_state, outbox):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(legacy_state), encoding="utf-8")

    import_state(p, outbox)

    conn = outbox._get_conn()

    # Task 1001 was rejected:price_too_low
    cur1001 = conn.execute(
        "SELECT payload FROM events WHERE task_id = 1001 AND kind = 'classification';"
    )
    row1001 = cur1001.fetchone()
    assert row1001 is not None
    p1001 = json.loads(row1001["payload"])
    assert p1001["passed"] is False
    assert p1001["reason"] == "price_too_low"

    # Task 1002 was matched
    cur1002 = conn.execute(
        "SELECT payload FROM events WHERE task_id = 1002 AND kind = 'classification';"
    )
    row1002 = cur1002.fetchone()
    assert row1002 is not None
    p1002 = json.loads(row1002["payload"])
    assert p1002["passed"] is True
    assert p1002["reason"] == "matched"


def test_import_state_with_string_path_and_empty_states(tmp_path, outbox):
    # Empty seen_tasks
    p1 = tmp_path / "empty1.json"
    p1.write_text(json.dumps({"seen_tasks": {}}), encoding="utf-8")
    res1 = import_state(str(p1), outbox)
    assert res1 == {
        "tasks_scanned": 0,
        "events_generated": 0,
        "inserted": 0,
        "skipped_duplicates": 0,
    }

    # Missing seen_tasks key
    p2 = tmp_path / "empty2.json"
    p2.write_text(json.dumps({}), encoding="utf-8")
    res2 = import_state(str(p2), outbox)
    assert res2 == {
        "tasks_scanned": 0,
        "events_generated": 0,
        "inserted": 0,
        "skipped_duplicates": 0,
    }


def test_failed_offer_result_recording(tmp_path, outbox):
    state = {
        "seen_tasks": {
            "3001": {
                "seen_at": 1712345678.0,
                "status": "offered:postpaid",
                "extra": {
                    "title": "Заказ с ошибкой отправки",
                    "pitch": "pitch for failed task",
                    "price": 2500,
                    "result": {
                        "success": False,
                        "status_code": 403,
                        "error": "Payment required",
                        "response": {"Message": "Payment required"},
                    },
                },
            }
        }
    }
    p = tmp_path / "failed.json"
    p.write_text(json.dumps(state), encoding="utf-8")

    res = import_state(p, outbox)
    assert res["tasks_scanned"] == 1
    assert res["events_generated"] == 3
    assert res["inserted"] == 3

    conn = outbox._get_conn()
    cur = conn.execute(
        "SELECT payload FROM events WHERE task_id = 3001 AND kind = 'offer_result';"
    )
    row = cur.fetchone()
    assert row is not None
    payload = json.loads(row["payload"])
    assert payload["success"] is False
    assert payload["status"] == "failed"
    assert payload["status_code"] == 403
    assert payload["message"] == "Payment required"


def test_malformed_task_entries_handled_gracefully(tmp_path, outbox):
    state = {
        "seen_tasks": {
            "not_an_int": {"seen_at": 1712345678.0, "status": "matched"},
            "4001": "not a dictionary",
            "4002": {
                # missing seen_at, missing status, missing extra
            },
        }
    }
    p = tmp_path / "malformed.json"
    p.write_text(json.dumps(state), encoding="utf-8")

    res = import_state(p, outbox)
    assert res["tasks_scanned"] == 1  # only 4002 is valid task dict
    assert res["events_generated"] == 1  # task_observed only
    assert res["inserted"] == 1


def test_triple_import_idempotency_and_event_count(tmp_path, legacy_state, outbox):
    p = tmp_path / "state.json"
    p.write_text(json.dumps(legacy_state), encoding="utf-8")
    sha_orig = hashlib.sha256(p.read_bytes()).hexdigest()

    r1 = import_state(p, outbox)
    r2 = import_state(p, outbox)
    r3 = import_state(p, outbox)

    assert r1["inserted"] > 0
    assert r2["inserted"] == 0
    assert r3["inserted"] == 0
    assert r2["skipped_duplicates"] == r1["events_generated"]
    assert r3["skipped_duplicates"] == r1["events_generated"]

    # Source file remains pristine
    assert hashlib.sha256(p.read_bytes()).hexdigest() == sha_orig

    # Total events in outbox matches initial inserted count
    conn = outbox._get_conn()
    cur = conn.execute("SELECT COUNT(*) AS total FROM events;")
    assert cur.fetchone()["total"] == r1["inserted"]

