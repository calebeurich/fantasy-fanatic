"""Startup drafts: drafting edge (S1), edge as a skill across a manager's leagues (S2), edge
paying in season 1 (S3), and going all-in vs building young (S4) over seasons 1-3.
Pre-registered in research/README.md "Startup drafts, going all-in, and league survival".

Run: python -m research.trade_grades && python -m research.startups [league_id to place]
"""

import datetime
import itertools
import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from research.draft_skill import Values, is_superflex, sleeper_to_fp
from research.manager_ev import realized
from research.roster_strength import fit
from research.schedule_luck import season_luck
from research.trade_grades import OUT as GRADE_FILE

CRAWL_DIR = Path("data/crawl")
STARTUP_ROUNDS = 15
SLOT_WINDOW = 3
BOOT = 300


def main(place: str | None) -> None:
    values, xwalk = Values(), sleeper_to_fp()
    sold = defaultdict(float)
    for line in open(GRADE_FILE, encoding="utf-8"):
        r = json.loads(line)
        if not r["in_season"]:
            sold[(r["league"], r["manager"])] -= r["pick_margin"]   # + = future value sold for now
    startups, seasons = [], {}
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        lg = d["league"]
        luck = season_luck(d)
        if luck:
            seasons[lg["league_id"]] = (lg.get("previous_league_id"),
                                        {o: (v["allplay"], realized(d).get(o, 0.0)) for o, v in luck.items()})
        for dr in d.get("drafts") or []:
            if (((dr.get("settings") or {}).get("rounds") or 0) >= STARTUP_ROUNDS and dr.get("status") == "complete"
                    and dr.get("picks") and dr.get("type") != "auction" and dr.get("start_time")):
                day = datetime.date.fromtimestamp(dr["start_time"] / 1000)
                startups.append({"league": lg["league_id"], "season": int(dr["season"]), "sf": is_superflex(lg),
                                 "day": day.isoformat(), "later": (day + datetime.timedelta(days=365)).isoformat(),
                                 "picks": dr["picks"]})
    next_of = {prev: lid for lid, (prev, _) in seasons.items()}

    picks = []
    for s in startups:
        for p in s["picks"]:
            fp = xwalk.get(str(p.get("player_id")))
            v0, v12 = values.at(fp, s["day"], s["sf"]), values.at(fp, s["later"], s["sf"])
            row = values.rows_at(fp, s["day"])
            if v0 is None or v12 is None or not p.get("picked_by"):
                continue
            age = float(row["age"]) if row and row["age"] not in ("", "NA", None) else None
            picks.append({"s": s, "user": p["picked_by"], "pick": p["pick_no"], "v0": v0, "v12": v12, "age": age})
    group = defaultdict(list)
    for p in picks:
        group[(p["s"]["sf"], p["pick"])].append(p)
    for p in picks:
        near = [q for k in range(p["pick"] - SLOT_WINDOW, p["pick"] + SLOT_WINDOW + 1)
                for q in group.get((p["s"]["sf"], k), []) if abs(q["s"]["season"] - p["s"]["season"]) <= 1]
        p["edge"] = p["v0"] - statistics.mean(q["v0"] for q in near)
        p["out"] = p["v12"] - statistics.mean(q["v12"] for q in near)
    print(f"{len(startups)} startup drafts, {len(picks)} priced picks")
    print(f"S1 draft-day edge -> value a year on, per pick: r = "
          f"{statistics.correlation([p['edge'] for p in picks], [p['out'] for p in picks]):+.3f}")

    teams = defaultdict(list)
    for p in picks:
        teams[(p["s"]["league"], p["user"])].append(p)
    by_user = defaultdict(list)
    rows = []
    for (lid, user), ps in teams.items():
        known = [(p["v0"], p["age"]) for p in ps if p["age"] is not None]
        tv = sum(v for v, _ in known)
        rows.append({"league": lid, "user": user, "edge": statistics.mean(p["edge"] for p in ps),
                     "slot": statistics.mean(sorted(p["pick"] for p in ps)[:3]),   # its three earliest picks
                     "value": sum(p["v0"] for p in ps), "age": sum(v * a for v, a in known) / tv if tv else None,
                     "sold": sold[(lid, user)]})
        by_user[user].append(rows[-1]["edge"])
    pairs = [(a, b) for v in by_user.values() if len(v) >= 2 for a, b in itertools.combinations(v, 2)]
    print(f"S2 edge across a manager's startups: r = {statistics.correlation([a for a, _ in pairs], [b for _, b in pairs]):+.3f} "
          f"({sum(len(v) >= 2 for v in by_user.values())} managers, {len(pairs)} pairs)")

    by_league = defaultdict(list)
    for r in rows:
        by_league[r["league"]].append(r)
    for lid, rs in by_league.items():
        for k in ("value", "age", "sold", "slot"):
            known = [r[k] for r in rs if r[k] is not None]
            if not known:
                continue
            mean, sd = statistics.mean(known), statistics.pstdev(known) or 1
            for r in rs:
                if r[k] is not None:
                    r[k + "_z"] = (r[k] - mean) / sd
        later = [lid]
        for _ in range(2):
            later.append(next_of.get(later[-1]))
        for r in rs:
            r["seasons"] = [seasons.get(l, (None, {}))[1].get(r["user"]) if l else None for l in later]
            r["all_in"] = r.get("age_z", 0) + r.get("sold_z", 0)
    ok = [r for r in rows if "value_z" in r and r["seasons"][0]]
    data = [(r, r["seasons"][0][0]) for r in ok]
    c = fit(data, ["slot_z", "edge"])
    print(f"S3 season-1 all-play per SD of edge (draft slot held): "
          f"{c[2] * statistics.pstdev(r['edge'] for r, _ in data):+.4f} (n={len(data)})")

    rng = random.Random(0)

    def weight(rs, y, label):
        by = defaultdict(list)
        for r in rs:
            by[r["league"]].append((r, y(r)))
        keys, cols = list(by), ["value_z", "all_in"]
        est = sorted(fit([p for k in (rng.choice(keys) for _ in keys) for p in by[k]], cols)[2] for _ in range(BOOT))
        w = fit([p for v in by.values() for p in v], cols)[2]
        print(f"   {label:38} n={len(rs):4}  per SD of all-in {w:+.3f} [{est[int(.05 * BOOT)]:+.3f}, {est[int(.95 * BOOT)]:+.3f}]")

    print("S4 all-in (older drafted roster + future picks sold), starting value held:")
    one_two = [r for r in ok if r["seasons"][1]]
    weight(one_two, lambda r: r["seasons"][0][1] + r["seasons"][1][1], "payout seasons 1-2 ($)")
    three = [r for r in ok if r["seasons"][1] and r["seasons"][2]]
    weight(three, lambda r: r["seasons"][2][0], "all-play season 3")
    weight(three, lambda r: sum(s[1] for s in r["seasons"]), "payout seasons 1-3 ($)")
    for k, label in ((0, "season 1"), (1, "season 2"), (2, "season 3")):
        q = sorted(r["all_in"] for r in three)
        top, bottom = [r for r in three if r["all_in"] >= q[3 * len(q) // 4]], [r for r in three if r["all_in"] <= q[len(q) // 4]]
        print(f"     {label}: all-in quartile ${statistics.mean(r['seasons'][k][1] for r in top):5.1f} payout, "
              f"{statistics.mean(r['seasons'][k][0] for r in top):.3f} all-play | youth quartile "
              f"${statistics.mean(r['seasons'][k][1] for r in bottom):5.1f}, {statistics.mean(r['seasons'][k][0] for r in bottom):.3f}")
    if place:
        mine = [r for r in rows if r["league"] == place]
        q = sorted(r["all_in"] for r in rows if "all_in" in r)
        for r in sorted(mine, key=lambda r: -r.get("all_in", 0)):
            pct = 100 * sum(x <= r["all_in"] for x in q) / len(q)
            print(f"   {place[-4:]} {r['user']}: all-in {r['all_in']:+.2f} ({pct:.0f}th pct of all startups), "
                  f"age {r['age'] or 0:.1f}, future value sold {r['sold']:+.2f}, edge {r['edge']:+.0f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
