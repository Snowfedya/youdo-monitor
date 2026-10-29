"""Backfill the funnel ledger from historical run.log offer responses.

The ledger only starts counting from the moment it exists, and the question it
exists to answer ("how much of the spend was refused for lack of money") is
already answerable from the log. Rows are marked ``source="backfill:run.log"``
so they never pass for live observations, and timestamps come from the log
line, not from now.
"""
import ast
import re
import sys
from pathlib import Path
from typing import Iterator, Tuple

from youdo_monitor.funnel import Funnel, classify_offer_result

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+\[INFO\].*?"
    r"Offer API response for task (?P<task>\d+): (?P<payload>\{.*\})\s*$"
)


def parse_run_log(path: Path) -> Iterator[Tuple[str, int, dict]]:
    """Yield (timestamp, task_id, offer_result) for every logged API response."""
    with Path(path).open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            match = LINE_RE.match(line.strip())
            if not match:
                continue
            try:
                payload = ast.literal_eval(match.group("payload"))
            except (ValueError, SyntaxError):
                continue
            data = payload.get("data") or {}
            message = data.get("Message")
            if not message:
                result_object = data.get("ResultObject")
                if isinstance(result_object, dict):
                    message = result_object.get("Message")
            yield match.group("ts"), int(match.group("task")), {
                "success": payload.get("ok") is True and data.get("IsSuccess") is True,
                "status": "failed" if data.get("IsSuccess") is False else "unknown",
                "error": message,
            }


def backfill(log_path: Path, funnel_path: Path) -> dict:
    funnel = Funnel(funnel_path)
    counts: dict = {}
    for ts, task_id, result in parse_run_log(log_path):
        outcome = classify_offer_result(result)
        counts[outcome] = counts.get(outcome, 0) + 1
        funnel.record(
            "offer_result",
            ts=ts,
            task_id=task_id,
            result=outcome,
            source="backfill:run.log",
        )
    return counts


def main(argv: list) -> int:
    log_path = Path(argv[1] if len(argv) > 1 else "data/run.log")
    funnel_path = Path(argv[2] if len(argv) > 2 else "data/funnel.jsonl")
    if not log_path.exists():
        print(f"No log at {log_path}", file=sys.stderr)
        return 1
    counts = backfill(log_path, funnel_path)
    for outcome, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"{outcome:24} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
