"""A second sweep of manager habits from the fantasy literature: lineup efficiency (start/sit),
dead starts (neglect), market timing on trades, and offseason tinkering. Tests L1, L2, N1, S1
and O1 pre-registered in research/README.md "More skills".

Run: python -m research.more_skills
"""

import datetime
import json
import statistics
from collections import defaultdict
from pathlib import Path

from research.draft_skill import Values, is_superflex, sleeper_to_fp
from research.roster_strength import fit, season_rosters
from research.schedule_luck import season_luck
from research.trade_grades import lineup_ppg
from sources.nflverse_ids import sleeper_names

CRAWL_DIR = Path("data/crawl")
HABITS = ("efficiency", "dead_rate", "timing", "offseason_trades")


def lineup_habits(d: dict, position: dict) -> dict:
    """owner_id -> {"efficiency", "dead_rate"} over the regular season."""
    weeks = (d["league"]["settings"].get("playoff_week_start") or 15) - 1
    slots = d["league"].get("roster_positions") or []
    owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
    got, best, dead, starts = (defaultdict(float) for _ in range(4))
    for w in range(1, weeks + 1):
        for m in d["matchups"].get(str(w)) or []:
            rid = m["roster_id"]
            pts = m.get("players_points") or {}
            got[rid] += sum(p or 0 for p in m.get("starters_points") or [])
            best[rid] += lineup_ppg(set(m.get("players") or []), pts, position, slots)
            for p in m.get("starters_points") or []:
                starts[rid] += 1
                dead[rid] += not p
    return {owner[r]: {"efficiency": min(got[r] / best[r], 1.0) if best[r] else None,
                       "dead_rate": dead[r] / starts[r] if starts[r] else None}
            for r in owner if owner.get(r)}


def trade_habits(d: dict, values: Values, xwalk: dict) -> dict:
    """owner_id -> {"timing", "offseason_trades"}: value margin 12 months on minus on the
    day (players only), and trades filed before week 2."""
    sf = is_superflex(d["league"])
    owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
    timing, n_off = defaultdict(list), defaultdict(int)
    for txs in (d.get("transactions") or {}).values():
        for t in txs or []:
            if t.get("type") != "trade" or t.get("status") != "complete" or len(t.get("roster_ids") or []) != 2:
                continue
            day0 = datetime.date.fromtimestamp(t["created"] / 1000)
            d0, d12 = day0.isoformat(), (day0 + datetime.timedelta(days=365)).isoformat()
            val = lambda p, day: values.at(xwalk.get(str(p)), day, sf)
            for me in t["roster_ids"]:
                if (t.get("leg") or 1) < 2:
                    n_off[owner.get(me)] += 1
                gets = [p for p, to in (t.get("adds") or {}).items() if to == me]
                gives = [p for p, frm in (t.get("drops") or {}).items() if frm == me]
                margins = []
                for day in (d0, d12):
                    v = [val(p, day) for p in gets] + [val(p, day) for p in gives]
                    if any(x is None for x in v):
                        break
                    margins.append(sum(v[:len(gets)]) - sum(v[len(gets):]))
                if len(margins) == 2 and (gets or gives):
                    timing[owner.get(me)].append(margins[1] - margins[0])
    return {o: {"timing": statistics.mean(timing[o]) if timing[o] else None,
                "offseason_trades": n_off[o]} for o in owner.values() if o}


def main():
    values, xwalk = Values(), sleeper_to_fp()
    position = {p: v["position"] for p, v in sleeper_names().items()}
    seasons = {}
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        if not season_luck(d):
            continue
        for m in d["matchups"].get("1") or []:
            for p in m.get("players") or []:
                position.setdefault(p, "DEF" if not p.isdigit() else None)
        rosters = season_rosters(d, values, xwalk)
        lineup, trades = lineup_habits(d, position), trade_habits(d, values, xwalk)
        for o, r in rosters.items():
            r.update(lineup.get(o, {}), **trades.get(o, {"timing": None, "offseason_trades": 0}))
        # Within the league that season: league setup moved efficiency more than managers did
        # (league-average SD 7.1 pts vs 3.7 within) - the ROADMAP's within-league normalization.
        for k in HABITS:
            known = [r[k] for r in rosters.values() if r.get(k) is not None]
            for r in rosters.values():
                if r.get(k) is not None:
                    r[k] -= statistics.mean(known)
        seasons[d["league"]["league_id"]] = (d["league"].get("previous_league_id"), rosters)
    rows = [r for _, rs in seasons.values() for r in rs.values()]
    pairs = [(seasons[prev][1][o], now[o]) for prev, now in seasons.values() if prev in seasons
             for o in now if o in seasons[prev][1]]
    corr = statistics.correlation
    pct = lambda k, q: statistics.quantiles([r[k] for r in rows if r.get(k) is not None], n=4)[q]

    def persist(k):
        xs = [(a[k], b[k]) for a, b in pairs if a.get(k) is not None and b.get(k) is not None]
        return corr([a for a, _ in xs], [b for _, b in xs]), len(xs)

    def next_weight(k):
        data = [({"allplay": a["allplay"], "starter": a["starter"], k: a[k]}, b["allplay"])
                for a, b in pairs if a.get(k) is not None]
        sd = statistics.pstdev(x[k] for x, _ in data)
        return fit(data, ["allplay", "starter", k])[3] * sd, len(data)

    print(f"{len(rows)} manager-seasons, {len(pairs)} back-to-back pairs\n")
    eff_gap = (pct("efficiency", 2) - pct("efficiency", 0))
    print(f"lineup efficiency vs the league that season: 25th-75th spread {100 * eff_gap:.1f} pts of the "
          f"hindsight-best lineup (all habits below are relative to the league)")
    for name, k in (("L1/L2 lineup efficiency", "efficiency"), ("N1 dead-start rate", "dead_rate"),
                    ("S1 trade timing edge", "timing"), ("O1 offseason trades", "offseason_trades")):
        r, n = persist(k)
        w, m = next_weight(k)
        print(f"{name:26} year-over-year r = {r:+.3f} (n={n:4})   next-season all-play per SD, "
              f"roster held: {w:+.4f} (n={m})")


if __name__ == "__main__":
    main()
