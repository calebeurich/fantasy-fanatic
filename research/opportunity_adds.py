"""Opportunity pickups: the Dameon Pierce move - adding a player the week his NFL teammate at
the same position goes down (or the week after). Do those adds start and score more than other
in-season adds (W4), and do some managers make them consistently (W5)? Pre-registered in
research/README.md "Beating the projections, opportunity pickups, and the EV ceiling".

Run: python -m research.opportunity_adds
"""

import json
import statistics
from collections import defaultdict
from pathlib import Path

import nflreadpy as nfl

from research.schedule_luck import season_luck
from sources.nflverse_ids import gsis_to_sleeper, sleeper_names

CRAWL_DIR = Path("data/crawl")
SEASONS = list(range(2020, 2026))
FOLLOW_WEEKS = 4
RELEVANT_PPG = {"QB": 15, "RB": 10, "WR": 10, "TE": 10}   # a teammate whose loss opens a real role


def hurt_and_teams() -> tuple[set, dict]:
    """(season, week, sleeper_id) hurt - IR, inactive, Out or Doubtful - and each player's NFL
    team that week."""
    weekly = nfl.load_rosters_weekly(SEASONS)
    hurt, team = set(), {}
    for season, week, sid, status, tm in weekly.select(["season", "week", "sleeper_id", "status", "team"]).iter_rows():
        if sid:
            team[(season, week, str(sid))] = tm
            if status in ("RES", "INA"):
                hurt.add((season, week, str(sid)))
    to_sleeper = gsis_to_sleeper()
    for season, week, gsis, status in nfl.load_injuries(SEASONS).select(
            ["season", "week", "gsis_id", "report_status"]).iter_rows():
        if status in ("Out", "Doubtful") and gsis in to_sleeper:
            hurt.add((season, week, to_sleeper[gsis]))
    return hurt, team


def ppg_to_date() -> dict:
    """(season, week, sleeper_id) -> PPR points per game played in that season before `week`."""
    to_sleeper = gsis_to_sleeper()
    games = defaultdict(list)
    for gsis, season, week, kind, fp in nfl.load_player_stats(SEASONS).select(
            ["player_id", "season", "week", "season_type", "fantasy_points_ppr"]).iter_rows():
        if kind == "REG" and gsis in to_sleeper:
            games[(season, to_sleeper[gsis])].append((week, fp or 0))
    out = {}
    for (season, sid), g in games.items():
        g.sort()
        for w in range(1, 19):
            before = [x for k, x in g if k < w]
            if before:
                out[(season, w, sid)] = sum(before) / len(before)
    return out


def main():
    position = {p: v["position"] for p, v in sleeper_names().items()}
    hurt, team = hurt_and_teams()
    ppg = ppg_to_date()
    # First cut (2026-10-26) counted ANY hurt teammate - game-day inactives and long-term IR
    # for irrelevant backups made 81% of all adds "opportunity adds". The heuristic is a real
    # contributor going down: fantasy-relevant to date, newly hurt (healthy the week before).
    by_team_week = defaultdict(set)          # (season, week, NFL team, position) -> newly hurt contributors
    for season, week, sid in hurt:
        tm, pos = team.get((season, week, sid)), position.get(sid)
        if (tm and pos in RELEVANT_PPG and (season, week - 1, sid) not in hurt
                and ppg.get((season, week, sid), 0) >= RELEVANT_PPG[pos]):
            by_team_week[(season, week, tm, pos)].add(sid)

    adds, share = [], {}
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        if not season_luck(d) or int(d["league"]["season"]) not in SEASONS:
            continue
        season = int(d["league"]["season"])
        weeks = (d["league"]["settings"].get("playoff_week_start") or 15) - 1
        owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
        starts = defaultdict(float)          # (roster, player, week) -> starter points
        for w in range(1, weeks + 1):
            for m in d["matchups"].get(str(w)) or []:
                for p, x in zip(m.get("starters") or [], m.get("starters_points") or []):
                    starts[(m["roster_id"], p, w)] = x or 0
        mine = defaultdict(lambda: [0, 0])
        for txs in (d.get("transactions") or {}).values():
            for t in txs or []:
                if t.get("type") not in ("waiver", "free_agent") or t.get("status") != "complete":
                    continue
                w = t.get("leg") or 0
                if not 2 <= w <= weeks - 1:
                    continue
                for pid, rid in (t.get("adds") or {}).items():
                    pos, tm = position.get(pid), team.get((season, w, pid))
                    if pos not in ("QB", "RB", "WR", "TE"):
                        continue
                    opp = bool(tm and ({s for k in (w, w - 1) for s in by_team_week.get((season, k, tm, pos), set())} - {pid}))
                    span = range(w, min(w + FOLLOW_WEEKS, weeks + 1))
                    pts = sum(starts.get((rid, pid, k), 0) for k in span)
                    n_starts = sum((rid, pid, k) in starts for k in span)
                    adds.append({"opp": opp, "pos": pos, "pts": pts, "starts": n_starts})
                    mine[owner.get(rid)][0] += 1
                    mine[owner.get(rid)][1] += opp
        share[d["league"]["league_id"]] = (d["league"].get("previous_league_id"),
                                           {o: b / a for o, (a, b) in mine.items() if o and a >= 3})
    for pos in ("ALL", "RB", "WR", "TE", "QB"):
        g = [a for a in adds if pos == "ALL" or a["pos"] == pos]
        o, n = [a for a in g if a["opp"]], [a for a in g if not a["opp"]]
        if o and n:
            print(f"W4 {pos:3} opportunity adds n={len(o):6} ({100 * len(o) / len(g):.0f}% of adds): "
                  f"{statistics.mean(a['pts'] for a in o):5.1f} starter pts, {statistics.mean(a['starts'] for a in o):.2f} starts "
                  f"in {FOLLOW_WEEKS} wks | other adds {statistics.mean(a['pts'] for a in n):5.1f} pts, "
                  f"{statistics.mean(a['starts'] for a in n):.2f} starts")
    pairs = [(share[prev][1][o], s[o]) for prev, s in share.values() if prev in share for o in s if o in share[prev][1]]
    print(f"W5 share of a manager's adds that are opportunity adds, year over year: r = "
          f"{statistics.correlation([a for a, _ in pairs], [b for _, b in pairs]):+.3f} (n={len(pairs)})")


if __name__ == "__main__":
    main()
