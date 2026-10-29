"""The funnel ledger: one append-only record per decision.

`state.json` is a cache with a 14-day TTL and the run log is prose. Neither can
answer "of the tasks I saw, how many did I filter out, how many offers did the
platform actually accept, and how many refusals were about money?" — so the
counts live here, separated by event type rather than collapsed into
`offers_skipped`.

A refusal is not an error and an ambiguous response is not a success. The
classes below are the only ones allowed into the ledger.
"""
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

# Texts the platform uses when it wants money. Matching is coarse on purpose:
# a misclassification here must still land in a refusal class, never in
# `confirmed_success`.
BALANCE_MARKERS = ("баланс", "balance", "пополн")
ESCROW_MARKERS = ("сделк", "без риска", "escrow")


def classify_offer_result(result: Dict[str, Any]) -> str:
    """Map an offer API result to one ledger class.

    `confirmed_success` is reserved for an explicit `IsSuccess=true` reply,
    which `OfferManager.submit_postpaid_offer` already requires before it sets
    ``success``. Everything else is a refusal, an error, or unknown.
    """
    if result.get("dry_run"):
        return "dry_run"
    if result.get("success"):
        return "confirmed_success"

    text = " ".join(
        str(result.get(key) or "") for key in ("error", "reason", "status")
    ).lower()
    if any(marker in text for marker in BALANCE_MARKERS):
        return "refused_balance"
    if any(marker in text for marker in ESCROW_MARKERS):
        return "refused_escrow_only"
    if result.get("status") == "failed":
        return "error"
    return "unknown"


class Funnel:
    """Append-only JSONL ledger. Disabled instances record nothing."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def record(self, event: str, **fields: Any) -> Optional[Dict[str, Any]]:
        if self.path is None:
            return None
        row: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "event": event,
        }
        row.update({k: v for k, v in fields.items() if v is not None})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row


def load(path: Path) -> list:
    """Read the ledger, skipping a torn final line."""
    rows = []
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return []
    return rows


def summarize(path: Path) -> Dict[str, Dict[str, int]]:
    """Count events, and event outcome fields where present.

    Returns ``{"task_seen": {"total": 12}, "offer_result": {"confirmed_success": 1,
    "refused_balance": 4}}``.
    """
    counts: Dict[str, Dict[str, int]] = {}
    for row in load(path):
        event = row.get("event") or "unknown"
        bucket = counts.setdefault(event, {"total": 0})
        bucket["total"] += 1
        outcome = row.get("status") or row.get("result") or row.get("decision")
        if outcome is not None:
            key = str(outcome)
            bucket[key] = bucket.get(key, 0) + 1
    return counts
