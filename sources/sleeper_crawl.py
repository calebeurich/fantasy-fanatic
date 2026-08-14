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


def snowball(seed_league_ids: list[str], max_chains: int = 60) -> None:
    """Widen the corpus one hop: every manager in the seed leagues, every dynasty
    league those managers play in, full chains. All public Sleeper data, stored only
    in the gitignored data/ dir. 13 own-league seasons is not a dataset."""
    user_ids = []
    for lid in seed_league_ids:
        for u in _get(f"/league/{lid}/users"):
            if u["user_id"] not in user_ids:
                user_ids.append(u["user_id"])
    print(f"{len(user_ids)} managers across {len(seed_league_ids)} seed leagues")

    chains = []
    for uid in user_ids:
        for year in ("2026", "2025", "2024"):
            for lg in _get(f"/user/{uid}/leagues/nfl/{year}") or []:
                if lg["settings"]["type"] == 2 and lg["league_id"] not in chains:
                    chains.append(lg["league_id"])
    print(f"{len(chains)} dynasty leagues discovered; crawling up to {max_chains}")

    for i, lid in enumerate(chains[:max_chains]):
        try:
            crawl_chain(lid)
        except requests.HTTPError as e:
            print(f"  skipped {lid}: {e}")
        if (i + 1) % 10 == 0:
            print(f"  ...{i + 1}/{min(len(chains), max_chains)} chains done, "
                  f"{len(list(CRAWL_DIR.glob('*.json')))} season files on disk")


if __name__ == "__main__":
    if sys.argv[1] == "snowball":
        snowball(sys.argv[2].split(","),
                 int(sys.argv[3]) if len(sys.argv) > 3 else 60)
    else:
        written = crawl_chain(sys.argv[1])
        print(f"{len(written)} new season file(s) in {CRAWL_DIR}")
