"""Depth on real injuries: when a regular starter is hurt (not on bye), does a better backup
at his position soften the team's week? Tests D1b pre-registered in research/README.md
"Depth, redone on real injuries".

Run: python -m research.depth_injury
"""

import json
import statistics
from collections import defaultdict
from pathlib import Path

import nflreadpy as nfl
import numpy as np

from research.schedule_luck import season_luck
from sources.nflverse_ids import gsis_to_sleeper, sleeper_names

CRAWL_DIR = Path("data/crawl")
REGULAR = 0.6
SEASONS = list(range(2020, 2026))


def injury_weeks() -> tuple[set, set]:
    """(season, week, sleeper_id) a player was hurt; (season, week, team) that played."""
    hurt = set()
    weekly = nfl.load_rosters_weekly(SEASONS)
    for season, week, sid, status in weekly.select(["season", "week", "sleeper_id", "status"]).iter_rows():
        if sid and status in ("RES", "INA"):
            hurt.add((season, week, str(sid)))
    to_sleeper = gsis_to_sleeper()
    reports = nfl.load_injuries(SEASONS)
    for season, week, gsis, status in reports.select(["season", "week", "gsis_id", "report_status"]).iter_rows():
        if status in ("Out", "Doubtful") and gsis in to_sleeper:
            hurt.add((season, week, to_sleeper[gsis]))
    played = set()
    games = nfl.load_schedules(SEASONS)
    for season, week, home, away, kind in games.select(["season", "week", "home_team", "away_team", "game_type"]).iter_rows():
        if kind == "REG":
            played.update({(season, week, home), (season, week, away)})
    team = {}
    for season, week, sid, tm in weekly.select(["season", "week", "sleeper_id", "team"]).iter_rows():
        if sid:
            team[(season, week, str(sid))] = tm
    return hurt, played, team


def events(d: dict, hurt: set, played: set, team: dict, position: dict) -> list:
    """(drop vs season average, backup PPG, missing starter PPG, team season average)."""
    season = int(d["league"]["season"])
    weeks = (d["league"]["settings"].get("playoff_week_start") or 15) - 1
    mus = {w: d["matchups"].get(str(w)) or [] for w in range(1, weeks + 1)}
    rostered, started, scored, team_pts = defaultdict(int), defaultdict(int), defaultdict(list), defaultdict(list)
    for ms in mus.values():
        for m in ms:
            rid = m["roster_id"]
            team_pts[rid].append(m.get("points") or 0)
            pp = m.get("players_points") or {}
            for p in m.get("players") or []:
                rostered[(rid, p)] += 1
                if pp.get(p):
                    scored[p].append(pp[p])
            for p in m.get("starters") or []:
                started[(rid, p)] += 1
    ppg = {p: statistics.mean(s) for p, s in scored.items()}
    regular = {k for k, n in rostered.items() if n >= 4 and started[k] / n >= REGULAR}
    out = []
    for w, ms in mus.items():
        for m in ms:
            rid = m["roster_id"]
            mean = statistics.mean(team_pts[rid])
            for p in m.get("players") or []:
                if (rid, p) not in regular or (season, w, p) not in hurt:
                    continue
                if (season, w, team.get((season, w, p))) not in played:
                    continue   # his team was on bye - not an injury week
                # The replacement: the best healthy, playing teammate at his position who is
                # not himself a regular starter - started this week or not (a first cut took
                # the best player left on the BENCH, which a good backup never is: he starts).
                bench = [ppg.get(q, 0) for q in m.get("players") or []
                         if q != p and (rid, q) not in regular and position.get(q) == position.get(p)
                         and (season, w, q) not in hurt
                         and (season, w, team.get((season, w, q))) in played]
                out.append(((m.get("points") or 0) - mean, max(bench, default=0), ppg.get(p, 0), mean))
    return out


def main():
    hurt, played, team = injury_weeks()
    position = {p: v["position"] for p, v in sleeper_names().items()}
    rows = []
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        if season_luck(d) and int(d["league"]["season"]) in SEASONS:
            rows += events(d, hurt, played, team, position)
    x = np.column_stack([np.ones(len(rows))] + [[r[i] for r in rows] for i in (1, 2, 3)])
    y = np.array([r[0] for r in rows])
    coef = np.linalg.lstsq(x, y, rcond=None)[0]
    print(f"D1b {len(rows)} confirmed-injury starter absences (byes excluded): mean team drop "
          f"{statistics.mean(y):+.1f} pts")
    print(f"   per backup PPG {coef[1]:+.2f}, per missing-starter PPG {coef[2]:+.2f}, "
          f"per team-average point {coef[3]:+.2f}")


if __name__ == "__main__":
    main()
