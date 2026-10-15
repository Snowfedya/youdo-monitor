"""Deterministic packaging, byte verification, and event partitioning for YouDo archive."""

from __future__ import annotations

import gzip
import hashlib
import json
from typing import Any, Dict, List, Tuple

SCHEMA_VERSION: int = 1
DEFAULT_TARGET_BYTES: int = 1024 * 1024  # 1 MiB (1,048,576 bytes)


def _dump_canonical_json(obj: Any) -> str:
    """Format dictionary or object as canonical UTF-8 JSON.

    Enforces sorted keys, no extra whitespace around separators, no NaN/Infinity,
    and direct UTF-8 encoding (ensure_ascii=False).
    """
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def build_package(batch_id: str, events: List[Dict[str, Any]]) -> Tuple[bytes, Dict[str, Any]]:
    """Build a deterministic, reproducible compressed gzip package from events.

    Stream layout:
      - Line 0: JSON manifest {"batch_id": ..., "event_count": ..., "schema_version": 1}
      - Lines 1..N: One line per event in canonical JSON.

    Compression:
      Uses gzip.compress(..., mtime=0) to ensure byte-for-byte reproducible output.

    Returns:
      (compressed_bytes, metadata_dict)
    """
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ValueError("batch_id must be a non-empty string")
    if not isinstance(events, list):
        raise ValueError("events must be a list of dictionaries")
    if not events:
        raise ValueError("Cannot package empty events")

    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            raise ValueError(f"Event at index {i} must be a dictionary")

    manifest = {
        "batch_id": batch_id.strip(),
        "event_count": len(events),
        "schema_version": SCHEMA_VERSION,
    }

    manifest_line = _dump_canonical_json(manifest)
    event_lines = [_dump_canonical_json(ev) for ev in events]

    uncompressed_text = "\n".join([manifest_line] + event_lines) + "\n"
    uncompressed_bytes = uncompressed_text.encode("utf-8")

    compressed_bytes = gzip.compress(uncompressed_bytes, mtime=0)
    sha256_hex = hashlib.sha256(compressed_bytes).hexdigest()

    metadata: Dict[str, Any] = {
        "batch_id": batch_id.strip(),
        "event_count": len(events),
        "schema_version": SCHEMA_VERSION,
        "sha256": sha256_hex,
        "compressed_bytes": len(compressed_bytes),
        "uncompressed_bytes": len(uncompressed_bytes),
    }

    return compressed_bytes, metadata


def verify_package(
    blob: bytes,
    *,
    expected_sha256: str,
    expected_count: int,
) -> List[Dict[str, Any]]:
    """Verify package SHA-256, decompress, validate manifest, and return parsed events.

    Raises ValueError if:
      - SHA-256 of blob does not match expected_sha256
      - blob is not valid gzip data
      - Decompressed payload is not valid UTF-8
      - Manifest is missing or corrupted
      - Manifest schema_version is not supported
      - Event count does not match expected_count or manifest["event_count"]
      - Any event line is corrupted or not a valid JSON object
    """
    if not isinstance(blob, (bytes, bytearray)):
        raise ValueError("blob must be bytes or bytearray")
    if not isinstance(expected_sha256, str):
        raise ValueError("expected_sha256 must be a string")
    if not isinstance(expected_count, int) or expected_count < 0:
        raise ValueError("expected_count must be a non-negative integer")

    actual_sha256 = hashlib.sha256(blob).hexdigest()
    if actual_sha256.lower() != expected_sha256.lower():
        raise ValueError(f"SHA-256 mismatch: expected {expected_sha256}, got {actual_sha256}")

    try:
        uncompressed_bytes = gzip.decompress(blob)
    except Exception as e:
        raise ValueError(f"Corrupted or invalid gzip package: {e}") from e

    try:
        uncompressed_text = uncompressed_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"Corrupted UTF-8 payload: {e}") from e

    if not uncompressed_text:
        raise ValueError("Package payload is empty")

    lines = uncompressed_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    if not lines:
        raise ValueError("Package has no content lines")

    # Line 0 is manifest
    try:
        manifest = json.loads(lines[0])
    except Exception as e:
        raise ValueError(f"Invalid manifest JSON: {e}") from e

    if not isinstance(manifest, dict):
        raise ValueError("Manifest is not a JSON object")
    if (
        "schema_version" not in manifest
        or "batch_id" not in manifest
        or "event_count" not in manifest
    ):
        raise ValueError("Manifest missing required keys: schema_version, batch_id, event_count")

    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"Unsupported schema version: {manifest['schema_version']}")

    manifest_count = manifest["event_count"]
    if not isinstance(manifest_count, int):
        raise ValueError("Manifest event_count must be an integer")

    event_lines = lines[1:]
    if len(event_lines) != expected_count:
        raise ValueError(
            f"Event count mismatch: expected {expected_count}, got {len(event_lines)}"
        )
    if len(event_lines) != manifest_count:
        raise ValueError(
            f"Manifest count mismatch: manifest says {manifest_count}, found {len(event_lines)}"
        )

    events: List[Dict[str, Any]] = []
    for idx, line in enumerate(event_lines, start=1):
        try:
            ev = json.loads(line)
        except Exception as e:
            raise ValueError(f"Invalid event JSON on line {idx}: {e}") from e
        if not isinstance(ev, dict):
            raise ValueError(f"Event on line {idx} is not a JSON object")
        events.append(ev)

    return events


def partition_events(
    events: List[Dict[str, Any]],
    target_bytes: int = DEFAULT_TARGET_BYTES,
) -> List[List[Dict[str, Any]]]:
    """Partition events into sublists where total uncompressed JSON size <= target_bytes.

    If a single event exceeds target_bytes, it is placed in its own single-item list
    without error or truncation.
    Preserves original order, drops zero events, creates zero duplicates.
    """
    if not isinstance(events, list):
        raise ValueError("events must be a list of dictionaries")
    if not isinstance(target_bytes, int) or target_bytes <= 0:
        raise ValueError("target_bytes must be a positive integer")
    if not events:
        return []

    for i, ev in enumerate(events):
        if not isinstance(ev, dict):
            raise ValueError(f"Event at index {i} must be a dictionary")

    partitions: List[List[Dict[str, Any]]] = []
    current_partition: List[Dict[str, Any]] = []
    current_bytes: int = 0

    for ev in events:
        ev_bytes = len(_dump_canonical_json(ev).encode("utf-8"))

        # Oversized event: place in its own single-item partition
        if ev_bytes > target_bytes:
            if current_partition:
                partitions.append(current_partition)
                current_partition = []
                current_bytes = 0
            partitions.append([ev])
            continue

        # If adding event exceeds target_bytes, flush current partition
        if current_partition and (current_bytes + ev_bytes > target_bytes):
            partitions.append(current_partition)
            current_partition = [ev]
            current_bytes = ev_bytes
        else:
            current_partition.append(ev)
            current_bytes += ev_bytes

    if current_partition:
        partitions.append(current_partition)

    return partitions
