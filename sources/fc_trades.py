"""FantasyCalc's real-trade stream - the empirical side of the trade question.

The /trades endpoint returns only the latest ~49 trades per stream (superflex and 1QB
are separate streams; every pagination and date parameter is ignored - probed), so
history has to be built by ACCUMULATING: each `accumulate()` call appends whatever is
new, deduped by trade id. Measured volume is ~500-700 trades/day per stream, so a few
polls a day builds a real dataset in weeks.

Records are trimmed before storing: each piece keeps name/position/sleeperId (enough
to join values - players by sleeperId via fantasycalc.get_players, picks by name via
get_pick_values), and the trade keeps its format fields plus maybeTradedValueDiff,
FantasyCalc's own value imbalance at trade time. The raw payload's dozen maybe-fields
per piece would multiply storage for nothing.

The dataset lives in data/fc_trades.jsonl, gitignored - it regrows anywhere by
polling, and a growing data file has no business in the repo's history.

Smoke test: python -m sources.fc_trades
"""

import json
import sys
from pathlib import Path

import requests

TRADES_URL = "https://api.fantasycalc.com/trades"
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "fc_trades.jsonl"

# (numQbs, isDynasty) per stream. Both dynasty streams; redraft trades answer a
# different question than this project asks.
STREAMS = ((2, True), (1, True))


def fetch_recent(num_qbs: int, is_dynasty: bool = True) -> list[dict]:
    resp = requests.get(TRADES_URL,
                        params={"isDynasty": str(is_dynasty).lower(), "numQbs": num_qbs},
                        headers={"User-Agent": "fantasy-fanatic (github.com/calebeurich)"},
                        timeout=30)
    resp.raise_for_status()
    return resp.json()


def _trim(raw: dict) -> dict:
    return {
        "id": raw["id"],
        "date": raw["date"],
        "numQbs": raw["numQbs"],
        "numTeams": raw["numTeams"],
        "ppr": raw["ppr"],
        "isDynasty": raw["isDynasty"],
        "tePremium": raw["tePremium"],
        "maybeTradedValueDiff": raw.get("maybeTradedValueDiff"),
        "maybeScore": raw.get("maybeScore"),
        "sides": [
            [{"name": p["name"], "position": p["position"], "sleeperId": p.get("sleeperId")}
             for p in raw[side]]
            for side in ("side1", "side2")
        ],
    }


def load(path: Path = DEFAULT_PATH) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def accumulate(path: Path = DEFAULT_PATH) -> dict:
    """Fetch every stream and append what's new. Safe to call as often as you like -
    dedupe is by trade id, so overlapping windows just contribute nothing."""
    seen = {t["id"] for t in load(path)}
    path.parent.mkdir(parents=True, exist_ok=True)
    new = 0
    with open(path, "a", encoding="utf-8") as f:
        for num_qbs, is_dynasty in STREAMS:
            for raw in fetch_recent(num_qbs, is_dynasty):
                if raw["id"] in seen:
                    continue
                seen.add(raw["id"])
                f.write(json.dumps(_trim(raw)) + "\n")
                new += 1
    return {"new": new, "total": len(seen), "path": str(path)}


if __name__ == "__main__":
    summary = accumulate()
    print(f"accumulated {summary['new']} new trades -> {summary['total']} total "
          f"({summary['path']})")
    trades = load()
    shapes = {}
    for t in trades:
        a, b = sorted(len(s) for s in t["sides"])
        shapes[f"{a}v{b}"] = shapes.get(f"{a}v{b}", 0) + 1
    print("shapes:", dict(sorted(shapes.items(), key=lambda kv: -kv[1])))
    with_picks = sum(1 for t in trades
                     if any(p["position"] == "PICK" for s in t["sides"] for p in s))
    print(f"{with_picks}/{len(trades)} include a pick")
