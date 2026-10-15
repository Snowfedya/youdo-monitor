"""Pseudonymized training dataset export and direct contact scrubbing for YouDo archive.

Transforms internal monitoring events into an allowlisted, pseudonymized dataset
suitable for offline machine learning model training without exposing private contact info.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Union

DEFAULT_KEY_PATH: Path = Path("/root/youdo-monitor/data/archive_export.key")
PRIVACY_NOTICE: str = (
    "Direct identifiers and contacts redacted via allowlist and pattern scrubbing. "
    "Residual indirect re-identification risk in free text cannot be mathematically excluded."
)

# Ordered regex patterns for direct contact scrubbing
_REDACTION_PATTERNS = [
    # 1. Bearer tokens, API keys, JWTs
    (
        re.compile(
            r"(?i)\b(?:Bearer\s+|api[_-]?key\s*[:=]\s*[\"']?|access[_-]?token\s*[:=]\s*[\"']?|"
            r"auth[_-]?token\s*[:=]\s*[\"']?|secret[_-]?key\s*[:=]\s*[\"']?|"
            r"private[_-]?key\s*[:=]\s*[\"']?)[A-Za-z0-9_\-\.+=/]{8,}[\"']?"
        ),
        "[REDACTED_TOKEN]",
    ),
    (
        re.compile(
            r"\b(?:sk-[a-zA-Z0-9_\-]{16,}|ghp_[a-zA-Z0-9]{20,}|"
            r"eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,})\b"
        ),
        "[REDACTED_TOKEN]",
    ),
    # 2. URLs (http, https, www)
    (
        re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"'`;]+(?<![\.,?!:])"),
        "[REDACTED_URL]",
    ),
    # 3. Emails
    (
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "[REDACTED_EMAIL]",
    ),
    # 4. Russian phone numbers (+7/8 with spaces, parens, dashes, dots)
    (
        re.compile(
            r"(?:(?:\+7|8)[\s.-]?(?:\(\s*\d{3}\s*\)|\d{3})|\(\s*\d{3}\s*\))[\s.-]?(?:\d[\s.-]?){6}\d\b"
        ),
        "[REDACTED_PHONE]",
    ),
    (
        re.compile(r"\b9\d{2}[-.\s]\d{3}[-.\s]\d{2}[-.\s]\d{2}\b"),
        "[REDACTED_PHONE]",
    ),
]


def redact_text(text: Optional[str]) -> str:
    """Scrub direct contact identifiers and auth tokens from text while preserving safe content."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)

    scrubbed = text
    for pattern, repl in _REDACTION_PATTERNS:
        scrubbed = pattern.sub(repl, scrubbed)
    return scrubbed


def get_or_create_export_key(
    key_path: Union[Path, str] = DEFAULT_KEY_PATH,
) -> bytes:
    """Read durable 32-byte export key or generate a new one with 0600 permissions."""
    path = Path(key_path).resolve()
    if path.is_file():
        raw = path.read_bytes()
        if len(raw) < 32:
            raise ValueError(
                f"Export key at {path} contains less than 32 bytes ({len(raw)} bytes)"
            )
        return raw[:32]

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    new_key = os.urandom(32)

    # Atomically write with 0600 file mode
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, new_key)
    finally:
        os.close(fd)

    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass

    return new_key


def pseudonymize_id(raw_id: Union[int, str], namespace: str, key: bytes) -> str:
    """Derive deterministic truncated HMAC SHA-256 pseudonym for an entity ID within a namespace."""
    if not isinstance(key, (bytes, bytearray)):
        raise TypeError(f"key must be bytes, got {type(key).__name__}")
    if raw_id is None:
        raise ValueError("raw_id cannot be None")

    clean_ns = str(namespace).strip()
    data = f"{clean_ns}:{raw_id}".encode("utf-8")
    hash16 = hmac.new(key, data, hashlib.sha256).hexdigest()[:16]
    return f"ps_{clean_ns}_{hash16}"


