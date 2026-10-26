"""Beating the projections: did a manager's lineup out-score the lineup that week's Sleeper
projections would have started, from the same roster, in actual points? Only QB/RB/WR/TE and
their flex slots (the projections' positions); a week with no real decision scores 0. Tests
L3-L5 pre-registered in research/README.md "Beating the projections, opportunity pickups, and
the EV ceiling".

Run: python -m research.lineup_vs_projections
"""

import json
import statistics
from collections import defaultdict
from pathlib import Path

from research.schedule_luck import season_luck
from research.trade_grades import ELIGIBLE
from sources import sleeper
from sources.nflverse_ids import sleeper_names

CRAWL_DIR = Path("data/crawl")
OFFENSE = {"QB", "RB", "WR", "TE", "FLEX", "SUPER_FLEX", "REC_FLEX", "WRRB_FLEX"}


def pick_lineup(roster: set, score: dict, position: dict, slots: list) -> list:
    """The projection-best lineup: narrowest slots first, best projected player each."""
    left = sorted(roster, key=lambda p: -score.get(p, 0))
    chosen = []
    for slot in sorted((s for s in slots if s in OFFENSE), key=lambda s: len(ELIGIBLE[s])):
        pick = next((p for p in left if position.get(p) in ELIGIBLE[slot]), None)
        if pick is not None:
            left.remove(pick)
            chosen.append(pick)
    return chosen


def season_edges(d: dict, proj: dict, position: dict) -> dict:
    """owner_id -> mean weekly lineup edge (actual starter points minus the projection lineup's
    actual points) over the regular season."""
    lg = d["league"]
    season, weeks = lg["season"], (lg["settings"].get("playoff_week_start") or 15) - 1
    scoring = lg.get("scoring_settings") or {}
    slots = [s for s in lg.get("roster_positions") or [] if s not in ("BN", "IR", "TAXI")]
    owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
    edges = defaultdict(list)
    for w in range(1, weeks + 1):
        stats = proj.get((season, w))
        if stats is None:
            continue
        for m in d["matchups"].get(str(w)) or []:
            pts = m.get("players_points") or {}
            roster = set(m.get("players") or [])
            score = {p: sleeper.score(stats.get(p, {}), scoring) for p in roster if p in stats}
            actual = sum((x or 0) for slot, x in zip(slots, m.get("starters_points") or []) if slot in OFFENSE)
            follow = sum(pts.get(p, 0) or 0 for p in pick_lineup(roster, score, position, slots))
            edges[owner.get(m["roster_id"])].append(actual - follow)
    return {o: statistics.mean(v) for o, v in edges.items() if o and v}


def main():
    position = {p: v["position"] for p, v in sleeper_names().items()}
    proj = {}
    seasons = {}
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        if not season_luck(d) or int(d["league"]["season"]) > 2025:
            continue
        season = d["league"]["season"]
        weeks = (d["league"]["settings"].get("playoff_week_start") or 15) - 1
        for w in range(1, weeks + 1):
            if (season, w) not in proj:
                proj[(season, w)] = sleeper.get_projections(season, w)
        e = season_edges(d, proj, position)
        mean = statistics.mean(e.values()) if e else 0
        seasons[d["league"]["league_id"]] = (d["league"].get("previous_league_id"), e,
                                             {o: v - mean for o, v in e.items()})
    raw = [v for _, e, _ in seasons.values() for v in e.values()]
    rel = [v for *_, r in seasons.values() for v in r.values()]
    q = statistics.quantiles(rel, n=4)
    print(f"{len(seasons)} seasons, {len(raw)} manager-seasons")
    print(f"L3 mean weekly lineup edge vs following the projections: {statistics.mean(raw):+.2f} pts "
          f"(median {statistics.median(raw):+.2f}; {100 * sum(v > 0 for v in raw) / len(raw):.0f}% beat them)")
    pairs = [(seasons[prev][2][o], r[o]) for prev, _, r in seasons.values() if prev in seasons
             for o in r if o in seasons[prev][2]]
    print(f"L4 within-league edge year over year: r = "
          f"{statistics.correlation([a for a, _ in pairs], [b for _, b in pairs]):+.3f} (n={len(pairs)})")
    print(f"L5 25th -> 75th percentile manager (within league): {q[2] - q[0]:+.2f} pts a week "
          f"({14 * (q[2] - q[0]):+.0f} over a 14-week season)")


if __name__ == "__main__":
    main()
