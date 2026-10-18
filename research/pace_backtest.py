"""Are the playoff odds honest? Owner (2026-08-21): "cant we do this in season stuff as
if it was last year and check it on that."

Replay every completed crawled season week by week - at each week k, feed only what a
live system would have known (record to date with median games, points per game to date
as the strength input, points for, the remaining posted schedule, the league's playoff
settings) - through BOTH the closed-form `team_state.playoff_pace` and the Monte Carlo
`season_sim.simulate`, and score them against the real winners bracket: who made it,
who got a bye, who won it.

The one substitution: historical projections are not retrievable, so ppg-to-date stands
in for ePPG. That validates the MATH (record + schedule + rules + the noise model), not
projection quality.

Run: python -m research.pace_backtest
"""

import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

from analysis.season_sim import STRENGTH_SD_SHARE, WEEKLY_SD_SHARE, simulate
from analysis.team_state import playoff_pace

CRAWL_DIR = Path("data/crawl")
FROM_WEEK = 3   # before this, ppg-to-date is too noisy to call a strength input
RUNS = 1000     # per frame; the live default is 5000, this keeps the replay to minutes


def season_frames(d: dict) -> list[dict]:
    """Replay frames for one completed crawled season, or []."""
    st = (d.get("league") or {}).get("settings") or {}
    slots, season_weeks = st.get("playoff_teams"), (st.get("playoff_week_start") or 15) - 1
    matchups = {int(w): ms for w, ms in (d.get("matchups") or {}).items() if ms}
    bracket = d.get("winners_bracket") or []
    field = {m[k] for m in bracket for k in ("t1", "t2") if isinstance(m.get(k), int)}
    if not slots or not field or any(w not in matchups for w in range(1, season_weeks + 1)):
        return []
    # An unplayed season (the 2026 crawl) scores every game 0 and still carries a
    # placeholder bracket - the first run counted 60 of them as real.
    if any(not any(m.get("points") for m in matchups[w]) for w in range(1, season_weeks + 1)):
        return []
    round1 = {m[k] for m in bracket if m.get("r") == 1 for k in ("t1", "t2")}
    champ = next((m["w"] for m in bracket if m.get("p") == 1), None)
    division = {r["roster_id"]: (r.get("settings") or {}).get("division") for r in d["rosters"]}
    pts, pair = defaultdict(dict), defaultdict(dict)
    wins_in = defaultdict(lambda: defaultdict(int))   # roster -> week -> wins that week
    ties_in = defaultdict(set)                        # roster -> weeks tied
    for w in range(1, season_weeks + 1):
        by_matchup = defaultdict(list)
        for m in matchups[w]:
            if m.get("points") is not None:
                pts[m["roster_id"]][w] = m["points"]
                by_matchup[m["matchup_id"]].append(m["roster_id"])
        for a, b in (two for two in by_matchup.values() if len(two) == 2):
            pair[a][w], pair[b][w] = b, a
            wins_in[a][w] += pts[a][w] > pts[b][w]
            wins_in[b][w] += pts[b][w] > pts[a][w]
            if pts[a][w] == pts[b][w]:
                ties_in[a].add(w); ties_in[b].add(w)
        if st.get("league_average_match"):
            median = statistics.median(pts[r][w] for r in pts if w in pts[r])
            for r in pts:
                wins_in[r][w] += pts[r].get(w, 0) > median
    rids = sorted(pts)
    if len(rids) < 4 or slots >= len(rids) or not field <= set(rids):
        return []
    frames = []
    for k in range(FROM_WEEK, season_weeks):
        teams = []
        for r in rids:
            played = [pts[r][w] for w in range(1, k + 1) if w in pts[r]]
            wins = sum(wins_in[r][w] for w in range(1, k + 1))
            ties = sum(1 for w in ties_in[r] if w <= k)
            games = len(played) * (2 if st.get("league_average_match") else 1)
            teams.append({"owner_id": r, "wins": wins, "losses": games - wins - ties, "ties": ties,
                          "points_for": sum(played), "eppg": sum(played) / max(len(played), 1),
                          "division": division.get(r)})
        frames.append({
            "k": k, "teams": teams,
            "schedule": {r: {w: o for w, o in pair[r].items() if w > k} for r in rids},
            "settings": {**st, "playoff_week_start": season_weeks + 1},
            "field": field, "byes": field - round1, "champ": champ})
    return frames


def _pace(frame: dict) -> dict:
    rows = [{"owner_id": t["owner_id"], "starting_production": t["eppg"], "path_reason": "",
             "record": {k: t[k] for k in ("wins", "losses", "ties")}}
            for t in frame["teams"]]
    playoff_pace(rows, frame["settings"], frame["schedule"])
    return {r["owner_id"]: r["playoff_pace"] / 100 for r in rows}


def _sim(frame: dict) -> dict:
    """The live drift, plus how wrong ppg-to-date is as a strength after k games - the
    live input (ePPG) doesn't carry that second term."""
    level = statistics.mean(t["eppg"] for t in frame["teams"])
    sd = math.hypot(WEEKLY_SD_SHARE * level / math.sqrt(frame["k"]), STRENGTH_SD_SHARE * level)
    return simulate(frame["teams"], frame["settings"], frame["k"], frame["schedule"], strength_sd=sd,
                    runs=RUNS)


def _deciles(pairs: list) -> str:
    buckets = defaultdict(lambda: [0, 0])
    for p, hit in pairs:
        b = buckets[min(int(p * 10), 9)]
        b[0] += 1; b[1] += hit
    return "\n".join(f"  {10 * d:3}-{10 * d + 10:3}%   {100 * h / n:5.1f}%  {n:5}"
                     for d, (n, h) in sorted(buckets.items()))


def main():
    scores = defaultdict(list)   # metric -> [(p, hit)]
    seasons = 0
    for f in sorted(CRAWL_DIR.glob("*.json")):
        frames = season_frames(json.load(open(f, encoding="utf-8")))
        seasons += bool(frames)
        for fr in frames:
            pace, sim = _pace(fr), _sim(fr)
            n, slots = len(fr["teams"]), fr["settings"]["playoff_teams"]
            in_now = {t["owner_id"] for t in sorted(
                fr["teams"], key=lambda t: (-t["wins"], -t["points_for"]))[:slots]}
            for t in fr["teams"]:
                me = t["owner_id"]
                made = me in fr["field"]
                scores["pace"].append((pace[me], made))
                scores["sim"].append((sim[me]["playoffs"], made))
                scores["top-k today"].append((float(me in in_now), made))
                scores["base rate"].append((slots / n, made))
                if fr["byes"]:
                    scores["bye: sim"].append((sim[me]["bye"], me in fr["byes"]))
                    scores["bye: base"].append((len(fr["byes"]) / n, me in fr["byes"]))
                if fr["champ"] is not None:
                    scores["title: sim"].append((sim[me]["title"], me == fr["champ"]))
                    scores["title: base"].append((1 / n, me == fr["champ"]))
    brier = lambda xs: sum((p - h) ** 2 for p, h in xs) / len(xs)
    print(f"{seasons} seasons, {len(scores['pace'])} team-week predictions (weeks {FROM_WEEK}+)\n")
    for name in ("pace", "sim"):
        print(f"made the playoffs - {name} said / observed / n\n{_deciles(scores[name])}\n")
    print(f"title - sim said / observed / n\n{_deciles(scores['title: sim'])}\n")
    print("Brier (lower is better):")
    for name, xs in scores.items():
        print(f"  {name:12} {brier(xs):.4f}")


if __name__ == "__main__":
    main()
