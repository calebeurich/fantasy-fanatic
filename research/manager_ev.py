"""Expected dollars: are the dollar grades honest (E1), is adding EV a trait (E2), does it pay
later (E3), and is a manager's EV reliable across his seasons (E4)? Pre-registered in
research/README.md "Expected dollars". Payouts are XFL 2's ($350 / $150 / $50 + $50 points
leader); a manager's EV added is the sum of his trades' this-season + next-season EV.

Run: python -m research.trade_grades && python -m research.manager_ev
"""

import json
import random
import statistics
from collections import defaultdict
from pathlib import Path

from research.draft_skill import Values, is_superflex, sleeper_to_fp
from research.roster_strength import fit
from research.schedule_luck import season_luck
from research.trade_grades import OUT as GRADE_FILE, PAYOUT

CRAWL_DIR = Path("data/crawl")
BINS = (0, 10, 25, 50, 100, 1000)
BOOT = 300


def realized(d: dict) -> dict:
    """owner_id -> what the season actually paid, at XFL 2's payouts."""
    owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
    paid = defaultdict(float)
    for m in d.get("winners_bracket") or []:
        if m.get("p") == 1:
            paid[owner.get(m["w"])] += PAYOUT["title"]
            paid[owner.get(m["l"])] += PAYOUT["second"]
        elif m.get("p") == 3:
            paid[owner.get(m["w"])] += PAYOUT["third"]
    fpts = {r["roster_id"]: (r.get("settings") or {}).get("fpts", 0) for r in d["rosters"]}
    paid[owner.get(max(fpts, key=fpts.get))] += PAYOUT["pf_leader"]
    return paid


def start_value(d: dict, values: Values, xwalk: dict) -> dict:
    """owner_id -> the roster each team STARTED the season with, valued, league-relative."""
    sf, day = is_superflex(d["league"]), f"{d['league']['season']}-09-05"
    owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
    raw = {owner.get(m["roster_id"]): sum(values.at(xwalk.get(str(p)), day, sf) or 0 for p in m.get("players") or [])
           for m in d["matchups"].get("1") or []}
    mean = statistics.mean(raw.values()) if raw else 0
    return {o: v / mean for o, v in raw.items()} if mean >= 5000 else {}


def main():
    values, xwalk = Values(), sleeper_to_fp()
    ev = defaultdict(float)
    calib = []                    # (pre-trade EV, post-trade EV, league, manager)
    for line in open(GRADE_FILE, encoding="utf-8"):
        r = json.loads(line)
        ev[(r["league"], r["manager"])] += r["ev"]
        if r["in_season"] and r.get("ev_before") is not None:
            calib.append((r["ev_before"], r["ev_before"] + r["now_ev"], r["league"], r["manager"]))
    seasons, paid = {}, {}
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        luck = season_luck(d)
        if not luck:
            continue
        lid = d["league"]["league_id"]
        paid[lid] = realized(d)
        sv = start_value(d, values, xwalk)
        seasons[lid] = (d["league"].get("previous_league_id"),
                        {o: {"allplay": luck[o]["allplay"], "start": sv[o], "ev": ev[(lid, o)],
                             "paid": paid[lid].get(o, 0.0)} for o in luck if o in sv})

    print("E1 trading teams, binned by post-trade expected payout (this season):")
    for lo, hi in zip(BINS, BINS[1:]):
        g = [(pre, post, paid[lg].get(o, 0.0)) for pre, post, lg, o in calib if lo <= post < hi and lg in paid]
        if g:
            print(f"   ${lo:>3}-{hi:<4} n={len(g):5}  expected pre ${statistics.mean(x for x, _, _ in g):6.1f}  "
                  f"post ${statistics.mean(x for _, x, _ in g):6.1f}  realized ${statistics.mean(x for *_, x in g):6.1f}")
    g = [(pre, post, paid[lg].get(o, 0.0)) for pre, post, lg, o in calib if lg in paid]
    print(f"   all n={len(g)}: pre ${statistics.mean(x for x, _, _ in g):.1f}, post ${statistics.mean(x for _, x, _ in g):.1f}, "
          f"realized ${statistics.mean(x for *_, x in g):.1f}")

    next_of = {prev: lid for lid, (prev, _) in seasons.items()}
    pairs = [(x, seasons[next_of[lid]][1][o], lid) for lid, (_, xs) in seasons.items() if lid in next_of
             for o, x in xs.items() if o in seasons[next_of[lid]][1]]
    corr = statistics.correlation
    print(f"\nE2 EV added by trades, year over year: r = {corr([a['ev'] for a, _, _ in pairs], [b['ev'] for _, b, _ in pairs]):+.3f} "
          f"(n={len(pairs)})")

    rng = random.Random(0)
    by = defaultdict(list)
    for a, b, lid in pairs:
        by[lid].append((a, b["paid"]))
    keys = list(by)
    cols = ["allplay", "start", "ev"]
    sd = statistics.pstdev(a["ev"] for a, _, _ in pairs)
    est = sorted(fit([p for k in (rng.choice(keys) for _ in keys) for p in by[k]], cols)[3] * sd for _ in range(BOOT))
    w = fit([(a, b["paid"]) for a, b, _ in pairs], cols)[3] * sd
    print(f"E3 next season's realized payout per SD of EV added (all-play, starting roster held): ${w:+.2f} "
          f"[{est[int(.05 * BOOT)]:+.2f}, {est[int(.95 * BOOT)]:+.2f}]")

    chains = defaultdict(lambda: defaultdict(list))
    for lid, (prev, xs) in seasons.items():
        head, n = lid, 0
        while seasons.get(head, (None,))[0] in seasons:
            head, n = seasons[head][0], n + 1
        for o, x in xs.items():
            chains[(head, o)][n % 2].append(x["ev"])
    halves = [(statistics.mean(v[0]), statistics.mean(v[1])) for v in chains.values()
              if len(v[0]) + len(v[1]) >= 4 and v[0] and v[1]]
    print(f"E4 managers with 4+ seasons in one league, odd vs even seasons' EV added: r = "
          f"{corr([a for a, _ in halves], [b for _, b in halves]):+.3f} (n={len(halves)})")


if __name__ == "__main__":
    main()
