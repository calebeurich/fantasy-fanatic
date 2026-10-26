"""How often do dynasty leagues die, and when? The crawl can't say - it was found through
managers active in 2026, so its chains survived by construction. This samples managers who
did startup drafts in the crawl, lists their dynasty leagues each year, and follows each league
forward: a league season "continued" if any later league in any sampled manager's lists chains
back to it (Sleeper links seasons backward only). Writes data/research/league_survival.json.

Run: python -m research.league_survival [n_managers]
"""

import json
import random
import sys
from collections import defaultdict
from pathlib import Path

import requests

from sources import sleeper

CRAWL_DIR = Path("data/crawl")
OUT = Path("data/research/league_survival.json")
YEARS = range(2019, 2027)


def startup_managers() -> list:
    users = set()
    for f in CRAWL_DIR.glob("*.json"):
        d = json.load(open(f, encoding="utf-8"))
        for dr in d.get("drafts") or []:
            if ((dr.get("settings") or {}).get("rounds") or 0) >= 15 and dr.get("status") == "complete":
                users.update(p.get("picked_by") for p in dr.get("picks") or [] if p.get("picked_by"))
    return sorted(users)


def main(n: int) -> None:
    rng = random.Random(0)
    sample = rng.sample(startup_managers(), n)
    leagues = {}                       # league_id -> (season, previous_league_id)
    for i, uid in enumerate(sample):
        for year in YEARS:
            for lg in sleeper._get(f"user/{uid}/leagues/nfl/{year}") or []:
                if (lg.get("settings") or {}).get("type") == 2:
                    leagues[lg["league_id"]] = (int(lg["season"]), lg.get("previous_league_id"))
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{n} managers, {len(leagues)} dynasty league-seasons", flush=True)
    # Walk every league back through its chain (a sampled manager may have left a league that
    # kept going; the chain of any later season it reached still links it).
    for lid in list(leagues):
        prev = leagues[lid][1]
        while prev and prev not in leagues:
            try:
                lg = sleeper.get_league(prev)
            except requests.HTTPError:
                break          # a deleted league - the chain behind it is gone
            leagues[prev] = (int(lg["season"]), lg.get("previous_league_id"))
            prev = lg.get("previous_league_id")
    continued = {prev for _, prev in leagues.values() if prev}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    json.dump({"leagues": {k: list(v) for k, v in leagues.items()}, "continued": sorted(continued)},
              open(OUT, "w"))
    print(f"{len(leagues)} league-seasons -> {OUT}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 400)
