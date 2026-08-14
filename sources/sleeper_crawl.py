"""Archive a league's full dynasty history from Sleeper - every season in the chain,
everything that season recorded. One JSON file per league-season under data/crawl/,
restart-safe (a season already on disk is skipped), gitignored.

This is the ground-truth corpus the roadmap's model era stands on: matchups carry
per-player points in the league's OWN scoring, transactions carry real trades with
dates, brackets carry who actually won. Nothing here analyzes - it only collects.

Smoke test: python -m sources.sleeper_crawl <league_id>
"""

import json
import sys
import time
from pathlib import Path

import requests

from .sleeper import BASE, get_season_chain

CRAWL_DIR = Path("data/crawl")
MAX_WEEK = 18
PAUSE_S = 0.1  # politeness between requests; Sleeper's API is free and unauthenticated


def _get(path: str):
    time.sleep(PAUSE_S)
    resp = requests.get(f"{BASE}{path}")
    resp.raise_for_status()
    return resp.json()


def crawl_season(league_id: str) -> dict:
    """Everything Sleeper keeps for one league-season, in one dict."""
    season = {
        "league": _get(f"/league/{league_id}"),
        "users": _get(f"/league/{league_id}/users"),
        "rosters": _get(f"/league/{league_id}/rosters"),
        "traded_picks": _get(f"/league/{league_id}/traded_picks"),
        "winners_bracket": _get(f"/league/{league_id}/winners_bracket"),
        "transactions": {},
        "matchups": {},
        "drafts": [],
    }
    for week in range(1, MAX_WEEK + 1):
        season["transactions"][str(week)] = _get(f"/league/{league_id}/transactions/{week}")
        season["matchups"][str(week)] = _get(f"/league/{league_id}/matchups/{week}")
    for draft in _get(f"/league/{league_id}/drafts"):
        draft["picks"] = _get(f"/draft/{draft['draft_id']}/picks")
        season["drafts"].append(draft)
    return season


def crawl_chain(league_id: str) -> list[Path]:
    """Walk the season chain and archive every season not already on disk."""
    CRAWL_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for season_id in get_season_chain(league_id):
        out = CRAWL_DIR / f"{season_id}.json"
        if out.exists():
            continue
        season = crawl_season(season_id)
        out.write_text(json.dumps(season), encoding="utf-8")
        written.append(out)
        lg = season["league"]
        n_trades = sum(1 for wk in season["transactions"].values()
                       for t in wk if t.get("type") == "trade")
        print(f"  {lg['season']} {lg['name']}: {len(season['rosters'])} teams, "
              f"{n_trades} trades, {len(season['drafts'])} draft(s)")
    return written


if __name__ == "__main__":
    written = crawl_chain(sys.argv[1])
    print(f"{len(written)} new season file(s) in {CRAWL_DIR}")
