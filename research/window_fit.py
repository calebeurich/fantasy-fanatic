"""Window fit: did a manager do what his roster's window called for? Owner: "the skill part is
more did you do everything you could to compete well at the right time and not just let your
team's value atrophy for no reason while not doing what you could to compete well."

Per team-season: the WINDOW from how the team played at the START (weeks 1-3 points, league
thirds - before trades reshape it; dynasty value was the first cut and mislabeled a young,
weak-now roster), the dynasty-value change split into DRIFT (what the players it held all
along did on their own - aging, injuries) and MOVES (everything gained or lost by trades and
pickups, picks included at their grader price), and the PUSH (this-season title points its
trades added, ex-ante). A contender is judged on the push, everyone else on value.

Run: python -m research.window_fit <current league_id>
"""

import datetime
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from research.draft_skill import Values, is_superflex, sleeper_to_fp
from research.trade_grades import OUT as GRADE_FILE, build_context, grade_season, live_season
from sources import sleeper

CRAWL_DIR = Path("data/crawl")
PUSHED = 0.02      # 2+ title points added by this season's trades
ATROPHY = -0.05    # drift worse than 5% of an average roster
MOVED = 0.02       # net moves smaller than 2% of an average roster are no move at all
WINDOW_WEEKS = 3


def window_rows(d: dict, push: dict, picks: dict, values: Values, xwalk: dict, end_week: int,
                end_day: str) -> dict:
    lg = d["league"]
    sf = is_superflex(lg)
    start_day = f"{lg['season']}-09-05"
    owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
    first = {m["roster_id"]: set(m.get("players") or []) for m in d["matchups"].get("1") or []}
    last = {m["roster_id"]: set(m.get("players") or []) for m in d["matchups"].get(str(end_week)) or []}
    val = lambda p, day: values.at(xwalk.get(str(p)), day, sf) or 0.0
    rows = {}
    for rid in first:
        if rid not in last:
            continue
        start = [val(p, start_day) for p in first[rid]]
        early = statistics.mean(m.get("points") or 0 for w in range(1, WINDOW_WEEKS + 1)
                                for m in d["matchups"].get(str(w)) or [] if m["roster_id"] == rid)
        held = first[rid] & last[rid]
        drift = sum(val(p, end_day) - val(p, start_day) for p in held)
        total_end = sum(val(p, end_day) for p in last[rid])
        rows[owner[rid]] = {"early": early, "start": sum(start), "drift": drift,
                            "moves": total_end - sum(start) - drift, "push": push.get(owner[rid], 0.0),
                            "picks": picks.get(owner[rid], 0.0)}
    mean_start = statistics.mean(r["start"] for r in rows.values())
    ranked = sorted(rows, key=lambda o: -rows[o]["early"])
    third = len(ranked) / 3
    for i, o in enumerate(ranked):
        r = rows[o]
        r["window"] = "contend" if i < third else "rebuild" if i >= 2 * third else "middle"
        r["drift"] /= mean_start
        r["moves"] = r["moves"] / mean_start + r["picks"]   # picks are already league-relative
        r["verdict"] = verdict(r)
    return rows


def verdict(r: dict) -> str:
    if r["window"] == "contend":
        return "pushed" if r["push"] >= PUSHED else "stood pat"
    if r["push"] >= PUSHED:
        return "pushed (pivot)"
    if r["moves"] >= MOVED and r["moves"] + r["drift"] > 0:
        return "built"
    if r["drift"] < ATROPHY and r["moves"] < MOVED:
        return "let value atrophy"
    if r["moves"] <= -MOVED:
        return "lost value in moves"
    return "held" if r["window"] == "rebuild" else "stuck in the middle"


def main(league_id: str) -> None:
    values, xwalk = Values(), sleeper_to_fp()
    live = live_season(league_id)
    prev_id = live["league"]["previous_league_id"]
    last = json.load(open(CRAWL_DIR / f"{prev_id}.json", encoding="utf-8"))
    names = {u["user_id"]: u.get("display_name") for u in last["users"] + sleeper.get_users(league_id)}

    push_last, picks_last = defaultdict(float), defaultdict(float)
    for line in open(GRADE_FILE, encoding="utf-8"):
        r = json.loads(line)
        if r["league"] == prev_id:
            picks_last[r["manager"]] += r["pick_margin"]
            if r["in_season"]:
                push_last[r["manager"]] += r["now_titles"]
    ctx, _ = build_context()
    push_now, picks_now = defaultdict(float), defaultdict(float)
    for r in grade_season(live, ctx):
        picks_now[r["manager"]] += r["pick_margin"]
        if r["in_season"]:
            push_now[r["manager"]] += r["now_titles"]

    weeks = (last["league"]["settings"].get("playoff_week_start") or 15) - 1
    a = window_rows(last, push_last, picks_last, values, xwalk, weeks, f"{last['league']['season']}-12-10")
    current = int(sleeper.get_nfl_state()["week"])
    b = window_rows(live, push_now, picks_now, values, xwalk, current - 1, values.dates[-1])

    print(f"{live['league']['name']}: window from weeks 1-3 scoring; value change in "
          f"shares of an average roster (drift = held players on their own, moves = trades + adds + picks)\n")
    for label, rows in ((last["league"]["season"], a), (f"{live['league']['season']} so far", b)):
        print(f"--- {label}")
        for o in sorted(rows, key=lambda o: -rows[o]["early"]):
            r = rows[o]
            print(f"  {(names.get(o) or o)[:15]:15} {r['window']:8} drift {r['drift']:+.2f}  moves {r['moves']:+.2f}  "
                  f"push {100 * r['push']:+5.1f} title pts  -> {r['verdict']}")


if __name__ == "__main__":
    main(sys.argv[1])
