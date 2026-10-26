"""The leak check on expected dollars: a trade's next-season EV comes from curves (all-play +
value -> next all-play; all-play -> each paid place) fitted on the whole corpus, including the
seasons whose payouts E3 then predicts. Refit them with the target season held out, recompute
every trade's next-season EV from the fields the grader stored, and re-run E2-E4.

Run: python -m research.trade_grades && python -m research.heldout_ev
"""

import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path

from research.draft_skill import Values, sleeper_to_fp
from research.manager_ev import realized, start_value
from research.roster_strength import fit, season_rosters
from research.schedule_luck import season_luck
from research.trade_grades import OUT as GRADE_FILE, PAYOUT, _logistic

CRAWL_DIR = Path("data/crawl")
BOOT = 300


def main():
    values, xwalk = Values(), sleeper_to_fp()
    future = []                                    # (features t, all-play t+1, season t+1)
    places = defaultdict(lambda: defaultdict(lambda: ([], [])))   # season -> outcome -> (x, y)
    seasons, rosters = {}, {}
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        luck = season_luck(d)
        if not luck:
            continue
        lid, season = d["league"]["league_id"], int(d["league"]["season"])
        rosters[lid] = (d["league"].get("previous_league_id"), season, season_rosters(d, values, xwalk))
        owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
        place = {}
        for m in d.get("winners_bracket") or []:
            if m.get("p") == 1:
                place[m["w"]], place[m["l"]] = "title", "second"
            elif m.get("p") == 3:
                place[m["w"]] = "third"
        fpts = {r["roster_id"]: (r.get("settings") or {}).get("fpts", 0) for r in d["rosters"]}
        leader, has3 = max(fpts, key=fpts.get), "third" in place.values()
        for rid, o in owner.items():
            if o in luck and "title" in place.values():
                for k in PAYOUT:
                    if k != "third" or has3:
                        hit = (rid == leader) if k == "pf_leader" else place.get(rid) == k
                        places[season][k][0].append(luck[o]["allplay"])
                        places[season][k][1].append(1.0 if hit else 0.0)
        sv = start_value(d, values, xwalk)
        paid = realized(d)
        seasons[lid] = (d["league"].get("previous_league_id"),
                        {o: {"allplay": luck[o]["allplay"], "start": sv[o], "paid": paid.get(o, 0.0)}
                         for o in luck if o in sv})
    for prev, season, now in rosters.values():
        if prev in rosters:
            future += [(rosters[prev][2][o], now[o]["allplay"], season) for o in now if o in rosters[prev][2]]

    curves = {}
    for target in sorted({s for *_, s in future} | set(places)):
        c = fit([(x, y) for x, y, s in future if s != target], ["allplay", "total"])
        pc = {k: _logistic(sum((places[s][k][0] for s in places if s != target), []),
                           sum((places[s][k][1] for s in places if s != target), [])) for k in PAYOUT}
        curves[target] = (c, pc)

    def ev(pc, ap):
        return sum(PAYOUT[k] / (1 + math.exp(-(a + b * ap))) for k, (a, b) in pc.items())

    added = defaultdict(float)
    for line in open(GRADE_FILE, encoding="utf-8"):
        r = json.loads(line)
        target = r["season"] + (1 if r["in_season"] else 0)
        c, pc = curves.get(target, curves[max(curves)])
        base = min(max(c[0] + c[1] * r["allplay_now"] + c[2] * r["value_rank_before"], 0.0), 1.0)
        moved = min(max(base + c[2] * (r["value_margin"] - r["pick_margin"]), 0.0), 1.0)
        added[(r["league"], r["manager"])] += r["now_ev"] + ev(pc, moved) - ev(pc, base)

    next_of = {prev: lid for lid, (prev, _) in seasons.items()}
    pairs = [(dict(x, ev=added[(lid, o)]), dict(seasons[next_of[lid]][1][o], ev=added[(next_of[lid], o)]), lid)
             for lid, (_, xs) in seasons.items() if lid in next_of
             for o, x in xs.items() if o in seasons[next_of[lid]][1]]
    corr = statistics.correlation
    print(f"held-out curves (each target season refit without its own outcomes):")
    print(f"  E2 year over year r = {corr([a['ev'] for a, _, _ in pairs], [b['ev'] for _, b, _ in pairs]):+.3f} (n={len(pairs)})")
    rng = random.Random(0)
    by = defaultdict(list)
    for a, b, lid in pairs:
        by[lid].append((a, b["paid"]))
    keys, cols = list(by), ["allplay", "start", "ev"]
    sd = statistics.pstdev(a["ev"] for a, _, _ in pairs)
    est = sorted(fit([p for k in (rng.choice(keys) for _ in keys) for p in by[k]], cols)[3] * sd for _ in range(BOOT))
    print(f"  E3 next season's realized payout per SD of EV added: ${fit([(a, b['paid']) for a, b, _ in pairs], cols)[3] * sd:+.2f} "
          f"[{est[int(.05 * BOOT)]:+.2f}, {est[int(.95 * BOOT)]:+.2f}]")
    chains = defaultdict(lambda: defaultdict(list))
    for lid, (prev, xs) in seasons.items():
        head, n = lid, 0
        while seasons.get(head, (None,))[0] in seasons:
            head, n = seasons[head][0], n + 1
        for o in xs:
            chains[(head, o)][n % 2].append(added[(lid, o)])
    halves = [(statistics.mean(v[0]), statistics.mean(v[1])) for v in chains.values()
              if len(v[0]) + len(v[1]) >= 4 and v[0] and v[1]]
    print(f"  E4 odd vs even seasons r = {corr([a for a, _ in halves], [b for _, b in halves]):+.3f} (n={len(halves)})")


if __name__ == "__main__":
    main()
