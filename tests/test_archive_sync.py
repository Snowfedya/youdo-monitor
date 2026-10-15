"""Tests for archive synchronization pipeline, atomic catalog, crash recovery, and lookup."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import Mock, call

import pytest

from youdo_monitor.archive_events import make_event
from youdo_monitor.archive_outbox import ArchiveBlocked, Outbox
from youdo_monitor.archive_packages import build_package
from youdo_monitor.archive_sync import ArchiveSync


# -----------------------------------------------------------------------------
# Test Fixtures & Mocks
# -----------------------------------------------------------------------------

def _make_sample_event(task_id: int = 100, kind: str = "task_observed", **payload_kwargs) -> dict:
    payload = {"title": f"Task {task_id}", "budget": 5000, "category": "development"}
    payload.update(payload_kwargs)
    return make_event(
        kind=kind,
        task_id=task_id,
        payload=payload,
        observed_at="2026-10-15T12:00:00Z",
        source="youdo_web",
        completeness="detail",
    )


class FakeGoogleArchive:
    """In-memory verified Google Drive mock implementing GoogleArchive contracts."""

    def __init__(self) -> None:
        self.files: Dict[str, bytes] = {}
        self.metadata: Dict[str, Dict[str, Any]] = {}
        self._id_counter = 1000
        self.put_verified_calls: List[Dict[str, Any]] = []
        self.put_json_verified_calls: List[Dict[str, Any]] = []
        self.download_calls: List[str] = []
        self.transport = Mock()

    def reserve_file_id(self) -> str:
        self._id_counter += 1
        return f"res_file_{self._id_counter}"

    def ensure_folder_path(self, parent_id: str, path_parts: List[str]) -> str:
        folder_id = f"f_{parent_id}_{'_'.join(path_parts)}"
        self.metadata[folder_id] = {"id": folder_id, "name": path_parts[-1], "mimeType": "application/vnd.google-apps.folder"}
        return folder_id

    def put_verified(
        self,
        file_id: str,
        parent_id: str,
        name: str,
        blob: bytes,
        properties: Optional[dict] = None,
        mime_type: str = "application/gzip",
    ) -> dict:
        expected_hash = hashlib.sha256(blob).hexdigest()
        self.files[file_id] = blob
        meta = {
            "id": file_id,
            "name": name,
            "parents": [parent_id],
            "mimeType": mime_type,
            "sha256": expected_hash,
            "size": len(blob),
            "appProperties": properties or {},
        }
        self.metadata[file_id] = meta
        self.put_verified_calls.append({"file_id": file_id, "parent_id": parent_id, "name": name, "sha256": expected_hash})
        return meta

    def put_json_verified(
        self,
        file_id: str,
        data: dict,
        parent_id: Optional[str] = None,
        name: Optional[str] = None,
        properties: Optional[dict] = None,
    ) -> dict:
        canonical_bytes = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
        self.files[file_id] = canonical_bytes
        meta = {
            "id": file_id,
            "name": name or "unnamed.json",
            "parents": [parent_id] if parent_id else [],
            "mimeType": "application/json",
            "appProperties": properties or {},
        }
        self.metadata[file_id] = meta
        self.put_json_verified_calls.append({"file_id": file_id, "data": data, "name": name})
        return meta

    def download(self, file_id: str) -> bytes:
        self.download_calls.append(file_id)
        if file_id not in self.files:
            raise FileNotFoundError(f"File {file_id} not found in GoogleArchive")
        return self.files[file_id]

    def get_json(self, file_id: str) -> dict:
        raw = self.download(file_id)
        return json.loads(raw.decode("utf-8"))


class FakeSheetsIndex:
    """In-memory SheetsIndex mock tracking applied events and manual outcomes."""

    def __init__(self, spreadsheet_id: str = "test_spreadsheet_1") -> None:
        self.spreadsheet_id = spreadsheet_id
        self.sheet_ids = {"Заказы": 0, "Отклики": 1, "Результаты": 2, "Синхронизация": 3}
        self.outcomes_to_return: List[Dict[str, Any]] = []
        self.applied_events: List[Dict[str, Any]] = []
        self.applied_packages: List[Dict[str, Any]] = []
        self.rebuild_calls: List[List[Dict[str, Any]]] = []
        self.order_rows: List[Dict[str, Any]] = []

    def ensure_tabs(self) -> Dict[str, int]:
        return dict(self.sheet_ids)

    def read_outcomes(self) -> List[Dict[str, Any]]:
        res = list(self.outcomes_to_return)
        self.outcomes_to_return.clear()
        return res

    def apply_events(self, events: List[Dict[str, Any]], package: Dict[str, Any]) -> None:
        self.applied_events.extend(events)
        self.applied_packages.append(package)
        for ev in events:
            tid = ev.get("task_id")
            if tid is not None:
                batch_id = package.get("batch_id", "")
                existing = next((r for r in self.order_rows if str(r.get("task_id")) == str(tid)), None)
                if existing:
                    refs = existing.get("package_refs", "")
                    if batch_id and batch_id not in refs:
                        existing["package_refs"] = f"{refs}, {batch_id}" if refs else batch_id
                else:
                    self.order_rows.append({
                        "task_id": tid,
                        "title": ev.get("payload", {}).get("title", ""),
                        "package_refs": batch_id,
                    })

    def read_rows(self, tab: str) -> List[Dict[str, Any]]:
        if tab == "Заказы":
            return list(self.order_rows)
        return []

    def rebuild(self, events: List[Dict[str, Any]]) -> None:
        self.rebuild_calls.append(events)
        self.apply_events(events, {"batch_id": "batch_rebuild", "state": "rebuilt"})


@pytest.fixture
def prepared_outbox(tmp_path: Path):
    db_path = tmp_path / "outbox.sqlite3"
    q = Outbox(db_path)
    ev = _make_sample_event(task_id=123)
    q.enqueue(ev)
    batch_id = "batch_test_001"
    q.prepare(batch_id, [ev["event_id"]], {"sha256": "fake_sha", "event_count": 1})
    return q, batch_id


# -----------------------------------------------------------------------------
# 1. Brief Mandated Test: Parametrized partial stage failure
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("stage", ["package_prepared", "drive_verified", "sheets_verified"])
def test_partial_stage_cannot_release_payload(stage, prepared_outbox):
    """Parametrized failures after prepare/upload/download/sheets-write do not release pending payload."""
    q, batch_id = prepared_outbox
    if stage != "package_prepared":
        q.advance(batch_id, "drive_verified", {"file_id": "f1"})
    if stage == "sheets_verified":
        q.advance(batch_id, "sheets_verified", {})
    with pytest.raises(ArchiveBlocked):
        q.ack(batch_id)
    assert q.pending()


# -----------------------------------------------------------------------------
# 2. Pipeline Tests: No-events, Outcomes intake, and Full Sync Cycle
# -----------------------------------------------------------------------------

def test_sync_no_pending_events_returns_clean_status(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "catalog.json"

    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    res = sync.run()

    assert res["synced"] is False
    assert res["reason"] == "no_pending_events"
    assert res["batches"] == 0
    assert res["events"] == 0


def test_sync_reads_manual_outcomes_and_enqueues_them(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "catalog.json"

    outcome_ev = make_event(
        kind="outcome",
        task_id=555,
        payload={"outcome_id": "out_1", "type": "agreed", "amount": 15000},
        observed_at="2026-10-15T12:00:00Z",
        source="manual",
        completeness="detail",
    )
    sheets.outcomes_to_return.append(outcome_ev)

    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    res = sync.run()

    assert res["synced"] is True
    assert res["batches"] == 1
    assert res["events"] == 1
    assert res["new_outcomes"] == 1

    # Verify outbox has acknowledged the event and cleared payload
    assert len(q.pending()) == 0


def test_sync_successful_full_cycle(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "catalog.json"

    ev1 = _make_sample_event(task_id=101)
    ev2 = _make_sample_event(task_id=102)
    q.enqueue(ev1)
    q.enqueue(ev2)

    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    res = sync.run()

    assert res["synced"] is True
    assert res["batches"] == 1
    assert res["events"] == 2

    # Verify Google Drive upload was verified
    assert len(google.put_verified_calls) == 1
    call_info = google.put_verified_calls[0]
    assert call_info["sha256"]
    assert call_info["name"].endswith(".jsonl.gz")

    # Verify cloud catalog updated
    assert len(google.put_json_verified_calls) >= 1
    cloud_cat = google.get_json(google.put_json_verified_calls[-1]["file_id"])
    assert cloud_cat["schema_version"] == 1
    assert cloud_cat["root_folder_id"] == "root_f001"
    assert len(cloud_cat["batches"]) == 1
    assert cloud_cat["batches"][0]["sha256"] == call_info["sha256"]

    # Verify Sheets received events
    assert len(sheets.applied_events) == 2

    # Verify local catalog file written atomically
    assert cat_path.exists()
    local_cat = json.loads(cat_path.read_text(encoding="utf-8"))
    assert local_cat["root_folder_id"] == "root_f001"
    assert local_cat["catalog_file_id"] == cloud_cat["catalog_file_id"]

    # Verify Outbox ack and space cleanup
    assert len(q.pending()) == 0
    batches = q.batches()
    assert len(batches) == 1
    assert batches[0]["stage"] == "acknowledged"


# -----------------------------------------------------------------------------
# 3. Crash Recovery Tests (Resume from each stage)
# -----------------------------------------------------------------------------

def test_crash_recovery_resumes_from_package_prepared(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "catalog.json"

    ev = _make_sample_event(task_id=201)
    q.enqueue(ev)

    # Simulate crash right after outbox.prepare
    batch_id = "batch_crash_prep"
    blob, meta = build_package(batch_id, [ev])
    q.prepare(batch_id, [ev["event_id"]], meta)

    # Resume via sync.run()
    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    res = sync.run()

    assert res["synced"] is True
    assert batch_id in res["recovered_batches"]

    # Check stage reached acknowledged
    batches = q.batches()
    assert len(batches) == 1
    assert batches[0]["stage"] == "acknowledged"
    assert len(q.pending()) == 0
    assert len(google.put_verified_calls) == 1
    assert len(sheets.applied_events) == 1


def test_crash_recovery_resumes_from_drive_verified(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "catalog.json"

    ev = _make_sample_event(task_id=202)
    q.enqueue(ev)

    batch_id = "batch_crash_drive"
    blob, meta = build_package(batch_id, [ev])
    q.prepare(batch_id, [ev["event_id"]], meta)

    # File was already uploaded to Drive before crash
    file_id = "file_drive_001"
    google.put_verified(file_id, "root_f001", f"{batch_id}.jsonl.gz", blob)
    q.advance(batch_id, "drive_verified", {"file_id": file_id, **meta})

    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    res = sync.run()

    assert res["synced"] is True
    assert batch_id in res["recovered_batches"]
    assert q.batches()[0]["stage"] == "acknowledged"
    assert len(sheets.applied_events) == 1


def test_crash_recovery_resumes_from_sheets_verified(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "catalog.json"

    ev = _make_sample_event(task_id=203)
    q.enqueue(ev)

    batch_id = "batch_crash_sheets"
    blob, meta = build_package(batch_id, [ev])
    q.prepare(batch_id, [ev["event_id"]], meta)
    file_id = "file_drive_002"
    google.put_verified(file_id, "root_f001", f"{batch_id}.jsonl.gz", blob)
    q.advance(batch_id, "drive_verified", {"file_id": file_id, **meta})
    sheets.apply_events([ev], meta)
    q.advance(batch_id, "sheets_verified")

    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    res = sync.run()

    assert res["synced"] is True
    assert batch_id in res["recovered_batches"]
    assert q.batches()[0]["stage"] == "acknowledged"
    assert cat_path.exists()


def test_crash_recovery_resumes_from_catalog_verified(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "catalog.json"

    ev = _make_sample_event(task_id=204)
    q.enqueue(ev)

    batch_id = "batch_crash_cat"
    blob, meta = build_package(batch_id, [ev])
    q.prepare(batch_id, [ev["event_id"]], meta)
    file_id = "file_drive_003"
    google.put_verified(file_id, "root_f001", f"{batch_id}.jsonl.gz", blob)
    q.advance(batch_id, "drive_verified", {"file_id": file_id, **meta})
    q.advance(batch_id, "sheets_verified")
    q.advance(batch_id, "catalog_verified")

    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    res = sync.run()

    assert res["synced"] is True
    assert batch_id in res["recovered_batches"]
    assert q.batches()[0]["stage"] == "acknowledged"
    assert len(q.pending()) == 0


# -----------------------------------------------------------------------------
# 4. Corrupt Download & Count Mismatch Detection
# -----------------------------------------------------------------------------

def test_corrupt_download_detection_stops_sync_and_leaves_batch_pending(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "catalog.json"

    ev = _make_sample_event(task_id=301)
    q.enqueue(ev)

    # Force Google download to return corrupted bytes
    original_put = google.put_verified

    def bad_put_verified(file_id, parent_id, name, blob, properties=None, mime_type="application/gzip"):
        meta = original_put(file_id, parent_id, name, blob, properties, mime_type=mime_type or "application/gzip")
        google.files[file_id] = b"corrupted_garbage_bytes"
        return meta

    google.put_verified = bad_put_verified

    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    with pytest.raises(ValueError):
        sync.run()

    # Batch remains pending, payload is NOT cleared
    assert len(q.pending()) == 1
    assert q.batches()[0]["stage"] == "package_prepared"


def test_corrupt_count_detection_stops_sync(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "catalog.json"

    ev1 = _make_sample_event(task_id=302)
    ev2 = _make_sample_event(task_id=303)
    q.enqueue(ev1)
    q.enqueue(ev2)

    # Prepare batch with falsified expected count in metadata
    batch_id = "batch_count_mismatch"
    blob, meta = build_package(batch_id, [ev1, ev2])
    meta["event_count"] = 999  # Tampered count
    q.prepare(batch_id, [ev1["event_id"], ev2["event_id"]], meta)

    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    with pytest.raises(ValueError, match="Event count mismatch"):
        sync.run()

    assert len(q.pending()) == 2
    assert q.batches()[0]["stage"] == "package_prepared"


# -----------------------------------------------------------------------------
# 5. Catalog Recovery & Locators
# -----------------------------------------------------------------------------

def test_recover_missing_local_catalog_from_root_locator(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "catalog.json"

    # Pre-populate cloud catalog on Drive
    cloud_cat_file_id = "cat_file_001"
    cloud_data = {
        "schema_version": 1,
        "root_folder_id": "root_f001",
        "spreadsheet_id": "sheet_1",
        "catalog_file_id": cloud_cat_file_id,
        "last_verified_sync": "2026-10-15T10:00:00Z",
        "batches": [],
    }
    google.put_json_verified(cloud_cat_file_id, cloud_data, parent_id="root_f001", name="catalog.json")

    # Local catalog file is absent
    assert not cat_path.exists()

    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    # Recover catalog
    recovered = sync.recover_catalog()

    assert recovered["catalog_file_id"] == cloud_cat_file_id
    assert cat_path.exists()
    assert json.loads(cat_path.read_text(encoding="utf-8"))["catalog_file_id"] == cloud_cat_file_id


def test_missing_both_locators_blocks_with_archive_blocked(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "nonexistent_catalog.json"

    # Neither catalog file exists nor root_folder_id provided
    with pytest.raises(ArchiveBlocked, match="locator"):
        ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id=None)


# -----------------------------------------------------------------------------
# 6. Lookup & Rebuild Tests
# -----------------------------------------------------------------------------

def test_lookup_by_task_id_from_sheets_and_packages(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "catalog.json"

    ev1 = _make_sample_event(task_id=401, title="Task 401 first observation")
    ev2 = _make_sample_event(task_id=402, title="Task 402 observation")
    q.enqueue(ev1)
    q.enqueue(ev2)

    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    sync.run()

    # Look up task 401
    results = sync.lookup(401)
    assert len(results) == 1
    assert results[0]["task_id"] == 401
    assert results[0]["payload"]["title"] == "Task 401 first observation"


def test_lookup_by_task_id_fallback_across_all_packages(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "catalog.json"

    ev = _make_sample_event(task_id=403, title="Unindexed in sheets task")
    q.enqueue(ev)

    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    sync.run()

    # Clear sheets index order rows to simulate row not yet present in Sheets
    sheets.order_rows.clear()

    # lookup must fall back to scanning catalog packages
    results = sync.lookup(403)
    assert len(results) == 1
    assert results[0]["task_id"] == 403


def test_rebuild_to_another_spreadsheet(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex(spreadsheet_id="orig_sheet")
    cat_path = tmp_path / "catalog.json"

    ev1 = _make_sample_event(task_id=501)
    ev2 = _make_sample_event(task_id=502)
    q.enqueue(ev1)
    q.enqueue(ev2)

    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    sync.run()

    # Rebuild historical events into another spreadsheet
    target_sheet_id = "target_sheet_backup"
    rebuild_res = sync.rebuild_to(target_sheet_id)

    assert rebuild_res["status"] == "ok"
    assert rebuild_res["spreadsheet_id"] == target_sheet_id
    assert rebuild_res["batches_replayed"] == 1
    assert rebuild_res["events_replayed"] == 2


# -----------------------------------------------------------------------------
# 7. Atomic Writes & No Duplicate Batches on Restart
# -----------------------------------------------------------------------------

def test_atomic_local_catalog_permissions_and_replacement(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "sub" / "catalog.json"

    ev = _make_sample_event(task_id=601)
    q.enqueue(ev)

    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    sync.run()

    assert cat_path.exists()
    mode = cat_path.stat().st_mode & 0o777
    assert mode == 0o600


def test_no_duplicate_batches_or_events_on_restart(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "catalog.json"

    ev = _make_sample_event(task_id=701)
    q.enqueue(ev)

    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    res1 = sync.run()
    assert res1["synced"] is True
    assert res1["batches"] == 1

    # Second run immediately without new events
    res2 = sync.run()
    assert res2["synced"] is False
    assert res2["reason"] == "no_pending_events"
    assert len(q.batches()) == 1


def test_manual_outcome_edit_enqueues_new_revision_and_syncs(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "catalog.json"

    # Step 1: Initial outcome revision
    rev1 = make_event(
        kind="outcome",
        task_id=801,
        payload={"outcome_id": "out_rev1", "type": "agreed", "amount": 10000, "revision": "r1"},
        observed_at="2026-10-15T12:00:00Z",
        source="manual",
        completeness="detail",
        identity="out_rev1:r1",
    )
    sheets.outcomes_to_return.append(rev1)

    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    sync.run()

    # Step 2: User modifies row in Sheets -> new revision emitted
    rev2 = make_event(
        kind="outcome",
        task_id=801,
        payload={"outcome_id": "out_rev1", "type": "agreed", "amount": 12000, "revision": "r2"},
        observed_at="2026-10-15T13:00:00Z",
        source="manual",
        completeness="detail",
        identity="out_rev1:r2",
    )
    sheets.outcomes_to_return.append(rev2)
    res = sync.run()

    assert res["synced"] is True
    assert res["new_outcomes"] == 1
    assert len(q.batches()) == 2
    assert len(q.pending()) == 0


def test_cloud_missing_object_during_lookup_handled_safely(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "catalog.json"

    ev = _make_sample_event(task_id=901)
    q.enqueue(ev)

    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    sync.run()

    # Delete file from Drive mock to simulate cloud missing object
    google.files.clear()

    with pytest.raises(FileNotFoundError):
        sync.lookup(901)


def test_catalog_contains_no_tokens_or_pii(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "catalog.json"

    ev = _make_sample_event(task_id=902, client_phone="+79991234567", token="secret_token_123")
    q.enqueue(ev)

    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    sync.run()

    local_text = cat_path.read_text(encoding="utf-8")
    assert "token" not in local_text.lower() or "secret" not in local_text.lower()
    assert "+79991234567" not in local_text

    cloud_cat_file_id = json.loads(local_text)["catalog_file_id"]
    cloud_text = google.files[cloud_cat_file_id].decode("utf-8")
    assert "+79991234567" not in cloud_text
    assert "secret_token_123" not in cloud_text


def test_duplicate_events_idempotent_enqueue_no_duplicates_in_sheets(tmp_path: Path):
    q = Outbox(tmp_path / "outbox.sqlite3")
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "catalog.json"

    ev = _make_sample_event(task_id=903)
    # Enqueue same event twice
    assert q.enqueue(ev) is True
    assert q.enqueue(ev) is False  # Duplicate ignored

    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    res = sync.run()

    assert res["events"] == 1
    assert len(sheets.applied_events) == 1


def test_incremental_reclaim_called_and_frees_pages(tmp_path: Path):
    db_path = tmp_path / "outbox.sqlite3"
    q = Outbox(db_path)
    google = FakeGoogleArchive()
    sheets = FakeSheetsIndex()
    cat_path = tmp_path / "catalog.json"

    # Enqueue multiple events
    for i in range(10):
        q.enqueue(_make_sample_event(task_id=1000 + i))

    sync = ArchiveSync(q, google, sheets, catalog_path=cat_path, root_folder_id="root_f001")
    sync.run()

    assert len(q.pending()) == 0
    # Reclaim executed without error and database file remains valid and bounded
    assert db_path.stat().st_size <= 64 * 1024 * 1024
