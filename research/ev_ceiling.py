"""The EV ceiling (owner: "having high expected dollars makes it hard to add more expected
dollars, which could make comparisons odd"). C1: does in-season EV added fall as EV before
rises? C2: compared within EV-before bins, is EV added still reliable across a manager's
seasons and still paid out later? Pre-registered in research/README.md "Beating the
projections, opportunity pickups, and the EV ceiling".

Run: python -m research.trade_grades && python -m research.ev_ceiling
"""

import json
import random
import statistics
from collections import defaultdict
from pathlib import Path

from research.draft_skill import Values, sleeper_to_fp
from research.manager_ev import realized, start_value
from research.roster_strength import fit
from research.schedule_luck import season_luck
from research.trade_grades import OUT as GRADE_FILE

CRAWL_DIR = Path("data/crawl")
BINS = (0, 5, 15, 30, 60, 120, 1000)
BOOT = 300


def bin_of(x: float) -> int:
    return next(i for i, (lo, hi) in enumerate(zip(BINS, BINS[1:])) if lo <= x < hi)


def main():
    rows = [json.loads(line) for line in open(GRADE_FILE, encoding="utf-8")]
    ins = [r for r in rows if r["in_season"] and r.get("ev_before") is not None]
    buys = [r for r in ins if r["now_titles"] >= 0.01]
    print("C1 buys (1+ title point), EV added this season by EV before the trade:")
    for i, (lo, hi) in enumerate(zip(BINS, BINS[1:])):
        g = [r["now_ev"] for r in buys if bin_of(r["ev_before"]) == i]
        if g:
            print(f"   ${lo:>3}-{hi:<4} n={len(g):5}  mean EV added ${statistics.mean(g):+6.2f}  "
                  f"(as % of EV before: {100 * statistics.mean(g) / max((lo + hi) / 2, 1):5.1f}%)")

    norm = defaultdict(list)
    for r in ins:
        norm[bin_of(r["ev_before"])].append(r["now_ev"])
    bin_mean = {k: statistics.mean(v) for k, v in norm.items()}
    adjusted = defaultdict(float)
    for r in rows:
        adjusted[(r["league"], r["manager"])] += r["next_ev"] + (
            r["now_ev"] - bin_mean[bin_of(r["ev_before"])] if r["in_season"] and r.get("ev_before") is not None else 0)

    values, xwalk = Values(), sleeper_to_fp()
    seasons = {}
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        luck = season_luck(d)
        if not luck:
            continue
        lid, sv, paid = d["league"]["league_id"], start_value(d, values, xwalk), realized(d)
        seasons[lid] = (d["league"].get("previous_league_id"),
                        {o: {"allplay": luck[o]["allplay"], "start": sv[o], "ev": adjusted[(lid, o)],
                             "paid": paid.get(o, 0.0)} for o in luck if o in sv})
    chains = defaultdict(lambda: defaultdict(list))
    for lid, (prev, xs) in seasons.items():
        head, n = lid, 0
        while seasons.get(head, (None,))[0] in seasons:
            head, n = seasons[head][0], n + 1
        for o, x in xs.items():
            chains[(head, o)][n % 2].append(x["ev"])
    halves = [(statistics.mean(v[0]), statistics.mean(v[1])) for v in chains.values()
              if len(v[0]) + len(v[1]) >= 4 and v[0] and v[1]]
    print(f"\nC2 EV added within its EV-before bin, odd vs even seasons: r = "
          f"{statistics.correlation([a for a, _ in halves], [b for _, b in halves]):+.3f} (n={len(halves)})")
    next_of = {prev: lid for lid, (prev, _) in seasons.items()}
    by = defaultdict(list)
    for lid, (_, xs) in seasons.items():
        if lid in next_of:
            by[lid] += [(x, seasons[next_of[lid]][1][o]["paid"]) for o, x in xs.items() if o in seasons[next_of[lid]][1]]
    data, keys, cols = [p for v in by.values() for p in v], list(by), ["allplay", "start", "ev"]
    sd = statistics.pstdev(x["ev"] for x, _ in data)
    rng = random.Random(0)
    est = sorted(fit([p for k in (rng.choice(keys) for _ in keys) for p in by[k]], cols)[3] * sd for _ in range(BOOT))
    print(f"   next season's realized payout per SD: ${fit(data, cols)[3] * sd:+.2f} "
          f"[{est[int(.05 * BOOT)]:+.2f}, {est[int(.95 * BOOT)]:+.2f}]")


if __name__ == "__main__":
    main()
