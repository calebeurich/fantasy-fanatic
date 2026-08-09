"""Structured per-question observability log - one JSON line appended per
run_query call. Plain JSONL over SQLite, deliberately: no schema, no migrations,
and still trivially summarizable (see log_summary.py) at the scale this project
actually runs at - see LOGIC.md for the reasoning.

The log directory is gitignored - this is real local usage data, not code.
"""

import json
import time
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "agent_runs.jsonl"


def log_run(record: dict) -> None:
    LOG_PATH.parent.mkdir(exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"timestamp": time.time(), **record}) + "\n")


def read_runs() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    with LOG_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
