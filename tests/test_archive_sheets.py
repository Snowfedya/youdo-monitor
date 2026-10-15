"""Tests for Google Sheets keyed index projections, formula safety, and manual outcomes intake."""
import hashlib
import json
from unittest.mock import Mock, call
import pytest

from youdo_monitor.archive_sheets import SheetsIndex


def test_manual_fields_are_not_owned():
    g = Mock()
    s = SheetsIndex(g, "sheet-1")
    assert "примечание" not in s.ORDER_MACHINE_COLUMNS
    assert "task_id" in s.ORDER_MACHINE_COLUMNS
    assert "outcome" in s.ORDER_MACHINE_COLUMNS
    assert "package_refs" in s.ORDER_MACHINE_COLUMNS


def test_tab_and_column_constants():
    g = Mock()
    s = SheetsIndex(g, "sheet-1")
    assert s.TABS == ("Заказы", "Отклики", "Результаты", "Синхронизация")
    assert s.ORDER_MACHINE_COLUMNS == [
        "task_id",
        "title",
        "budget",
        "budget_source",
        "category",
        "url",
        "first_seen",
        "last_seen",
        "completeness",
        "verdict",
        "reason",
        "tags",
        "rules_fingerprint",
        "observation_id",
        "outcome",
        "package_refs",
    ]
    assert s.OFFER_MACHINE_COLUMNS == [
        "attempt_id",
        "task_id",
        "observed_at",
        "price",
        "mode",
        "status",
        "offer_id",
        "batch_id",
    ]
    assert s.SYNC_MACHINE_COLUMNS == [
        "batch_id",
        "period",
        "drive_file_id",
        "sha256",
        "event_count",
        "state",
        "verified_at",
        "error",
    ]
    assert s.OUTCOME_USER_COLUMNS == [
        "type",
        "task_id",
        "time",
        "amount",
        "currency",
        "source",
        "note",
    ]
    assert s.OUTCOME_MACHINE_COLUMNS == [
        "outcome_id",
        "revision",
        "status_or_error",
    ]


def test_ensure_tabs_creates_missing_tabs_and_headers():
    transport = Mock()
    # Metadata returns only Sheet1 (default blank sheet)
    transport.get_json.return_value = {
        "sheets": [
            {"properties": {"sheetId": 0, "title": "Sheet1"}}
        ]
    }
    transport.post.return_value = {
        "replies": [
            {"addSheet": {"properties": {"sheetId": 101, "title": "Заказы"}}},
            {"addSheet": {"properties": {"sheetId": 102, "title": "Отклики"}}},
            {"addSheet": {"properties": {"sheetId": 103, "title": "Результаты"}}},
            {"addSheet": {"properties": {"sheetId": 104, "title": "Синхронизация"}}},
        ]
    }

    s = SheetsIndex(transport, "sheet-123")
    tab_map = s.ensure_tabs()

    assert "Заказы" in tab_map
    assert "Отклики" in tab_map
    assert "Результаты" in tab_map
    assert "Синхронизация" in tab_map

    # Verified metadata GET
    transport.get_json.assert_called_once_with(
        "https://sheets.googleapis.com/v4/spreadsheets/sheet-123",
        params={"fields": "sheets(properties(sheetId,title))"},
    )

    # Verified AddSheet requests via batchUpdate
    post_calls = transport.post.call_args_list
    assert len(post_calls) >= 2  # 1 for addSheet, 1 for batchUpdate headers
    add_sheet_call = post_calls[0]
    assert add_sheet_call.args[0] == "https://sheets.googleapis.com/v4/spreadsheets/sheet-123:batchUpdate"
    reqs = add_sheet_call.kwargs["json"]["requests"]
    assert len(reqs) == 4
    assert reqs[0]["addSheet"]["properties"]["title"] == "Заказы"

    # Header initialization with RAW valueInputOption
    header_call = post_calls[1]
    assert header_call.args[0] == "https://sheets.googleapis.com/v4/spreadsheets/sheet-123/values:batchUpdate"
    assert header_call.kwargs["json"]["valueInputOption"] == "RAW"
    batch_data = header_call.kwargs["json"]["data"]
    # 4 tabs should have header ranges
    tab_ranges = [d["range"] for d in batch_data]
    assert any("Заказы" in r for r in tab_ranges)
    assert any("Отклики" in r for r in tab_ranges)
    assert any("Результаты" in r for r in tab_ranges)
    assert any("Синхронизация" in r for r in tab_ranges)


