"""Google Sheets keyed index projections, RAW formula safety, and manual outcomes intake."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

from youdo_monitor.archive_events import make_event


def col_to_letter(col_idx: int) -> str:
    """Convert zero-based column index to spreadsheet column letter (0 -> A, 25 -> Z, 26 -> AA)."""
    result = []
    col_idx += 1
    while col_idx > 0:
        col_idx, remainder = divmod(col_idx - 1, 26)
        result.append(chr(65 + remainder))
    return "".join(reversed(result))


class SheetsIndex:
    """Maintains keyed, formula-safe Sheets index with strict preservation of manual columns."""

    TABS = ("Заказы", "Отклики", "Результаты", "Синхронизация")

    ORDER_MACHINE_COLUMNS = [
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

    OFFER_MACHINE_COLUMNS = [
        "attempt_id",
        "task_id",
        "observed_at",
        "price",
        "mode",
        "status",
        "offer_id",
        "batch_id",
    ]

    SYNC_MACHINE_COLUMNS = [
        "batch_id",
        "period",
        "drive_file_id",
        "sha256",
        "event_count",
        "state",
        "verified_at",
        "error",
    ]

    OUTCOME_USER_COLUMNS = [
        "type",
        "task_id",
        "time",
        "amount",
        "currency",
        "source",
        "note",
    ]

    OUTCOME_MACHINE_COLUMNS = [
        "outcome_id",
        "revision",
        "status_or_error",
    ]

    VALID_OUTCOME_TYPES = {
        "confirmed_reply",
        "agreed",
        "completed",
        "declined",
        "revenue",
        "expense",
    }

    def __init__(self, google: Any, spreadsheet_id: str):
        self.google = google
        self.spreadsheet_id = str(spreadsheet_id)
        if hasattr(google, "_mock_children"):
            if "transport" in google._mock_children:
                self.transport = google.transport
            else:
                self.transport = google
        elif hasattr(google, "transport"):
            self.transport = google.transport
        else:
            self.transport = google

    def _default_headers_for(self, tab: str) -> List[str]:
        if tab == "Заказы":
            return list(self.ORDER_MACHINE_COLUMNS)
        elif tab == "Отклики":
            return list(self.OFFER_MACHINE_COLUMNS)
        elif tab == "Результаты":
            return list(self.OUTCOME_USER_COLUMNS + self.OUTCOME_MACHINE_COLUMNS)
        elif tab == "Синхронизация":
            return list(self.SYNC_MACHINE_COLUMNS)
        return []

    def ensure_tabs(self) -> Dict[str, int]:
        """Verify presence of required tabs, creating any missing ones with initial headers."""
        meta_url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}"
        )
        res = self.transport.get_json(
            meta_url,
            params={"fields": "sheets(properties(sheetId,title))"},
        )
        sheets_info = res.get("sheets", []) if isinstance(res, dict) else []
        tab_map: Dict[str, int] = {}
        for s in sheets_info:
            p = s.get("properties", {})
            title = p.get("title")
            sid = p.get("sheetId")
            if title is not None:
                tab_map[title] = sid

        missing = [t for t in self.TABS if t not in tab_map]
        if missing:
            add_requests = [
                {"addSheet": {"properties": {"title": t}}}
                for t in missing
            ]
            batch_url = (
                f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}:batchUpdate"
            )
            add_res = self.transport.post(batch_url, json={"requests": add_requests})
            replies = add_res.get("replies", []) if isinstance(add_res, dict) else []
            for r in replies:
                props = (r.get("addSheet") or {}).get("properties", {})
                if props.get("title") and props.get("sheetId") is not None:
                    tab_map[props["title"]] = props["sheetId"]

            # Initialize headers for newly created tabs using RAW valueInputOption
            header_data = []
            for t in missing:
                headers = self._default_headers_for(t)
                if headers:
                    end_letter = col_to_letter(len(headers) - 1)
                    header_data.append({
                        "range": f"{t}!A1:{end_letter}1",
                        "values": [headers],
                    })

            if header_data:
                values_batch_url = (
                    f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}/values:batchUpdate"
                )
                self.transport.post(
                    values_batch_url,
                    json={
                        "valueInputOption": "RAW",
                        "data": header_data,
                    },
                )

        return tab_map

    def _read_sheet_raw(self, tab: str) -> Tuple[List[str], List[List[Any]]]:
        """Fetch raw header row and subsequent data rows with UNFORMATTED_VALUE."""
        url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}/values/{tab}!A1:ZZ"
        )
        res = self.transport.get_json(
            url,
            params={"valueRenderOption": "UNFORMATTED_VALUE"},
        )
        values = res.get("values", []) if isinstance(res, dict) else []
        if not values:
            return [], []

        headers = [str(h) for h in values[0]]
        data_rows = values[1:]
        return headers, data_rows

    def read_rows(self, tab: str) -> List[Dict[str, Any]]:
        """Read all rows from tab, returning list of dictionaries keyed by header row."""
        headers, data_rows = self._read_sheet_raw(tab)
        if not headers or not data_rows:
            return []

        rows: List[Dict[str, Any]] = []
        for r_vals in data_rows:
            r_dict: Dict[str, Any] = {}
            for idx, h in enumerate(headers):
                r_dict[h] = r_vals[idx] if idx < len(r_vals) else ""
            rows.append(r_dict)
        return rows

    @staticmethod
    def _cells_match(expected: Any, actual: Any) -> bool:
        """Compare expected machine cell against actual value read back from Sheets."""
        if expected == actual:
            return True
        if (expected is None or expected == "") and (actual is None or actual == ""):
            return True
        if isinstance(expected, bool) or isinstance(actual, bool):
            return str(expected).strip().lower() == str(actual).strip().lower()
        try:
            if expected is not None and actual is not None:
                e_f = float(expected)
                a_f = float(actual)
                if math.isfinite(e_f) and math.isfinite(a_f):
                    return e_f == a_f
        except (ValueError, TypeError):
            pass
        if str(expected).strip() == str(actual).strip():
            return True
        return False

    def _verify_written_ranges(self, batch_data: List[Dict[str, Any]], chunk_size: int = 50) -> None:
        """Fetch exact written ranges and assert cell values match expected machine cells."""
        if not batch_data:
            return

        for i in range(0, len(batch_data), chunk_size):
            chunk = batch_data[i : i + chunk_size]
            ranges = [d["range"] for d in chunk]
            url = (
                f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}/values:batchGet"
            )
            res = self.transport.get_json(
                url,
                params={
                    "ranges": ranges,
                    "valueRenderOption": "UNFORMATTED_VALUE",
                },
            )
            if not isinstance(res, dict):
                raise RuntimeError(
                    f"Readback failed: expected dict response from batchGet, got {type(res)}"
                )

            if "valueRanges" in res:
                value_ranges = res["valueRanges"]
            elif len(chunk) == 1 and "values" in res:
                value_ranges = [{"range": chunk[0]["range"], "values": res["values"]}]
            else:
                value_ranges = res.get("valueRanges", [])

            if len(value_ranges) != len(chunk):
                raise RuntimeError(
                    f"Readback verification failed: expected {len(chunk)} ranges, got {len(value_ranges)}"
                )

            for j, (expected_item, actual_vr) in enumerate(zip(chunk, value_ranges)):
                target_range = expected_item["range"]
                expected_rows = expected_item.get("values", [])
                actual_rows = actual_vr.get("values", [])

                for r_idx, exp_row in enumerate(expected_rows):
                    act_row = actual_rows[r_idx] if r_idx < len(actual_rows) else []
                    for c_idx, exp_val in enumerate(exp_row):
                        act_val = act_row[c_idx] if c_idx < len(act_row) else ""
                        if not self._cells_match(exp_val, act_val):
                            raise RuntimeError(
                                f"Readback verification mismatch in range '{target_range}' at "
                                f"row {r_idx}, col {c_idx}: expected {exp_val!r}, got {act_val!r}"
                            )

    def upsert(
        self,
        tab: str,
        rows: List[Dict[str, Any]],
        *,
        key: str,
        owned_columns: List[str],
    ) -> None:
        """Keyed in-place upsert with RAW formula safety, preserving human-edited columns."""
        if not rows:
            return

        headers, current_data = self._read_sheet_raw(tab)

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

        # Map current existing keys to 1-based sheet row numbers (header is row 1, data starts at row 2)
        existing_keys: Dict[str, int] = {}
        for r_idx, r_vals in enumerate(current_data):
            row_num = r_idx + 2
            if key_idx < len(r_vals):
                raw_k = r_vals[key_idx]
                if raw_k is not None and str(raw_k).strip() != "":
                    k_str = str(raw_k).strip()
                    if k_str not in existing_keys:
                        existing_keys[k_str] = row_num

        # Owned column indices
        owned_set = set(owned_columns)
        owned_indices = [i for i, h in enumerate(headers) if h in owned_set]

        # Group contiguous owned column indices to form tight ranges that never touch human columns
        contiguous_segments: List[Tuple[int, int]] = []
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

        batch_data: List[Dict[str, Any]] = []
        next_append_row = len(current_data) + 2

        for r_dict in rows:
            k_val = str(r_dict.get(key, "")).strip()
            if not k_val:
                continue

            if k_val in existing_keys:
                # Update existing row in place, strictly for owned columns
                target_row = existing_keys[k_val]
                existing_row_vals = current_data[target_row - 2]
                existing_dict = {
                    headers[c]: existing_row_vals[c] if c < len(existing_row_vals) else ""
                    for c in range(len(headers))
                }

                # Tab-specific intelligent in-place merges
                if tab == "Заказы":
                    # Preserve first_seen
                    old_fs = str(existing_dict.get("first_seen") or "").strip()
                    new_fs = str(r_dict.get("first_seen") or "").strip()
                    if old_fs and new_fs:
                        r_dict["first_seen"] = min(old_fs, new_fs)
                    elif old_fs:
                        r_dict["first_seen"] = old_fs

                    # Update last_seen
                    old_ls = str(existing_dict.get("last_seen") or "").strip()
                    new_ls = str(r_dict.get("last_seen") or "").strip()
                    if old_ls and new_ls:
                        r_dict["last_seen"] = max(old_ls, new_ls)
                    elif old_ls:
                        r_dict["last_seen"] = old_ls

                    # Append package_refs
                    ex_refs = [r.strip() for r in str(existing_dict.get("package_refs") or "").split(",") if r.strip()]
                    in_refs = [r.strip() for r in str(r_dict.get("package_refs") or "").split(",") if r.strip()]
                    for nr in in_refs:
                        if nr not in ex_refs:
                            ex_refs.append(nr)
                    r_dict["package_refs"] = ", ".join(ex_refs)

                    # Completeness monotonic retention
                    if existing_dict.get("completeness") == "detail" and r_dict.get("completeness") != "detail":
                        r_dict["completeness"] = "detail"

                    # Outcome preservation
                    if existing_dict.get("outcome") and existing_dict["outcome"] != "unknown" and r_dict.get("outcome") in ("unknown", "", None):
                        r_dict["outcome"] = existing_dict["outcome"]

                    # Preserve other existing non-empty owned values if not overwritten
                    for col in ("title", "budget", "budget_source", "category", "url", "verdict", "reason", "tags", "rules_fingerprint", "observation_id"):
                        if not r_dict.get(col) and existing_dict.get(col):
                            r_dict[col] = existing_dict[col]

                elif tab == "Отклики":
                    for col in ("offer_id", "price", "mode"):
                        if not r_dict.get(col) and existing_dict.get(col):
                            r_dict[col] = existing_dict[col]
                    if existing_dict.get("status") == "success" and r_dict.get("status") != "success":
                        r_dict["status"] = "success"

                for start_c, end_c in contiguous_segments:
                    seg_vals = [
                        r_dict.get(headers[c], "")
                        for c in range(start_c, end_c + 1)
                    ]
                    start_letter = col_to_letter(start_c)
                    end_letter = col_to_letter(end_c)
                    cell_range = f"{tab}!{start_letter}{target_row}:{end_letter}{target_row}"
                    batch_data.append({
                        "range": cell_range,
                        "values": [seg_vals],
                    })
            else:
                # Append unseen key at the bottom of the sheet
                target_row = next_append_row
                existing_keys[k_val] = target_row
                next_append_row += 1

                # Construct full row according to headers
                full_row_vals = [
                    r_dict.get(h, "")
                    for h in headers
                ]
                end_letter = col_to_letter(len(headers) - 1)
                cell_range = f"{tab}!A{target_row}:{end_letter}{target_row}"
                batch_data.append({
                    "range": cell_range,
                    "values": [full_row_vals],
                })

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

            # Readback verification: fetch exact written ranges and assert machine cells match
            self._verify_written_ranges(batch_data)

    def apply_events(self, events: List[Dict[str, Any]], package: Dict[str, Any]) -> None:
        """Route canonical events and package metadata into Заказы, Отклики, and Синхронизация tabs."""
        batch_id = str(package.get("batch_id") or "").strip()

        # 1. Заказы (Orders) projection
        order_events_by_task: Dict[int, List[Dict[str, Any]]] = {}
        for ev in events:
            tid = ev.get("task_id")
            if tid is not None:
                try:
                    order_events_by_task.setdefault(int(tid), []).append(ev)
                except (ValueError, TypeError):
                    continue

        if order_events_by_task:
            order_rows: List[Dict[str, Any]] = []
            for tid, t_events in order_events_by_task.items():
                title = ""
                budget = ""
                budget_source = ""
                category = ""
                url = ""
                first_seen = ""
                last_seen = ""
                completeness = "partial"
                verdict = ""
                reason = ""
                tags = ""
                rules_fp = ""
                obs_id = ""
                outcome = "unknown"

                for ev in t_events:
                    obs_time = str(ev.get("observed_at") or "")
                    if obs_time:
                        if not first_seen or obs_time < first_seen:
                            first_seen = obs_time
                        if not last_seen or obs_time > last_seen:
                            last_seen = obs_time

                    kind = ev.get("kind")
                    payload = ev.get("payload") or {}

                    if kind == "task_observed":
                        obs_id = ev.get("event_id") or obs_id
                        comp = ev.get("completeness")
                        if comp:
                            if comp == "detail" or completeness != "detail":
                                completeness = comp
                        if payload.get("title"):
                            title = payload["title"]
                        if payload.get("budget") is not None:
                            budget = str(payload["budget"])
                        if payload.get("budget_source"):
                            budget_source = payload["budget_source"]
                        if payload.get("category"):
                            category = payload["category"]
                        if payload.get("url"):
                            url = payload["url"]

                    elif kind == "classification":
                        obs_id = payload.get("observation_id") or ev.get("event_id") or obs_id
                        p = payload.get("passed")
                        if p is True:
                            verdict = "passed"
                        elif p is False:
                            verdict = "rejected"
                        elif payload.get("verdict"):
                            verdict = str(payload["verdict"])

                        if payload.get("reason"):
                            reason = str(payload["reason"])
                        t_tags = payload.get("tags")
                        if isinstance(t_tags, list):
                            tags = ", ".join(str(t) for t in t_tags)
                        elif t_tags:
                            tags = str(t_tags)
                        if payload.get("rules_fingerprint"):
                            rules_fp = str(payload["rules_fingerprint"])

                    elif kind == "outcome":
                        out_type = payload.get("type") or ev.get("type")
                        if out_type:
                            outcome = str(out_type)

                order_row = {
                    "task_id": tid,
                    "title": title,
                    "budget": budget,
                    "budget_source": budget_source,
                    "category": category,
                    "url": url,
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                    "completeness": completeness,
                    "verdict": verdict,
                    "reason": reason,
                    "tags": tags,
                    "rules_fingerprint": rules_fp,
                    "observation_id": obs_id,
                    "outcome": outcome,
                    "package_refs": batch_id,
                }
                order_rows.append(order_row)

            self.upsert(
                "Заказы",
                order_rows,
                key="task_id",
                owned_columns=self.ORDER_MACHINE_COLUMNS,
            )

        # 2. Отклики (Offers) projection
        offer_events = [
            ev for ev in events
            if ev.get("kind") in ("offer_attempt", "offer_result")
        ]
        if offer_events:
            offer_rows: List[Dict[str, Any]] = []
            for ev in offer_events:
                payload = ev.get("payload") or {}
                attempt_id = (
                    payload.get("attempt_id")
                    or ev.get("attempt_id")
                    or ev.get("identity")
                    or ev.get("event_id")
                )
                if not attempt_id:
                    continue

                tid = ev.get("task_id") or payload.get("task_id")
                price = payload.get("price", "")
                mode = payload.get("mode", "auto")

                kind = ev.get("kind")
                if kind == "offer_attempt":
                    status = payload.get("status", "attempted")
                else:
                    if "status" in payload:
                        status = payload["status"]
                    elif payload.get("success") is True:
                        status = "success"
                    elif payload.get("success") is False:
                        status = "failed"
                    else:
                        status = "unknown"

                offer_id = payload.get("offer_id", "")

                offer_rows.append({
                    "attempt_id": str(attempt_id),
                    "task_id": int(tid) if tid is not None else "",
                    "observed_at": str(ev.get("observed_at") or ""),
                    "price": price,
                    "mode": mode,
                    "status": status,
                    "offer_id": offer_id,
                    "batch_id": batch_id,
                })

            self.upsert(
                "Отклики",
                offer_rows,
                key="attempt_id",
                owned_columns=self.OFFER_MACHINE_COLUMNS,
            )

        # 3. Синхронизация (Sync) projection (skipped on rebuild)
        if batch_id and not package.get("is_rebuild"):
            all_times = [
                str(ev.get("observed_at"))
                for ev in events
                if ev.get("observed_at")
            ]
            period = package.get("period")
            if not period:
                if all_times:
                    min_t = min(all_times).split("T")[0]
                    max_t = max(all_times).split("T")[0]
                    period = min_t if min_t == max_t else f"{min_t}/{max_t}"
                else:
                    period = ""

            drive_fid = (
                package.get("drive_file_id")
                or package.get("file_id")
                or package.get("id")
                or ""
            )

            sync_row = {
                "batch_id": batch_id,
                "period": str(period),
                "drive_file_id": str(drive_fid),
                "sha256": str(package.get("sha256") or ""),
                "event_count": int(package.get("event_count") or len(events)),
                "state": str(package.get("state") or "verified"),
                "verified_at": str(package.get("verified_at") or ""),
                "error": str(package.get("error") or ""),
            }
            self.upsert(
                "Синхронизация",
                [sync_row],
                key="batch_id",
                owned_columns=self.SYNC_MACHINE_COLUMNS,
            )

    def read_outcomes(self) -> List[Dict[str, Any]]:
        """Intake user outcomes from Результаты, validate, compute revisions, and emit canonical events."""
        headers, data_rows = self._read_sheet_raw("Результаты")
        if not headers or not data_rows:
            return []

        # Count existing non-empty outcome_ids to detect duplicates
        outcome_id_counts: Dict[str, int] = {}
        if "outcome_id" in headers:
            oid_idx = headers.index("outcome_id")
            for r in data_rows:
                if oid_idx < len(r):
                    oid_val = str(r[oid_idx] or "").strip()
                    if oid_val:
                        outcome_id_counts[oid_val] = outcome_id_counts.get(oid_val, 0) + 1

        # Check required columns
        for c in self.OUTCOME_MACHINE_COLUMNS:
            if c not in headers:
                # Missing machine columns: extend headers
                old_len = len(headers)
                headers.append(c)
                col_let = col_to_letter(old_len)
                ext_url = (
                    f"https://sheets.googleapis.com/v4/spreadsheets/{self.spreadsheet_id}/values:batchUpdate"
                )
                self.transport.post(
                    ext_url,
                    json={
                        "valueInputOption": "RAW",
                        "data": [{"range": f"Результаты!{col_let}1:{col_let}1", "values": [[c]]}],
                    },
                )

        oid_col = headers.index("outcome_id")
        rev_col = headers.index("revision")
        stat_col = headers.index("status_or_error")

        valid_events: List[Dict[str, Any]] = []
        updates_to_write: List[Dict[str, Any]] = []

        for idx, r_vals in enumerate(data_rows):
            row_num = idx + 2
            r_dict = {
                headers[i]: r_vals[i] if i < len(r_vals) else ""
                for i in range(len(headers))
            }

            # Check if row is completely empty
            all_empty = all(str(v).strip() == "" for v in r_dict.values())
            if all_empty:
                continue

            type_val = str(r_dict.get("type") or "").strip()
            task_id_raw = r_dict.get("task_id")
            time_val = str(r_dict.get("time") or "").strip()
            amount_raw = r_dict.get("amount")
            currency_val = str(r_dict.get("currency") or "").strip() or "RUB"
            source_val = str(r_dict.get("source") or "").strip() or "manual"
            note_val = str(r_dict.get("note") or "").strip()

            outcome_id = str(r_dict.get("outcome_id") or "").strip()
            old_revision = str(r_dict.get("revision") or "").strip()
            old_status = str(r_dict.get("status_or_error") or "").strip()

            # Assign missing outcome_id
            assigned_new_oid = False
            if not outcome_id:
                outcome_id = f"out_{uuid.uuid4().hex[:12]}"
                assigned_new_oid = True

            # Duplicate outcome_id check
            is_dup = outcome_id_counts.get(outcome_id, 0) > 1
            if is_dup:
                status_or_error = f"conflict: duplicate outcome_id '{outcome_id}'"
                if (
                    status_or_error != old_status
                    or assigned_new_oid
                ):
                    updates_to_write.append({
                        "row_num": row_num,
                        "outcome_id": outcome_id,
                        "revision": old_revision,
                        "status_or_error": status_or_error,
                    })
                continue

            # Validation
            validation_error: Optional[str] = None
            if type_val not in self.VALID_OUTCOME_TYPES:
                validation_error = f"invalid: unsupported type '{type_val}'"

            task_id_int: Optional[int] = None
            if not validation_error:
                try:
                    task_id_int = int(str(task_id_raw).strip())
                except (ValueError, TypeError):
                    validation_error = "invalid: task_id must be integer"

            amount_val: Optional[Any] = None
            if not validation_error and amount_raw is not None and str(amount_raw).strip() != "":
                clean_amt = re.sub(r"[\s\u00a0\u202f]+", "", str(amount_raw)).replace(",", ".")
                if not clean_amt:
                    amount_val = None
                else:
                    try:
                        amt_num = float(clean_amt)
                        if not math.isfinite(amt_num):
                            validation_error = "invalid: amount must be a number"
                        else:
                            amount_val = int(amt_num) if amt_num.is_integer() else amt_num
                    except ValueError:
                        validation_error = "invalid: amount must be a number"

            if not validation_error and not time_val:
                validation_error = "invalid: time is required"

            if validation_error:
                status_or_error = validation_error
                if (
                    status_or_error != old_status
                    or assigned_new_oid
                ):
                    updates_to_write.append({
                        "row_num": row_num,
                        "outcome_id": outcome_id,
                        "revision": old_revision,
                        "status_or_error": status_or_error,
                    })
                continue

            # Valid outcome: compute deterministic canonical revision
            canonical_user_fields = {
                "amount": amount_val,
                "currency": currency_val,
                "note": note_val,
                "source": source_val,
                "task_id": task_id_int,
                "time": time_val,
                "type": type_val,
            }
            canonical_json = json.dumps(
                canonical_user_fields,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            revision = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
            status_or_error = "valid"

            if (
                revision != old_revision
                or status_or_error != old_status
                or assigned_new_oid
            ):
                updates_to_write.append({
                    "row_num": row_num,
                    "outcome_id": outcome_id,
                    "revision": revision,
                    "status_or_error": status_or_error,
                })

            assert task_id_int is not None
            event_payload = {
                "outcome_id": outcome_id,
                "type": type_val,
                "task_id": task_id_int,
                "time": time_val,
                "amount": amount_val,
                "currency": currency_val,
                "source": source_val,
                "note": note_val,
                "revision": revision,
            }
            event = make_event(
                kind="outcome",
                task_id=task_id_int,
                payload=event_payload,
                observed_at=time_val,
                source=source_val or "manual",
                completeness="detail",
                identity=f"{outcome_id}:{revision}",
            )
            valid_events.append(event)

        # Write back machine columns to sheet if any updates occurred
        if updates_to_write:
            batch_data: List[Dict[str, Any]] = []
            for u in updates_to_write:
                r_num = u["row_num"]
                batch_data.append({
                    "range": f"Результаты!{col_to_letter(oid_col)}{r_num}",
                    "values": [[u["outcome_id"]]],
                })
                batch_data.append({
                    "range": f"Результаты!{col_to_letter(rev_col)}{r_num}",
                    "values": [[u["revision"]]],
                })
                batch_data.append({
                    "range": f"Результаты!{col_to_letter(stat_col)}{r_num}",
                    "values": [[u["status_or_error"]]],
                })

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

            # Readback verification: fetch exact written ranges and assert machine cells match
            self._verify_written_ranges(batch_data)

        return valid_events

    def rebuild(self, events: List[Dict[str, Any]]) -> None:
        """Reconstruct machine tabs (Заказы, Отклики) from historical canonical event stream."""
        self.ensure_tabs()
        synthetic_package = {
            "batch_id": "batch_rebuild",
            "period": "rebuild",
            "drive_file_id": "rebuild",
            "sha256": "",
            "event_count": len(events),
            "state": "rebuilt",
            "is_rebuild": True,
        }
        self.apply_events(events, synthetic_package)
