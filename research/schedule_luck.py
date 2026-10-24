"""Schedule luck: how many wins did the schedule hand each manager? Track 3's first
channel - the part of a record that is NOT skill, separated before anything else is
called skill (ROADMAP "Manager scores").

Luck = actual head-to-head wins - all-play expected wins, where each week's all-play
share is the fraction of the league that team outscored. Median-league games are left out
of both sides: a median game is already an all-play game. Tests the four hypotheses
pre-registered in research/README.md "Schedule luck", on every completed crawled season,
following each manager into the next season of the same league.

Run: python -m research.schedule_luck
"""

import json
import statistics
from collections import defaultdict
from pathlib import Path

CRAWL_DIR = Path("data/crawl")


def season_luck(d: dict) -> dict:
    """owner_id -> {"wins", "expected", "luck", "allplay", "games"} for one completed
    regular season, or {} if it isn't one."""
    st = (d.get("league") or {}).get("settings") or {}
    weeks = (st.get("playoff_week_start") or 15) - 1
    matchups = {int(w): ms for w, ms in (d.get("matchups") or {}).items() if ms}
    if not d.get("winners_bracket") or any(w not in matchups for w in range(1, weeks + 1)):
        return {}
    if any(not any(m.get("points") for m in matchups[w]) for w in range(1, weeks + 1)):
        return {}   # unplayed: every score 0 (the 2026 crawl)
    return luck_by_owner(matchups, d["rosters"], weeks)


def luck_by_owner(matchups: dict, rosters: list, weeks: int) -> dict:
    """The luck arithmetic over weeks 1..`weeks` of {week: [matchup rows]} - shared by the
    crawl replay and a live league mid-season."""
    owner = {r["roster_id"]: r.get("owner_id") for r in rosters}
    out = defaultdict(lambda: {"wins": 0.0, "expected": 0.0, "games": 0})
    for w in range(1, weeks + 1):
        pts = {m["roster_id"]: m["points"] for m in matchups[w] if m.get("points") is not None}
        games = defaultdict(list)
        for m in matchups[w]:
            if m["roster_id"] in pts:
                games[m.get("matchup_id")].append(m["roster_id"])
        for a, b in (g for g in games.values() if len(g) == 2):
            for me, them in ((a, b), (b, a)):
                rec = out[owner.get(me)]
                rec["wins"] += 1 if pts[me] > pts[them] else 0.5 if pts[me] == pts[them] else 0
                rec["expected"] += sum(pts[me] > pts[o] for o in pts if o != me) / (len(pts) - 1)
                rec["games"] += 1
    out.pop(None, None)
    for rec in out.values():
        rec["luck"] = rec["wins"] - rec["expected"]
        rec["allplay"] = rec["expected"] / rec["games"] if rec["games"] else 0
    return dict(out)


def main():
    seasons = {}
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        luck = season_luck(d)
        if luck:
            seasons[d["league"]["league_id"]] = (d["league"].get("previous_league_id"), luck)
    rows = [r for _, luck in seasons.values() for r in luck.values()]
    lucks = [r["luck"] for r in rows]
    print(f"{len(seasons)} completed seasons, {len(rows)} manager-seasons")
    print(f"H1 luck SD {statistics.pstdev(lucks):.2f} wins "
          f"(games/season median {statistics.median(r['games'] for r in rows):.0f}); "
          f"10th-90th pct {statistics.quantiles(lucks, n=10)[0]:+.1f} to {statistics.quantiles(lucks, n=10)[-1]:+.1f}")

    pairs = []   # (this season, next season) for the same manager in the same league
    for prev_id, luck in seasons.values():
        if prev_id in seasons:
            before = seasons[prev_id][1]
            pairs += [(before[o], luck[o]) for o in luck if o in before]
    corr = lambda xs, ys: statistics.correlation(xs, ys)
    print(f"\n{len(pairs)} manager-season pairs (same manager, same league, back to back)")
    print(f"H2 luck year-over-year r = {corr([a['luck'] for a, _ in pairs], [b['luck'] for _, b in pairs]):+.3f}")
    print(f"H3 all-play win% year-over-year r = "
          f"{corr([a['allplay'] for a, _ in pairs], [b['allplay'] for _, b in pairs]):+.3f}")
    nxt = [b["wins"] / b["games"] for _, b in pairs]
    print(f"H4 next season's win% from this season's: actual win% r = "
          f"{corr([a['wins'] / a['games'] for a, _ in pairs], nxt):+.3f}, "
          f"all-play win% r = {corr([a['allplay'] for a, _ in pairs], nxt):+.3f}")


if __name__ == "__main__":
    main()
