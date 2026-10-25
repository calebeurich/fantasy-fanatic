"""When do picks pay? Pick value bought in trades vs all-play one and two seasons out, with
all-play and roster value held fixed. Tests P2 pre-registered in research/README.md "Picks
two seasons out, and folded leagues".

Run: python -m research.trade_grades && python -m research.picks_horizon
"""

import json
import random
import statistics
from collections import defaultdict
from pathlib import Path

from research.draft_skill import Values, sleeper_to_fp
from research.roster_strength import fit, season_rosters
from research.schedule_luck import season_luck

CRAWL_DIR = Path("data/crawl")
GRADES = Path("data/research/trade_grades.jsonl")
BOOT = 300


def main():
    values, xwalk = Values(), sleeper_to_fp()
    bought = defaultdict(float)
    for line in open(GRADES, encoding="utf-8"):
        r = json.loads(line)
        bought[(r["league"], r["manager"])] += r["pick_margin"]
    seasons = {}
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        if season_luck(d):
            seasons[d["league"]["league_id"]] = (d["league"].get("previous_league_id"),
                                                 season_rosters(d, values, xwalk))
    next_of = {prev: lid for lid, (prev, _) in seasons.items()}
    cols = ["allplay", "total", "picks"]
    rng = random.Random(0)
    for h in (1, 2, 3):
        by = defaultdict(list)
        for lid, (_, rs) in seasons.items():
            later = lid
            for _ in range(h):
                later = next_of.get(later)
            if later:
                by[lid] += [({**x, "picks": bought[(lid, o)]}, seasons[later][1][o]["allplay"])
                            for o, x in rs.items() if o in seasons[later][1]]
        data = [p for v in by.values() for p in v]
        if len(data) < 50:
            continue
        sd = statistics.pstdev(x["picks"] for x, _ in data)
        keys = list(by)
        est = sorted(fit([p for k in (rng.choice(keys) for _ in keys) for p in by[k]], cols)[3] * sd
                     for _ in range(BOOT))
        print(f"P2 t+{h}: n={len(data):5}  pick value bought, all-play per SD {fit(data, cols)[3] * sd:+.4f}  "
              f"league bootstrap 90% [{est[int(.05 * BOOT)]:+.4f}, {est[int(.95 * BOOT)]:+.4f}]")


if __name__ == "__main__":
    main()
