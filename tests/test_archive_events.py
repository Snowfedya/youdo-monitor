"""Tests for canonical archive events and safe data extraction."""
from pathlib import Path
import pytest

from youdo_monitor.archive_events import (
    make_event,
    task_payload,
    offer_payload,
    rules_fingerprint,
    task_completeness,
    get_code_revision,
    TASK_PAYLOAD_ALLOWLIST,
    OFFER_PAYLOAD_ALLOWLIST,
)


def test_content_identity_is_not_clock_identity():
    """Identical content observed at different timestamps must yield the same event_id."""
    a = make_event(
        "task_observed",
        10,
        {"description": "abc"},
        observed_at="2026-10-15T00:00:00Z",
        source="youdo",
        completeness="detail",
    )
    b = make_event(
        "task_observed",
        10,
        {"description": "abc"},
        observed_at="2026-10-15T01:00:00Z",
        source="youdo",
        completeness="detail",
    )
    assert a["event_id"] == b["event_id"]
    assert a["observed_at"] != b["observed_at"]
    assert len(a["event_id"]) == 64


def test_content_change_changes_event_id():
    """Modifying the task payload or metadata must alter the event_id."""
    base = make_event(
        "task_observed",
        10,
        {"description": "abc"},
        observed_at="2026-10-15T00:00:00Z",
        source="youdo",
        completeness="detail",
    )
    diff_payload = make_event(
        "task_observed",
        10,
        {"description": "modified"},
        observed_at="2026-10-15T00:00:00Z",
        source="youdo",
        completeness="detail",
    )
    diff_task = make_event(
        "task_observed",
        11,
        {"description": "abc"},
        observed_at="2026-10-15T00:00:00Z",
        source="youdo",
        completeness="detail",
    )
    diff_kind = make_event(
        "classification",
        10,
        {"description": "abc"},
        observed_at="2026-10-15T00:00:00Z",
        source="youdo",
        completeness="detail",
    )
    diff_completeness = make_event(
        "task_observed",
        10,
        {"description": "abc"},
        observed_at="2026-10-15T00:00:00Z",
        source="youdo",
        completeness="partial",
    )

    assert diff_payload["event_id"] != base["event_id"]
    assert diff_task["event_id"] != base["event_id"]
    assert diff_kind["event_id"] != base["event_id"]
    assert diff_completeness["event_id"] != base["event_id"]


def test_dict_key_ordering_is_canonical():
    """Dictionary key order in payload must not affect the event_id."""
    p1 = {"a": 1, "b": 2, "nested": {"x": 10, "y": 20}}
    p2 = {"b": 2, "a": 1, "nested": {"y": 20, "x": 10}}
    e1 = make_event(
        "task_observed",
        10,
        p1,
        observed_at="2026-10-15T00:00:00Z",
        source="youdo",
        completeness="detail",
    )
    e2 = make_event(
        "task_observed",
        10,
        p2,
        observed_at="2026-10-15T00:00:00Z",
        source="youdo",
        completeness="detail",
    )
    assert e1["event_id"] == e2["event_id"]


def test_event_shape_and_types():
    """Event dictionary must conform to schema version 1 specifications."""
    ev = make_event(
        "task_observed",
        15273447,
        {"title": "Test Title"},
        observed_at="2026-10-15T12:00:00Z",
        source="youdo",
        completeness="partial",
    )
    assert ev["schema_version"] == 1
    assert ev["task_id"] == 15273447
    assert isinstance(ev["task_id"], int)
    assert ev["kind"] == "task_observed"
    assert ev["observed_at"] == "2026-10-15T12:00:00Z"
    assert ev["source"] == "youdo"
    assert ev["completeness"] == "partial"
    assert ev["payload"] == {"title": "Test Title"}
    assert isinstance(ev["event_id"], str)
    assert len(ev["event_id"]) == 64