def test_read_rows_unformatted_value():
    transport = Mock()
    transport.get_json.return_value = {
        "values": [
            ["task_id", "title", "budget"],
            [101, "Сайт на WordPress", 2000],
            [102, "Telegram бот", 5000],
        ]
    }
    s = SheetsIndex(transport, "sheet-123")
    rows = s.read_rows("Заказы")

    assert len(rows) == 2
    assert rows[0] == {"task_id": 101, "title": "Сайт на WordPress", "budget": 2000}
    assert rows[1] == {"task_id": 102, "title": "Telegram бот", "budget": 5000}

    transport.get_json.assert_called_once_with(
        "https://sheets.googleapis.com/v4/spreadsheets/sheet-123/values/Заказы!A1:Z",
        params={"valueRenderOption": "UNFORMATTED_VALUE"},
    )


def test_upsert_raw_formula_safety():
    """Formulas or symbols starting with =, +, -, @ must be written with RAW valueInputOption."""
    transport = Mock()
    # Existing tab has header only
    transport.get_json.return_value = {
        "values": [
            ["task_id", "title", "budget"],
        ]
    }
    transport.post.return_value = {"updatedRows": 1}

    s = SheetsIndex(transport, "sheet-123")
    s.upsert(
        "Заказы",
        [
            {"task_id": 101, "title": "=CMD('calc')", "budget": "+79991234567"},
        ],
        key="task_id",
        owned_columns=["task_id", "title", "budget"],
    )

    transport.post.assert_called_once()
    post_args, post_kwargs = transport.post.call_args
    assert "values:batchUpdate" in post_args[0]
    payload = post_kwargs["json"]
    assert payload["valueInputOption"] == "RAW"
    assert payload["valueInputOption"] != "USER_ENTERED"
    # Content must retain the exact raw string without modification
    assert payload["data"][0]["values"][0] == [101, "=CMD('calc')", "+79991234567"]


def test_upsert_keyed_in_place_preserves_human_columns():
    """Matching row is updated only in owned columns; human columns are preserved."""
    transport = Mock()
    # Existing tab has a human column 'примечание' in column C
    transport.get_json.side_effect = [
        # read_rows in upsert
        {
            "values": [
                ["task_id", "title", "примечание", "last_seen"],
                [101, "Старый заголовок", "Человеческая заметка", "2026-10-15T10:00:00Z"],
            ]
        },
        # readback verification
        {
            "values": [
                ["task_id", "title", "примечание", "last_seen"],
                [101, "Новый заголовок", "Человеческая заметка", "2026-10-15T12:00:00Z"],
            ]
        },
    ]
    transport.post.return_value = {"totalUpdatedCells": 2}

    s = SheetsIndex(transport, "sheet-123")
    s.upsert(
        "Заказы",
        [
            {"task_id": 101, "title": "Новый заголовок", "last_seen": "2026-10-15T12:00:00Z"},
        ],
        key="task_id",
        owned_columns=["task_id", "title", "last_seen"],
    )

    transport.post.assert_called_once()
    post_payload = transport.post.call_args[1]["json"]
    assert post_payload["valueInputOption"] == "RAW"
    # Column 'примечание' is at column index 2 (C). It MUST NOT be in the update ranges.
    ranges = [d["range"] for d in post_payload["data"]]
    # The update ranges should update columns A:B (task_id, title) and D (last_seen), skipping C
    for item in post_payload["data"]:
        assert "C2" not in item["range"]  # Column C (human note) never overwritten!


