"""Aligned trading over two seasons: do contenders who buy production and rebuilds who buy
youth do better - and when? Tests A1-A3 pre-registered in research/README.md "Aligned
trading", plus A0 - the same season, where a contender's deadline trade should show first.
Reads research/trade_grades.py's output (which records each side's value-weighted
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


def weekly_allplay(d: dict) -> dict:
    """owner_id -> {week: share of the league that team outscored that week}."""
    weeks = (d["league"]["settings"].get("playoff_week_start") or 15) - 1
    owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
    out = defaultdict(dict)
    for w in range(1, weeks + 1):
        pts = {m["roster_id"]: m["points"] for m in d["matchups"].get(str(w)) or []
               if m.get("points") is not None}
        for rid, p in pts.items():
            out[owner.get(rid)][w] = sum(p > q for q in pts.values()) / max(len(pts) - 1, 1)
    return out


def same_season(rows: list, crawl: dict) -> None:
    """A0: contenders' in-season trades, judged inside the season they were made."""
    rng = random.Random(1)
    lift = defaultdict(list)   # aligned? -> [(rest-of-season all-play - before, league)]
    titles = []                # (won it, odds before, odds after)
    for r in rows:
        if not r["in_season"] or situation(r["allplay_now"]) != "contend" or r["league"] not in crawl:
            continue
        weekly, champ = crawl[r["league"]]
        ap = weekly.get(r["manager"], {})
        before = [v for w, v in ap.items() if w < r["week"]]
        after = [v for w, v in ap.items() if w >= r["week"]]
        if before and after:
            lift[aligned(r) == 1].append((sum(after) / len(after) - sum(before) / len(before), r["league"]))
        if r.get("title_before") is not None:
            titles.append((r["manager"] == champ, r["title_before"], r["title_before"] + r["now_titles"]))
    mean = lambda xs: sum(xs) / len(xs)
    for flag in (True, False):
        xs = lift[flag]
        leagues = sorted({lg for _, lg in xs})
        by = defaultdict(list)
        for v, lg in xs:
            by[lg].append(v)
        est = sorted(mean([v for lg in (rng.choice(leagues) for _ in leagues) for v in by[lg]])
                     for _ in range(BOOT))
        print(f"  A0a contender trades, {'aligned' if flag else 'not aligned'}: n={len(xs):4} "
              f"rest-of-season all-play minus before {100 * mean([v for v, _ in xs]):+.1f} pts "
              f"[{100 * est[int(0.05 * BOOT)]:+.1f}, {100 * est[int(0.95 * BOOT)]:+.1f}]")
    won = sum(w for w, _, _ in titles)
    print(f"  A0b {len(titles)} contender trade sides: won the title {100 * won / len(titles):.1f}% vs "
          f"pre-trade odds {100 * mean([b for _, b, _ in titles]):.1f}% and post-trade odds "
          f"{100 * mean([a for _, _, a in titles]):.1f}%")


def main():
    values, xwalk = Values(), sleeper_to_fp()
    align = defaultdict(int)
    graded = [json.loads(line) for line in open(GRADES, encoding="utf-8")]
    for r in graded:
        align[(r["league"], r["manager"])] += aligned(r)
    seasons, crawl = {}, {}
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        if season_luck(d):
            lid = d["league"]["league_id"]
            seasons[lid] = (d["league"].get("previous_league_id"), season_rosters(d, values, xwalk))
            owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
            champ = next((m["w"] for m in d.get("winners_bracket") or [] if m.get("p") == 1), None)
            crawl[lid] = (weekly_allplay(d), owner.get(champ))
    print("A0 - the same season (contenders' in-season trades):")
    same_season(graded, crawl)
    print()
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
