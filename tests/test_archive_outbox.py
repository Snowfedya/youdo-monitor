"""Tests for bounded durable archive outbox, lifecycle stages, budget enforcement, and offer attempt guards."""
import os
import stat
import sqlite3
import pytest
from pathlib import Path
from youdo_monitor.archive_events import make_event
from youdo_monitor.archive_outbox import Outbox, ArchiveBlocked, STAGES


def _sample_event(event_id: str = "e1", task_id: int = 1, payload: dict | None = None) -> dict:
    return {
        "schema_version": 1,
        "event_id": event_id,
        "task_id": task_id,
        "kind": "task_observed",
        "observed_at": "2026-10-15T00:00:00Z",
        "source": "test",
        "completeness": "partial",
        "payload": payload if payload is not None else {"title": f"Task {task_id}"},
    }


def test_no_ack_before_cloud_verification(tmp_path: Path):
    """ack() must raise ArchiveBlocked before catalog_verified stage is reached."""
    q = Outbox(tmp_path / "out.sqlite3")
    q.enqueue(_sample_event("e1", 1, {}))
    q.prepare("b1", ["e1"], {})
    with pytest.raises(ArchiveBlocked):
        q.ack("b1")
    assert len(q.pending()) == 1


def test_monotonic_stages_and_successful_ack(tmp_path: Path):
    """Batches must progress monotonically through STAGES until acknowledged."""
    q = Outbox(tmp_path / "out.sqlite3")
    q.enqueue(_sample_event("e1", 1))

    q.prepare("b1", ["e1"], {"desc": "initial batch"})
    batches = q.batches()
    assert len(batches) == 1
    assert batches[0]["batch_id"] == "b1"
    assert batches[0]["stage"] == "package_prepared"
    assert batches[0]["event_ids"] == ["e1"]
    assert batches[0]["metadata"]["desc"] == "initial batch"

    # Non-monotonic jump: cannot skip drive_verified to jump to sheets_verified
    with pytest.raises(ArchiveBlocked):
        q.advance("b1", "sheets_verified")

    # Non-monotonic reverse: cannot advance to a stage backwards or same stage
    with pytest.raises(ArchiveBlocked):
        q.advance("b1", "package_prepared")

    # Step 1: advance to drive_verified
    q.advance("b1", "drive_verified", {"drive_id": "file_123"})
    assert q.batches()[0]["stage"] == "drive_verified"
    assert q.batches()[0]["metadata"]["drive_id"] == "file_123"

    # Step 2: advance to sheets_verified
    q.advance("b1", "sheets_verified", {"sheet_row": 5})
    assert q.batches()[0]["stage"] == "sheets_verified"

    # Step 3: advance to catalog_verified
    q.advance("b1", "catalog_verified", {"catalog_rev": 1})
    assert q.batches()[0]["stage"] == "catalog_verified"

    # Now ack succeeds
    q.ack("b1")
    assert q.batches()[0]["stage"] == "acknowledged"
    # Acknowledged events are no longer in pending()
    assert len(q.pending()) == 0

    # Cannot ack an already acknowledged batch
    with pytest.raises(ArchiveBlocked):
        q.ack("b1")


def test_deduplication_and_persistence_across_reopen(tmp_path: Path):
    """Events persist across close/reopen and duplicate event_id is ignored idempotently."""
    db_path = tmp_path / "outbox.sqlite3"
    q1 = Outbox(db_path)
    ev = _sample_event("e1", 10)

    # First enqueue succeeds
    assert q1.enqueue(ev) is True
    # Duplicate enqueue returns False (deduped)
    assert q1.enqueue(ev) is False
    assert len(q1.pending()) == 1

    q1.close()

    # Reopen in a new Outbox instance
    q2 = Outbox(db_path)
    assert len(q2.pending()) == 1
    assert q2.pending()[0]["event_id"] == "e1"
    assert q2.pending()[0]["task_id"] == 10
    # Duplicate still ignored after reopen
    assert q2.enqueue(ev) is False
    assert len(q2.pending()) == 1


def test_unresolved_attempts_and_retry_blocking(tmp_path: Path):
    """Recorded attempt durably blocks auto-offer until resolution; unknown keeps blocking."""
    db_path = tmp_path / "outbox.sqlite3"
    q1 = Outbox(db_path)

    assert q1.has_unresolved_attempt(42) is False

    # Durable offer attempt recorded BEFORE API call
    q1.record_attempt(42, "attempt-42-1", {"price": 500, "bid": "hello"})
    assert q1.has_unresolved_attempt(42) is True

    # Survives restart / process interruption
    q1.close()
    q2 = Outbox(db_path)
    assert q2.has_unresolved_attempt(42) is True

    # Duplicate attempt_id is rejected
    with pytest.raises(ArchiveBlocked):
        q2.record_attempt(42, "attempt-42-1")

    # Ambiguous/unknown result -> durable block against retry must be preserved
    q2.resolve_attempt("attempt-42-1", status="unknown", result={"error": "ReadTimeout"})
    assert q2.has_unresolved_attempt(42) is True

    # Definite failure or success on a different task
    q2.record_attempt(99, "attempt-99-1", {"price": 1000})
    assert q2.has_unresolved_attempt(99) is True
    q2.resolve_attempt("attempt-99-1", status="success", result={"offer_id": 12345})
    assert q2.has_unresolved_attempt(99) is False