def test_upsert_row_shuffling():
    """Updates must target rows by stable key, not by previous row index."""
    transport = Mock()
    # Sheet where rows were swapped/reordered by human: task 202 is row 2, task 101 is row 3
    transport.get_json.side_effect = [
        {
            "values": [
                ["task_id", "title", "last_seen"],
                [202, "Заказ 202", "2026-10-15T09:00:00Z"],
                [101, "Заказ 101 старый", "2026-10-15T08:00:00Z"],
            ]
        },
        # readback
        {
            "values": [
                ["task_id", "title", "last_seen"],
                [202, "Заказ 202", "2026-10-15T09:00:00Z"],
                [101, "Заказ 101 обновленный", "2026-10-15T12:00:00Z"],
            ]
        },
    ]
    transport.post.return_value = {"updatedRows": 1}

    s = SheetsIndex(transport, "sheet-123")
    s.upsert(
        "Заказы",
        [
            {"task_id": 101, "title": "Заказ 101 обновленный", "last_seen": "2026-10-15T12:00:00Z"},
        ],
        key="task_id",
        owned_columns=["task_id", "title", "last_seen"],
    )

    post_payload = transport.post.call_args[1]["json"]
    # Must target row 3 (where task 101 currently is!), NOT row 2
    assert any("3" in d["range"] for d in post_payload["data"])
    assert not any("2" in d["range"] for d in post_payload["data"])


def test_upsert_appends_unseen_keys_without_duplicates():
    """Unseen keys are appended at the end of the sheet; subsequent upsert updates without duplicating."""
    transport = Mock()
    transport.get_json.side_effect = [
        # Call 1: existing sheet has only headers
        {
            "values": [
                ["task_id", "title"],
            ]
        },
        # Call 1 readback: now has row 2
        {
            "values": [
                ["task_id", "title"],
                [303, "Новый заказ"],
            ]
        },
        # Call 2: sheet now has row 2
        {
            "values": [
                ["task_id", "title"],
                [303, "Новый заказ"],
            ]
        },
        # Call 2 readback
        {
            "values": [
                ["task_id", "title"],
                [303, "Обновленный заказ"],
            ]
        },
    ]
    transport.post.return_value = {"updatedRows": 1}

    s = SheetsIndex(transport, "sheet-123")
    # First call: appends at row 2
    s.upsert(
        "Заказы",
        [{"task_id": 303, "title": "Новый заказ"}],
        key="task_id",
        owned_columns=["task_id", "title"],
    )
    payload1 = transport.post.call_args_list[0][1]["json"]
    assert any("2" in d["range"] for d in payload1["data"])

    # Second call with same key: updates row 2 in place, does not append row 3
    s.upsert(
        "Заказы",
        [{"task_id": 303, "title": "Обновленный заказ"}],
        key="task_id",
        owned_columns=["task_id", "title"],
    )
    payload2 = transport.post.call_args_list[1][1]["json"]
    assert any("2" in d["range"] for d in payload2["data"])
    assert not any("3" in d["range"] for d in payload2["data"])


def test_read_outcomes_assigns_missing_outcome_id_and_writes_back():
    """Manual row missing outcome_id gets assigned out_<uuid[:12]>, revision SHA-256, and written back."""
    transport = Mock()
    transport.get_json.side_effect = [
        # First read: manual entry without outcome_id or revision
        {
            "values": [
                ["type", "task_id", "time", "amount", "currency", "source", "note", "outcome_id", "revision", "status_or_error"],
                ["agreed", 101, "2026-10-15T12:00:00Z", 5000, "RUB", "manual", "Договорились", "", "", ""],
            ]
        },
        # Readback after writing back outcome_id and revision
        {
            "values": [
                ["type", "task_id", "time", "amount", "currency", "source", "note", "outcome_id", "revision", "status_or_error"],
                ["agreed", 101, "2026-10-15T12:00:00Z", 5000, "RUB", "manual", "Договорились", "out_abc123456789", "rev_hash", "valid"],
            ]
        },
    ]
    transport.post.return_value = {"updatedRows": 1}

    s = SheetsIndex(transport, "sheet-123")
    events = s.read_outcomes()

    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "outcome"
    assert ev["task_id"] == 101
    assert ev["payload"]["type"] == "agreed"
    assert ev["payload"]["amount"] == 5000
    assert ev["payload"]["currency"] == "RUB"
    assert ev["payload"]["outcome_id"].startswith("out_")
    assert len(ev["payload"]["revision"]) == 64

    # Verify write-back was performed with RAW valueInputOption
    transport.post.assert_called_once()
    payload = transport.post.call_args[1]["json"]
    assert payload["valueInputOption"] == "RAW"
    # Ensure user fields were NOT in the write-back
    user_cols = {"agreed", "Договорились"}
    for item in payload["data"]:
        for val_list in item["values"]:
            for v in val_list:
                assert v not in user_cols


