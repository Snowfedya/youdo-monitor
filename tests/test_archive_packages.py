"""Tests for deterministic archive packaging, verification, and event partitioning."""

import gzip
import hashlib
import json
import pytest

from youdo_monitor.archive_packages import (
    build_package,
    partition_events,
    verify_package,
)


def test_package_is_reproducible():
    """Identical events and batch_id must yield bit-for-bit identical gzip bytes."""
    events = [{"event_id": "a", "task_id": 1, "kind": "task_observed"}]
    a, meta_a = build_package("b1", events)
    b, meta_b = build_package("b1", events)

    assert a == b
    assert meta_a == meta_b
    assert meta_a["batch_id"] == "b1"
    assert meta_a["event_count"] == 1
    assert meta_a["schema_version"] == 1
    assert meta_a["sha256"] == hashlib.sha256(a).hexdigest()
    assert meta_a["compressed_bytes"] == len(a)
    assert meta_a["uncompressed_bytes"] > 0

    verified = verify_package(
        a, expected_sha256=hashlib.sha256(a).hexdigest(), expected_count=1
    )
    assert verified == events


def test_package_reproducibility_with_dict_key_ordering():
    """Event dict key ordering should not affect the resulting package bytes."""
    events_1 = [
        {"kind": "task_observed", "task_id": 100, "payload": {"b": 2, "a": 1}},
        {"task_id": 200, "kind": "offer_attempt", "payload": {"y": "test", "x": 10}},
    ]
    events_2 = [
        {"payload": {"a": 1, "b": 2}, "task_id": 100, "kind": "task_observed"},
        {"kind": "offer_attempt", "payload": {"x": 10, "y": "test"}, "task_id": 200},
    ]

    pkg1, meta1 = build_package("batch_order", events_1)
    pkg2, meta2 = build_package("batch_order", events_2)

    assert pkg1 == pkg2
    assert meta1["sha256"] == meta2["sha256"]


def test_manifest_structure_and_no_clock_leak():
    """First line in uncompressed stream is manifest; mtime is 0; no clock leak."""
    events = [
        {"event_id": "ev1", "kind": "task_observed", "task_id": 42},
        {"event_id": "ev2", "kind": "offer_sent", "task_id": 42},
    ]
    blob, meta = build_package("batch_manifest_check", events)

    # Gzip mtime check: bytes 4-8 in gzip header contain mtime in little-endian
    # mtime=0 means bytes 4..7 must be 0x00, 0x00, 0x00, 0x00
    assert blob[4:8] == b"\x00\x00\x00\x00"

    uncompressed = gzip.decompress(blob).decode("utf-8")
    lines = uncompressed.rstrip("\n").split("\n")

    # Line 0 is manifest
    assert len(lines) == 3
    manifest = json.loads(lines[0])
    assert manifest == {
        "batch_id": "batch_manifest_check",
        "event_count": 2,
        "schema_version": 1,
    }
    # Manifest is NOT included in event_count
    assert manifest["event_count"] == len(events)
    # Manifest contains no timestamps / clock values
    assert "timestamp" not in manifest
    assert "created_at" not in manifest
    assert "time" not in manifest


def test_empty_events_forbidden():
    """Empty event list must raise ValueError."""
    with pytest.raises(ValueError, match="Cannot package empty events"):
        build_package("empty_batch", [])


@pytest.mark.parametrize("invalid_batch_id", ["", "   ", None, 123, []])
def test_invalid_batch_id_rejected(invalid_batch_id):
    """Invalid batch_id must raise ValueError."""
    events = [{"event_id": "1", "task_id": 10}]
    with pytest.raises(ValueError):
        build_package(invalid_batch_id, events)  # type: ignore


@pytest.mark.parametrize("invalid_events", [None, "not-a-list", [None], [{"ok": 1}, "string"]])
def test_invalid_events_type_rejected(invalid_events):
    """Non-list or list containing non-dict must raise ValueError."""
    with pytest.raises(ValueError):
        build_package("batch_invalid", invalid_events)  # type: ignore


def test_verify_package_sha256_mismatch():
    """Corrupted blob or wrong expected_sha256 must be rejected."""
    events = [{"event_id": "e1", "task_id": 1}]
    blob, meta = build_package("b_hash_check", events)

    # Wrong expected hash
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        verify_package(blob, expected_sha256="0" * 64, expected_count=1)

    # Bit-flipped corrupted blob
    corrupted_blob = bytearray(blob)
    corrupted_blob[-1] ^= 0xFF
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        verify_package(bytes(corrupted_blob), expected_sha256=meta["sha256"], expected_count=1)


