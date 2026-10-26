"""Manager scorecards for one league: every channel the research found, per manager, for the
last completed season and the live one - plus the full-corpus model's prediction of this
season from last season's profile, checked against this season so far.

Run: python -m research.manager_cards <current league_id>
"""

import datetime
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from research.bias_checks import placements
from research.draft_skill import Values, is_superflex, sleeper_to_fp
from research.manager_habits import moves
from research.more_skills import lineup_habits
from research.roster_strength import fit, predict
from research.schedule_luck import luck_by_owner, season_luck
from research.trade_grades import build_context, grade_season, live_season
from research.what_wins import BASE, BEHAVIOR, all_pairs, load_grades, season_features
from sources import sleeper
from sources.nflverse_ids import sleeper_names

CRAWL_DIR = Path("data/crawl")


def draft_edge(draft: dict, sf: bool, values: Values, xwalk: dict) -> dict:
    """user_id -> mean draft-day value over the slot's class expectation (pick +/-2, same
    season and format, every crawled rookie draft) - the drafting trait that held: not reaching."""
    season, day = draft["season"], datetime.date.fromtimestamp(draft["start_time"] / 1000).isoformat()
    by_pick = defaultdict(list)
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        if is_superflex(d["league"]) != sf:
            continue
        for dr in d.get("drafts") or []:
            if (dr.get("season") == season and dr.get("status") == "complete" and dr.get("picks")
                    and ((dr.get("settings") or {}).get("rounds") or 99) <= 6 and dr.get("start_time")):
                dday = datetime.date.fromtimestamp(dr["start_time"] / 1000).isoformat()
                for p in dr["picks"]:
                    v = values.at(xwalk.get(str(p.get("player_id"))), dday, sf)
                    if v is not None:
                        by_pick[p["pick_no"]].append(v)
    edges = defaultdict(list)
    for p in draft.get("picks") or []:
        near = [v for k in range(p["pick_no"] - 2, p["pick_no"] + 3) for v in by_pick.get(k, [])]
        v = values.at(xwalk.get(str(p.get("player_id"))), day, sf)
        if near and v is not None:
            edges[p.get("picked_by")].append(v - statistics.mean(near))
    return {u: statistics.mean(e) for u, e in edges.items()}


def main(league_id: str) -> None:
    values, xwalk = Values(), sleeper_to_fp()
    grades = load_grades()
    pairs = all_pairs(values, xwalk, grades)
    cols = BASE + BEHAVIOR
    coef = fit([(x, n["allplay"]) for x, n, _ in pairs], cols)

    live = live_season(league_id)
    prev_id = live["league"]["previous_league_id"]
    last = json.load(open(CRAWL_DIR / f"{prev_id}.json", encoding="utf-8"))
    names = {u["user_id"]: u.get("display_name") for u in last["users"] + sleeper.get_users(league_id)}
    feats = season_features(last, values, xwalk, grades)
    owner = {r["roster_id"]: r.get("owner_id") for r in last["rosters"]}
    place = {owner.get(rid): pl for rid, pl in placements(last).items()}
    position = {p: v["position"] for p, v in sleeper_names().items()}
    dead = lineup_habits(last, position)
    league_dead = statistics.mean(v["dead_rate"] for v in dead.values() if v["dead_rate"] is not None)

    current = int(sleeper.get_nfl_state()["week"])
    played = {int(w): ms for w, ms in live["matchups"].items()
              if int(w) < current and any(m.get("points") for m in ms)}
    now = luck_by_owner(played, live["rosters"], len(played))
    live_trades, live_moves = moves(live)
    ctx, _ = build_context()
    live_grade = defaultdict(lambda: [0.0, 0.0])
    for r in grade_season(live, ctx):
        live_grade[r["manager"]][0] += r["titles"]
        live_grade[r["manager"]][1] += r["pick_margin"]
    draft = next((dr for dr in sleeper._get(f"/league/{league_id}/drafts") or []
                  if dr.get("status") == "complete"), None)
    edge = draft_edge(sleeper._get(f"/draft/{draft['draft_id']}") | {"picks": sleeper._get(f"/draft/{draft['draft_id']}/picks")},
                      is_superflex(live["league"]), values, xwalk) if draft else {}

    print(f"{live['league']['name']}: {last['league']['season']} complete, {live['league']['season']} through week {len(played)}")
    print(f"model fitted on {len(pairs)} manager-season pairs; 'pred' = predicted {live['league']['season']} all-play from the {last['league']['season']} profile\n")
    head = (f"{'manager':15} | {'fin':>3} {'allpl':>5} {'luck':>5} {'moves':>5} {'trd':>3} {'trTitl':>6} {'picks':>6} {'dead':>5} "
            f"| {'pred':>5} {'now':>5} {'luck':>5} {'moves':>5} {'trd':>3} {'trTitl':>6} {'picks':>6} {'draft':>6}")
    print(head)
    print("-" * len(head))
    order = sorted(set(feats) | set(now), key=lambda o: -(now.get(o, {}).get("allplay", 0)))
    for o in order:
        f, n = feats.get(o), now.get(o, {})
        pred = predict(coef, [(f, 0)], cols)[0] if f else None
        dr = dead.get(o, {}).get("dead_rate")
        last_cols = (f"{place.get(o, '-'):>3} {100 * f['allplay']:5.0f} {f['luck']:+5.1f} {f['moves']:5} {f['trades']:3} "
                     f"{100 * (f['trade_now'] + f['trade_next']):+6.1f} {f['picks_bought']:+6.2f} "
                     f"{100 * (dr - league_dead):+5.1f}" if f else f"{'(new)':>45}")
        g = live_grade.get(o, [0.0, 0.0])
        print(f"{(names.get(o) or o)[:15]:15} | {last_cols} | "
              f"{(100 * pred if pred is not None else float('nan')):5.0f} {100 * n.get('allplay', 0):5.0f} "
              f"{n.get('luck', 0):+5.1f} {live_moves[o]:5} {live_trades[o]:3} {100 * g[0]:+6.1f} {g[1]:+6.2f} "
              f"{edge.get(o, float('nan')):+6.0f}")
    print("\nfin = 2025 finish (1-3); allpl = all-play %; luck = wins over earned; trTitl = trade grades in title "
          "points (this + next season); picks = pick value bought (share of an average roster); dead = dead-start "
          "rate vs league (pts); draft = 2026 rookie picks' draft-day value over their slot (DP points, + = took value)")


if __name__ == "__main__":
    main(sys.argv[1])