def test_unknown_attempt_survives_payload_cleanup(tmp_path: Path):
    """Active unknown attempts do not disappear when acknowledged events have payloads cleaned."""
    q = Outbox(tmp_path / "out.sqlite3")
    q.record_attempt(55, "att-55", {"price": 700})
    q.resolve_attempt("att-55", status="unknown")
    assert q.has_unresolved_attempt(55) is True

    # Enqueue, prepare, verify and ack an event batch
    q.enqueue(_sample_event("e55", 55))
    q.prepare("b55", ["e55"], {})
    for stg in ("drive_verified", "sheets_verified", "catalog_verified"):
        q.advance("b55", stg)
    q.ack("b55")

    # Payloads are cleaned, but unresolved attempt remains active
    assert len(q.pending()) == 0
    assert q.has_unresolved_attempt(55) is True


def test_storage_budget_usage_and_75_percent_warning(tmp_path: Path):
    """Usage tracking calculates ratio, 75% warning flag, and full status."""
    # 100 KiB budget (sufficient to hold SQLite metadata + several events)
    budget = 100 * 1024
    q = Outbox(tmp_path / "budget.sqlite3", max_bytes=budget)

    u0 = q.usage()
    assert u0["max_bytes"] == budget
    assert u0["total_bytes"] > 0  # schema exists
    assert u0["warning"] is False
    assert u0["full"] is False

    # Insert events with moderate payload to cross 75%
    # Schema is ~56 KB, 75% of 100 KB is 76.8 KB
    ev_num = 0
    while not q.usage()["warning"]:
        ev_num += 1
        ev = _sample_event(f"ev_{ev_num}", ev_num, {"blob": "a" * 1000})
        q.enqueue(ev)

    u_final = q.usage()
    assert u_final["warning"] is True
    assert u_final["usage_ratio"] >= 0.75
    assert u_final["total_bytes"] <= budget


def test_oversized_event_rejected_without_truncation(tmp_path: Path):
    """Events exceeding the storage limit are rejected without partial writes or truncation."""
    budget = 100 * 1024
    q = Outbox(tmp_path / "budget.sqlite3", max_bytes=budget)

    # Event payload larger than entire budget
    huge_payload = {"text": "z" * (150 * 1024)}
    huge_event = _sample_event("huge_1", 999, huge_payload)

    with pytest.raises(ArchiveBlocked, match="budget|oversized"):
        q.enqueue(huge_event)

    # Queue remains clean and uncorrupted
    assert len(q.pending()) == 0


def test_storage_budget_exceeded_blocks_enqueue_and_attempt(tmp_path: Path):
    """Exceeding max_bytes strictly raises ArchiveBlocked for enqueue and record_attempt."""
    budget = 90 * 1024
    q = Outbox(tmp_path / "capped.sqlite3", max_bytes=budget)

    # Enqueue events until full
    idx = 0
    hit_block = False
    for idx in range(100):
        try:
            q.enqueue(_sample_event(f"e_{idx}", idx, {"data": "x" * 2048}))
        except ArchiveBlocked:
            hit_block = True
            break

    assert hit_block is True
    # Further enqueue raises ArchiveBlocked
    with pytest.raises(ArchiveBlocked):
        q.enqueue(_sample_event("overflow", 9999))

    # Recording attempt also blocked when budget exhausted
    with pytest.raises(ArchiveBlocked):
        q.record_attempt(8888, "att-overflow")

    # Disk usage never exceeds max_bytes
    assert q.usage()["total_bytes"] <= budget


