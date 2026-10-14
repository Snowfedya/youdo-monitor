"""Bounded durable SQLite outbox for YouDo experience archive.

Enforces:
- File permissions: 0600 on database/journal files, 0700 on parent and staging dirs.
- SQLite foreign keys and strict transaction atomicity.
- Monotonic batch lifecycle progression along STAGES.
- Strict 64 MiB storage budget accounting for DB, rollback journals, and staging files.
- Conservative pre-write reservation to prevent peak usage overruns.
- Durable offer attempt guards against duplicate auto-responses across runs/restarts.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

DEFAULT_MAX_BYTES: int = 64 * 1024 * 1024  # 64 MiB

STAGES: Tuple[str, ...] = (
    "package_prepared",
    "drive_verified",
    "sheets_verified",
    "catalog_verified",
    "acknowledged",
)


class ArchiveBlocked(RuntimeError):
    """Raised when an outbox operation is blocked by budget, invariant, or storage error."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Outbox:
    """Durable transactional SQLite outbox queue, batch ledger, and attempt guard."""

    def __init__(
        self,
        path: Union[Path, str],
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        staging_dir: Optional[Union[Path, str]] = None,
    ) -> None:
        self.path: Path = Path(path).resolve()
        self.max_bytes: int = int(max_bytes)

        # Ensure parent directory exists with 0700 permissions
        parent_dir = self.path.parent
        parent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(parent_dir, 0o700)
        except OSError:
            pass

        # Configure staging directory with 0700 permissions
        if staging_dir is None:
            self.staging_dir: Path = parent_dir / "staging"
        else:
            self.staging_dir = Path(staging_dir).resolve()
        self.staging_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.staging_dir, 0o700)
        except OSError:
            pass

        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _apply_pragmas(self, conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = TRUNCATE;")
        conn.execute("PRAGMA synchronous = FULL;")
        max_pages = max(1, self.max_bytes // 4096)
        conn.execute(f"PRAGMA max_page_count = {max_pages};")

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            old_umask = os.umask(0o077)
            try:
                self._conn = sqlite3.connect(str(self.path), timeout=30.0)
                self._conn.row_factory = sqlite3.Row
                self._apply_pragmas(self._conn)
                if self.path.exists():
                    os.chmod(self.path, 0o600)
            except sqlite3.Error as e:
                raise ArchiveBlocked(f"Database connection error: {e}") from e
            finally:
                os.umask(old_umask)
        return self._conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        try:
            with conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        event_id TEXT PRIMARY KEY,
                        task_id INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        schema_version INTEGER NOT NULL DEFAULT 1,
                        observed_at TEXT NOT NULL,
                        source TEXT NOT NULL,
                        completeness TEXT NOT NULL,
                        identity TEXT,
                        payload TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at TEXT NOT NULL,
                        acknowledged_at TEXT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_events_task_id ON events(task_id);"
                )

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS batches (
                        batch_id TEXT PRIMARY KEY,
                        stage TEXT NOT NULL,
                        metadata TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_batches_stage ON batches(stage);"
                )

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS batch_events (
                        batch_id TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
                        event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
                        event_order INTEGER NOT NULL,
                        PRIMARY KEY (batch_id, event_id)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_batch_events_event_id ON batch_events(event_id);"
                )

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS unresolved_attempts (
                        attempt_id TEXT PRIMARY KEY,
                        task_id INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        metadata TEXT NOT NULL,
                        result TEXT,
                        created_at TEXT NOT NULL,
                        resolved_at TEXT
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_unresolved_attempts_task_id ON unresolved_attempts(task_id);"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_unresolved_attempts_status ON unresolved_attempts(status);"
                )
        except sqlite3.OperationalError as e:
            raise ArchiveBlocked(f"Database schema initialization blocked: {e}") from e
        except sqlite3.Error as e:
            raise ArchiveBlocked(f"Database error during schema init: {e}") from e

        # Ensure permissions
        if self.path.exists():
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def total_bytes(self) -> int:
        """Calculate total disk usage: DB + journal/wal/shm + staging files."""
        total = 0
        if self.path.exists():
            try:
                total += self.path.stat().st_size
            except OSError:
                pass

        for suffix in ("-journal", "-wal", "-shm"):
            j_path = self.path.with_name(self.path.name + suffix)
            if j_path.exists():
                try:
                    total += j_path.stat().st_size
                except OSError:
                    pass

        if self.staging_dir and self.staging_dir.exists():
            try:
                for entry in self.staging_dir.rglob("*"):
                    if entry.is_file():
                        try:
                            total += entry.stat().st_size
                        except OSError:
                            pass
            except OSError:
                pass

        return total

    def usage(self) -> Dict[str, Any]:
        """Return usage dictionary with total_bytes, max_bytes, ratio, warning, and full flags."""
        tb = self.total_bytes()
        ratio = float(tb) / float(self.max_bytes) if self.max_bytes > 0 else 1.0
        return {
            "total_bytes": tb,
            "max_bytes": self.max_bytes,
            "usage_ratio": ratio,
            "warning": tb >= int(0.75 * self.max_bytes),
            "full": tb >= self.max_bytes,
        }

    def _check_budget_for_write(self, estimated_bytes: int) -> None:
        """Conservative pre-write reservation check to prevent exceeding max_bytes."""
        tb = self.total_bytes()
        if tb >= self.max_bytes:
            raise ArchiveBlocked(
                f"Storage budget exceeded: current {tb} bytes >= limit {self.max_bytes} bytes"
            )

        if estimated_bytes > self.max_bytes:
            raise ArchiveBlocked(
                f"Storage budget exceeded: oversized item ({estimated_bytes} bytes > {self.max_bytes} limit)"
            )

        pages_needed = max(1, (estimated_bytes + 4095) // 4096) + 1
        # Conservative reservation includes database pages, index growth, and rollback journal pages
        reserve_bytes = (2 * pages_needed + 1) * 4096

        if tb + reserve_bytes > self.max_bytes:
            raise ArchiveBlocked(
                f"Storage budget exceeded: current {tb} + reserve {reserve_bytes} > limit {self.max_bytes}"
            )

    def enqueue(self, event: Dict[str, Any]) -> bool:
        """Enqueue a canonical event.

        Returns True if newly inserted, False if duplicate event_id (idempotent).
        Raises ArchiveBlocked if storage budget is exceeded or oversized.
        """
        if not isinstance(event, dict):
            raise ValueError("Event must be a dictionary")
        event_id = event.get("event_id")
        if not event_id or not isinstance(event_id, str):
            raise ValueError("Event must contain a valid string 'event_id'")
        if "task_id" not in event:
            raise ValueError("Event must contain 'task_id'")

        task_id = int(event["task_id"])
        kind = str(event.get("kind", ""))
        schema_version = int(event.get("schema_version", 1))
        observed_at = str(event.get("observed_at", ""))
        source = str(event.get("source", ""))
        completeness = str(event.get("completeness", ""))
        identity = event.get("identity")
        identity_val = str(identity) if identity is not None else None

        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("Event payload must be a dictionary")

        conn = self._get_conn()

        # Idempotency check: if event_id already exists, return False immediately without consuming budget
        try:
            cur = conn.execute(
                "SELECT 1 FROM events WHERE event_id = ? LIMIT 1;", (event_id,)
            )
            if cur.fetchone() is not None:
                return False
        except sqlite3.Error as e:
            raise ArchiveBlocked(f"Database error during enqueue lookup: {e}") from e

        # Estimate serialized payload and full event size
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        full_event_json = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        event_bytes = len(full_event_json.encode("utf-8"))

        # Pre-write budget verification
        self._check_budget_for_write(event_bytes)

        now_iso = _utc_now_iso()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO events (
                        event_id, task_id, kind, schema_version, observed_at,
                        source, completeness, identity, payload, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?);
                    """,
                    (
                        event_id,
                        task_id,
                        kind,
                        schema_version,
                        observed_at,
                        source,
                        completeness,
                        identity_val,
                        payload_json,
                        now_iso,
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            # Race condition duplicate check
            return False
        except sqlite3.OperationalError as e:
            raise ArchiveBlocked(f"Storage budget exceeded or disk full: {e}") from e
        except sqlite3.Error as e:
            raise ArchiveBlocked(f"Database error during enqueue: {e}") from e

    def pending(self) -> List[Dict[str, Any]]:
        """Return all events that are unacknowledged, ordered by insertion time."""
        conn = self._get_conn()
        try:
            cur = conn.execute(
                """
                SELECT schema_version, event_id, task_id, kind, observed_at,
                       source, completeness, identity, payload
                FROM events
                WHERE status != 'acknowledged'
                ORDER BY rowid ASC;
                """
            )
            rows = cur.fetchall()
            events: List[Dict[str, Any]] = []
            for row in rows:
                ev: Dict[str, Any] = {
                    "schema_version": row["schema_version"],
                    "event_id": row["event_id"],
                    "task_id": row["task_id"],
                    "kind": row["kind"],
                    "observed_at": row["observed_at"],
                    "source": row["source"],
                    "completeness": row["completeness"],
                    "payload": json.loads(row["payload"]),
                }
                if row["identity"] is not None:
                    ev["identity"] = row["identity"]
                events.append(ev)
            return events
        except sqlite3.Error as e:
            raise ArchiveBlocked(f"Database error during pending fetch: {e}") from e

    def prepare(
        self, batch_id: str, event_ids: List[str], metadata: Dict[str, Any]
    ) -> None:
        """Create a new batch with initial stage 'package_prepared' and bind events."""
        if not batch_id or not isinstance(batch_id, str):
            raise ArchiveBlocked("batch_id must be a non-empty string")
        if not event_ids:
            raise ArchiveBlocked("event_ids list cannot be empty")
        if not isinstance(metadata, dict):
            raise ArchiveBlocked("metadata must be a dictionary")

        conn = self._get_conn()
        now_iso = _utc_now_iso()
        meta_json = json.dumps(metadata, ensure_ascii=False)

        # Budget check for batch creation metadata
        self._check_budget_for_write(len(meta_json.encode("utf-8")) + len(event_ids) * 128)

        try:
            with conn:
                # Check batch_id uniqueness
                cur = conn.execute(
                    "SELECT 1 FROM batches WHERE batch_id = ? LIMIT 1;", (batch_id,)
                )
                if cur.fetchone() is not None:
                    raise ArchiveBlocked(f"Batch {batch_id} already exists")

                # Verify each event exists, is not acknowledged, and is not already in an active batch
                for eid in event_ids:
                    cur = conn.execute(
                        "SELECT status FROM events WHERE event_id = ? LIMIT 1;", (eid,)
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise ArchiveBlocked(f"Event {eid} not found in outbox")
                    if row["status"] == "acknowledged":
                        raise ArchiveBlocked(f"Event {eid} is already acknowledged")

                    # Check if already in another batch
                    cur_b = conn.execute(
                        """
                        SELECT b.stage FROM batch_events be
                        JOIN batches b ON be.batch_id = b.batch_id
                        WHERE be.event_id = ? LIMIT 1;
                        """,
                        (eid,),
                    )
                    existing_b = cur_b.fetchone()
                    if existing_b is not None:
                        raise ArchiveBlocked(
                            f"Event {eid} is already in batch (stage: {existing_b['stage']})"
                        )

                # Insert batch
                conn.execute(
                    """
                    INSERT INTO batches (batch_id, stage, metadata, created_at, updated_at)
                    VALUES (?, 'package_prepared', ?, ?, ?);
                    """,
                    (batch_id, meta_json, now_iso, now_iso),
                )

                # Insert batch_events links
                for order, eid in enumerate(event_ids):
                    conn.execute(
                        """
                        INSERT INTO batch_events (batch_id, event_id, event_order)
                        VALUES (?, ?, ?);
                        """,
                        (batch_id, eid, order),
                    )

                # Mark events as batched
                placeholders = ",".join("?" for _ in event_ids)
                conn.execute(
                    f"UPDATE events SET status = 'batched' WHERE event_id IN ({placeholders});",
                    event_ids,
                )
        except ArchiveBlocked:
            raise
        except sqlite3.OperationalError as e:
            raise ArchiveBlocked(f"Storage error during prepare: {e}") from e
        except sqlite3.Error as e:
            raise ArchiveBlocked(f"Database error during prepare: {e}") from e

    def batches(self) -> List[Dict[str, Any]]:
        """Return all batches ordered by insertion time."""
        conn = self._get_conn()
        try:
            cur = conn.execute(
                """
                SELECT batch_id, stage, metadata, created_at, updated_at
                FROM batches
                ORDER BY rowid ASC;
                """
            )
            rows = cur.fetchall()
            result: List[Dict[str, Any]] = []
            for row in rows:
                cur_ev = conn.execute(
                    """
                    SELECT event_id FROM batch_events
                    WHERE batch_id = ?
                    ORDER BY event_order ASC;
                    """,
                    (row["batch_id"],),
                )
                event_ids = [e_row["event_id"] for e_row in cur_ev.fetchall()]
                result.append(
                    {
                        "batch_id": row["batch_id"],
                        "stage": row["stage"],
                        "metadata": json.loads(row["metadata"]),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "event_ids": event_ids,
                    }
                )
            return result
        except sqlite3.Error as e:
            raise ArchiveBlocked(f"Database error during batches fetch: {e}") from e

    def advance(
        self, batch_id: str, stage: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Advance a batch monotonically along STAGES."""
        if stage not in STAGES:
            raise ArchiveBlocked(f"Unknown stage: '{stage}'. Allowed stages: {STAGES}")

        conn = self._get_conn()
        now_iso = _utc_now_iso()

        try:
            with conn:
                cur = conn.execute(
                    "SELECT stage, metadata FROM batches WHERE batch_id = ? LIMIT 1;",
                    (batch_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise ArchiveBlocked(f"Batch {batch_id} not found")

                current_stage = row["stage"]
                if current_stage == "acknowledged":
                    raise ArchiveBlocked(f"Batch {batch_id} is already acknowledged")

                curr_idx = STAGES.index(current_stage)
                next_idx = STAGES.index(stage)

                if next_idx != curr_idx + 1:
                    raise ArchiveBlocked(
                        f"Cannot advance batch {batch_id} from '{current_stage}' to '{stage}': "
                        f"stages must advance monotonically ({current_stage} -> {STAGES[curr_idx + 1]})"
                    )

                meta = json.loads(row["metadata"])
                if metadata:
                    meta.update(metadata)
                meta_json = json.dumps(meta, ensure_ascii=False)

                if stage == "acknowledged":
                    # Cannot advance to acknowledged directly via advance unless coming from catalog_verified
                    # Clean payloads and acknowledge
                    self._do_acknowledge(conn, batch_id, meta_json, now_iso)
                else:
                    conn.execute(
                        """
                        UPDATE batches
                        SET stage = ?, metadata = ?, updated_at = ?
                        WHERE batch_id = ?;
                        """,
                        (stage, meta_json, now_iso, batch_id),
                    )
        except ArchiveBlocked:
            raise
        except sqlite3.OperationalError as e:
            raise ArchiveBlocked(f"Storage error during advance: {e}") from e
        except sqlite3.Error as e:
            raise ArchiveBlocked(f"Database error during advance: {e}") from e

    def _do_acknowledge(
        self,
        conn: sqlite3.Connection,
        batch_id: str,
        meta_json: str,
        now_iso: str,
    ) -> None:
        """Helper to mark batch acknowledged and clean payload of associated events."""
        # Update batch status
        conn.execute(
            """
            UPDATE batches
            SET stage = 'acknowledged', metadata = ?, updated_at = ?
            WHERE batch_id = ?;
            """,
            (meta_json, now_iso, batch_id),
        )

        # Update events: set status = 'acknowledged', clean payload to '{}'
        conn.execute(
            """
            UPDATE events
            SET status = 'acknowledged', acknowledged_at = ?, payload = '{}'
            WHERE event_id IN (
                SELECT event_id FROM batch_events WHERE batch_id = ?
            );
            """,
            (now_iso, batch_id),
        )

    def ack(self, batch_id: str) -> None:
        """Acknowledge batch.

        Succeeds ONLY if current stage is 'catalog_verified'.
        Advances stage to 'acknowledged' and marks batched events as acknowledged,
        cleaning their payload while preserving event_id and metadata.
        """
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "SELECT stage FROM batches WHERE batch_id = ? LIMIT 1;", (batch_id,)
            )
            row = cur.fetchone()
            if row is None:
                raise ArchiveBlocked(f"Batch {batch_id} not found")

            current_stage = row["stage"]
            if current_stage != "catalog_verified":
                raise ArchiveBlocked(
                    f"Cannot ack batch {batch_id} at stage '{current_stage}': must be 'catalog_verified'"
                )

            self.advance(batch_id, "acknowledged")
        except ArchiveBlocked:
            raise
        except sqlite3.Error as e:
            raise ArchiveBlocked(f"Database error during ack: {e}") from e

    def has_unresolved_attempt(self, task_id: int) -> bool:
        """Return True if an offer attempt for task_id is pending or unknown."""
        conn = self._get_conn()
        try:
            cur = conn.execute(
                """
                SELECT COUNT(*) FROM unresolved_attempts
                WHERE task_id = ? AND status IN ('pending', 'unknown');
                """,
                (int(task_id),),
            )
            count = cur.fetchone()[0]
            return count > 0
        except sqlite3.Error as e:
            raise ArchiveBlocked(f"Database error checking unresolved attempt: {e}") from e

    def record_attempt(
        self, task_id: int, attempt_id: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Durable offer attempt record committed to SQLite BEFORE any external API call."""
        if not attempt_id or not isinstance(attempt_id, str):
            raise ArchiveBlocked("attempt_id must be a non-empty string")

        conn = self._get_conn()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        now_iso = _utc_now_iso()

        # Check budget
        self._check_budget_for_write(len(meta_json.encode("utf-8")) + 256)

        try:
            with conn:
                cur = conn.execute(
                    "SELECT 1 FROM unresolved_attempts WHERE attempt_id = ? LIMIT 1;",
                    (attempt_id,),
                )
                if cur.fetchone() is not None:
                    raise ArchiveBlocked(f"Attempt {attempt_id} already exists")

                conn.execute(
                    """
                    INSERT INTO unresolved_attempts (
                        attempt_id, task_id, status, metadata, result, created_at, resolved_at
                    ) VALUES (?, ?, 'pending', ?, NULL, ?, NULL);
                    """,
                    (attempt_id, int(task_id), meta_json, now_iso),
                )
        except ArchiveBlocked:
            raise
        except sqlite3.OperationalError as e:
            raise ArchiveBlocked(f"Storage error recording attempt: {e}") from e
        except sqlite3.Error as e:
            raise ArchiveBlocked(f"Database error recording attempt: {e}") from e

    def resolve_attempt(
        self, attempt_id: str, status: str, result: Optional[Dict[str, Any]] = None
    ) -> None:
        """Resolve attempt status.

        Status 'unknown' preserves durable blocking in has_unresolved_attempt.
        Status 'success' / 'failed' / 'resolved' clears the unresolved block.
        """
        conn = self._get_conn()
        result_json = json.dumps(result or {}, ensure_ascii=False)
        now_iso = _utc_now_iso()

        try:
            with conn:
                cur = conn.execute(
                    "SELECT 1 FROM unresolved_attempts WHERE attempt_id = ? LIMIT 1;",
                    (attempt_id,),
                )
                if cur.fetchone() is None:
                    raise ArchiveBlocked(f"Attempt {attempt_id} not found")

                conn.execute(
                    """
                    UPDATE unresolved_attempts
                    SET status = ?, result = ?, resolved_at = ?
                    WHERE attempt_id = ?;
                    """,
                    (str(status), result_json, now_iso, attempt_id),
                )
        except ArchiveBlocked:
            raise
        except sqlite3.OperationalError as e:
            raise ArchiveBlocked(f"Storage error resolving attempt: {e}") from e
        except sqlite3.Error as e:
            raise ArchiveBlocked(f"Database error resolving attempt: {e}") from e

    def close(self) -> None:
        """Close SQLite database connection if open."""
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            finally:
                self._conn = None

    def __enter__(self) -> Outbox:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
