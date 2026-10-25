"""What the trade grades say: are they real (T-A, T-B), was the owner's thesis right
(T-C), does consolidation pay (T-D), and is trading a skill (T-E)? Reads the grades
research/trade_grades.py writes; pre-registered in research/README.md "Trade grades".

Run: python -m research.trade_grades && python -m research.trade_analysis
"""

import json
import statistics
from collections import defaultdict
from pathlib import Path

from research.roster_strength import fit
from research.schedule_luck import season_luck

CRAWL_DIR = Path("data/crawl")
GRADES = Path("data/research/trade_grades.jsonl")
CONTENDER = 0.6


def weekly_points(d: dict) -> dict:
    """owner_id -> {week: points} over the regular season."""
    weeks = (d["league"]["settings"].get("playoff_week_start") or 15) - 1
    owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
    out = defaultdict(dict)
    for w in range(1, weeks + 1):
        for m in d["matchups"].get(str(w)) or []:
            if m.get("points") is not None:
                out[owner.get(m["roster_id"])][w] = m["points"]
    return out


def main():
    rows = [json.loads(line) for line in open(GRADES, encoding="utf-8")]
    seasons, allplay, points, next_of = {}, {}, {}, {}
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        luck = season_luck(d)
        if luck:
            lid = d["league"]["league_id"]
            allplay[lid] = {o: v["allplay"] for o, v in luck.items()}
            points[lid] = weekly_points(d)
            next_of[d["league"].get("previous_league_id")] = lid
    corr = statistics.correlation
    ins = [r for r in rows if r["in_season"]]

    # T-A: predicted lineup gain vs the team's real scoring after the trade
    xs, ys = [], []
    for r in ins:
        pts = points[r["league"]].get(r["manager"], {})
        before = [p for w, p in pts.items() if w < r["week"]]
        after = [p for w, p in pts.items() if w >= r["week"]]
        if len(before) >= 2 and len(after) >= 2:
            xs.append(r["now_lineup"])
            ys.append(statistics.mean(after) - statistics.mean(before))
    reg = statistics.linear_regression(xs, ys)
    print(f"T-A lineup half: {len(xs)} in-season sides - real scoring change after the trade per "
          f"predicted lineup point: slope {reg.slope:.2f}, r {corr(xs, ys):+.3f}")

    # T-B: value margin vs the next season it was meant to move - players and picks apart
    for in_season in (False, True):
        data = []
        for r in rows:
            if r["in_season"] != in_season:
                continue
            target_league = next_of.get(r["league"]) if in_season else r["league"]
            ap = allplay.get(target_league, {}).get(r["manager"])
            if ap is not None:
                data.append(({"allplay": r["allplay_now"], "value": r["value_rank_before"],
                              "players": r["value_margin"] - r["pick_margin"],
                              "picks": r["pick_margin"]}, ap))
        coef = fit(data, ["allplay", "value", "players", "picks"])
        print(f"T-B {'in-season' if in_season else 'offseason'} n={len(data)}: next-season all-play "
              f"weight on player value won {coef[3]:+.3f}, on pick value won {coef[4]:+.3f} "
              f"(curve's weight on value {coef[2]:+.3f})")

    # T-C: contenders losing on value in-season
    c_lose = [r["now_titles"] for r in ins if r["allplay_now"] >= CONTENDER and r["value_margin"] < 0]
    c_win = [r["now_titles"] for r in ins if r["allplay_now"] >= CONTENDER and r["value_margin"] >= 0]
    print(f"T-C contenders in-season: LOST on value n={len(c_lose)} mean this-season titles "
          f"{100 * statistics.mean(c_lose):+.2f} pts; won on value n={len(c_win)} "
          f"{100 * statistics.mean(c_win):+.2f} pts")

    # T-D: consolidation
    fewer = [r["now_titles"] for r in ins if r["n_got"] < r["n_gave"]]
    more = [r["now_titles"] for r in ins if r["n_got"] > r["n_gave"]]
    print(f"T-D consolidation in-season: received fewer pieces n={len(fewer)} "
          f"{100 * statistics.mean(fewer):+.2f} title pts vs received more n={len(more)} "
          f"{100 * statistics.mean(more):+.2f}")

    # T-E: is trading a skill - a manager's mean grade, season to season
    per = defaultdict(list)
    for r in rows:
        per[(r["league"], r["manager"])].append(r["titles"])
    pairs = [(statistics.mean(per[(prev, m)]), statistics.mean(v)) for (lid, m), v in per.items()
             for prev in [next((p for p, n in next_of.items() if n == lid), None)] if (prev, m) in per]
    print(f"T-E trade grade year over year: {len(pairs)} manager pairs, r = "
          f"{corr([a for a, _ in pairs], [b for _, b in pairs]):+.3f}")

    # Descriptive: who gains what, by the team's situation when it traded
    print("\nby situation when trading (in-season; mean title points):")
    for name, lo, hi in (("rebuild (< .400)", 0, 0.4), ("middle", 0.4, CONTENDER), ("contender (.600+)", CONTENDER, 1.01)):
        g = [r for r in ins if lo <= r["allplay_now"] < hi]
        print(f"   {name:18} n={len(g):4}  this season {100 * statistics.mean(r['now_titles'] for r in g):+.2f}  "
              f"next season {100 * statistics.mean(r['next_titles'] for r in g):+.2f}  "
              f"value margin {statistics.mean(r['value_margin'] for r in g):+.3f}")


if __name__ == "__main__":
    main()
