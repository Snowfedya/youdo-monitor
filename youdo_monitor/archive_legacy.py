"""Historical monitoring state import into archive outbox without profile leakage.

Reads legacy state.json files read-only and emits canonical archive events
with source="legacy_state" and completeness="partial". Strictly filters
any private API profile information (CreatorInfo, UserProfile, phones, emails).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Union

from youdo_monitor.archive_events import make_event, offer_payload, task_payload


def import_state(path: Union[Path, str], outbox: Any) -> Dict[str, int]:
    """Import historical state from state.json into the durable archive outbox.

    Enforces:
    - Input file is read-only and remains byte-identical.
    - Malformed or corrupt JSON raises explicit ValueError or json.JSONDecodeError.
    - source is strictly 'legacy_state' and completeness is 'partial'.
    - Missing budget, description, and outcomes are preserved as None.
    - No private profile objects, phones, emails, or tokens are leaked into events.
    - Unconfirmed offer status without successful result does not produce an API-confirmed result.
    - Deterministic event IDs guarantee idempotent re-imports (inserted == 0).

    Returns:
        {
            "tasks_scanned": int,
            "events_generated": int,
            "inserted": int,
            "skipped_duplicates": int,
        }
    """
    path_obj = Path(path)
    if not path_obj.exists():
        raise FileNotFoundError(f"State file not found: {path}")

    # Read-only access to preserve source file bytes exactly
    raw_text = path_obj.read_text(encoding="utf-8")
    data = json.loads(raw_text)

    if not isinstance(data, dict):
        raise ValueError(f"State file must contain a JSON object, got {type(data).__name__}")

    seen_tasks = data.get("seen_tasks")
    if seen_tasks is None:
        seen_tasks = {}
    elif not isinstance(seen_tasks, dict):
        raise ValueError("seen_tasks must be a dictionary")

    tasks_scanned = 0
    events_generated = 0
    inserted = 0
    skipped_duplicates = 0

    # Sort tasks deterministically for reproducible event order
    sorted_tasks = sorted(
        seen_tasks.items(),
        key=lambda item: (0, int(item[0])) if str(item[0]).isdigit() else (1, str(item[0])),
    )

    source = "legacy_state"
    completeness = "partial"

    for raw_task_id, task_data in sorted_tasks:
        if not isinstance(task_data, dict):
            continue

        try:
            task_id = int(raw_task_id)
        except (ValueError, TypeError):
            continue

        tasks_scanned += 1

        # Timestamp conversion to UTC ISO 8601
        seen_at = task_data.get("seen_at")
        if isinstance(seen_at, (int, float)):
            observed_at = datetime.fromtimestamp(float(seen_at), tz=timezone.utc).isoformat()
        elif isinstance(seen_at, str):
            try:
                observed_at = datetime.fromtimestamp(float(seen_at), tz=timezone.utc).isoformat()
            except ValueError:
                observed_at = seen_at
        else:
            observed_at = datetime.fromtimestamp(0, tz=timezone.utc).isoformat()

        extra = task_data.get("extra")
        if not isinstance(extra, dict):
            extra = {}

        status = str(task_data.get("status") or "")

        events_to_enqueue: List[Dict[str, Any]] = []

        # 1. task_observed event (allowlist only; description and budget remain None)
        title = extra.get("title")
        obs_payload = task_payload({"id": task_id, "title": title}, None)
        obs_event = make_event(
            kind="task_observed",
            task_id=task_id,
            payload=obs_payload,
            observed_at=observed_at,
            source=source,
            completeness=completeness,
        )
        events_to_enqueue.append(obs_event)

        # 2. classification event (matched or rejected)
        if status == "matched":
            cls_payload = {
                "observation_id": obs_event["event_id"],
                "passed": True,
                "reason": "matched",
            }
            cls_event = make_event(
                kind="classification",
                task_id=task_id,
                payload=cls_payload,
                observed_at=observed_at,
                source=source,
                completeness=completeness,
            )
            events_to_enqueue.append(cls_event)
        elif status.startswith("rejected:"):
            reason = status.split(":", 1)[1]
            cls_payload = {
                "observation_id": obs_event["event_id"],
                "passed": False,
                "reason": reason,
            }
            cls_event = make_event(
                kind="classification",
                task_id=task_id,
                payload=cls_payload,
                observed_at=observed_at,
                source=source,
                completeness=completeness,
            )
            events_to_enqueue.append(cls_event)
        elif status == "rejected":
            cls_payload = {
                "observation_id": obs_event["event_id"],
                "passed": False,
                "reason": "rejected",
            }
            cls_event = make_event(
                kind="classification",
                task_id=task_id,
                payload=cls_payload,
                observed_at=observed_at,
                source=source,
                completeness=completeness,
            )
            events_to_enqueue.append(cls_event)

        # 3. offer_attempt & offer_result
        result = extra.get("result")
        has_result = isinstance(result, dict) and bool(result)
        is_offered = status.startswith("offered") or has_result

        if is_offered:
            # Pitch priority: ResultObject.Offer.Description > extra.pitch
            pitch = None
            if isinstance(result, dict):
                resp = result.get("response") or {}
                ro = resp.get("ResultObject") or result.get("ResultObject") or {}
                if isinstance(ro, dict):
                    offer = ro.get("Offer") or {}
                    if isinstance(offer, dict):
                        desc = offer.get("Description") or offer.get("description")
                        if desc:
                            pitch = str(desc)
            if not pitch and extra.get("pitch") is not None:
                pitch = str(extra["pitch"])

            # Price & Mode
            price = extra.get("price")
            if price is None and isinstance(result, dict):
                price = result.get("price")
            if price is not None:
                try:
                    price = int(price)
                except (ValueError, TypeError):
                    price = None

            mode = extra.get("mode") or "postpaid"

            # Offer ID
            offer_id = None
            if isinstance(result, dict):
                safe_off = offer_payload(result)
                offer_id = safe_off.get("offer_id")
            if offer_id is None and extra.get("offer_id") is not None:
                try:
                    offer_id = int(extra["offer_id"])
                except (ValueError, TypeError):
                    offer_id = None

            # Stable attempt identity: legacy:<task_id>:<offer_id-or-seen_at>
            if offer_id is not None:
                offer_id_or_seen_at = str(offer_id)
            else:
                offer_id_or_seen_at = str(seen_at) if seen_at is not None else "0"
            identity = f"legacy:{task_id}:{offer_id_or_seen_at}"

            attempt_payload = {
                "attempt_id": identity,
                "mode": mode,
                "pitch": pitch,
                "price": price,
            }
            attempt_event = make_event(
                kind="offer_attempt",
                task_id=task_id,
                payload=attempt_payload,
                observed_at=observed_at,
                source=source,
                completeness=completeness,
                identity=identity,
            )
            events_to_enqueue.append(attempt_event)

            # offer_result is ONLY generated if result exists and is non-empty
            if has_result and isinstance(result, dict):
                safe = offer_payload(result)
                safe["attempt_id"] = identity
                if safe.get("task_id") is None:
                    safe["task_id"] = task_id
                if safe.get("price") is None and price is not None:
                    safe["price"] = price

                if result.get("status") == "unknown" or result.get("unknown"):
                    status_str = "unknown"
                elif safe.get("success"):
                    status_str = "success"
                else:
                    status_str = "failed"
                safe["status"] = status_str

                res_event = make_event(
                    kind="offer_result",
                    task_id=task_id,
                    payload=safe,
                    observed_at=observed_at,
                    source=source,
                    completeness=completeness,
                    identity=identity,
                )
                events_to_enqueue.append(res_event)

        # Enqueue all generated events into outbox
        for ev in events_to_enqueue:
            events_generated += 1
            if outbox.enqueue(ev):
                inserted += 1
            else:
                skipped_duplicates += 1

    return {
        "tasks_scanned": tasks_scanned,
        "events_generated": events_generated,
        "inserted": inserted,
        "skipped_duplicates": skipped_duplicates,
    }
