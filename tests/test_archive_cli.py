"""Unit tests for archive_cli operations, CLI parser enforcement, and side-effect guards."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import stat
import sys
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, Mock, patch

import pytest

from youdo_monitor.archive_events import make_event
from youdo_monitor.archive_google import GooglePermissionBlocked
from youdo_monitor.archive_outbox import Outbox


class FakeGoogleArchive:
    """In-memory GoogleArchive fake for CLI testing."""

    def __init__(self) -> None:
        self.files: Dict[str, bytes] = {}
        self.metadata: Dict[str, Dict[str, Any]] = {}
        self._id_counter = 5000
        self.transport = Mock()
        self.transport.get_json = self._mock_transport_get_json
        self.transport.post = self._mock_transport_post
        self.transport.patch = self._mock_transport_patch

    def _mock_transport_get_json(self, url: str, params: Optional[dict] = None) -> dict:
        params = params or {}
        q = str(params.get("q", ""))
        matched_files = []
        for fid, meta in self.metadata.items():
            if "name = 'catalog.json'" in q:
                if meta.get("name") == "catalog.json":
                    matched_files.append({"id": fid, "name": meta.get("name")})
            elif "mimeType = 'application/vnd.google-apps.folder'" in q:
                if meta.get("mimeType") == "application/vnd.google-apps.folder":
                    matched_files.append(meta)
            elif "mimeType = 'application/vnd.google-apps.spreadsheet'" in q:
                if meta.get("mimeType") == "application/vnd.google-apps.spreadsheet":
                    matched_files.append(meta)
        return {"files": matched_files}

    def _mock_transport_post(self, url: str, json: Optional[dict] = None, **kwargs) -> dict:
        body = json or {}
        fid = body.get("id") or self.reserve_file_id()
        meta = {
            "id": fid,
            "name": body.get("name", "untitled"),
            "mimeType": body.get("mimeType", "application/octet-stream"),
            "parents": body.get("parents", []),
            "appProperties": body.get("appProperties", {}),
        }
        self.metadata[fid] = meta
        return meta

    def _mock_transport_patch(self, url: str, data: Optional[bytes] = None, **kwargs) -> dict:
        return {"status": "ok"}

    def preflight(self) -> dict:
        return {"status": "ok", "user": {"emailAddress": "test@domain.com"}, "storageQuota": {"limit": "1000"}}

    def reserve_file_id(self) -> str:
        self._id_counter += 1
        return f"file_id_{self._id_counter}"

    def assert_private_permissions(self, file_id: str) -> list[dict]:
        return [{"id": "perm_owner", "type": "user", "role": "owner"}]

    def ensure_private_root(self, archive_key: str, name: str = "YouDo Experience Archive") -> dict:
        for fid, meta in self.metadata.items():
            if meta.get("mimeType") == "application/vnd.google-apps.folder":
                props = meta.get("appProperties", {})
                if props.get("archive_key") == archive_key:
                    return meta
        fid = self.reserve_file_id()
        meta = {
            "id": fid,
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "appProperties": {"archive_key": archive_key},
        }
        self.metadata[fid] = meta
        return meta

    def ensure_private_spreadsheet(self, archive_key: str, parent_id: str, title: str = "YouDo Experience Index") -> dict:
        for fid, meta in self.metadata.items():
            if meta.get("mimeType") == "application/vnd.google-apps.spreadsheet":
                props = meta.get("appProperties", {})
                if props.get("archive_key") == archive_key and parent_id in meta.get("parents", []):
                    return meta
        fid = self.reserve_file_id()
        meta = {
            "id": fid,
            "name": title,
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "parents": [parent_id],
            "appProperties": {"archive_key": archive_key},
        }
        self.metadata[fid] = meta
        return meta

    def put_json_verified(self, file_id: str, data: dict, parent_id: Optional[str] = None, name: Optional[str] = None, properties: Optional[dict] = None) -> dict:
        raw = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")
        self.files[file_id] = raw
        meta = {
            "id": file_id,
            "name": name or "unnamed.json",
            "parents": [parent_id] if parent_id else [],
            "mimeType": "application/json",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
            "appProperties": properties or {},
        }
        self.metadata[file_id] = meta
        return meta

    def put_verified(self, file_id: str, parent_id: str, name: str, blob: bytes, properties: Optional[dict] = None, mime_type: str = "application/gzip") -> dict:
        h = hashlib.sha256(blob).hexdigest()
        self.files[file_id] = blob
        meta = {
            "id": file_id,
            "name": name,
            "parents": [parent_id],
            "mimeType": mime_type,
            "sha256": h,
            "size": len(blob),
            "appProperties": properties or {},
        }
        self.metadata[file_id] = meta
        return meta

    def download(self, file_id: str) -> bytes:
        if file_id not in self.files:
            raise FileNotFoundError(f"File {file_id} not found in FakeGoogleArchive")
        return self.files[file_id]

    def get_json(self, file_id: str) -> dict:
        raw = self.download(file_id)
        return json.loads(raw.decode("utf-8"))


class FakeSheetsIndex:
    """In-memory SheetsIndex fake for CLI testing."""

    def __init__(self, google: Any, spreadsheet_id: str) -> None:
        self.google = google
        self.spreadsheet_id = spreadsheet_id
        self.sheet_ids = {"Заказы": 0, "Отклики": 1, "Результаты": 2, "Синхронизация": 3}
        self.rows: Dict[str, List[Dict[str, Any]]] = {
            "Заказы": [],
            "Отклики": [],
            "Результаты": [],
            "Синхронизация": [],
        }
        self.rebuild_calls: List[List[Dict[str, Any]]] = []

    def ensure_tabs(self) -> Dict[str, int]:
        return dict(self.sheet_ids)

    def apply_events(self, events: List[Dict[str, Any]], package: Dict[str, Any]) -> None:
        for ev in events:
            if ev.get("kind") == "task_observed":
                self.rows["Заказы"].append({
                    "task_id": ev.get("task_id"),
                    "title": ev.get("payload", {}).get("title"),
                    "package_refs": package.get("batch_id"),
                })

    def read_outcomes(self) -> List[Dict[str, Any]]:
        return []

    def read_rows(self, tab: str) -> List[Dict[str, Any]]:
        return list(self.rows.get(tab, []))

    def rebuild(self, events: List[Dict[str, Any]]) -> None:
        self.rebuild_calls.append(events)
        self.rows["Заказы"].clear()
        for ev in events:
            if ev.get("kind") == "task_observed":
                self.rows["Заказы"].append({"task_id": ev.get("task_id"), "title": ev.get("payload", {}).get("title")})


# -----------------------------------------------------------------------------
# Parser & Safety Tests
# -----------------------------------------------------------------------------

def test_verify_has_explicit_no_side_effect_flags():
    """RED check from task brief: verify requires explicit safety flags."""
    from youdo_monitor.archive_cli import build_parser

    a = build_parser().parse_args(["verify", "--live-feed", "--no-offers", "--no-notifications"])
    assert a.no_offers and a.no_notifications and a.live_feed


def test_cli_without_subcommand_refuses():
    """Parser must reject invocation with no subcommand."""
    from youdo_monitor.archive_cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_verify_refuses_when_safety_flags_omitted():
    """Parser must refuse verify without all three required safety flags."""
    from youdo_monitor.archive_cli import build_parser

    parser = build_parser()

    # Missing all flags
    with pytest.raises(SystemExit):
        parser.parse_args(["verify"])

    # Missing no-offers
    with pytest.raises(SystemExit):
        parser.parse_args(["verify", "--live-feed", "--no-notifications"])

    # Missing no-notifications
    with pytest.raises(SystemExit):
        parser.parse_args(["verify", "--live-feed", "--no-offers"])

    # Missing live-feed
    with pytest.raises(SystemExit):
        parser.parse_args(["verify", "--no-offers", "--no-notifications"])


def test_rebuild_index_refuses_without_destination_spreadsheet_id():
    """rebuild-index must require explicit destination spreadsheet id."""
    from youdo_monitor.archive_cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["rebuild-index"])


def test_rebuild_index_refuses_production_spreadsheet(tmp_path: Path):
    """rebuild-index must reject target id matching production catalog."""
    from youdo_monitor.archive_cli import main

    cat_file = tmp_path / "catalog.json"
    cat_file.write_text(json.dumps({
        "schema_version": 1,
        "active": True,
        "root_folder_id": "root_123",
        "spreadsheet_id": "prod_sheet_456",
        "catalog_file_id": "cat_789",
    }))

    with pytest.raises(ValueError, match="production"):
        main([
            "rebuild-index",
            "--destination-spreadsheet-id", "prod_sheet_456",
            "--catalog-path", str(cat_file),
            "--outbox-path", str(tmp_path / "outbox.sqlite3"),
        ])


def test_verify_never_calls_offer_or_telegram(tmp_path: Path, monkeypatch):
    """Verify live-feed mode NEVER invokes OfferManager.submit or Telegram notifier."""
    from youdo_monitor.archive_cli import main

    cat_file = tmp_path / "archive_catalog.json"
    fake_google = FakeGoogleArchive()
    fake_sheets = FakeSheetsIndex(fake_google, "sheet_test")

    cat_file.write_text(json.dumps({
        "schema_version": 1,
        "active": True,
        "root_folder_id": "root_test",
        "spreadsheet_id": "sheet_test",
        "catalog_file_id": "cat_test",
    }))
    fake_google.files["cat_test"] = json.dumps({
        "schema_version": 1,
        "root_folder_id": "root_test",
        "spreadsheet_id": "sheet_test",
        "catalog_file_id": "cat_test",
        "batches": [],
    }).encode("utf-8")

    outbox_file = tmp_path / "archive_outbox.sqlite3"

    # Mock YouDoClient to return dummy feed and details
    mock_client = MagicMock()
    mock_client.fetch_feed.return_value = [
        {"id": 99901, "name": "Task 1", "budget": "1000", "description": "Need python dev"},
        {"id": 99902, "name": "Task 2", "budget": "2000", "description": "Need website"},
    ]
    mock_client.fetch_task_details.return_value = [
        {"id": 99901, "name": "Task 1", "description": "Full python dev details"},
        {"id": 99902, "name": "Task 2", "description": "Full website details"},
    ]

    offer_manager_cls = MagicMock()
    offer_manager_submit = MagicMock()
    offer_manager_cls.return_value.submit = offer_manager_submit

    telegram_mock = MagicMock()

    # Monkeypatch client and guards
    monkeypatch.setattr("youdo_monitor.archive_cli.get_youdo_client", lambda *a, **kw: mock_client)
    monkeypatch.setattr("youdo_monitor.archive_cli.GoogleArchive", lambda *a, **kw: fake_google)
    monkeypatch.setattr("youdo_monitor.archive_cli.SheetsIndex", lambda *a, **kw: fake_sheets)
    monkeypatch.setattr("youdo_monitor.archive_cli.ArchiveSync._create_sheets_index", lambda s, sid: fake_sheets)

    # If OfferManager or Notifier are accessed, mock them
    monkeypatch.setattr("youdo_monitor.offers.OfferManager", offer_manager_cls)
    monkeypatch.setattr("youdo_monitor.notifier.Notifier", telegram_mock)

    ret = main([
        "verify",
        "--live-feed",
        "--no-offers",
        "--no-notifications",
        "--catalog-path", str(cat_file),
        "--outbox-path", str(outbox_file),
        "--limit", "2",
    ])
    assert ret == 0

    # Strict assertion: NO offers submitted, NO telegram sent
    assert offer_manager_submit.call_count == 0
    assert telegram_mock.call_count == 0

    # Outbox should have captured observations and synced them
    assert len(fake_sheets.rows["Заказы"]) > 0


# -----------------------------------------------------------------------------
# Subcommands Operations Tests
# -----------------------------------------------------------------------------

def test_preflight_cli(monkeypatch):
    """preflight subcommand runs GoogleArchive.preflight() cleanly."""
    from youdo_monitor.archive_cli import main

    fake_google = FakeGoogleArchive()
    monkeypatch.setattr("youdo_monitor.archive_cli.GoogleArchive", lambda *a, **kw: fake_google)

    ret = main(["preflight"])
    assert ret == 0


def test_bootstrap_creates_resources_and_permissions(tmp_path: Path, monkeypatch):
    """bootstrap creates private root, spreadsheet, tabs, cloud catalog and local catalog with 0600."""
    from youdo_monitor.archive_cli import main

    fake_google = FakeGoogleArchive()
    cat_file = tmp_path / "data" / "archive_catalog.json"

    monkeypatch.setattr("youdo_monitor.archive_cli.GoogleArchive", lambda *a, **kw: fake_google)
    monkeypatch.setattr("youdo_monitor.archive_cli.SheetsIndex", lambda *a, **kw: FakeSheetsIndex(fake_google, "test_sheet"))

    ret = main([
        "bootstrap",
        "--name", "Test Experience Archive",
        "--archive-key", "test_experience_archive",
        "--catalog-path", str(cat_file),
        "--spreadsheet-title", "Test Index",
    ])
    assert ret == 0
    assert cat_file.exists()

    # Permissions check (0600)
    mode = stat.S_IMODE(cat_file.stat().st_mode)
    assert mode == 0o600

    cat_data = json.loads(cat_file.read_text(encoding="utf-8"))
    assert cat_data.get("active") is True
    assert cat_data.get("archive_key") == "test_experience_archive"
    assert cat_data.get("root_folder_id") is not None
    assert cat_data.get("spreadsheet_id") is not None
    assert cat_data.get("catalog_file_id") is not None

    # Idempotent second bootstrap should succeed and retain same handles
    ret2 = main([
        "bootstrap",
        "--name", "Test Experience Archive",
        "--archive-key", "test_experience_archive",
        "--catalog-path", str(cat_file),
    ])
    assert ret2 == 0
    cat_data2 = json.loads(cat_file.read_text(encoding="utf-8"))
    assert cat_data2["root_folder_id"] == cat_data["root_folder_id"]


def test_import_state_and_sync_cli(tmp_path: Path, monkeypatch):
    """import-state imports legacy state, sync packages it, and second run generates 0 new items."""
    from youdo_monitor.archive_cli import main

    fake_google = FakeGoogleArchive()
    fake_sheets = FakeSheetsIndex(fake_google, "sheet_test")
    monkeypatch.setattr("youdo_monitor.archive_cli.GoogleArchive", lambda *a, **kw: fake_google)
    monkeypatch.setattr("youdo_monitor.archive_cli.SheetsIndex", lambda *a, **kw: fake_sheets)
    monkeypatch.setattr("youdo_monitor.archive_cli.ArchiveSync._create_sheets_index", lambda s, sid: fake_sheets)

    cat_file = tmp_path / "archive_catalog.json"
    outbox_file = tmp_path / "archive_outbox.sqlite3"
    state_file = tmp_path / "state.json"

    # Setup bootstrap catalog
    cat_file.write_text(json.dumps({
        "schema_version": 1,
        "active": True,
        "root_folder_id": "root_test",
        "spreadsheet_id": "sheet_test",
        "catalog_file_id": "cat_test",
    }))
    fake_google.files["cat_test"] = json.dumps({
        "schema_version": 1,
        "root_folder_id": "root_test",
        "spreadsheet_id": "sheet_test",
        "catalog_file_id": "cat_test",
        "batches": [],
    }).encode("utf-8")

    # Sample legacy state
    state_file.write_text(json.dumps({
        "seen_tasks": {
            "1001": {"seen_at": 1700000000, "status": "matched", "extra": {"title": "Task 1001"}},
            "1002": {"seen_at": 1700000010, "status": "rejected:stop_word", "extra": {"title": "Task 1002"}},
        }
    }))

    # 1. First import
    ret = main(["import-state", "--path", str(state_file), "--outbox-path", str(outbox_file)])
    assert ret == 0

    outbox = Outbox(outbox_file)
    pending1 = outbox.pending()
    assert len(pending1) == 4  # 2 tasks * (observation + classification)

    # 2. Sync
    ret_sync = main(["sync", "--catalog-path", str(cat_file), "--outbox-path", str(outbox_file)])
    assert ret_sync == 0

    assert len(outbox.pending()) == 0

    # 3. Second import of exact same state: inserted must be 0
    ret_dup = main(["import-state", "--path", str(state_file), "--outbox-path", str(outbox_file)])
    assert ret_dup == 0
    assert len(outbox.pending()) == 0


def test_lookup_cli(tmp_path: Path, monkeypatch, capsys):
    """lookup returns canonical events for given task-id."""
    from youdo_monitor.archive_cli import main

    fake_google = FakeGoogleArchive()
    fake_sheets = FakeSheetsIndex(fake_google, "sheet_test")
    monkeypatch.setattr("youdo_monitor.archive_cli.GoogleArchive", lambda *a, **kw: fake_google)
    monkeypatch.setattr("youdo_monitor.archive_cli.SheetsIndex", lambda *a, **kw: fake_sheets)

    cat_file = tmp_path / "archive_catalog.json"
    outbox_file = tmp_path / "archive_outbox.sqlite3"
    cat_file.write_text(json.dumps({
        "schema_version": 1,
        "active": True,
        "root_folder_id": "root_test",
        "spreadsheet_id": "sheet_test",
        "catalog_file_id": "cat_test",
    }))
    fake_google.files["cat_test"] = json.dumps({
        "schema_version": 1,
        "root_folder_id": "root_test",
        "spreadsheet_id": "sheet_test",
        "catalog_file_id": "cat_test",
        "batches": [],
    }).encode("utf-8")

    outbox = Outbox(outbox_file)
    ev = make_event(
        kind="task_observed",
        task_id=15320824,
        payload={"title": "Looked up task"},
        observed_at="2026-10-15T12:00:00Z",
        source="youdo",
        completeness="summary",
    )
    outbox.enqueue(ev)

    ret = main([
        "lookup",
        "--task-id", "15320824",
        "--catalog-path", str(cat_file),
        "--outbox-path", str(outbox_file),
    ])
    assert ret == 0
    captured = capsys.readouterr()
    assert "15320824" in captured.out
    assert "Looked up task" in captured.out


def test_export_cli_filters_integration_test_by_default(tmp_path: Path, monkeypatch):
    """export excludes source='integration_test' by default, includes with --include-tests."""
    from youdo_monitor.archive_cli import main

    fake_google = FakeGoogleArchive()
    fake_sheets = FakeSheetsIndex(fake_google, "sheet_test")
    monkeypatch.setattr("youdo_monitor.archive_cli.GoogleArchive", lambda *a, **kw: fake_google)
    monkeypatch.setattr("youdo_monitor.archive_cli.SheetsIndex", lambda *a, **kw: fake_sheets)

    cat_file = tmp_path / "archive_catalog.json"
    outbox_file = tmp_path / "archive_outbox.sqlite3"
    key_file = tmp_path / "export.key"
    cat_file.write_text(json.dumps({
        "schema_version": 1,
        "active": True,
        "root_folder_id": "root_test",
        "spreadsheet_id": "sheet_test",
        "catalog_file_id": "cat_test",
    }))
    fake_google.files["cat_test"] = json.dumps({
        "schema_version": 1,
        "root_folder_id": "root_test",
        "spreadsheet_id": "sheet_test",
        "catalog_file_id": "cat_test",
        "batches": [],
    }).encode("utf-8")

    outbox = Outbox(outbox_file)
    ev1 = make_event(
        kind="task_observed",
        task_id=101,
        payload={"title": "Real task"},
        source="youdo",
        observed_at="2026-10-15T12:00:00Z",
        completeness="summary",
    )
    ev2 = make_event(
        kind="task_observed",
        task_id=102,
        payload={"title": "Test task"},
        source="integration_test",
        observed_at="2026-10-15T12:00:00Z",
        completeness="summary",
    )
    outbox.enqueue(ev1)
    outbox.enqueue(ev2)

    export_out = tmp_path / "export_prod.jsonl.gz"
    ret = main([
        "export",
        "--output", str(export_out),
        "--catalog-path", str(cat_file),
        "--outbox-path", str(outbox_file),
        "--key-path", str(key_file),
    ])
    assert ret == 0
    assert export_out.exists()

    with gzip.open(export_out, "rt", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f]
    # Line 0 is manifest
    assert lines[0]["export_version"] == 1
    # Only Real task exported, Test task excluded
    assert len(lines) == 2
    assert "Real task" in lines[1]["title"]

    # Now with --include-tests
    export_all = tmp_path / "export_all.jsonl.gz"
    ret2 = main([
        "export",
        "--output", str(export_all),
        "--catalog-path", str(cat_file),
        "--outbox-path", str(outbox_file),
        "--key-path", str(key_file),
        "--include-tests",
    ])
    assert ret2 == 0
    with gzip.open(export_all, "rt", encoding="utf-8") as f:
        lines_all = [json.loads(line) for line in f]
    assert len(lines_all) == 3


def test_rebuild_index_cli(tmp_path: Path, monkeypatch):
    """rebuild-index replays catalog packages to destination spreadsheet."""
    from youdo_monitor.archive_cli import main

    fake_google = FakeGoogleArchive()
    fake_sheets = FakeSheetsIndex(fake_google, "dest_sheet_999")
    monkeypatch.setattr("youdo_monitor.archive_cli.GoogleArchive", lambda *a, **kw: fake_google)
    monkeypatch.setattr("youdo_monitor.archive_cli.SheetsIndex", lambda *a, **kw: fake_sheets)
    monkeypatch.setattr("youdo_monitor.archive_cli.ArchiveSync._create_sheets_index", lambda s, sid: fake_sheets)

    cat_file = tmp_path / "archive_catalog.json"
    outbox_file = tmp_path / "archive_outbox.sqlite3"
    cat_file.write_text(json.dumps({
        "schema_version": 1,
        "active": True,
        "root_folder_id": "root_test",
        "spreadsheet_id": "prod_sheet_111",
        "catalog_file_id": "cat_test",
    }))
    fake_google.files["cat_test"] = json.dumps({
        "schema_version": 1,
        "root_folder_id": "root_test",
        "spreadsheet_id": "prod_sheet_111",
        "catalog_file_id": "cat_test",
        "batches": [],
    }).encode("utf-8")

    ret = main([
        "rebuild-index",
        "--destination-spreadsheet-id", "dest_sheet_999",
        "--catalog-path", str(cat_file),
        "--outbox-path", str(outbox_file),
    ])
    assert ret == 0


def test_bootstrap_halts_on_multiple_roots(tmp_path: Path, monkeypatch):
    """Bootstrap must halt with an error if multiple root folders exist for archive_key."""
    from youdo_monitor.archive_cli import main

    fake_google = FakeGoogleArchive()
    cat_file = tmp_path / "data" / "archive_catalog.json"

    # Inject two conflicting root folders with same archive_key
    fake_google.metadata["f1"] = {
        "id": "f1",
        "name": "Archive 1",
        "mimeType": "application/vnd.google-apps.folder",
        "appProperties": {"archive_key": "conflicting_key"},
    }
    fake_google.metadata["f2"] = {
        "id": "f2",
        "name": "Archive 2",
        "mimeType": "application/vnd.google-apps.folder",
        "appProperties": {"archive_key": "conflicting_key"},
    }

    # Custom ensure_private_root to simulate GoogleArchive raising GoogleTransportError
    from youdo_monitor.archive_google import GoogleTransportError

    def ambiguous_root(*a, **kw):
        raise GoogleTransportError("Multiple root folders found for archive_key")

    fake_google.ensure_private_root = ambiguous_root

    monkeypatch.setattr("youdo_monitor.archive_cli.GoogleArchive", lambda *a, **kw: fake_google)

    with pytest.raises(GoogleTransportError, match="Multiple root folders"):
        main([
            "bootstrap",
            "--archive-key", "conflicting_key",
            "--catalog-path", str(cat_file),
        ])


def test_lookup_detects_download_corruption_on_fake(tmp_path: Path, monkeypatch):
    """Lookup must fail cleanly when cloud package blob fails verification."""
    from youdo_monitor.archive_cli import main

    fake_google = FakeGoogleArchive()
    cat_file = tmp_path / "archive_catalog.json"
    outbox_file = tmp_path / "archive_outbox.sqlite3"

    cat_file.write_text(json.dumps({
        "schema_version": 1,
        "active": True,
        "root_folder_id": "root_test",
        "spreadsheet_id": "sheet_test",
        "catalog_file_id": "cat_test",
    }))
    fake_google.files["cat_test"] = json.dumps({
        "schema_version": 1,
        "root_folder_id": "root_test",
        "spreadsheet_id": "sheet_test",
        "catalog_file_id": "cat_test",
        "batches": [
            {
                "batch_id": "batch_corrupt",
                "file_id": "pkg_corrupt",
                "sha256": "expected_hash_does_not_match",
                "event_count": 1,
            }
        ],
    }).encode("utf-8")
    # Put corrupted garbage
    fake_google.files["pkg_corrupt"] = b"not-a-valid-gzip-stream"

    monkeypatch.setattr("youdo_monitor.archive_cli.GoogleArchive", lambda *a, **kw: fake_google)
    monkeypatch.setattr("youdo_monitor.archive_cli.SheetsIndex", lambda *a, **kw: FakeSheetsIndex(fake_google, "sheet_test"))

    with pytest.raises(ValueError, match="SHA-256 mismatch|Corrupted"):
        main([
            "lookup",
            "--task-id", "99999",
            "--catalog-path", str(cat_file),
            "--outbox-path", str(outbox_file),
        ])