def test_identity_parameter_determines_event_id():
    """When identity is provided, it replaces payload in the content hash."""
    e1 = make_event(
        "offer_attempt",
        42,
        {"price": 1000, "note": "attempt 1"},
        observed_at="2026-10-15T00:00:00Z",
        source="youdo",
        completeness="full",
        identity="attempt:42:1",
    )
    e2 = make_event(
        "offer_attempt",
        42,
        {"price": 2000, "note": "attempt 1 modified payload"},
        observed_at="2026-10-15T01:00:00Z",
        source="youdo",
        completeness="full",
        identity="attempt:42:1",
    )
    e3 = make_event(
        "offer_attempt",
        42,
        {"price": 1000, "note": "attempt 1"},
        observed_at="2026-10-15T00:00:00Z",
        source="youdo",
        completeness="full",
        identity="attempt:42:2",
    )
    assert e1["event_id"] == e2["event_id"]
    assert e1["event_id"] != e3["event_id"]
    assert e1["identity"] == "attempt:42:1"


def test_response_profiles_are_not_archived():
    """Offer payload must extract offer_id and scrub all creator profile data."""
    safe = offer_payload(
        {
            "success": True,
            "response": {
                "ResultObject": {
                    "Offer": {
                        "Id": 2,
                        "CreatorInfo": {
                            "Phone": "SECRET_PHONE",
                            "Email": "secret@example.com",
                            "UserName": "secret_user",
                        },
                    }
                }
            },
        }
    )
    assert safe["offer_id"] == 2
    assert "SECRET_PHONE" not in str(safe)
    assert "secret@example.com" not in str(safe)
    assert "secret_user" not in str(safe)
    assert "CreatorInfo" not in safe
    assert safe["success"] is True


def test_offer_payload_allowlist_and_sanitization():
    """offer_payload returns only allowlisted fields with sanitized errors."""
    raw_error = {
        "success": False,
        "task_id": 100,
        "price": 1500,
        "error": "Payment card declined",
        "status_code": 402,
        "internal_debug": "Bearer secret_token_xyz",
        "response": {
            "Message": "Declined",
            "ResultObject": {"Profiles": [{"id": 1, "card": "1234"}]},
        },
    }
    safe = offer_payload(raw_error)
    assert set(safe.keys()) == OFFER_PAYLOAD_ALLOWLIST
    assert safe["success"] is False
    assert safe["task_id"] == 100
    assert safe["price"] == 1500
    assert safe["status_code"] == 402
    assert safe["message"] == "Payment card declined"
    assert safe["offer_id"] is None
    assert "secret_token_xyz" not in str(safe)
    assert "Profiles" not in safe
    assert "card" not in safe


def test_task_payload_allowlist_excludes_profiles_and_pii():
    """task_payload must strictly enforce allowlist and exclude creator/profile PII."""
    summary = {
        "id": 15273447,
        "name": "Наладить работу сайта на wordpress",
        "budget": "2 000 руб.",
        "category": "web_dev",
        "url": "/t15273447",
        "is_b2b": False,
        "creator_name": "Иван ПИИ",
        "creator_reviews": 12,
        "CreatorInfo": {
            "Phone": "+79991234567",
            "Email": "ivan@example.com",
        },
    }
    detail = {
        "description": "Проверить работу сайта на наличие ошибок.",
        "offers_count": 18,
        "is_in_progress": False,
        "status_flag": 1,
        "is_virtual": True,
        "is_b2b": False,
        "created_date": "2026-10-15T10:00:00",
        "subcategory_name": "WordPress",
        "user_profile": {"secret": "hidden_info"},
    }

    safe = task_payload(summary, detail)
    assert set(safe.keys()) == TASK_PAYLOAD_ALLOWLIST
    assert safe["title"] == "Наладить работу сайта на wordpress"
    assert safe["description"] == "Проверить работу сайта на наличие ошибок."
    assert safe["budget"] == "2 000 руб."
    assert safe["budget_source"] == "feed"
    assert safe["category"] == "web_dev"
    assert safe["url"] == "/t15273447"
    assert safe["offers_count"] == 18
    assert safe["is_in_progress"] is False
    assert safe["status_flag"] == 1
    assert safe["is_virtual"] is True
    assert safe["is_b2b"] is False
    assert safe["created_date"] == "2026-10-15T10:00:00"
    assert safe["subcategory_name"] == "WordPress"

    # Strict PII exclusions
    assert "Иван ПИИ" not in str(safe)
    assert "+79991234567" not in str(safe)
    assert "ivan@example.com" not in str(safe)
    assert "creator_name" not in safe
    assert "CreatorInfo" not in safe
    assert "user_profile" not in safe
    assert "hidden_info" not in str(safe)


