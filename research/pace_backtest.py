"""Are the playoff odds honest? Owner (2026-08-21): "cant we do this in season stuff as
if it was last year and check it on that."

Replay every completed crawled season week by week - at each week k, feed only what a
live system would have known (record to date with median games, points per game to date
as the strength input, points for, the remaining posted schedule, the league's playoff
settings) - through `season_sim.simulate` (what `team_state.playoff_pace` serves),
and score it against the real winners bracket: who made it, who got a bye, who won it.
The closed form it replaced scored 0.107 here (research/README.md).

The one substitution: ppg-to-date stands in for ePPG. Sleeper keeps each past week's
final line, but not what it projected for later weeks at the time - ePPG's view, which
`research/save_projections.py` saves from 2026 on. That validates the MATH (record + schedule + rules + the noise model), not
projection quality.

`--projected` (2026-11-07) scores a second strength on the same frames: the lineup the live
code would start from Sleeper's week k+1 projections - see research/README.md "Pace backtest
on projected strength" for the leaks it is built to avoid.

`--settles` (2026-11-23) scores `settles`, the live "ride it out" signal: at each week
before the trade deadline, the chance the sim gives that a team's odds will be clearly in
or out (75%+ / 25%-) by the deadline - against what the sim actually said at the deadline
frame. That tests the app's forecast of its own future answer, which is exactly what the
"wait - the season decides" path promises.

Run: python -m research.pace_backtest [--projected | --settles]
"""

import functools
import gzip
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from analysis.roster_needs import fill_lineup, lineup_slots
from analysis.season_sim import STRENGTH_SD_SHARE, WEEKLY_SD_SHARE, simulate
from sources import sleeper
from sources.nflverse_ids import sleeper_names

CRAWL_DIR = Path("data/crawl")
FROM_WEEK = 3   # before this, ppg-to-date is too noisy to call a strength input
RUNS = 1000     # per frame; the live default is 5000, this keeps the replay to minutes
PROJ_CACHE = Path("data/research/pace_projected")
IDP = {"DL", "LB", "DB", "IDP_FLEX"}


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
            "field": field, "byes": field - round1, "champ": champ,
            "rosters": {m["roster_id"]: m.get("players") or [] for m in matchups[k]}})
    return frames


@functools.cache
def week_lines(season: str, week: int) -> tuple[dict, set, dict]:
    """One week's final projections: player_id -> raw stat line, the NFL teams on bye, and
    player_id -> NFL team. Cached on disk - past weeks don't change."""
    path = PROJ_CACHE / f"{season}_{week}.json.gz"
    if not path.exists():
        rows = sleeper.projection_rows(season, week)
        bye = sorted({r["team"] for r in rows if r.get("team")} - {r["team"] for r in rows if r.get("opponent")})
        out = [{r["player_id"]: r["stats"] for r in rows if r.get("stats")}, bye,
               {r["player_id"]: r.get("team") for r in rows}]
        path.write_bytes(gzip.compress(json.dumps(out).encode()))
    lines, bye, team = json.loads(gzip.decompress(path.read_bytes()))
    return lines, set(bye), team


def projected_strength(players: list, league: dict, k: int, last_week: int, position: dict) -> tuple:
    """(eppg, {week: points}) for the week k roster: its optimal lineup on week k+1's
    projections, league-scored the way the live app scores them, re-filled each remaining
    week without whoever is on bye then - the bye schedule is public all season, and the
    live app's weekly strengths route around byes the same way. A player on bye in k+1
    itself is priced from his week k line."""
    lines, bye, team = week_lines(league["season"], k + 1)
    before, _, _ = week_lines(league["season"], k)
    week = {pid: {"position": position.get(pid),
                  "projected_ppg": sleeper.score((before if team.get(pid) in bye else lines).get(pid, {}),
                                                 league["scoring_settings"])} for pid in players}
    slots, flex = lineup_slots(league["roster_positions"])
    best = lambda out: sum(week[pid]["projected_ppg"] for _, pid in fill_lineup(
        {"players": [p for p in players if team.get(p) not in out]}, week, slots, flex))
    return best(set()), {w: best(week_lines(league["season"], w)[1]) for w in range(k + 1, last_week + 1)}


def _sim(frame: dict, deadline_played: int | None = None) -> dict:
    """The live drift, plus how wrong ppg-to-date is as a strength after k games - the
    live input (ePPG) doesn't carry that second term."""
    level = statistics.mean(t["eppg"] for t in frame["teams"])
    sd = math.hypot(WEEKLY_SD_SHARE * level / math.sqrt(frame["k"]), STRENGTH_SD_SHARE * level)
    return simulate(frame["teams"], frame["settings"], frame["k"], frame["schedule"], strength_sd=sd,
                    runs=RUNS, deadline_played=deadline_played)


def settles_pairs(frames: list, settings: dict) -> list:
    """(settles forecast, odds actually clear at the deadline) for every team-week before
    the deadline - the deadline read the way team_state.playoff_pace reads it."""
    season_weeks = (settings.get("playoff_week_start") or 15) - 1
    deadline = settings.get("trade_deadline") or 0
    deadline = deadline if 0 < deadline <= season_weeks else season_weeks
    at = next((fr for fr in frames if fr["k"] == deadline - 1), None)
    if at is None:
        return []
    clear = {me: not 0.25 < o["playoffs"] < 0.75 for me, o in _sim(at).items()}
    return [(o["settles"], clear[me], o["playoffs"])
            for fr in frames if fr["k"] < deadline - 1
            for me, o in _sim(fr, deadline - 1).items()]


