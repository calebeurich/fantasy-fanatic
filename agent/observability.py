"""Structured per-question observability log - one JSON record per run_query call.

Emitted two ways, because the two environments need different things:
- **stdout**, always. Cloud Run pipes stdout straight into Cloud Logging, so this is
  what makes the log durable and queryable when hosted, for free, with no client
  library or extra service.
- **a local JSONL file**, only when LOG_TO_FILE is on (the default locally, off when
  running on Cloud Run). A hosted container's filesystem is in-memory tmpfs: writing
  there would quietly consume the memory limit and then vanish entirely when the
  instance scales to zero - so the file half is genuinely local-only, not just
  redundant.

Plain JSONL over SQLite, deliberately: no schema, no migrations, and still trivially
summarizable (see log_summary.py) at the scale this project actually runs at.
"""

import json
import os
import sys
import time
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "agent_runs.jsonl"

# Cloud Run always sets K_SERVICE. Using it as the signal means there's no separate
# config to remember to flip at deploy time - the environment identifies itself.
IS_CLOUD_RUN = bool(os.environ.get("K_SERVICE"))
LOG_TO_FILE = not IS_CLOUD_RUN


def log_run(record: dict) -> None:
    entry = {"timestamp": time.time(), **record}
    line = json.dumps(entry)

    # stdout first, and never let a file-write problem lose the record entirely -
    # a read-only or full filesystem shouldn't take the observability with it.
    print(line, file=sys.stdout, flush=True)

    if not LOG_TO_FILE:
        return
    try:
        LOG_PATH.parent.mkdir(exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"[observability] could not write log file: {e}", file=sys.stderr, flush=True)


def read_runs() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    with LOG_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