def test_read_outcomes_validation_and_error_flagging():
    """Invalid outcome rows get flagged in status_or_error and do NOT produce outcome events."""
    transport = Mock()
    transport.get_json.side_effect = [
        {
            "values": [
                ["type", "task_id", "time", "amount", "currency", "source", "note", "outcome_id", "revision", "status_or_error"],
                # Row 2: invalid type
                ["not_a_type", 101, "2026-10-15T12:00:00Z", 1000, "RUB", "manual", "", "", "", ""],
                # Row 3: non-integer task_id
                ["agreed", "not_int", "2026-10-15T12:00:00Z", 1000, "RUB", "manual", "", "", "", ""],
                # Row 4: invalid amount
                ["revenue", 103, "2026-10-15T12:00:00Z", "one thousand", "RUB", "manual", "", "", "", ""],
                # Row 5: missing time
                ["completed", 104, "", 1000, "RUB", "manual", "", "", "", ""],
            ]
        },
        # readback
        {
            "values": [
                ["type", "task_id", "time", "amount", "currency", "source", "note", "outcome_id", "revision", "status_or_error"],
                ["not_a_type", 101, "2026-10-15T12:00:00Z", 1000, "RUB", "manual", "", "out_1", "", "invalid: unsupported type 'not_a_type'"],
                ["agreed", "not_int", "2026-10-15T12:00:00Z", 1000, "RUB", "manual", "", "out_2", "", "invalid: task_id must be integer"],
                ["revenue", 103, "2026-10-15T12:00:00Z", "one thousand", "RUB", "manual", "", "out_3", "", "invalid: amount must be a number"],
                ["completed", 104, "", 1000, "RUB", "manual", "", "out_4", "", "invalid: time is required"],
            ]
        },
    ]
    transport.post.return_value = {"updatedRows": 4}

    s = SheetsIndex(transport, "sheet-123")
    events = s.read_outcomes()

    # Zero valid events returned
    assert len(events) == 0

    # Errors written back to status_or_error
    transport.post.assert_called_once()
    payload = transport.post.call_args[1]["json"]
    assert payload["valueInputOption"] == "RAW"
    written_values = [v for item in payload["data"] for row in item["values"] for v in row]
    assert any("unsupported type" in str(v) for v in written_values)
    assert any("task_id must be integer" in str(v) for v in written_values)
    assert any("amount must be a number" in str(v) for v in written_values)
    assert any("time is required" in str(v) for v in written_values)


def test_read_outcomes_duplicate_outcome_id_conflict():
    """Duplicate outcome_ids across rows trigger conflict alert in status_or_error."""
    transport = Mock()
    transport.get_json.side_effect = [
        {
            "values": [
                ["type", "task_id", "time", "amount", "currency", "source", "note", "outcome_id", "revision", "status_or_error"],
                ["agreed", 101, "2026-10-15T12:00:00Z", 1000, "RUB", "manual", "Первый", "out_dup_123", "rev1", ""],
                ["agreed", 102, "2026-10-15T12:00:00Z", 2000, "RUB", "manual", "Второй", "out_dup_123", "rev2", ""],
            ]
        },
        # readback
        {
            "values": [
                ["type", "task_id", "time", "amount", "currency", "source", "note", "outcome_id", "revision", "status_or_error"],
                ["agreed", 101, "2026-10-15T12:00:00Z", 1000, "RUB", "manual", "Первый", "out_dup_123", "rev1", "conflict: duplicate outcome_id 'out_dup_123'"],
                ["agreed", 102, "2026-10-15T12:00:00Z", 2000, "RUB", "manual", "Второй", "out_dup_123", "rev2", "conflict: duplicate outcome_id 'out_dup_123'"],
            ]
        },
    ]
    transport.post.return_value = {"updatedRows": 2}

    s = SheetsIndex(transport, "sheet-123")
    events = s.read_outcomes()

    # Neither duplicate row is treated as valid outcome event
    assert len(events) == 0

    transport.post.assert_called_once()
    payload = transport.post.call_args[1]["json"]
    written_values = [v for item in payload["data"] for row in item["values"] for v in row]
    assert any("duplicate outcome_id" in str(v) for v in written_values)


