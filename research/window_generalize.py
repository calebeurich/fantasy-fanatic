"""Does window fit generalize? `window_fit.py`'s per-team-season verdicts, computed on every
completed season in the corpus, tested against what came next. V1-V5 pre-registered in
research/README.md "Window fit, generalized".

Run: python -m research.trade_grades && python -m research.window_generalize
"""

import json
import random
import statistics
from collections import defaultdict
from pathlib import Path

from research.bias_checks import placements
from research.draft_skill import Values, sleeper_to_fp
from research.roster_strength import fit, predict, season_rosters
from research.schedule_luck import season_luck
from research.trade_grades import OUT as GRADE_FILE
from research.window_fit import _future_picks, window_rows

CRAWL_DIR = Path("data/crawl")
SCORE = {"pushed": 1, "pushed (pivot)": 1, "spent future to push": 1, "built": 1,
         "rebuilt younger": 1, "held": 0, "stood pat": 0,
         "let value atrophy": -1, "lost value in moves": -1, "stuck in the middle": -1}
BOOT = 300
MIN_ROSTER_VALUE = 5000


def main():
    values, xwalk = Values(), sleeper_to_fp()
    push, picks = defaultdict(lambda: defaultdict(float)), defaultdict(lambda: defaultdict(float))
    for line in open(GRADE_FILE, encoding="utf-8"):
        r = json.loads(line)
        if r["in_season"]:
            push[r["league"]][r["manager"]] += r["now_titles"]
        if r["in_season"] or _future_picks(r):
            picks[r["league"]][r["manager"]] += r["pick_margin"]

    seasons = {}
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        luck = season_luck(d)
        if not luck:
            continue
        lid, lg = d["league"]["league_id"], d["league"]
        weeks = (lg["settings"].get("playoff_week_start") or 15) - 1
        try:
            wf = window_rows(d, push[lid], picks[lid], values, xwalk, weeks, f"{lg['season']}-12-10")
        except (KeyError, statistics.StatisticsError, ZeroDivisionError):
            continue
        if not wf or statistics.mean(r["start"] for r in wf.values()) < MIN_ROSTER_VALUE:
            continue   # rosters this far back barely map to DP ids - no honest price
        rosters = season_rosters(d, values, xwalk)
        owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
        top3 = {owner.get(rid) for rid, pl in placements(d).items() if pl <= 3}
        has3 = bool(top3)
        early = statistics.mean(r["early"] for r in wf.values()) or 1
        mean_start = statistics.mean(r["start"] for r in wf.values())
        feats = {}
        for o, w in wf.items():
            if o in rosters:
                feats[o] = {**rosters[o], **w, "score": SCORE[w["verdict"]],
                            "atrophy": float(w["verdict"] == "let value atrophy"),
                            "pushed": float(w["push"] >= 0.02), "early_rel": w["early"] / early,
                            "start_rel": w["start"] / mean_start,
                            "top3": o in top3, "has3": has3}
        seasons[lid] = (lg.get("previous_league_id"), int(lg["season"]), feats)
    next_of = {prev: lid for lid, (prev, _, _) in seasons.items()}
    corr = statistics.correlation
    rng = random.Random(0)

    def later(lid, h):
        for _ in range(h):
            lid = next_of.get(lid)
        return lid

    def pairs(h, keep=lambda x: True):
        by = defaultdict(list)
        for lid, (_, _, fs) in seasons.items():
            nxt = later(lid, h)
            if nxt:
                by[lid] += [(x, seasons[nxt][2][o]["allplay"]) for o, x in fs.items()
                            if o in seasons[nxt][2] and keep(x)]
        return by

    def weight(by, cols, k):
        data = [p for v in by.values() for p in v]
        keys = list(by)
        sd = statistics.pstdev(x[k] for x, _ in data) or 1
        est = sorted(fit([p for kk in (rng.choice(keys) for _ in keys) for p in by[kk]], cols)[cols.index(k) + 1] * sd
                     for _ in range(BOOT))
        return fit(data, cols)[cols.index(k) + 1] * sd, est[int(.05 * BOOT)], est[int(.95 * BOOT)], len(data)

    rows = [x for _, _, fs in seasons.values() for x in fs.values()]
    print(f"{len(seasons)} seasons, {len(rows)} team-seasons; verdicts: "
          + ", ".join(f"{v} {sum(x['verdict'] == v for x in rows)}" for v in SCORE))

    noncontend = lambda x: x["window"] != "contend"
    w, lo, hi, n = weight(pairs(1, noncontend), ["allplay", "total", "atrophy"], "atrophy")
    print(f"\nV1 atrophy -> next season, non-contenders n={n}: {w:+.4f} per SD [{lo:+.4f}, {hi:+.4f}]")
    w, lo, hi, n = weight(pairs(2, noncontend), ["allplay", "total", "moves"], "moves")
    print(f"V2 non-contenders' moves -> two seasons out n={n}: {w:+.4f} per SD [{lo:+.4f}, {hi:+.4f}]")

    contenders = [x for x in rows if x["window"] == "contend" and x["has3"]]
    data = [(x, float(x["top3"])) for x in contenders]
    c = fit(data, ["early_rel", "pushed"])
    print(f"V3 contenders n={len(data)}: top-3 this season - pushed {100 * statistics.mean(y for x, y in data if x['pushed']):.1f}% "
          f"vs stood pat {100 * statistics.mean(y for x, y in data if not x['pushed']):.1f}%; "
          f"with early strength held, pushing adds {100 * c[2]:+.1f} pts")

    per = defaultdict(list)
    for lid, (_, _, fs) in seasons.items():
        nxt = later(lid, 1)
        if nxt:
            per["pairs"] += [(x["score"], seasons[nxt][2][o]["score"]) for o, x in fs.items() if o in seasons[nxt][2]]
    print(f"V4 verdict score year over year: r = {corr([a for a, _ in per['pairs']], [b for _, b in per['pairs']]):+.3f} "
          f"(n={len(per['pairs'])})")

    flat = [(x, y, seasons[lid][1]) for lid, v in pairs(1).items() for x, y in v]
    for name, cols in (("all-play + starter + bench", ["allplay", "starter", "bench"]),
                       ("+ verdict score", ["allplay", "starter", "bench", "score"]),
                       ("+ push, moves, drift", ["allplay", "starter", "bench", "push", "moves", "drift"])):
        preds, ys = [], []
        for s in sorted({t for *_, t in flat}):
            c = fit([(x, y) for x, y, t in flat if t != s], cols)
            te = [(x, y) for x, y, t in flat if t == s]
            preds += predict(c, te, cols)
            ys += [y for _, y in te]
        print(f"V5 {name:30} leave-one-season-out r = {corr(preds, ys):+.4f}")
    isolate(flat)


def isolate(flat: list) -> None:
    """V5 credits the END-of-season roster, which already holds the manager's moves. Start from
    the roster the team BEGAN with and ask what the season's decisions added on top."""
    corr = statistics.correlation
    for name, cols in (("start roster + all-play", ["allplay", "start_rel"]),
                       ("+ the season's moves and push", ["allplay", "start_rel", "moves", "push"]),
                       ("+ drift too", ["allplay", "start_rel", "moves", "push", "drift"])):
        preds, ys = [], []
        for s in sorted({t for *_, t in flat}):
            c = fit([(x, y) for x, y, t in flat if t != s], cols)
            te = [(x, y) for x, y, t in flat if t == s]
            preds += predict(c, te, cols)
            ys += [y for _, y in te]
        full = fit([(x, y) for x, y, _ in flat], cols)
        sd = lambda k: statistics.pstdev(x[k] for x, _, _ in flat)
        print(f"V6 {name:30} r = {corr(preds, ys):+.4f}   per SD: "
              + "  ".join(f"{c} {b * sd(c):+.4f}" for c, b in zip(cols, full[1:])))


if __name__ == "__main__":
    main()