def test_verify_package_count_mismatch():
    """Mismatched expected_count must be rejected."""
    events = [{"event_id": "e1", "task_id": 1}, {"event_id": "e2", "task_id": 2}]
    blob, meta = build_package("b_count_check", events)

    with pytest.raises(ValueError, match="Event count mismatch"):
        verify_package(blob, expected_sha256=meta["sha256"], expected_count=1)

    with pytest.raises(ValueError, match="Event count mismatch"):
        verify_package(blob, expected_sha256=meta["sha256"], expected_count=3)


def test_verify_package_corrupted_gzip():
    """Non-gzip or truncated gzip bytes must raise ValueError."""
    garbage = b"this is not gzip data at all"
    with pytest.raises(ValueError, match="Corrupted or invalid gzip"):
        verify_package(garbage, expected_sha256=hashlib.sha256(garbage).hexdigest(), expected_count=1)


def test_verify_package_manifest_mismatch_and_invalid_schema():
    """If manifest event_count does not match actual lines or schema != 1, reject."""
    manifest = json.dumps({"batch_id": "tampered", "event_count": 99, "schema_version": 1})
    event_line = json.dumps({"event_id": "e1", "task_id": 1})
    tampered_uncompressed = (manifest + "\n" + event_line + "\n").encode("utf-8")
    tampered_blob = gzip.compress(tampered_uncompressed, mtime=0)
    h = hashlib.sha256(tampered_blob).hexdigest()

    with pytest.raises(ValueError, match="Manifest count mismatch"):
        verify_package(tampered_blob, expected_sha256=h, expected_count=1)

    # Bad schema version
    manifest_bad_schema = json.dumps({"batch_id": "tampered", "event_count": 1, "schema_version": 999})
    bad_schema_uncompressed = (manifest_bad_schema + "\n" + event_line + "\n").encode("utf-8")
    bad_schema_blob = gzip.compress(bad_schema_uncompressed, mtime=0)
    h_bad = hashlib.sha256(bad_schema_blob).hexdigest()

    with pytest.raises(ValueError, match="Unsupported schema version"):
        verify_package(bad_schema_blob, expected_sha256=h_bad, expected_count=1)


def test_verify_package_corrupted_event_line():
    """If an event line in the package is not valid JSON, reject."""
    manifest = json.dumps({"batch_id": "b1", "event_count": 1, "schema_version": 1})
    bad_line = "{not valid json"
    uncompressed = (manifest + "\n" + bad_line + "\n").encode("utf-8")
    blob = gzip.compress(uncompressed, mtime=0)
    h = hashlib.sha256(blob).hexdigest()

    with pytest.raises(ValueError, match="Invalid event JSON on line 1"):
        verify_package(blob, expected_sha256=h, expected_count=1)


def test_unicode_and_cyrillic_content():
    """Cyrillic characters, emojis, and unicode symbols must be preserved exactly."""
    events = [
        {
            "event_id": "cyrillic-1",
            "task_id": 15286588,
            "title": "Доработать скрипт на Python 🐍 для мониторинга",
            "description": "Нужно настроить парсер, проверить логи и отправить отчёт.",
            "city": "Москва",
            "price": "5 000 руб.",
            "tags": ["разработка", "боты", "ai"],
        }
    ]
    blob, meta = build_package("batch_cyrillic", events)
    verified = verify_package(blob, expected_sha256=meta["sha256"], expected_count=1)

    assert verified == events
    assert verified[0]["title"] == "Доработать скрипт на Python 🐍 для мониторинга"
    assert verified[0]["description"] == "Нужно настроить парсер, проверить логи и отправить отчёт."

    # Verify Cyrillic is not escaped as \uXXXX in the uncompressed stream
    uncompressed_text = gzip.decompress(blob).decode("utf-8")
    assert "Доработать" in uncompressed_text
    assert "\\u" not in uncompressed_text


def test_multiline_strings_json_escaping():
    """Multiline strings with newlines must not break JSONL line-per-record invariant."""
    multiline_desc = (
        "Первая строка описания.\n"
        "Вторая строка с переводами.\r\n"
        "\t- Пункт 1: цитата \"в кавычках\"\n"
        "\t- Пункт 2: обратный слэш \\ и спецсимволы: \\n, \\r, \\t\n"
        "Третья строка."
    )
    events = [
        {"event_id": "m1", "task_id": 101, "desc": multiline_desc},
        {"event_id": "m2", "task_id": 102, "desc": "Однострочное описание"},
        {"event_id": "m3", "task_id": 103, "desc": "Ещё одно\nмногострочное"},
    ]

    blob, meta = build_package("batch_multiline", events)

    # Check uncompressed line count: 1 manifest line + 3 event lines = 4 lines
    uncompressed_text = gzip.decompress(blob).decode("utf-8")
    lines = uncompressed_text.rstrip("\n").split("\n")
    assert len(lines) == 4, f"Expected 4 lines (1 manifest + 3 events), got {len(lines)}"

    verified = verify_package(blob, expected_sha256=meta["sha256"], expected_count=3)
    assert verified == events
    assert verified[0]["desc"] == multiline_desc