def test_read_outcomes_user_edit_produces_new_revision():
    """Editing note or amount on existing row changes the revision SHA-256."""
    transport = Mock()
    rev_initial = hashlib.sha256(
        json.dumps(
            {"amount": 5000, "currency": "RUB", "note": "Изначально", "source": "manual", "task_id": 101, "time": "2026-10-15T12:00:00Z", "type": "agreed"},
            sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()

    # User changed note to 'Договорились на 6000' and amount to 6000
    transport.get_json.side_effect = [
        {
            "values": [
                ["type", "task_id", "time", "amount", "currency", "source", "note", "outcome_id", "revision", "status_or_error"],
                ["agreed", 101, "2026-10-15T12:00:00Z", 6000, "RUB", "manual", "Договорились на 6000", "out_fixed_1", rev_initial, "valid"],
            ]
        },
        # readback
        {
            "values": [
                ["type", "task_id", "time", "amount", "currency", "source", "note", "outcome_id", "revision", "status_or_error"],
                ["agreed", 101, "2026-10-15T12:00:00Z", 6000, "RUB", "manual", "Договорились на 6000", "out_fixed_1", "new_rev", "valid"],
            ]
        },
    ]
    transport.post.return_value = {"updatedRows": 1}

    s = SheetsIndex(transport, "sheet-123")
    events = s.read_outcomes()

    assert len(events) == 1
    ev = events[0]
    assert ev["payload"]["amount"] == 6000
    assert ev["payload"]["note"] == "Договорились на 6000"
    assert ev["payload"]["revision"] != rev_initial

    # Revision was updated in the sheet
    transport.post.assert_called_once()
    payload = transport.post.call_args[1]["json"]
    written_values = [v for item in payload["data"] for row in item["values"] for v in row]
    assert ev["payload"]["revision"] in written_values


def test_apply_events_orders_and_offers_and_sync():
    """apply_events routes events to Заказы, Отклики, and Синхронизация, updating last_seen and package_refs."""
    transport = Mock()
    # Sheet state before apply: task 101 already exists in Заказы
    transport.get_json.side_effect = [
        # read_rows for Заказы in upsert
        {
            "values": [
                ["task_id", "title", "budget", "budget_source", "category", "url", "first_seen", "last_seen", "completeness", "verdict", "reason", "tags", "rules_fingerprint", "observation_id", "outcome", "package_refs"],
                [101, "Старый заголовок", "1000", "feed", "web", "/t101", "2026-10-15T08:00:00Z", "2026-10-15T08:00:00Z", "partial", "", "", "", "", "obs_old", "unknown", "batch_001"],
            ]
        },
        # readback for Заказы
        {
            "values": [
                ["task_id", "title", "budget", "budget_source", "category", "url", "first_seen", "last_seen", "completeness", "verdict", "reason", "tags", "rules_fingerprint", "observation_id", "outcome", "package_refs"],
                [101, "Новый заголовок", "1000", "feed", "web", "/t101", "2026-10-15T08:00:00Z", "2026-10-15T11:00:00Z", "detail", "passed", "Good fit", "wordpress", "fp123", "obs_new", "unknown", "batch_001, batch_002"],
            ]
        },
        # read_rows for Отклики in upsert
        {
            "values": [
                ["attempt_id", "task_id", "observed_at", "price", "mode", "status", "offer_id", "batch_id"],
            ]
        },
        # readback for Отклики
        {
            "values": [
                ["attempt_id", "task_id", "observed_at", "price", "mode", "status", "offer_id", "batch_id"],
                ["att_101", 101, "2026-10-15T11:05:00Z", 2000, "auto", "success", 9999, "batch_002"],
            ]
        },
        # read_rows for Синхронизация in upsert
        {
            "values": [
                ["batch_id", "period", "drive_file_id", "sha256", "event_count", "state", "verified_at", "error"],
            ]
        },
        # readback for Синхронизация
        {
            "values": [
                ["batch_id", "period", "drive_file_id", "sha256", "event_count", "state", "verified_at", "error"],
                ["batch_002", "2026-10-15", "file_gdrive_1", "sha_hex", 3, "verified", "2026-10-15T11:10:00Z", ""],
            ]
        },
    ]
    transport.post.return_value = {"updatedRows": 1}

    events = [
        {
            "schema_version": 1,
            "event_id": "obs_new",
            "kind": "task_observed",
            "task_id": 101,
            "observed_at": "2026-10-15T11:00:00Z",
            "source": "feed",
            "completeness": "detail",
            "payload": {
                "title": "Новый заголовок",
                "budget": "1000",
                "budget_source": "feed",
                "category": "web",
                "url": "/t101",
            },
        },
        {
            "schema_version": 1,
            "event_id": "cls_101",
            "kind": "classification",
            "task_id": 101,
            "observed_at": "2026-10-15T11:01:00Z",
            "source": "classifier",
            "completeness": "detail",
            "payload": {
                "passed": True,
                "reason": "Good fit",
                "tags": ["wordpress"],
                "rules_fingerprint": "fp123",
                "observation_id": "obs_new",
            },
        },
        {
            "schema_version": 1,
            "event_id": "off_101",
            "kind": "offer_attempt",
            "task_id": 101,
            "observed_at": "2026-10-15T11:05:00Z",
            "source": "runner",
            "completeness": "detail",
            "payload": {
                "attempt_id": "att_101",
                "price": 2000,
                "mode": "auto",
                "status": "success",
                "offer_id": 9999,
            },
        },
    ]

    package = {
        "batch_id": "batch_002",
        "period": "2026-10-15",
        "drive_file_id": "file_gdrive_1",
        "sha256": "sha_hex",
        "event_count": 3,
        "state": "verified",
        "verified_at": "2026-10-15T11:10:00Z",
        "error": "",
    }

    s = SheetsIndex(transport, "sheet-123")
    s.apply_events(events, package)

    # Verify 3 updates were performed (Заказы, Отклики, Синхронизация)
    assert transport.post.call_count == 3
    # First post is Заказы
    order_post = transport.post.call_args_list[0][1]["json"]
    assert order_post["valueInputOption"] == "RAW"
    # Verify package_refs appended 'batch_002' to 'batch_001'
    order_values = [v for item in order_post["data"] for row in item["values"] for v in row]
    assert "batch_001, batch_002" in order_values or "batch_001,batch_002" in order_values or "batch_002" in order_values
    # Verify first_seen preserved old date 2026-10-15T08:00:00Z
    assert "2026-10-15T08:00:00Z" in order_values


def test_rebuild_from_events():
    """rebuild creates tabs if missing and writes orders and offers from event stream."""
    transport = Mock()
    # ensure_tabs
    transport.get_json.side_effect = [
        # ensure_tabs metadata
        {"sheets": [{"properties": {"sheetId": 0, "title": "Заказы"}}, {"properties": {"sheetId": 1, "title": "Отклики"}}, {"properties": {"sheetId": 2, "title": "Результаты"}}, {"properties": {"sheetId": 3, "title": "Синхронизация"}}]},
        # read_rows Заказы
        {"values": [SheetsIndex.ORDER_MACHINE_COLUMNS]},
        # readback Заказы
        {"values": [SheetsIndex.ORDER_MACHINE_COLUMNS, [505, "Rebuilt task", "1000", "feed", "dev", "/t505", "2026-10-15T10:00:00Z", "2026-10-15T10:00:00Z", "detail", "passed", "Fit", "tag1", "fp1", "obs_505", "unknown", "batch_rebuild"]]},
        # read_rows Отклики
        {"values": [SheetsIndex.OFFER_MACHINE_COLUMNS]},
        # readback Отклики
        {"values": [SheetsIndex.OFFER_MACHINE_COLUMNS, ["att_505", 505, "2026-10-15T10:05:00Z", 1500, "auto", "success", 777, "batch_rebuild"]]},
    ]
    transport.post.return_value = {"updatedRows": 1}

    events = [
        {
            "schema_version": 1,
            "event_id": "obs_505",
            "kind": "task_observed",
            "task_id": 505,
            "observed_at": "2026-10-15T10:00:00Z",
            "source": "feed",
            "completeness": "detail",
            "payload": {"title": "Rebuilt task", "budget": "1000", "budget_source": "feed", "category": "dev", "url": "/t505"},
        },
        {
            "schema_version": 1,
            "event_id": "cls_505",
            "kind": "classification",
            "task_id": 505,
            "observed_at": "2026-10-15T10:01:00Z",
            "source": "classifier",
            "completeness": "detail",
            "payload": {"passed": True, "reason": "Fit", "tags": ["tag1"], "rules_fingerprint": "fp1", "observation_id": "obs_505"},
        },
        {
            "schema_version": 1,
            "event_id": "att_505",
            "kind": "offer_attempt",
            "task_id": 505,
            "observed_at": "2026-10-15T10:05:00Z",
            "source": "runner",
            "completeness": "detail",
            "payload": {"attempt_id": "att_505", "price": 1500, "mode": "auto", "status": "success", "offer_id": 777, "batch_id": "batch_rebuild"},
        },
    ]

    s = SheetsIndex(transport, "sheet-123")
    s.rebuild(events)

    assert transport.post.call_count >= 2
    # Verify RAW on all writes
    for c in transport.post.call_args_list:
        payload = c[1].get("json")
        if payload and "valueInputOption" in payload:
            assert payload["valueInputOption"] == "RAW"


def test_ensure_tabs_noop_when_all_tabs_exist():
    transport = Mock()
    transport.get_json.return_value = {
        "sheets": [
            {"properties": {"sheetId": 1, "title": "Заказы"}},
            {"properties": {"sheetId": 2, "title": "Отклики"}},
            {"properties": {"sheetId": 3, "title": "Результаты"}},
            {"properties": {"sheetId": 4, "title": "Синхронизация"}},
        ]
    }
    s = SheetsIndex(transport, "sheet-123")
    tab_map = s.ensure_tabs()
    assert len(tab_map) == 4
    transport.post.assert_not_called()


def test_last_seen_updates_without_content_change():
    """When a task is observed again with identical content, last_seen is updated in Заказы."""
    transport = Mock()
    transport.get_json.side_effect = [
        # read Заказы
        {
            "values": [
                SheetsIndex.ORDER_MACHINE_COLUMNS,
                [707, "Повторный заказ", "5000", "feed", "web", "/t707", "2026-10-15T08:00:00Z", "2026-10-15T08:00:00Z", "partial", "", "", "", "", "obs_707", "unknown", "b1"],
            ]
        },
        # readback
        {
            "values": [
                SheetsIndex.ORDER_MACHINE_COLUMNS,
                [707, "Повторный заказ", "5000", "feed", "web", "/t707", "2026-10-15T08:00:00Z", "2026-10-15T14:30:00Z", "partial", "", "", "", "", "obs_707", "unknown", "b1, b2"],
            ]
        },
        # read Синхронизация
        {"values": [SheetsIndex.SYNC_MACHINE_COLUMNS]},
        # readback Синхронизация
        {"values": [SheetsIndex.SYNC_MACHINE_COLUMNS]},
    ]
    transport.post.return_value = {"updatedRows": 1}

    # Event with identical payload but later observed_at
    events = [
        {
            "schema_version": 1,
            "event_id": "obs_707_repeat",
            "kind": "task_observed",
            "task_id": 707,
            "observed_at": "2026-10-15T14:30:00Z",
            "source": "feed",
            "completeness": "partial",
            "payload": {
                "title": "Повторный заказ",
                "budget": "5000",
                "budget_source": "feed",
                "category": "web",
                "url": "/t707",
            },
        }
    ]
    package = {"batch_id": "b2"}

    s = SheetsIndex(transport, "sheet-123")
    s.apply_events(events, package)

    # Verify write payload updated last_seen to 2026-10-15T14:30:00Z and package_refs to 'b1, b2'
    order_post = transport.post.call_args_list[0][1]["json"]
    all_written = [v for item in order_post["data"] for row in item["values"] for v in row]
    assert "2026-10-15T14:30:00Z" in all_written
    assert "2026-10-15T08:00:00Z" in all_written  # first_seen preserved
    assert "b1, b2" in all_written


def test_append_ambiguity_rereads_keys_no_duplicates():
    """If network times out after commit, retrying upsert rereads keys and does not append duplicate rows."""
    transport = Mock()
    # Scenario:
    # First attempt: reads sheet (empty), attempts write, but network throws ambiguity exception.
    # On Google server, row 2 was committed.
    # Second attempt (retry): reads sheet (finds row 2 with key 808), updates row 2 in place, NO row 3!
    transport.get_json.side_effect = [
        # Call 1 read
        {"values": [["task_id", "title"]]},
        # Call 2 read (retry): row 808 already exists
        {"values": [["task_id", "title"], [808, "Task 808 committed"]]},
        # Call 2 readback
        {"values": [["task_id", "title"], [808, "Task 808 committed"]]},
    ]
    # First post throws network ambiguity
    # Second post succeeds
    transport.post.side_effect = [
        RuntimeError("Connection reset by peer after commit"),
        {"updatedRows": 1},
    ]

    s = SheetsIndex(transport, "sheet-123")
    with pytest.raises(RuntimeError):
        s.upsert("Заказы", [{"task_id": 808, "title": "Task 808 committed"}], key="task_id", owned_columns=["task_id", "title"])

    # Retry call
    s.upsert("Заказы", [{"task_id": 808, "title": "Task 808 committed"}], key="task_id", owned_columns=["task_id", "title"])

    # Second call must update row 2, NOT append row 3
    retry_post_data = transport.post.call_args_list[1][1]["json"]["data"]
    assert any("2" in d["range"] for d in retry_post_data)
    assert not any("3" in d["range"] for d in retry_post_data)


def test_read_outcomes_float_amount_and_whitespace():
    """Amounts with comma and spacing like '2 500,50' are parsed correctly."""
    transport = Mock()
    transport.get_json.side_effect = [
        {
            "values": [
                SheetsIndex.OUTCOME_USER_COLUMNS + SheetsIndex.OUTCOME_MACHINE_COLUMNS,
                ["revenue", 909, "2026-10-15T15:00:00Z", " 2 500,50 ", "RUB", "manual", "Оплата 1", "", "", ""],
            ]
        },
        # readback
        {
            "values": [
                SheetsIndex.OUTCOME_USER_COLUMNS + SheetsIndex.OUTCOME_MACHINE_COLUMNS,
                ["revenue", 909, "2026-10-15T15:00:00Z", " 2 500,50 ", "RUB", "manual", "Оплата 1", "out_1", "rev_1", "valid"],
            ]
        },
    ]
    transport.post.return_value = {"updatedRows": 1}

    s = SheetsIndex(transport, "sheet-123")
    events = s.read_outcomes()

    assert len(events) == 1
    assert events[0]["payload"]["amount"] == 2500.5
    assert events[0]["payload"]["type"] == "revenue"


def test_read_rows_empty_sheet_returns_empty_list():
    transport = Mock()
    transport.get_json.return_value = {"values": []}
    s = SheetsIndex(transport, "sheet-123")
    assert s.read_rows("Заказы") == []

