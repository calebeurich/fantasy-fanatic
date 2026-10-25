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


def crawl_chain(league_id: str, out_dir: Path = CRAWL_DIR) -> list[Path]:
    """Walk the season chain and archive every season not already on disk. Written to a
    temp file and renamed, so an analysis reading the folder mid-crawl never sees half a file."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for season_id in get_season_chain(league_id):
        out = out_dir / f"{season_id}.json"
        if out.exists():
            continue
        season = crawl_season(season_id)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(season), encoding="utf-8")
        tmp.replace(out)
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


def deep(seed_league_ids: list[str], min_seasons: int = 5, max_chains: int = 400,
         max_users: int = 600, hops: int = 2) -> None:
    """Two hops out, keeping only dynasty leagues with min_seasons+ seasons of history: a
    rebuild pays off over 2-3 seasons, and the one-hop corpus had 13 leagues with four
    straight completed seasons (owner, 2026-10-24: "deeper seasoned leagues so true rebuilds
    and picks have enough time to convert into wins")."""
    leagues, users = list(seed_league_ids), []
    for hop in range(hops):
        for lid in list(leagues):
            for u in _get(f"/league/{lid}/users") or []:
                if u["user_id"] not in users and len(users) < max_users:
                    users.append(u["user_id"])
        for uid in users:
            for lg in _get(f"/user/{uid}/leagues/nfl/2026") or []:
                if lg["settings"]["type"] == 2 and lg["league_id"] not in leagues:
                    leagues.append(lg["league_id"])
        print(f"hop {hop + 1}: {len(users)} managers, {len(leagues)} dynasty leagues")
    crawled = 0
    for lid in leagues:
        if crawled >= max_chains:
            break
        try:
            if len(get_season_chain(lid)) < min_seasons:
                continue
            crawl_chain(lid)
            crawled += 1
        except requests.HTTPError as e:
            print(f"  skipped {lid}: {e}")
        if crawled and crawled % 10 == 0:
            print(f"  ...{crawled} deep chains, {len(list(CRAWL_DIR.glob('*.json')))} season files")


SHORT_DIR = Path("data/crawl_short")


def folded(seed_league_ids: list[str], max_chains: int = 80, max_users: int = 300) -> None:
    """A COMPARISON set, kept out of the main corpus in data/crawl_short/: dynasty leagues
    that stopped after 2022-2024 (owner: "we don't really want to cater to dead dynasty
    leagues, though good to know if they act different"). Sleeper links seasons backward
    only, so a league counts as folded when no member's 2025 league traces back to it."""
    users = []
    for lid in seed_league_ids:
        for u in _get(f"/league/{lid}/users") or []:
            if u["user_id"] not in users:
                users.append(u["user_id"])
    for lid in [lg["league_id"] for uid in list(users)[:60]
                for lg in _get(f"/user/{uid}/leagues/nfl/2026") or [] if lg["settings"]["type"] == 2]:
        for u in _get(f"/league/{lid}/users") or []:
            if u["user_id"] not in users and len(users) < max_users:
                users.append(u["user_id"])
    continued, candidates = set(), []
    for uid in users:
        for lg in _get(f"/user/{uid}/leagues/nfl/2025") or []:
            continued.update(get_season_chain(lg["league_id"]))
        for year in ("2022", "2023", "2024"):
            for lg in _get(f"/user/{uid}/leagues/nfl/{year}") or []:
                if lg["settings"]["type"] == 2 and lg["league_id"] not in candidates:
                    candidates.append(lg["league_id"])
    dead = [lid for lid in candidates if lid not in continued]
    print(f"{len(users)} managers, {len(candidates)} older dynasty leagues, {len(dead)} folded")
    for lid in dead[:max_chains]:
        try:
            crawl_chain(lid, SHORT_DIR)
        except requests.HTTPError as e:
            print(f"  skipped {lid}: {e}")


if __name__ == "__main__":
    if sys.argv[1] == "deep":
        deep(sys.argv[2].split(","), int(sys.argv[3]) if len(sys.argv) > 3 else 5,
             hops=int(sys.argv[4]) if len(sys.argv) > 4 else 2)
    elif sys.argv[1] == "folded":
        folded(sys.argv[2].split(","))
    elif sys.argv[1] == "snowball":
        snowball(sys.argv[2].split(","),
                 int(sys.argv[3]) if len(sys.argv) > 3 else 60)
    else:
        written = crawl_chain(sys.argv[1])
        print(f"{len(written)} new season file(s) in {CRAWL_DIR}")
