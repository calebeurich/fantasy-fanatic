"""Aligned trading over two seasons: do contenders who buy production and rebuilds who buy
youth do better - and when? Tests A1-A3 pre-registered in research/README.md "Aligned
trading". Reads research/trade_grades.py's output (which records each side's value-weighted
age in and out).

Run: python -m research.trade_grades && python -m research.alignment
"""

import json
import random
from collections import defaultdict
from pathlib import Path

from research.draft_skill import Values, sleeper_to_fp
from research.roster_strength import fit, season_rosters
from research.schedule_luck import season_luck

CRAWL_DIR = Path("data/crawl")
GRADES = Path("data/research/trade_grades.jsonl")
CONTENDER, REBUILD = 0.6, 0.4
AGE_SHIFT = 1.0   # a year older or younger in value-weighted age counts as a direction
BOOT = 300


def situation(allplay: float) -> str:
    return "contend" if allplay >= CONTENDER else "rebuild" if allplay < REBUILD else "middle"


def aligned(r: dict) -> int:
    """+1 aligned, -1 misaligned, 0 neither (middle team, or no clear age direction)."""
    if r["got_age"] is None or r["gave_age"] is None:
        return 0
    shift = r["got_age"] - r["gave_age"]
    want = {"contend": 1, "rebuild": -1}.get(situation(r["allplay_now"]), 0)
    if not want or abs(shift) < AGE_SHIFT:
        return 0
    return 1 if (shift > 0) == (want > 0) else -1


def main():
    values, xwalk = Values(), sleeper_to_fp()
    align = defaultdict(int)
    for line in open(GRADES, encoding="utf-8"):
        r = json.loads(line)
        align[(r["league"], r["manager"])] += aligned(r)
    seasons = {}
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        if season_luck(d):
            seasons[d["league"]["league_id"]] = (d["league"].get("previous_league_id"),
                                                 season_rosters(d, values, xwalk))
    next_of = {prev: lid for lid, (prev, _) in seasons.items()}
    rows = defaultdict(list)   # (horizon, situation) -> [(features, outcome, league)]
    for lid, (_, rosters) in seasons.items():
        for o, x in rosters.items():
            feats = {**x, "align": align[(lid, o)]}
            kind = situation(x["allplay"])
            later = lid
            for horizon in (1, 2):
                later = next_of.get(later)
                if later is None or o not in seasons[later][1]:
                    break
                for group in (kind, "all"):
                    rows[(horizon, group)].append((feats, seasons[later][1][o]["allplay"], lid))
    cols = ["allplay", "starter", "bench", "align"]
    rng = random.Random(0)
    print("weight of alignment (aligned minus misaligned trades) on all-play h seasons later,")
    print("holding all-play, starter and bench value fixed - league bootstrap 90% interval:")
    for group in ("all", "contend", "rebuild"):
        for horizon in (1, 2):
            data = rows[(horizon, group)]
            if len(data) < 30:
                continue
            leagues = sorted({lg for *_, lg in data})
            by = defaultdict(list)
            for x, y, lg in data:
                by[lg].append((x, y))
            est = sorted(fit([p for lg in (rng.choice(leagues) for _ in leagues) for p in by[lg]], cols)[4]
                         for _ in range(BOOT))
            n_al = sum(1 for x, *_ in data if x["align"] != 0)
            print(f"  {group:8} t+{horizon}: n={len(data):4} ({n_al} with aligned/misaligned trades)  "
                  f"weight {fit([(x, y) for x, y, _ in data], cols)[4]:+.4f}  "
                  f"[{est[int(0.05 * BOOT)]:+.4f}, {est[int(0.95 * BOOT)]:+.4f}]")


if __name__ == "__main__":
    main()