def test_partition_events_basic():
    """Partitions events preserving original order, dropping zero, creating zero duplicates."""
    events = [{"event_id": f"ev_{i}", "task_id": i, "data": f"payload_{i}"} for i in range(25)]

    # Compute single event size to choose a small target_bytes that splits into multiple chunks
    single_size = len(
        json.dumps(events[0], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    target_bytes = single_size * 5  # around 5 events per partition

    partitions = partition_events(events, target_bytes=target_bytes)

    # Must have multiple partitions
    assert len(partitions) > 1

    # Flattened partitions must match original events exactly
    flattened = [ev for part in partitions for ev in part]
    assert flattened == events
    assert len(flattened) == len(events)

    # Check that each partition (except possibly oversized) does not exceed target_bytes
    for part in partitions:
        part_bytes = sum(
            len(json.dumps(ev, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            for ev in part
        )
        assert part_bytes <= target_bytes


def test_partition_events_empty():
    """Empty events list must return empty partitions list."""
    assert partition_events([]) == []


def test_partition_events_single_oversized_event():
    """Oversized event exceeding target_bytes must be placed in its own single-item list."""
    small_1 = {"event_id": "s1", "kind": "small", "val": 1}
    oversized = {"event_id": "big", "kind": "large", "blob": "X" * 5000}
    small_2 = {"event_id": "s2", "kind": "small", "val": 2}
    small_3 = {"event_id": "s3", "kind": "small", "val": 3}

    events = [small_1, oversized, small_2, small_3]

    # Target bytes small enough that oversized exceeds it, but small_2 and small_3 can fit together
    target_bytes = 200

    partitions = partition_events(events, target_bytes=target_bytes)

    # We expect:
    # partition 0: [small_1]
    # partition 1: [oversized] (alone, no truncation or error)
    # partition 2: [small_2, small_3]
    assert len(partitions) == 3
    assert partitions[0] == [small_1]
    assert partitions[1] == [oversized]
    assert partitions[2] == [small_2, small_3]

    # Preserves order and drops zero events
    flattened = [ev for part in partitions for ev in part]
    assert flattened == events


def test_partition_events_invalid_inputs():
    """Invalid arguments to partition_events must raise ValueError."""
    with pytest.raises(ValueError, match="target_bytes must be a positive integer"):
        partition_events([{"id": 1}], target_bytes=0)

    with pytest.raises(ValueError, match="target_bytes must be a positive integer"):
        partition_events([{"id": 1}], target_bytes=-100)

    with pytest.raises(ValueError):
        partition_events("not-a-list")  # type: ignore

    with pytest.raises(ValueError):
        partition_events([{"id": 1}, "not-a-dict"])  # type: ignore


def test_single_oversized_event_with_outbox_budget(tmp_path):
    """Oversized event partitioned on its own packages cleanly; Outbox guards budget and batch_id."""
    from youdo_monitor.archive_outbox import Outbox, ArchiveBlocked

    # Events: e1 is normal, e_big is large, e2 is normal
    e1 = {"schema_version": 1, "event_id": "ev_norm_1", "task_id": 1, "kind": "task_observed", "payload": {"k": "v1"}}
    e_big = {"schema_version": 1, "event_id": "ev_big_2", "task_id": 2, "kind": "task_observed", "payload": {"large": "L" * 1500}}
    e2 = {"schema_version": 1, "event_id": "ev_norm_3", "task_id": 3, "kind": "task_observed", "payload": {"k": "v2"}}

    # Small target_bytes so e_big is isolated
    partitions = partition_events([e1, e_big, e2], target_bytes=500)
    assert len(partitions) == 3
    assert partitions[0] == [e1]
    assert partitions[1] == [e_big]
    assert partitions[2] == [e2]

    # Initialize outbox with enough budget for normal operations
    outbox = Outbox(tmp_path / "outbox.sqlite3", max_bytes=100 * 1024)
    outbox.enqueue(e1)
    outbox.enqueue(e_big)
    outbox.enqueue(e2)

    # Package partition 1 (oversized)
    blob_big, meta_big = build_package("batch_big", partitions[1])
    assert meta_big["event_count"] == 1
    assert meta_big["compressed_bytes"] == len(blob_big)
    assert verify_package(blob_big, expected_sha256=meta_big["sha256"], expected_count=1) == [e_big]

    # Outbox prepares batch for oversized event
    outbox.prepare("batch_big", ["ev_big_2"], meta_big)

    # Invariant: one batch_id cannot ever be reused with different payload/events
    blob_other, meta_other = build_package("batch_big", [e1])
    assert meta_other["sha256"] != meta_big["sha256"]
    with pytest.raises(ArchiveBlocked, match="already exists"):
        outbox.prepare("batch_big", ["ev_norm_1"], meta_other)