def _canonical_json(obj: Any) -> str:
    """Canonical JSON encoding with sorted keys and tight separators."""
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def export_events(
    events: List[Dict[str, Any]],
    output_path: Optional[Union[Path, str]] = None,
    *,
    key: bytes,
    output: Optional[Union[Path, str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Export pseudonymized, redacted dataset to .jsonl or .jsonl.gz with line-0 manifest."""
    target_path = output_path or output
    if target_path is None:
        raise ValueError("output_path must be specified")

    if not isinstance(key, (bytes, bytearray)):
        raise TypeError(f"key must be bytes, got {type(key).__name__}")
    if len(key) < 32:
        raise ValueError(f"key must be at least 32 bytes, got {len(key)}")

    out = Path(target_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    transformed_events: List[Dict[str, Any]] = []

    for ev in events:
        if not isinstance(ev, dict):
            continue

        raw_payload = ev.get("payload")
        payload: Dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}

        # 1. Resolve task_id
        raw_task_id = ev.get("task_id")
        if raw_task_id is None:
            raw_task_id = payload.get("task_id")
        if raw_task_id is None:
            raw_task_id = ev.get("id")

        if raw_task_id is None:
            continue

        item: Dict[str, Any] = {
            "pseudo_task_id": pseudonymize_id(raw_task_id, "task", key),
        }

        # 2. Attempt ID
        raw_attempt_id = ev.get("attempt_id") or payload.get("attempt_id")
        if raw_attempt_id is not None:
            pseudo_attempt = pseudonymize_id(raw_attempt_id, "attempt", key)
            item["pseudo_attempt_id"] = pseudo_attempt
            item["attempt_id"] = pseudo_attempt

        # 3. Offer ID
        raw_offer_id = ev.get("offer_id") or payload.get("offer_id")
        if raw_offer_id is not None:
            pseudo_offer = pseudonymize_id(raw_offer_id, "offer", key)
            item["pseudo_offer_id"] = pseudo_offer
            item["offer_id"] = pseudo_offer

        # 4. Structured fields
        if "kind" in ev:
            item["kind"] = str(ev["kind"])
        elif "kind" in payload:
            item["kind"] = str(payload["kind"])

        if "observed_at" in ev:
            item["observed_at"] = str(ev["observed_at"])
        elif "observed_at" in payload:
            item["observed_at"] = str(payload["observed_at"])

        completeness = ev.get("completeness") or payload.get("completeness")
        if completeness is not None:
            item["completeness"] = str(completeness)

        category = ev.get("category") or payload.get("category")
        if category is not None:
            item["category"] = str(category)

        budget = ev.get("budget") or payload.get("budget")
        if budget is not None:
            item["budget"] = redact_text(str(budget)) if isinstance(budget, str) else budget

        price = ev.get("price") or payload.get("price")
        if price is not None:
            item["price"] = price

        mode = ev.get("mode") or payload.get("mode")
        if mode is not None:
            item["mode"] = str(mode)

        # Classification decisions
        decision_passed = (
            ev.get("decision_passed")
            if "decision_passed" in ev
            else (
                payload.get("decision_passed")
                if "decision_passed" in payload
                else payload.get("passed")
            )
        )
        if decision_passed is not None:
            item["decision_passed"] = bool(decision_passed)

        decision_reason = (
            ev.get("decision_reason")
            if "decision_reason" in ev
            else (
                payload.get("decision_reason")
                if "decision_reason" in payload
                else payload.get("reason")
            )
        )
        if decision_reason is not None:
            item["decision_reason"] = redact_text(str(decision_reason))

        decision_tags = (
            ev.get("decision_tags")
            if "decision_tags" in ev
            else (
                payload.get("decision_tags")
                if "decision_tags" in payload
                else payload.get("tags")
            )
        )
        if decision_tags is not None and isinstance(decision_tags, (list, tuple, set)):
            item["decision_tags"] = list(decision_tags)

        # 5. Free-text fields (strictly scrubbed)
        title = ev.get("title") or payload.get("title") or ev.get("name")
        if title is not None:
            item["title"] = redact_text(str(title))

        description = ev.get("description") or payload.get("description")
        if description is not None:
            item["description"] = redact_text(str(description))

        pitch = ev.get("pitch") or payload.get("pitch")
        if pitch is not None:
            item["pitch"] = redact_text(str(pitch))

        note = ev.get("note") or payload.get("note")
        if note is not None:
            item["note"] = redact_text(str(note))

        transformed_events.append(item)

    # Line 1..N: Transformed events
    event_lines = [_canonical_json(item) for item in transformed_events]
    events_raw = "\n".join(event_lines).encode("utf-8") if event_lines else b""
    events_sha256 = hashlib.sha256(events_raw).hexdigest()

    # Line 0: Manifest
    manifest: Dict[str, Any] = {
        "export_version": 1,
        "event_count": len(transformed_events),
        "sha256": events_sha256,
        "privacy_notice": PRIVACY_NOTICE,
    }
    if metadata and isinstance(metadata, dict):
        manifest["metadata"] = metadata

    manifest_line = _canonical_json(manifest)
    full_text = "\n".join([manifest_line] + event_lines) + "\n"
    uncompressed_bytes = full_text.encode("utf-8")

    # Compression handling
    if str(out).endswith(".gz"):
        file_bytes = gzip.compress(uncompressed_bytes, mtime=0)
    else:
        file_bytes = uncompressed_bytes

    out.write_bytes(file_bytes)
    file_sha256 = hashlib.sha256(file_bytes).hexdigest()

    return {
        "events_exported": len(transformed_events),
        "output_path": str(out),
        "sha256": file_sha256,
        "size_bytes": len(file_bytes),
    }
