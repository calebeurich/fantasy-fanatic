"""Do folded dynasty leagues act differently? A comparison set only (data/crawl_short/, never
mixed into the main corpus - owner: "we don't really want to cater to dead dynasty leagues,
though good to know if they act different"). Tests F1 and F2 pre-registered in
research/README.md "Picks two seasons out, and folded leagues".

Run: python -m research.folded
"""

import json
import statistics
from pathlib import Path

from research.manager_habits import moves
from research.more_skills import lineup_habits
from research.schedule_luck import season_luck
from sources.nflverse_ids import sleeper_names

LIVING, FOLDED = Path("data/crawl"), Path("data/crawl_short")


def season_activity(d: dict, position: dict) -> dict:
    trades, total = moves(d)
    n = len(d["rosters"]) or 1
    dead = [v["dead_rate"] for v in lineup_habits(d, position).values() if v["dead_rate"] is not None]
    return {"moves": sum(total.values()) / n, "trades": sum(trades.values()) / n,
            "dead": statistics.mean(dead) if dead else None, "season": int(d["league"]["season"]),
            "prev": d["league"].get("previous_league_id")}


def load(folder: Path, position: dict) -> dict:
    out = {}
    for f in sorted(folder.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        if season_luck(d):
            for m in d["matchups"].get("1") or []:
                for p in m.get("players") or []:
                    position.setdefault(p, "DEF" if not p.isdigit() else None)
            out[d["league"]["league_id"]] = season_activity(d, position)
    return out


def main():
    position = {p: v["position"] for p, v in sleeper_names().items()}
    living, folded = load(LIVING, position), load(FOLDED, position)
    has_next = {s["prev"] for s in folded.values()}
    finals = [s for lid, s in folded.items() if lid not in has_next]   # each folded chain's last season
    mean = lambda xs, k: statistics.mean(x[k] for x in xs if x[k] is not None)
    print(f"living: {len(living)} completed seasons | folded: {len(folded)} completed seasons, "
          f"{len(finals)} final seasons")
    for k, label in (("moves", "moves per team"), ("trades", "trades per team"), ("dead", "dead-start rate")):
        print(f"F1 {label:16} living {mean(list(living.values()), k):7.3f}   folded final season {mean(finals, k):7.3f}")
    drops = []
    for lid, last in folded.items():
        if lid in has_next:
            continue
        first, cur = last, last
        while cur["prev"] in folded:
            cur = folded[cur["prev"]]
            first = cur
        if first is not last:
            drops.append(last["moves"] - first["moves"])
    if drops:
        print(f"F2 {len(drops)} folded chains with 2+ completed seasons: moves per team, last minus "
              f"first season, mean {statistics.mean(drops):+.1f} ({sum(d < 0 for d in drops)} declined)")


if __name__ == "__main__":
    main()
