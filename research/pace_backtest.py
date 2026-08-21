"""Is the playoff-pace number honest? Owner (2026-08-21): "cant we do this in season
stuff as if it was last year and check it on that."

Replay every crawled season through the REAL `team_state.playoff_pace` - at each week k,
feed it only what a live system would have known (record to date, points per game to
date as the strength input, the remaining posted schedule) and compare the pace it
prints against who actually made that league's playoffs (the winners bracket).

The one substitution: historical projections are not retrievable, so ppg-to-date stands
in for ePPG. That means this validates the MATH (record + schedule + the soft margin
turning strength into probability), not projection quality - which is the part we built.

Run: python -m research.pace_backtest
"""

import json
from collections import defaultdict
from pathlib import Path

from analysis.team_state import playoff_pace

CRAWL_DIR = Path("data/crawl")
FROM_WEEK = 3   # before this, ppg-to-date is too noisy to call a strength input


def season_frames(d: dict):
    """(week_k, rows, schedule, field) replay frames for one crawled season, or []."""
    st = (d.get("league") or {}).get("settings") or {}
    slots, season_weeks = st.get("playoff_teams"), (st.get("playoff_week_start") or 15) - 1
    matchups = {int(w): ms for w, ms in (d.get("matchups") or {}).items() if ms}
    field = {m[k] for m in d.get("winners_bracket") or [] for k in ("t1", "t2")
             if isinstance(m.get(k), int)}
    if not slots or not field or any(w not in matchups for w in range(1, season_weeks + 1)):
        return []
    pts, pair = defaultdict(dict), defaultdict(dict)
    for w in range(1, season_weeks + 1):
        by_matchup = defaultdict(list)
        for m in matchups[w]:
            if m.get("points") is not None:
                pts[m["roster_id"]][w] = m["points"]
                by_matchup[m["matchup_id"]].append(m["roster_id"])
        for two in by_matchup.values():
            if len(two) == 2:
                pair[two[0]][w], pair[two[1]][w] = two[1], two[0]
    rids = sorted(pts)
    if len(rids) < 4 or slots >= len(rids) or not field <= set(rids):
        return []
    frames = []
    for k in range(FROM_WEEK, season_weeks):
        rows = []
        for r in rids:
            wins = sum(1 for w in range(1, k + 1)
                       if pair[r].get(w) and pts[r].get(w, 0) > pts[pair[r][w]].get(w, 0))
            ties = sum(1 for w in range(1, k + 1)
                       if pair[r].get(w) and pts[r].get(w) == pts[pair[r][w]].get(w))
            played = [pts[r][w] for w in range(1, k + 1) if w in pts[r]]
            rows.append({"owner_id": r, "record": {"wins": wins, "ties": ties,
                                                   "losses": len(played) - wins - ties},
                         "starting_production": sum(played) / max(len(played), 1),
                         "path_reason": ""})
        schedule = {r: {w: o for w, o in pair[r].items() if w > k} for r in rids}
        frames.append((k, rows, schedule,
                       {"playoff_teams": slots, "playoff_week_start": season_weeks + 1},
                       field))
    return frames


def main():
    buckets = defaultdict(lambda: [0, 0])   # decile -> [n, made]
    brier = hard = n = 0
    seasons = 0
    for f in sorted(CRAWL_DIR.glob("*.json")):
        frames = season_frames(json.load(open(f, encoding="utf-8")))
        if frames:
            seasons += 1
        for k, rows, schedule, settings, field in frames:
            playoff_pace(rows, settings, schedule)
            in_now = {r["owner_id"] for r in sorted(
                rows, key=lambda r: (-r["record"]["wins"], -r["starting_production"])
            )[:settings["playoff_teams"]]}
            for r in rows:
                if r["playoff_pace"] is None:
                    continue
                made = r["owner_id"] in field
                p = r["playoff_pace"] / 100
                b = buckets[min(int(p * 10), 9)]
                b[0] += 1; b[1] += made
                brier += (p - made) ** 2
                hard += (float(r["owner_id"] in in_now) - made) ** 2   # the cliff we refused
                n += 1
    print(f"{seasons} seasons, {n} team-week predictions (weeks {FROM_WEEK}+)\n")
    print("pace said   observed   n")
    for d in range(10):
        cnt, made = buckets[d]
        if cnt:
            print(f"  {10 * d:3}-{10 * d + 10:3}%     {100 * made / cnt:5.1f}%   {cnt:5}")
    print(f"\nBrier: pace {brier / n:.4f} vs hard top-{'k'}-today cutoff {hard / n:.4f} "
          f"(lower is better; the gap is what the softness buys)")


if __name__ == "__main__":
    main()
