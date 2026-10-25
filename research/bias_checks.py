"""Checks on our own findings, from the owner's critiques and ours: are byes just strength
(B1, B2), do pivot buys deliver (B3), is top 3 the steadier target (B4), does the
same-season alignment lift survive a regression-to-the-mean control (B5), and how often a
"same manager" isn't one. Pre-registered in research/README.md "Bias checks".

Run: python -m research.trade_grades && python -m research.bias_checks
"""

import json
import random
import statistics
from collections import defaultdict
from pathlib import Path

import numpy as np

from research.alignment import aligned, situation, weekly_allplay
from research.roster_strength import fit
from research.schedule_luck import season_luck
from research.what_wins import auc

CRAWL_DIR = Path("data/crawl")
GRADES = Path("data/research/trade_grades.jsonl")
PIVOT_GAIN = 0.02


def logistic(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Newton-fitted logistic weights, intercept first."""
    x = np.column_stack([np.ones(len(x)), x])
    w = np.zeros(x.shape[1])
    for _ in range(50):
        p = 1 / (1 + np.exp(-x @ w))
        w += np.linalg.solve(x.T @ (x * (p * (1 - p))[:, None]) + 1e-9 * np.eye(len(w)), x.T @ (y - p))
    return w


def placements(d: dict) -> dict:
    """roster_id -> 1 / 2 / 3 from the winners bracket's placement games."""
    out = {}
    for m in d.get("winners_bracket") or []:
        if m.get("p") == 1:
            out[m["w"]], out[m["l"]] = 1, 2
        elif m.get("p") == 3:
            out[m["w"]] = 3
    return out


def main():
    seasons, rows_b = {}, []
    owner_changes = [0, 0]
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        luck = season_luck(d)
        if not luck:
            continue
        lid = d["league"]["league_id"]
        owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
        bracket = d.get("winners_bracket") or []
        field = {m[k] for m in bracket for k in ("t1", "t2") if isinstance(m.get(k), int)}
        first = {m[k] for m in bracket if m.get("r") == 1 for k in ("t1", "t2")}
        place = placements(d)
        for rid in field:
            if owner.get(rid) in luck:
                rows_b.append((luck[owner[rid]]["allplay"], rid not in first, place.get(rid) == 1))
        seasons[lid] = {"prev": d["league"].get("previous_league_id"), "owner": owner, "luck": luck,
                        "place": place, "weekly": weekly_allplay(d), "has3": any(m.get("p") == 3 for m in bracket)}
    for lid, s in seasons.items():
        prev = seasons.get(s["prev"])
        if prev:
            for rid, o in s["owner"].items():
                if rid in prev["owner"]:
                    owner_changes[0] += 1
                    owner_changes[1] += prev["owner"][rid] != o

    x = np.array([[a, float(b)] for a, b, _ in rows_b])
    y = np.array([float(t) for *_, t in rows_b])
    w = logistic(x, y)
    p = lambda ap, bye: 1 / (1 + np.exp(-(w[0] + w[1] * ap + w[2] * bye)))
    print(f"B1 playoff teams n={len(rows_b)}: title ~ all-play + bye -> bye weight {w[2]:+.2f} "
          f"(at a .650 all-play: {100 * p(.65, 0):.1f}% without a bye, {100 * p(.65, 1):.1f}% with)")
    raw = statistics.mean(t for _, b, t in rows_b if b) / statistics.mean(t for _, b, t in rows_b if not b)
    print(f"   raw bye ratio {raw:.1f}x; mean all-play bye {statistics.mean(a for a, b, _ in rows_b if b):.3f} "
          f"vs no bye {statistics.mean(a for a, b, _ in rows_b if not b):.3f}")

    # B2, as run: exact seeds aren't recorded in the bracket, so bye teams are compared with
    # the equally many strongest non-bye playoff teams (a stated deviation from "seed 2 vs 3").
    byes = [t for _, b, t in rows_b if b]
    nobye_top = sorted([(a, t) for a, b, t in rows_b if not b], reverse=True)[:len(byes)]
    print(f"B2 bye teams vs the strongest non-bye teams (equal count): titles {100 * statistics.mean(byes):.1f}% vs "
          f"{100 * statistics.mean(t for _, t in nobye_top):.1f}% (their all-play "
          f"{statistics.mean(a for a, b, _ in rows_b if b):.3f} vs {statistics.mean(a for a, _ in nobye_top):.3f})")

    grades = [json.loads(line) for line in open(GRADES, encoding="utf-8")]
    ins = [r for r in grades if r["in_season"] and r["league"] in seasons]

    def lift(r):
        ap = seasons[r["league"]]["weekly"].get(r["manager"], {})
        before = [v for wk, v in ap.items() if wk < r["week"]]
        after = [v for wk, v in ap.items() if wk >= r["week"]]
        return (statistics.mean(after) - statistics.mean(before), statistics.mean(before)) if before and after else None

    groups = {"aligned": [], "pivot (not aligned, +2 title pts)": [], "other": []}
    for r in ins:
        lf = lift(r)
        if lf is None:
            continue
        key = ("aligned" if aligned(r) == 1 else
               "pivot (not aligned, +2 title pts)" if r["now_titles"] >= PIVOT_GAIN else "other")
        groups[key].append((lf, r))
    print("\nB3 rest-of-season all-play lift, all in-season trade sides:")
    for k, v in groups.items():
        print(f"   {k:34} n={len(v):4}  {100 * statistics.mean(l[0] for l, _ in v):+.1f} pts")

    data = [({"before": lf[1], "aligned": float(aligned(r) == 1)}, lf[0]) for r in ins
            if situation(r["allplay_now"]) == "contend" and (lf := lift(r))]
    coef = fit(data, ["before", "aligned"])
    print(f"B5 contenders, lift ~ pre-trade all-play + aligned: aligned weight {100 * coef[2]:+.1f} pts "
          f"(pre-trade all-play weight {coef[1]:+.2f}) n={len(data)}")

    # B4 + A0b on top 3: same model, title vs top-3 target
    t3 = [(r["manager"] in [seasons[r["league"]]["owner"].get(rid) for rid, pl in seasons[r["league"]]["place"].items() if pl <= 3],
           r["top3_before"], r["top3_before"] + r["now_top3"]) for r in ins
          if situation(r["allplay_now"]) == "contend" and r.get("top3_before") is not None and seasons[r["league"]]["has3"]]
    print(f"A0b-top3 {len(t3)} contender sides: finished top 3 {100 * statistics.mean(h for h, _, _ in t3):.1f}% vs "
          f"pre-trade odds {100 * statistics.mean(b for _, b, _ in t3):.1f}% and post-trade {100 * statistics.mean(a for *_, a in t3):.1f}%")

    pairs = []
    for lid, s in seasons.items():
        prev = seasons.get(s["prev"])
        if not prev or not s["has3"]:
            continue
        top3 = {s["owner"].get(rid) for rid, pl in s["place"].items() if pl <= 3}
        champ = {s["owner"].get(rid) for rid, pl in s["place"].items() if pl == 1}
        for o, l in prev["luck"].items():
            if o in s["luck"]:
                pairs.append((l["allplay"], o in champ, o in top3))
    print(f"B4 predicting next season from this season's all-play, n={len(pairs)}: title AUC "
          f"{auc([a for a, _, _ in pairs], [c for _, c, _ in pairs]):.3f}, top-3 AUC "
          f"{auc([a for a, _, _ in pairs], [t for *_, t in pairs]):.3f}")
    print(f"\nsame roster, next season, different owner_id: {owner_changes[1]} of {owner_changes[0]} "
          f"({100 * owner_changes[1] / owner_changes[0]:.1f}%)")


if __name__ == "__main__":
    main()