def test_strict_file_permissions(tmp_path: Path):
    """Outbox sqlite file has 0600, parent dir 0700, and staging dir 0700."""
    sub_dir = tmp_path / "secure_vault"
    db_file = sub_dir / "vault.sqlite3"
    q = Outbox(db_file)

    assert stat.S_IMODE(sub_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(q.staging_dir.stat().st_mode) == 0o700


def test_foreign_keys_and_batch_validation(tmp_path: Path):
    """Foreign keys are enforced; prepare requires valid unacknowledged events."""
    q = Outbox(tmp_path / "fk.sqlite3")

    # Cannot prepare non-existent event
    with pytest.raises(ArchiveBlocked):
        q.prepare("b_ghost", ["non_existent_event"], {})

    # Cannot prepare empty event list
    with pytest.raises(ArchiveBlocked):
        q.prepare("b_empty", [], {})

    # Enqueue valid event
    q.enqueue(_sample_event("e1", 1))
    q.prepare("b1", ["e1"], {})

    # Cannot prepare same batch_id twice
    with pytest.raises(ArchiveBlocked):
        q.prepare("b1", ["e1"], {})

    # Cannot prepare an event that is already in another active batch
    with pytest.raises(ArchiveBlocked):
        q.prepare("b2", ["e1"], {})


def test_retry_after_process_interruption(tmp_path: Path):
    """Crash recovery across intermediate lifecycle stages."""
    db_path = tmp_path / "interrupted.sqlite3"
    q1 = Outbox(db_path)
    q1.enqueue(_sample_event("e1", 1))
    q1.enqueue(_sample_event("e2", 2))
    q1.prepare("b1", ["e1", "e2"], {"step": 0})
    q1.advance("b1", "drive_verified", {"drive_id": "drv_001"})
    q1.close()

    # Process restarted: resumes at drive_verified
    q2 = Outbox(db_path)
    batches = q2.batches()
    assert len(batches) == 1
    assert batches[0]["stage"] == "drive_verified"
    assert batches[0]["event_ids"] == ["e1", "e2"]

    # Continue progression to sheets_verified -> catalog_verified -> acknowledged
    q2.advance("b1", "sheets_verified")
    q2.advance("b1", "catalog_verified")
    q2.ack("b1")

    assert q2.batches()[0]["stage"] == "acknowledged"
    assert len(q2.pending()) == 0


def test_peak_usage_bounded_under_limit(tmp_path: Path):
    """Peak usage during transactions never exceeds max_bytes."""
    budget = 110 * 1024
    q = Outbox(tmp_path / "peak.sqlite3", max_bytes=budget)

    # Monitor disk size at each step
    for i in range(50):
        try:
            q.enqueue(_sample_event(f"e_peak_{i}", i, {"chunk": "p" * 1500}))
        except ArchiveBlocked:
            break
        # Total bytes must never violate budget
        assert q.total_bytes() <= budget

    assert q.total_bytes() <= budget


def test_crash_after_api_success_prevents_retry_in_next_run(tmp_path: Path):
    """If external API succeeds but process crashes before resolve_attempt, next run refuses retry."""
    db_path = tmp_path / "crash_recovery.sqlite3"
    q1 = Outbox(db_path)

    # 1. Step before dispatch: durable attempt is recorded in SQLite
    q1.record_attempt(task_id=77, attempt_id="att_77_auto", metadata={"price": 1500})
    assert q1.has_unresolved_attempt(77) is True

    # 2. Simulated API success happens, but process is abruptly killed before resolve_attempt()
    q1.close()

    # 3. Next process run starts
    q2 = Outbox(db_path)
    # The guard MUST still block any repeated auto-offer!
    assert q2.has_unresolved_attempt(77) is True

    # Once operator or resolution logic resolves it
    q2.resolve_attempt("att_77_auto", status="success", result={"offer_id": 999111})
    assert q2.has_unresolved_attempt(77) is False


def test_staging_directory_included_in_total_bytes_and_budget(tmp_path: Path):
    """Files placed in the staging directory are counted in total_bytes and trigger budget blocks."""
    budget = 100 * 1024
    q = Outbox(tmp_path / "staging_test.sqlite3", max_bytes=budget)

    initial_bytes = q.total_bytes()
    staging_file = q.staging_dir / "package_b1.tar.gz"
    # Write 30 KiB to staging
    staging_file.write_bytes(b"S" * (30 * 1024))

    assert q.total_bytes() >= initial_bytes + (30 * 1024)
    assert q.usage()["total_bytes"] == q.total_bytes()

    # If staging file consumes the rest of the budget, enqueue is blocked
    huge_stage = q.staging_dir / "huge_stage.bin"
    huge_stage.write_bytes(b"H" * (70 * 1024))

    assert q.usage()["full"] is True
    with pytest.raises(ArchiveBlocked):
        q.enqueue(_sample_event("e_stage_blocked", 123))


def test_disk_error_surfaces_as_archive_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Low-level SQLite OperationalErrors surface as ArchiveBlocked."""
    q = Outbox(tmp_path / "error_test.sqlite3")

    class FaultyConn:
        def execute(self, *args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(q, "_get_conn", lambda: FaultyConn())

    with pytest.raises(ArchiveBlocked, match="disk I/O error|error"):
        q.enqueue(_sample_event("e_err", 1))


def test_prepare_rejects_acknowledged_event(tmp_path: Path):
    """Acknowledged events cannot be prepared into a new batch."""
    q = Outbox(tmp_path / "repack.sqlite3")
    q.enqueue(_sample_event("e1", 1))
    q.prepare("b1", ["e1"], {})
    for stg in ("drive_verified", "sheets_verified", "catalog_verified"):
        q.advance("b1", stg)
    q.ack("b1")

    # Trying to prepare the already acknowledged event again
    with pytest.raises(ArchiveBlocked, match="already acknowledged"):
        q.prepare("b2", ["e1"], {})