def _deciles(pairs: list) -> str:
    buckets = defaultdict(lambda: [0, 0])
    for p, hit in pairs:
        b = buckets[min(int(p * 10), 9)]
        b[0] += 1; b[1] += hit
    return "\n".join(f"  {10 * d:3}-{10 * d + 10:3}%   {100 * h / n:5.1f}%  {n:5}"
                     for d, (n, h) in sorted(buckets.items()))


def main():
    if "--settles" in sys.argv:
        return main_settles()
    projected = "--projected" in sys.argv
    position = {p: v["position"] for p, v in sleeper_names().items()} if projected else {}
    PROJ_CACHE.mkdir(parents=True, exist_ok=True)
    scores = defaultdict(list)   # metric -> [(p, hit)]
    seasons = 0
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        league = d.get("league") or {}
        if projected and (int(league.get("season") or 0) < 2020
                          or IDP & set(league.get("roster_positions") or [])):
            continue
        frames = season_frames(d)
        seasons += bool(frames)
        for fr in frames:
            sim = _sim(fr)
            if projected:
                last = fr["settings"]["playoff_week_start"] - 1
                strong = {r: projected_strength(fr["rosters"].get(r, []), league, fr["k"], last, position)
                          for r in fr["rosters"]}
                teams = [{**t, "eppg": strong.get(t["owner_id"], (0, {}))[0]} for t in fr["teams"]]
                proj = simulate(teams, fr["settings"], fr["k"], fr["schedule"],
                                strengths={r: s[1] for r, s in strong.items()}, runs=RUNS)
            n, slots = len(fr["teams"]), fr["settings"]["playoff_teams"]
            in_now = {t["owner_id"] for t in sorted(
                fr["teams"], key=lambda t: (-t["wins"], -t["points_for"]))[:slots]}
            for t in fr["teams"]:
                me = t["owner_id"]
                made = me in fr["field"]
                scores["sim"].append((sim[me]["playoffs"], made))
                if projected:
                    scores["sim: projected"].append((proj[me]["playoffs"], made))
                    phase = "early" if fr["k"] <= 6 else "late"
                    scores[f"{phase}: ppg"].append((sim[me]["playoffs"], made))
                    scores[f"{phase}: projected"].append((proj[me]["playoffs"], made))
                scores["top-k today"].append((float(me in in_now), made))
                scores["base rate"].append((slots / n, made))
                if fr["byes"]:
                    scores["bye: sim"].append((sim[me]["bye"], me in fr["byes"]))
                    scores["bye: base"].append((len(fr["byes"]) / n, me in fr["byes"]))
                if fr["champ"] is not None:
                    scores["title: sim"].append((sim[me]["title"], me == fr["champ"]))
                    scores["title: base"].append((1 / n, me == fr["champ"]))
    brier = lambda xs: sum((p - h) ** 2 for p, h in xs) / len(xs)
    print(f"{seasons} seasons, {len(scores['sim'])} team-week predictions (weeks {FROM_WEEK}+)\n")
    print(f"made the playoffs - sim said / observed / n\n{_deciles(scores['sim'])}\n")
    if projected:
        print(f"made the playoffs, projected strength{chr(10)}{_deciles(scores['sim: projected'])}{chr(10)}")
    print(f"title - sim said / observed / n\n{_deciles(scores['title: sim'])}\n")
    print("Brier (lower is better):")
    for name, xs in scores.items():
        print(f"  {name:12} {brier(xs):.4f}")


def main_settles():
    pairs, seasons = [], 0
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        frames = season_frames(d)
        got = settles_pairs(frames, (d.get("league") or {}).get("settings") or {}) if frames else []
        seasons += bool(got)
        pairs += got
    brier = lambda xs: sum((p - h) ** 2 for p, h in xs) / len(xs)
    # The rule only reads `settles` for a middle team, so that is the population that counts.
    middle = [(p, h) for p, h, now in pairs if 0.25 < now < 0.75]
    pairs = [(p, h) for p, h, _ in pairs]
    base = sum(h for _, h in pairs) / len(pairs)
    print(f"middle teams (odds 25-75% now): {len(middle)} team-weeks - settles said / observed / n"
          f"{chr(10)}{_deciles(middle)}{chr(10)}")
    for cut in (0.2, 0.3, 0.4, 0.5):
        wait = [h for p, h in middle if p >= cut]
        dec = [h for p, h in middle if p < cut]
        print(f"  cut {cut:.1f}: ride it out {len(wait)} ({100 * sum(wait) / max(len(wait), 1):.0f}% settled), "
              f"decide {len(dec)} ({100 * sum(dec) / max(len(dec), 1):.0f}% settled)")
    print()
    print(f"{seasons} seasons, {len(pairs)} team-weeks before a deadline\n")
    print(f"odds clear at the deadline - settles said / observed / n\n{_deciles(pairs)}\n")
    print(f"Brier: settles {brier(pairs):.4f}   base rate ({100 * base:.0f}% clear) {brier([(base, h) for _, h in pairs]):.4f}")
    wait = [h for p, h in pairs if p >= 0.5]
    print(f"would ride it out (settles >= 50%): {len(wait)} team-weeks, {100 * sum(wait) / len(wait):.1f}% "
          f"actually clear by the deadline")


if __name__ == "__main__":
    main()