def test_missing_detail_and_api_error_do_not_produce_complete_tz():
    """Missing detail or API error must yield null description and not complete status."""
    summary = {
        "id": 10,
        "name": "Заказ без деталей",
        "budget": "1 000 руб.",
    }

    # 1. Detail is None
    payload_none = task_payload(summary, None)
    assert payload_none["description"] is None
    assert payload_none["offers_count"] is None
    assert payload_none["is_in_progress"] is None
    assert task_completeness(None) == "partial"

    # 2. Detail has error
    err_detail = {"id": 10, "error": "Gateway timeout 504"}
    payload_err = task_payload(summary, err_detail)
    assert payload_err["description"] is None
    assert payload_err["offers_count"] is None
    assert task_completeness(err_detail) == "partial"

    # 3. Successful detail yields "detail"
    ok_detail = {"description": "Полноценное ТЗ", "offers_count": 5}
    payload_ok = task_payload(summary, ok_detail)
    assert payload_ok["description"] == "Полноценное ТЗ"
    assert payload_ok["offers_count"] == 5
    assert task_completeness(ok_detail) == "detail"


def test_explicit_nulls_for_missing_fields():
    """All allowlist keys must exist explicitly with None when missing."""
    sparse = task_payload({}, None)
    assert set(sparse.keys()) == TASK_PAYLOAD_ALLOWLIST
    for k in TASK_PAYLOAD_ALLOWLIST:
        assert sparse[k] is None


def test_long_description_is_not_truncated():
    """Full task descriptions must never be truncated in payload."""
    long_desc = "Детальное техническое задание. " * 5000  # ~160KB
    payload = task_payload({"name": "Большой проект"}, {"description": long_desc})
    assert payload["description"] == long_desc
    assert len(payload["description"]) == len(long_desc)


def test_rules_fingerprint_deterministic_and_sorted(tmp_path: Path):
    """rules_fingerprint must produce identical sha256 regardless of input order."""
    f1 = tmp_path / "rules_a.py"
    f2 = tmp_path / "rules_b.py"
    f1.write_text("RULE_1 = 'alpha'\n", encoding="utf-8")
    f2.write_text("RULE_2 = 'beta'\n", encoding="utf-8")

    fp1 = rules_fingerprint([f1, f2])
    fp2 = rules_fingerprint([f2, f1])
    assert fp1 == fp2
    assert len(fp1) == 64

    # Modifying content must change fingerprint
    f2.write_text("RULE_2 = 'gamma'\n", encoding="utf-8")
    fp3 = rules_fingerprint([f1, f2])
    assert fp3 != fp1


def test_code_revision_retrieval():
    """get_code_revision returns a 40-char commit hash or 'unknown'."""
    rev = get_code_revision()
    assert isinstance(rev, str)
    assert len(rev) == 40 or rev == "unknown"


def test_nan_in_payload_raises_value_error():
    """allow_nan=False must prevent invalid NaN serialization in event_id hash."""
    with pytest.raises(ValueError):
        make_event(
            "task_observed",
            1,
            {"invalid_metric": float("nan")},
            observed_at="2026-10-15T00:00:00Z",
            source="youdo",
            completeness="partial",
        )


def test_rules_fingerprint_includes_filters_and_config():
    """Fingerprint works against real codebase filter and config modules."""
    base_dir = Path(__file__).resolve().parent.parent / "youdo_monitor"
    filters_path = base_dir / "filters.py"
    config_path = base_dir / "config.py"
    assert filters_path.exists()
    assert config_path.exists()

    fp = rules_fingerprint([filters_path, config_path])
    assert isinstance(fp, str)
    assert len(fp) == 64

