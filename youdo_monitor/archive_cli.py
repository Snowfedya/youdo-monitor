"""Command-line interface for YouDo Experience Archive operations and maintenance.

Exposes safe, explicit commands:
- preflight: probes Google Drive / Sheets authentication and API scopes
- bootstrap: creates private root folder, spreadsheet index, tabs, and catalog
- import-state: read-only import of historical state.json into archive outbox
- sync: batches pending outbox events, uploads to Drive, and projects to Sheets
- lookup: inspects canonical events for a given task ID across sheets and cloud packages
- export: exports pseudonymized dataset for model training with strict PII scrubbing
- verify: safe live feed check with mandatory --live-feed --no-offers --no-notifications
- rebuild-index: replays immutable historical packages into an isolated destination spreadsheet
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import stat
import sys
import tempfile
from typing import Any, Dict, List, Optional

from youdo_monitor.archive_capture import ArchiveCapture
from youdo_monitor.archive_events import make_event
from youdo_monitor.archive_export import export_events, get_or_create_export_key
from youdo_monitor.archive_google import GoogleArchive
from youdo_monitor.archive_legacy import import_state
from youdo_monitor.archive_outbox import ArchiveBlocked, Outbox
from youdo_monitor.archive_packages import verify_package
from youdo_monitor.archive_sheets import SheetsIndex, col_to_letter
from youdo_monitor.archive_sync import ArchiveSync
from youdo_monitor.config import CATEGORIES, ONLY_VIRTUAL
from youdo_monitor.filters import TaskClassifier

# Google Sheets values:batchGet query string URL limit fix:
# Chunk large range verification requests so GET URL doesn't exceed HTTP server limits.
_orig_verify_written_ranges = SheetsIndex._verify_written_ranges


def _chunked_verify_written_ranges(self: Any, batch_data: List[Dict[str, Any]], chunk_size: int = 50) -> None:
    if not batch_data:
        return
    for i in range(0, len(batch_data), chunk_size):
        chunk = batch_data[i : i + chunk_size]
        _orig_verify_written_ranges(self, chunk)


def _safe_upsert(
    self: Any,
    tab: str,
    rows: List[Dict[str, Any]],
    *,
    key: str,
    owned_columns: List[str],
) -> None:
    """Keyed in-place upsert with RAW formula safety, preserving human-edited columns and append memory."""
    if not rows:
        return

    headers, raw_current_data = self._read_sheet_raw(tab)
    current_data = [list(r) for r in raw_current_data]
    initial_row_count = len(current_data)

    # Initialize header row if tab has no header yet
    if not headers:
        headers = list(owned_columns)
        end_col = col_to_letter(len(headers) - 1)
        init_url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}/values:batchUpdate"
        )
        self.transport.post(
            init_url,
            json={
                "valueInputOption": "RAW",
                "data": [
                    {
                        "range": f"{tab}!A1:{end_col}1",
                        "values": [headers],
                    }
                ],
            },
        )
        current_data = []
        initial_row_count = 0

    # If existing headers lack any owned columns, extend header row
    missing_owned = [c for c in owned_columns if c not in headers]
    if missing_owned:
        old_len = len(headers)
        headers.extend(missing_owned)
        start_col = col_to_letter(old_len)
        end_col = col_to_letter(len(headers) - 1)
        ext_url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}/values:batchUpdate"
        )
        self.transport.post(
            ext_url,
            json={
                "valueInputOption": "RAW",
                "data": [
                    {
                        "range": f"{tab}!{start_col}1:{end_col}1",
                        "values": [missing_owned],
                    }
                ],
            },
        )

    if key not in headers:
        raise ValueError(f"Key column '{key}' not found in tab '{tab}' headers")

    key_idx = headers.index(key)

    existing_keys: Dict[str, int] = {}
    for r_idx, r_vals in enumerate(current_data):
        row_num = r_idx + 2
        if key_idx < len(r_vals):
            raw_k = r_vals[key_idx]
            if raw_k is not None and str(raw_k).strip() != "":
                k_str = str(raw_k).strip()
                if k_str not in existing_keys:
                    existing_keys[k_str] = row_num

    owned_set = set(owned_columns)
    owned_indices = [i for i, h in enumerate(headers) if h in owned_set]

    contiguous_segments: List[tuple[int, int]] = []
    if owned_indices:
        seg_start = owned_indices[0]
        seg_end = owned_indices[0]
        for idx in owned_indices[1:]:
            if idx == seg_end + 1:
                seg_end = idx
            else:
                contiguous_segments.append((seg_start, seg_end))
                seg_start = idx
                seg_end = idx
        contiguous_segments.append((seg_start, seg_end))

    # Single write entry per target row to prevent duplicate/conflicting range updates
    row_batch_data: Dict[str, Dict[str, Any]] = {}
    next_append_row = len(current_data) + 2

    for r_dict in rows:
        k_val = str(r_dict.get(key, "")).strip()
        if not k_val:
            continue

        if k_val in existing_keys:
            target_row = existing_keys[k_val]
            row_idx = target_row - 2
            if row_idx < len(current_data):
                existing_row_vals = current_data[row_idx]
            else:
                existing_row_vals = []
            existing_dict = {
                headers[c]: existing_row_vals[c] if c < len(existing_row_vals) else ""
                for c in range(len(headers))
            }

            if tab == "Заказы":
                old_fs = str(existing_dict.get("first_seen") or "").strip()
                new_fs = str(r_dict.get("first_seen") or "").strip()
                if old_fs and new_fs:
                    r_dict["first_seen"] = min(old_fs, new_fs)
                elif old_fs:
                    r_dict["first_seen"] = old_fs

                old_ls = str(existing_dict.get("last_seen") or "").strip()
                new_ls = str(r_dict.get("last_seen") or "").strip()
                if old_ls and new_ls:
                    r_dict["last_seen"] = max(old_ls, new_ls)
                elif old_ls:
                    r_dict["last_seen"] = old_ls

                ex_refs = [r.strip() for r in str(existing_dict.get("package_refs") or "").split(",") if r.strip()]
                in_refs = [r.strip() for r in str(r_dict.get("package_refs") or "").split(",") if r.strip()]
                for nr in in_refs:
                    if nr not in ex_refs:
                        ex_refs.append(nr)
                r_dict["package_refs"] = ", ".join(ex_refs)

                if existing_dict.get("completeness") == "detail" and r_dict.get("completeness") != "detail":
                    r_dict["completeness"] = "detail"

                if existing_dict.get("outcome") and existing_dict["outcome"] != "unknown" and r_dict.get("outcome") in ("unknown", "", None):
                    r_dict["outcome"] = existing_dict["outcome"]

                for col in ("title", "budget", "budget_source", "category", "url", "verdict", "reason", "tags", "rules_fingerprint", "observation_id"):
                    if not r_dict.get(col) and existing_dict.get(col):
                        r_dict[col] = existing_dict[col]

            elif tab == "Отклики":
                for col in ("offer_id", "price"):
                    if not r_dict.get(col) and existing_dict.get(col):
                        r_dict[col] = existing_dict[col]
                # Preserve non-default mode if incoming is default 'auto'
                if existing_dict.get("mode") and existing_dict["mode"] != "auto" and r_dict.get("mode") == "auto":
                    r_dict["mode"] = existing_dict["mode"]
                elif not r_dict.get("mode") and existing_dict.get("mode"):
                    r_dict["mode"] = existing_dict["mode"]

                if existing_dict.get("status") == "success" and r_dict.get("status") != "success":
                    r_dict["status"] = "success"

            # Update current_data row in memory
            for c in range(len(headers)):
                h = headers[c]
                if h in r_dict:
                    while len(existing_row_vals) <= c:
                        existing_row_vals.append("")
                    existing_row_vals[c] = r_dict[h]

            if target_row <= initial_row_count + 1:
                # Existing sheet row from before this upsert: only update owned contiguous segments
                for start_c, end_c in contiguous_segments:
                    seg_vals = [
                        existing_row_vals[c] if c < len(existing_row_vals) else ""
                        for c in range(start_c, end_c + 1)
                    ]
                    start_letter = col_to_letter(start_c)
                    end_letter = col_to_letter(end_c)
                    cell_range = f"{tab}!{start_letter}{target_row}:{end_letter}{target_row}"
                    row_batch_data[cell_range] = {
                        "range": cell_range,
                        "values": [seg_vals],
                    }
            else:
                # Row appended during this upsert: write full row
                end_letter = col_to_letter(len(headers) - 1)
                cell_range = f"{tab}!A{target_row}:{end_letter}{target_row}"
                row_batch_data[cell_range] = {
                    "range": cell_range,
                    "values": [existing_row_vals],
                }
        else:
            target_row = next_append_row
            existing_keys[k_val] = target_row
            next_append_row += 1

            full_row_vals = [
                r_dict.get(h, "")
                for h in headers
            ]
            current_data.append(full_row_vals)
            end_letter = col_to_letter(len(headers) - 1)
            cell_range = f"{tab}!A{target_row}:{end_letter}{target_row}"
            row_batch_data[cell_range] = {
                "range": cell_range,
                "values": [full_row_vals],
            }

    batch_data = list(row_batch_data.values())
    if batch_data:
        update_url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}/values:batchUpdate"
        )
        self.transport.post(
            update_url,
            json={
                "valueInputOption": "RAW",
                "data": batch_data,
            },
        )
        self._verify_written_ranges(batch_data)


SheetsIndex._verify_written_ranges = _chunked_verify_written_ranges
SheetsIndex.upsert = _safe_upsert

logger = logging.getLogger("youdo_monitor.archive_cli")


def get_youdo_client(headless: bool = True) -> Any:
    """Instantiate YouDoClient with isolated browser session."""
    from youdo_monitor.client import YouDoClient

    return YouDoClient(headless=headless)


def merge_task(summary: Dict[str, Any], detail: Dict[str, Any]) -> Dict[str, Any]:
    """Combine feed summary and detail payload into one unified task dictionary."""
    merged = dict(summary)
    if detail and "error" not in detail:
        merged.update({k: v for k, v in detail.items() if k != "id"})
    return merged


def build_parser() -> argparse.ArgumentParser:
    """Build root CLI argument parser with required subcommands."""
    parser = argparse.ArgumentParser(
        prog="youdo_monitor.archive_cli",
        description="YouDo Experience Archive CLI operations and maintenance",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # 1. preflight
    p_preflight = subparsers.add_parser("preflight", help="Probe Google Drive auth and API scopes")
    p_preflight.add_argument("--token-path", default="/root/.hermes/google_token.json")
    p_preflight.add_argument("--client-secret-path", default="/root/.hermes/google_client_secret.json")

    # 2. bootstrap
    p_bootstrap = subparsers.add_parser("bootstrap", help="Create or verify archive Drive folder, spreadsheet, and catalog")
    p_bootstrap.add_argument("--name", default="YouDo Experience Archive", help="Archive root folder name")
    p_bootstrap.add_argument("--archive-key", default="youdo_experience_archive", help="Durable unique archive key")
    p_bootstrap.add_argument("--catalog-path", default="data/archive_catalog.json", help="Local catalog JSON path")
    p_bootstrap.add_argument("--spreadsheet-title", default="YouDo Experience Index", help="Title of Google Spreadsheet index")
    p_bootstrap.add_argument("--token-path", default="/root/.hermes/google_token.json")
    p_bootstrap.add_argument("--client-secret-path", default="/root/.hermes/google_client_secret.json")

    # 3. import-state
    p_import = subparsers.add_parser("import-state", help="Import legacy state.json into durable outbox")
    p_import.add_argument("--path", required=True, help="Path to state.json to import")
    p_import.add_argument("--outbox-path", default="data/archive_outbox.sqlite3", help="Outbox sqlite3 db path")

    # 4. sync
    p_sync = subparsers.add_parser("sync", help="Synchronize outbox events to Google Drive and update Sheets index")
    p_sync.add_argument("--catalog-path", default="data/archive_catalog.json", help="Local catalog JSON path")
    p_sync.add_argument("--outbox-path", default="data/archive_outbox.sqlite3", help="Outbox sqlite3 db path")
    p_sync.add_argument("--token-path", default="/root/.hermes/google_token.json")
    p_sync.add_argument("--client-secret-path", default="/root/.hermes/google_client_secret.json")

    # 5. lookup
    p_lookup = subparsers.add_parser("lookup", help="Look up all canonical archive events for task ID")
    p_lookup.add_argument("--task-id", type=int, required=True, help="Numeric task ID to lookup")
    p_lookup.add_argument("--catalog-path", default="data/archive_catalog.json", help="Local catalog JSON path")
    p_lookup.add_argument("--outbox-path", default="data/archive_outbox.sqlite3", help="Outbox sqlite3 db path")
    p_lookup.add_argument("--token-path", default="/root/.hermes/google_token.json")
    p_lookup.add_argument("--client-secret-path", default="/root/.hermes/google_client_secret.json")

    # 6. export
    p_export = subparsers.add_parser("export", help="Export pseudonymized dataset for model training")
    p_export.add_argument("--output", required=True, help="Destination .jsonl or .jsonl.gz path")
    p_export.add_argument("--catalog-path", default="data/archive_catalog.json", help="Local catalog JSON path")
    p_export.add_argument("--outbox-path", default="data/archive_outbox.sqlite3", help="Outbox sqlite3 db path")
    p_export.add_argument("--key-path", default="data/archive_export.key", help="HMAC export key path")
    p_export.add_argument("--include-tests", action="store_true", default=False, help="Include integration test events")
    p_export.add_argument("--token-path", default="/root/.hermes/google_token.json")
    p_export.add_argument("--client-secret-path", default="/root/.hermes/google_client_secret.json")

    # 7. verify
    p_verify = subparsers.add_parser("verify", help="Live feed observation and archive pipeline verification without offers/notifications")
    p_verify.add_argument("--live-feed", action="store_true", required=True, help="Explicit confirmation to fetch live feed")
    p_verify.add_argument("--no-offers", action="store_true", required=True, help="Explicit confirmation: NO offers submitted")
    p_verify.add_argument("--no-notifications", action="store_true", required=True, help="Explicit confirmation: NO notifications sent")
    p_verify.add_argument("--limit", type=int, default=5, help="Max task details to fetch for deep capture")
    p_verify.add_argument("--catalog-path", default="data/archive_catalog.json", help="Local catalog JSON path")
    p_verify.add_argument("--outbox-path", default="data/archive_outbox.sqlite3", help="Outbox sqlite3 db path")
    p_verify.add_argument("--token-path", default="/root/.hermes/google_token.json")
    p_verify.add_argument("--client-secret-path", default="/root/.hermes/google_client_secret.json")

    # 8. rebuild-index
    p_rebuild = subparsers.add_parser("rebuild-index", help="Rebuild Sheets index into isolated destination spreadsheet")
    p_rebuild.add_argument("--destination-spreadsheet-id", required=True, help="Explicit target destination spreadsheet ID")
    p_rebuild.add_argument("--catalog-path", default="data/archive_catalog.json", help="Local catalog JSON path")
    p_rebuild.add_argument("--outbox-path", default="data/archive_outbox.sqlite3", help="Outbox sqlite3 db path")
    p_rebuild.add_argument("--token-path", default="/root/.hermes/google_token.json")
    p_rebuild.add_argument("--client-secret-path", default="/root/.hermes/google_client_secret.json")

    return parser


def cmd_preflight(args: argparse.Namespace) -> int:
    """Run Google Drive/Sheets preflight check."""
    google = GoogleArchive(token_path=args.token_path, client_secret_path=args.client_secret_path)
    res = google.preflight()
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Create or verify root folder, spreadsheet index, tabs, and catalog."""
    google = GoogleArchive(token_path=args.token_path, client_secret_path=args.client_secret_path)
    google.preflight()

    archive_key = str(args.archive_key)
    name = str(args.name)

    # 1. Ensure private root folder
    root = google.ensure_private_root(archive_key=archive_key, name=name)
    root_folder_id = root["id"]
    google.assert_private_permissions(root_folder_id)

    # 2. Ensure private spreadsheet
    sheet = google.ensure_private_spreadsheet(
        archive_key=archive_key,
        parent_id=root_folder_id,
        title=args.spreadsheet_title,
    )
    spreadsheet_id = sheet["id"]
    google.assert_private_permissions(spreadsheet_id)

    # 3. Ensure 4 tabs on spreadsheet
    sheets = SheetsIndex(google, spreadsheet_id)
    sheet_ids = sheets.ensure_tabs()

    # 4. Check or create cloud catalog.json
    q = (
        f"name = 'catalog.json' and "
        f"'{root_folder_id}' in parents and "
        f"trashed = false"
    )
    res = google.transport.get_json(
        "https://www.googleapis.com/drive/v3/files",
        params={"q": q, "spaces": "drive", "fields": "files(id, name)"},
    )
    files = res.get("files", []) if isinstance(res, dict) else []
    if files:
        catalog_file_id = files[0]["id"]
        cloud_cat = google.get_json(catalog_file_id)
    else:
        catalog_file_id = google.reserve_file_id()
        cloud_cat = {
            "schema_version": 1,
            "active": True,
            "archive_key": archive_key,
            "root_folder_id": root_folder_id,
            "spreadsheet_id": spreadsheet_id,
            "sheet_ids": sheet_ids,
            "catalog_file_id": catalog_file_id,
            "last_verified_sync": None,
            "batches": [],
        }
        google.put_json_verified(
            catalog_file_id,
            cloud_cat,
            parent_id=root_folder_id,
            name="catalog.json",
        )

    # 5. Write local catalog atomically with 0600 permissions
    cat_path = Path(args.catalog_path).resolve()
    cat_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    batches = cloud_cat.get("batches") or []
    last_b_id = batches[-1].get("batch_id") if batches else None
    local_data = {
        "schema_version": 1,
        "active": True,
        "archive_key": archive_key,
        "root_folder_id": root_folder_id,
        "spreadsheet_id": spreadsheet_id,
        "catalog_file_id": catalog_file_id,
        "last_verified_sync": cloud_cat.get("last_verified_sync"),
        "last_batch_id": last_b_id,
    }
    temp_fd, temp_path = tempfile.mkstemp(
        dir=str(cat_path.parent),
        prefix=".cat_tmp_",
        suffix=".json",
    )
    with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
        json.dump(local_data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, str(cat_path))
    try:
        os.chmod(cat_path, 0o600)
    except OSError:
        pass

    out = {
        "status": "ok",
        "archive_key": archive_key,
        "root_folder_id": root_folder_id,
        "spreadsheet_id": spreadsheet_id,
        "catalog_file_id": catalog_file_id,
        "sheet_ids": sheet_ids,
        "catalog_path": str(cat_path),
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_import_state(args: argparse.Namespace) -> int:
    """Import historical state from state.json into outbox."""
    outbox = Outbox(args.outbox_path)
    res = import_state(args.path, outbox)
    print(json.dumps({"status": "ok", **res}, indent=2, ensure_ascii=False))
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Run full ArchiveSync pipeline."""
    cat_path = Path(args.catalog_path).resolve()
    if not cat_path.exists():
        raise ArchiveBlocked(f"Catalog file {cat_path} does not exist. Run bootstrap first.")
    cat = json.loads(cat_path.read_text(encoding="utf-8"))
    google = GoogleArchive(token_path=args.token_path, client_secret_path=args.client_secret_path)
    spreadsheet_id = cat.get("spreadsheet_id")
    sheets = SheetsIndex(google, spreadsheet_id) if spreadsheet_id else None
    outbox = Outbox(args.outbox_path)
    sync = ArchiveSync(
        outbox=outbox,
        google=google,
        sheets=sheets,
        catalog_path=cat_path,
        root_folder_id=cat.get("root_folder_id"),
    )
    res = sync.run()
    print(json.dumps({"status": "ok", **res}, indent=2, ensure_ascii=False))
    return 0


def cmd_lookup(args: argparse.Namespace) -> int:
    """Look up canonical events for task ID."""
    cat_path = Path(args.catalog_path).resolve()
    if not cat_path.exists():
        raise ArchiveBlocked(f"Catalog file {cat_path} does not exist. Run bootstrap first.")
    cat = json.loads(cat_path.read_text(encoding="utf-8"))
    google = GoogleArchive(token_path=args.token_path, client_secret_path=args.client_secret_path)
    spreadsheet_id = cat.get("spreadsheet_id")
    sheets = SheetsIndex(google, spreadsheet_id) if spreadsheet_id else None
    outbox = Outbox(args.outbox_path)
    sync = ArchiveSync(
        outbox=outbox,
        google=google,
        sheets=sheets,
        catalog_path=cat_path,
        root_folder_id=cat.get("root_folder_id"),
    )
    events = sync.lookup(args.task_id)
    out = {
        "status": "ok",
        "task_id": args.task_id,
        "events_count": len(events),
        "events": events,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Export pseudonymized dataset for training."""
    cat_path = Path(args.catalog_path).resolve()
    outbox_path = Path(args.outbox_path).resolve()
    google = GoogleArchive(token_path=args.token_path, client_secret_path=args.client_secret_path)

    events: List[Dict[str, Any]] = []
    seen_ids = set()

    # 1. Read cloud packages from catalog if catalog exists
    if cat_path.exists():
        try:
            cat = json.loads(cat_path.read_text(encoding="utf-8"))
            cat_file_id = cat.get("catalog_file_id")
            cloud_cat = google.get_json(cat_file_id) if cat_file_id else cat
            for b in cloud_cat.get("batches", []):
                fid = b.get("file_id")
                if fid:
                    blob = google.download(fid)
                    pkg_events = verify_package(
                        blob,
                        expected_sha256=b.get("sha256", ""),
                        expected_count=b.get("event_count", 0),
                    )
                    for ev in pkg_events:
                        eid = ev.get("event_id")
                        if eid and eid not in seen_ids:
                            seen_ids.add(eid)
                            events.append(ev)
        except Exception as e:
            logger.warning("Could not download cloud packages for export: %s", e)

    # 2. Read local outbox pending events
    if outbox_path.exists():
        try:
            outbox = Outbox(outbox_path)
            for ev in outbox.pending():
                eid = ev.get("event_id")
                if eid and eid not in seen_ids:
                    seen_ids.add(eid)
                    events.append(ev)
        except Exception as e:
            logger.warning("Could not read outbox events for export: %s", e)

    # Exclude integration_test source events by default
    if not args.include_tests:
        events = [ev for ev in events if ev.get("source") != "integration_test"]

    key = get_or_create_export_key(args.key_path)
    res = export_events(events, args.output, key=key)
    print(json.dumps({"status": "ok", **res}, indent=2, ensure_ascii=False))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify live feed observation and outbox capture without side effects."""
    if not (args.live_feed and args.no_offers and args.no_notifications):
        sys.exit("Error: verify requires explicit --live-feed --no-offers --no-notifications")

    # Invariant: NEVER import or instantiate OfferManager or Notifier / Telegram!
    client = get_youdo_client(headless=True)
    try:
        client.start()
    except Exception as e:
        sys.exit(f"Failed to start browser session: {e}")

    try:
        feed = client.fetch_feed(categories=CATEGORIES, only_virtual=ONLY_VIRTUAL)
    except Exception as e:
        try:
            client.stop()
        except Exception:
            pass
        sys.exit(f"Failed to fetch live feed: {e}")

    outbox = Outbox(args.outbox_path)
    capture = ArchiveCapture(outbox=outbox, source="youdo")
    capture.observe_feed(feed)

    sample = feed[:args.limit] if args.limit else feed
    detail_ids = [t.get("id") for t in sample if t.get("id")]
    details = []
    if detail_ids:
        try:
            details = client.fetch_task_details(detail_ids)
        except Exception as e:
            logger.warning("Failed to fetch task details: %s", e)
            details = []

    try:
        client.stop()
    except Exception:
        pass

    detail_by_id = {d["id"]: d for d in details if isinstance(d, dict) and "id" in d}
    classifier = TaskClassifier()

    for summary in sample:
        tid = summary.get("id")
        dtl = detail_by_id.get(tid, {})
        merged = merge_task(summary, dtl)
        decision = classifier.evaluate(merged)
        capture.observe_detail(summary, dtl, decision)

    sync_result = None
    cat_path = Path(args.catalog_path).resolve()
    if cat_path.exists():
        try:
            cat = json.loads(cat_path.read_text(encoding="utf-8"))
            google = GoogleArchive(token_path=args.token_path, client_secret_path=args.client_secret_path)
            spreadsheet_id = cat.get("spreadsheet_id")
            sheets = SheetsIndex(google, spreadsheet_id) if spreadsheet_id else None
            sync = ArchiveSync(
                outbox=outbox,
                google=google,
                sheets=sheets,
                catalog_path=cat_path,
                root_folder_id=cat.get("root_folder_id"),
            )
            sync_result = sync.run()
        except Exception as e:
            logger.error("Sync in verify failed: %s", e)
            sync_result = {"error": str(e)}

    out = {
        "status": "ok",
        "live_feed_count": len(feed),
        "details_count": len(details),
        "pending_outbox_events": len(outbox.pending()),
        "sync_result": sync_result,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_rebuild_index(args: argparse.Namespace) -> int:
    """Rebuild Sheets index into isolated destination spreadsheet."""
    target_id = str(args.destination_spreadsheet_id or "").strip()
    if not target_id:
        raise ValueError("destination-spreadsheet-id must be non-empty")

    cat_path = Path(args.catalog_path).resolve()
    if cat_path.exists():
        cat = json.loads(cat_path.read_text(encoding="utf-8"))
        if target_id == cat.get("spreadsheet_id"):
            raise ValueError(
                f"Destination spreadsheet {target_id} matches production spreadsheet! "
                "Refusing rebuild into production."
            )
        root_folder_id = cat.get("root_folder_id")
    else:
        root_folder_id = None

    google = GoogleArchive(token_path=args.token_path, client_secret_path=args.client_secret_path)
    outbox = Outbox(args.outbox_path)
    target_sheets = SheetsIndex(google, target_id)
    sync = ArchiveSync(
        outbox=outbox,
        google=google,
        sheets=target_sheets,
        catalog_path=cat_path,
        root_folder_id=root_folder_id,
        sheets_factory=lambda g, sid: SheetsIndex(g, sid),
    )
    res = sync.rebuild_to(target_id)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Main CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "preflight":
        return cmd_preflight(args)
    elif args.subcommand == "bootstrap":
        return cmd_bootstrap(args)
    elif args.subcommand == "import-state":
        return cmd_import_state(args)
    elif args.subcommand == "sync":
        return cmd_sync(args)
    elif args.subcommand == "lookup":
        return cmd_lookup(args)
    elif args.subcommand == "export":
        return cmd_export(args)
    elif args.subcommand == "verify":
        return cmd_verify(args)
    elif args.subcommand == "rebuild-index":
        return cmd_rebuild_index(args)
    else:
        sys.exit(f"Unknown subcommand: {args.subcommand}")


if __name__ == "__main__":
    sys.exit(main())
