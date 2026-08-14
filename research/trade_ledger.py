"""Flatten every trade in the crawled corpus (data/crawl/, sources/sleeper_crawl.py)
into one dated ledger: when it happened, which league-season, and what each side gave.

Raw material only - player_ids stay ids (joins to values/names happen in the studies
that need them), and nothing here judges a trade. The date is what the accumulated
FantasyCalc stream never had: with it, every trade can be priced point-in-time against
the DP archive, and the deadline-rental hypothesis becomes testable.

Run: python -m research.trade_ledger
"""

import json
from datetime import datetime, timezone
from pathlib import Path

CRAWL_DIR = Path("data/crawl")


def trades() -> list[dict]:
    rows = []
    for f in sorted(CRAWL_DIR.glob("*.json")):
        season = json.loads(f.read_text(encoding="utf-8"))
        league = season["league"]
        for week, txns in season["transactions"].items():
            for t in txns:
                if t.get("type") != "trade" or t.get("status") != "complete":
                    continue
                sides = {rid: {"players": [], "picks": []} for rid in t["roster_ids"]}
                for pid, rid in (t.get("adds") or {}).items():
                    sides[rid]["players"].append(pid)
                for pk in t.get("draft_picks") or []:
                    sides[pk["owner_id"]]["picks"].append(
                        {"season": pk["season"], "round": pk["round"]})
                rows.append({
                    "league_id": league["league_id"],
                    "league_name": league["name"],
                    "season": league["season"],
                    "week": int(week),
                    "date": datetime.fromtimestamp(
                        t["status_updated"] / 1000, tz=timezone.utc).date().isoformat(),
                    "sides": sides,
                })
    return rows


if __name__ == "__main__":
    rows = trades()
    by_season = {}
    for r in rows:
        key = (r["league_name"], r["season"])
        by_season[key] = by_season.get(key, 0) + 1
    for (name, season), n in sorted(by_season.items(), key=lambda kv: kv[0][1]):
        print(f"{season} {name}: {n} trades")
    with_picks = sum(1 for r in rows
                     if any(s["picks"] for s in r["sides"].values()))
    months = {}
    for r in rows:
        months[r["date"][5:7]] = months.get(r["date"][5:7], 0) + 1
    print(f"\n{len(rows)} trades total, {with_picks} involve picks "
          f"({100 * with_picks // max(len(rows), 1)}%)")
    print("by calendar month:", dict(sorted(months.items())))
