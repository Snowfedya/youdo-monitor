"""Tests for pseudonymized experience dataset export and direct contact redaction."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
import stat
import pytest

from youdo_monitor.archive_export import (
    redact_text,
    get_or_create_export_key,
    pseudonymize_id,
    export_events,
    PRIVACY_NOTICE,
)


def test_known_direct_contacts_are_removed():
    """Scrubs emails, phones in Russian formats, URLs, and auth tokens while preserving Cyrillic text."""
    s = redact_text(
        "email demo@example.invalid; tel +7 999 000-11-22; https://example.invalid/contact"
    )
    assert "demo@example.invalid" not in s
    assert "000-11-22" not in s
    assert "https://" not in s
    assert "[REDACTED_EMAIL]" in s
    assert "[REDACTED_PHONE]" in s
    assert "[REDACTED_URL]" in s

    # Cyrillic preservation and various Russian phone formats
    cyrillic_sample = (
        "Требуется автоворонка Bitrix24. Позвоните 8 (916) 123-45-67 или 8-800-555-35-35. "
        "Почта: support@startup.ru. Сайт: www.my-company.ru"
    )
    redacted = redact_text(cyrillic_sample)
    assert "Требуется автоворонка Bitrix24." in redacted
    assert "123-45-67" not in redacted
    assert "555-35-35" not in redacted
    assert "support@startup.ru" not in redacted
    assert "www.my-company.ru" not in redacted
    assert "[REDACTED_PHONE]" in redacted
    assert "[REDACTED_EMAIL]" in redacted
    assert "[REDACTED_URL]" in redacted

    # Token and API key scrubbing
    token_sample = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-ID and api_key: sk-proj-1234567890abcdef"
    redacted_token = redact_text(token_sample)
    assert "eyJhbGci" not in redacted_token
    assert "sk-proj-1234567890abcdef" not in redacted_token
    assert "[REDACTED_TOKEN]" in redacted_token


def test_hmac_is_stable_and_links_events_for_same_task():
    """HMAC pseudonymization is stable, links events belonging to same task, and isolates namespaces."""
    key = b"k" * 32
    raw_task_id = 15274405

    # Determinism
    id1 = pseudonymize_id(raw_task_id, "task", key)
    id2 = pseudonymize_id(raw_task_id, "task", key)
    assert id1 == id2
    assert id1.startswith("ps_task_")
    assert len(id1) == len("ps_task_") + 16

    # Linking across events with the same task_id
    event_task = {"task_id": raw_task_id, "kind": "task_observed"}
    event_offer = {"task_id": raw_task_id, "kind": "offer_attempt", "attempt_id": "attempt_15274405_1"}

    pseudo_task_1 = pseudonymize_id(event_task["task_id"], "task", key)
    pseudo_task_2 = pseudonymize_id(event_offer["task_id"], "task", key)
    assert pseudo_task_1 == pseudo_task_2

    # Namespace isolation
    pseudo_attempt = pseudonymize_id(event_offer["attempt_id"], "attempt", key)
    assert pseudo_attempt.startswith("ps_attempt_")
    assert pseudo_attempt != pseudo_task_1

    # Key isolation
    different_key = b"z" * 32
    assert pseudonymize_id(raw_task_id, "task", different_key) != id1


def test_no_raw_ids_in_export(tmp_path: Path):
    """Raw task/attempt/offer/user IDs and profile objects are strictly excluded from output."""
    key = b"s" * 32
    raw_task_id = 15274405
    raw_attempt_id = "attempt_15274405_001"
    raw_offer_id = 9876543
    raw_user_id = 14316146

    events = [
        {
            "task_id": raw_task_id,
            "kind": "task_observed",
            "observed_at": "2026-10-15T10:00:00Z",
            "completeness": "detail",
            "user_id": raw_user_id,
            "creator": {"name": "Иван Д.", "phone": "+7 (999) 111-22-33"},
            "payload": {
                "title": "Настроить интеграцию. Почта ceo@corp.invalid",
                "description": "Звонить +7 999 888-77-66. Подробности на https://inner.corp.invalid/doc",
                "budget": "25000",
                "category": "webdevelopment",
                "url": "/t15274405",
            },
        },
        {
            "task_id": raw_task_id,
            "kind": "offer_result",
            "observed_at": "2026-10-15T10:05:00Z",
            "attempt_id": raw_attempt_id,
            "payload": {
                "offer_id": raw_offer_id,
                "price": 25000,
                "mode": "postpaid",
                "pitch": "Готов выполнить проект. Связь в тг или по тел +79997776655",
            },
        },
    ]

    out_file = tmp_path / "dataset.jsonl"
    res = export_events(events, out_file, key=key)
    assert res["events_exported"] == 2
    assert Path(res["output_path"]).exists()

    raw_text = out_file.read_text(encoding="utf-8")

    # Verify no raw IDs in entire file text (except line 0 manifest which has no event IDs)
    assert str(raw_task_id) not in raw_text
    assert str(raw_attempt_id) not in raw_text
    assert str(raw_offer_id) not in raw_text
    assert str(raw_user_id) not in raw_text
    assert "Иван Д." not in raw_text
    assert "+7 (999) 111-22-33" not in raw_text
    assert "+7 999 888-77-66" not in raw_text
    assert "ceo@corp.invalid" not in raw_text
    assert "https://inner.corp.invalid" not in raw_text
    assert "+79997776655" not in raw_text

    # Parse lines
    lines = [json.loads(line) for line in raw_text.strip().split("\n")]
    assert len(lines) == 3  # Manifest + 2 events

    ev1 = lines[1]
    assert "task_id" not in ev1
    assert "user_id" not in ev1
    assert "creator" not in ev1
    assert "url" not in ev1
    assert ev1["pseudo_task_id"] == pseudonymize_id(raw_task_id, "task", key)
    assert ev1["title"] == "Настроить интеграцию. Почта [REDACTED_EMAIL]"
    assert "[REDACTED_PHONE]" in ev1["description"]
    assert "[REDACTED_URL]" in ev1["description"]

    ev2 = lines[2]
    assert ev2["pseudo_task_id"] == pseudonymize_id(raw_task_id, "task", key)
    expected_pseudo_attempt = pseudonymize_id(raw_attempt_id, "attempt", key)
    expected_pseudo_offer = pseudonymize_id(raw_offer_id, "offer", key)
    assert ev2.get("pseudo_attempt_id") == expected_pseudo_attempt or ev2.get("attempt_id") == expected_pseudo_attempt
    assert ev2.get("pseudo_offer_id") == expected_pseudo_offer or ev2.get("offer_id") == expected_pseudo_offer
    assert "[REDACTED_PHONE]" in ev2["pitch"]


def test_key_creation_permissions_0600(tmp_path: Path):
    """Generates 32-byte key with 0600 permissions, and reads existing key reliably."""
    key_path = tmp_path / "data" / "archive_export.key"

    # 1. First invocation: creates key with 0600
    key1 = get_or_create_export_key(key_path)
    assert isinstance(key1, bytes)
    assert len(key1) == 32
    assert key_path.exists()

    file_mode = stat.S_IMODE(key_path.stat().st_mode)
    assert file_mode == 0o600, f"Expected 0600 mode, got {oct(file_mode)}"

    # 2. Second invocation: reads existing key byte-identical
    key2 = get_or_create_export_key(key_path)
    assert key1 == key2

    # 3. Corrupt/short key raises ValueError rather than silently mixing keys
    key_path.write_bytes(b"too_short")
    with pytest.raises(ValueError, match="less than 32 bytes"):
        get_or_create_export_key(key_path)


def test_manifest_header_and_privacy_notice(tmp_path: Path):
    """Line 0 manifest records schema version, event count, SHA-256, metadata and explicit privacy notice."""
    key = b"m" * 32
    events = [
        {"task_id": 1, "kind": "task_observed", "title": "First task"},
        {"task_id": 2, "kind": "task_observed", "title": "Second task"},
    ]
    out_file = tmp_path / "export.jsonl"
    meta = {"source": "unit_test", "dataset_split": "train"}

    res = export_events(events, out_file, key=key, metadata=meta)
    assert res["events_exported"] == 2
    assert res["size_bytes"] > 0
    assert len(res["sha256"]) == 64

    lines = out_file.read_text(encoding="utf-8").strip().split("\n")
    manifest = json.loads(lines[0])

    assert manifest["export_version"] == 1
    assert manifest["event_count"] == 2
    assert manifest["privacy_notice"] == PRIVACY_NOTICE
    assert "Direct identifiers and contacts redacted via allowlist and pattern scrubbing." in manifest["privacy_notice"]
    assert "Residual indirect re-identification risk in free text cannot be mathematically excluded." in manifest["privacy_notice"]
    assert "sha256" in manifest
    assert len(manifest["sha256"]) == 64
    assert manifest.get("metadata") == meta or manifest.get("source") == "unit_test"


def test_jsonl_gz_support(tmp_path: Path):
    """Supports both plain .jsonl and reproducible compressed .jsonl.gz with identical content."""
    key = b"g" * 32
    events = [
        {
            "task_id": 100,
            "kind": "task_observed",
            "observed_at": "2026-10-15T12:00:00Z",
            "title": "Task 100",
            "description": "Описание задачи",
            "category": "freelance",
            "budget": "1000",
        }
    ]

    jsonl_path = tmp_path / "dataset.jsonl"
    gz_path = tmp_path / "dataset.jsonl.gz"

    res_jsonl = export_events(events, jsonl_path, key=key)
    res_gz = export_events(events, gz_path, key=key)

    assert res_jsonl["events_exported"] == 1
    assert res_gz["events_exported"] == 1

    # Decompress gz bytes and compare to plain jsonl text
    decompressed_text = gzip.decompress(gz_path.read_bytes()).decode("utf-8")
    plain_text = jsonl_path.read_text(encoding="utf-8")

    assert decompressed_text == plain_text


def test_allowlist_preserves_structured_fields_and_sanitizes_text(tmp_path: Path):
    """Preserves classifier decisions, price, completeness, tags and drops unknown/sensitive keys."""
    key = b"t" * 32
    event = {
        "task_id": 999000,
        "kind": "classification",
        "observed_at": "2026-10-15T15:30:00Z",
        "completeness": "detail",
        "category": "copywriting",
        "budget": "5000 руб",
        "decision_passed": True,
        "decision_reason": "Подходит по фильтру: свяжитесь тел. 8 (495) 123-45-67",
        "decision_tags": ["python", "automation"],
        "price": 4500,
        "mode": "postpaid",
        "pitch": "Мой отклик: https://github.com/developer",
        "note": "Секретная заметка сотрудника: telegram @admin, email secret@box.com",
        "internal_debug_trace": "should_be_dropped",
        "auth_cookie": "secret_cookie_value",
    }
    out_file = tmp_path / "structured.jsonl"
    export_events([event], out_file, key=key)

    lines = [json.loads(line) for line in out_file.read_text(encoding="utf-8").strip().split("\n")]
    assert len(lines) == 2
    rec = lines[1]

    # Preserved structured fields
    assert rec["kind"] == "classification"
    assert rec["observed_at"] == "2026-10-15T15:30:00Z"
    assert rec["completeness"] == "detail"
    assert rec["category"] == "copywriting"
    assert rec["budget"] == "5000 руб"
    assert rec["decision_passed"] is True
    assert rec["decision_tags"] == ["python", "automation"]
    assert rec["price"] == 4500
    assert rec["mode"] == "postpaid"

    # Redacted fields
    assert "8 (495) 123-45-67" not in rec["decision_reason"]
    assert "[REDACTED_PHONE]" in rec["decision_reason"]
    assert "https://github.com/developer" not in rec["pitch"]
    assert "[REDACTED_URL]" in rec["pitch"]
    assert "secret@box.com" not in rec["note"]
    assert "[REDACTED_EMAIL]" in rec["note"]

    # Excluded fields
    assert "internal_debug_trace" not in rec
    assert "auth_cookie" not in rec


def test_invalid_inputs_raise_cleanly(tmp_path: Path):
    """Guards against invalid keys, missing paths, and malformed entity IDs."""
    key = b"v" * 32

    # Short key
    with pytest.raises(ValueError, match="at least 32 bytes"):
        export_events([{"task_id": 1}], tmp_path / "x.jsonl", key=b"short")

    # Non-bytes key
    with pytest.raises(TypeError, match="key must be bytes"):
        export_events([{"task_id": 1}], tmp_path / "x.jsonl", key="not_bytes")  # type: ignore

    # Missing output path
    with pytest.raises(ValueError, match="output_path must be specified"):
        export_events([{"task_id": 1}], key=key)

    # Empty raw_id in pseudonymize_id
    with pytest.raises(ValueError, match="raw_id cannot be None"):
        pseudonymize_id(None, "task", key)  # type: ignore

    # Non-bytes key in pseudonymize_id
    with pytest.raises(TypeError, match="key must be bytes"):
        pseudonymize_id(123, "task", "string_key")  # type: ignore

