"""The league report card: every manager-score channel for one live league, this season to
date. Each column is a finding from research/README.md - all-play is the skill half of a
record, luck is the rest, activity is the behavior that keeps paying, trades are graded in
title odds ex-ante, picks in value.

Run: python -m research.report_card <league_id>
"""

import sys
from collections import defaultdict

from research.manager_habits import moves
from research.schedule_luck import luck_by_owner
from research.trade_grades import build_context, grade_season, live_season
from sources import sleeper


def main(league_id: str) -> None:
    ctx, _ = build_context()
    d = live_season(league_id)
    current = int(sleeper.get_nfl_state()["week"])   # in progress - only weeks before it are final
    played = {w: ms for w, ms in d["matchups"].items()
              if int(w) < current and any(m.get("points") for m in ms)}
    luck = luck_by_owner({int(w): ms for w, ms in played.items()}, d["rosters"], len(played))
    trades, total = moves(d)
    graded = defaultdict(lambda: [0.0, 0.0, 0])
    for r in grade_season(d, ctx):
        g = graded[r["manager"]]
        g[0] += r["titles"]
        g[1] += r["pick_margin"]
        g[2] += 1
    users = {u["user_id"]: u["display_name"] for u in sleeper.get_users(league_id)}
    print(f"{d['league']['name']} - {len(played)} weeks\n")
    print(f"{'manager':16} {'record':>6} {'earned':>6} {'luck':>5} {'all-play':>8}  {'moves':>5} "
          f"{'trades':>6} {'trade titles':>12} {'picks':>6}")
    for o, l in sorted(luck.items(), key=lambda kv: -kv[1]["allplay"]):
        g = graded[o]
        print(f"{users.get(o, o)[:16]:16} {l['wins']:6.0f} {l['expected']:6.1f} {l['luck']:+5.1f} "
              f"{100 * l['allplay']:7.0f}%  {total[o]:5} {trades[o]:6} {100 * g[0]:+11.1f}% {g[1]:+6.2f}")


if __name__ == "__main__":
    main(sys.argv[1])
