"""Does missing the playoffs pay? The last team in vs the first team out of the same league:
near-identical seasons, split by the cliff in the pick order. Follows both as long as the
league and both managers last, in XFL 2's payout. Findings in research/README.md "Tanking".

Run: python -m research.tank_ev
"""

import json
import random
import statistics
from collections import defaultdict
from pathlib import Path

import numpy as np

from research.schedule_luck import season_luck

CRAWL_DIR = Path("data/crawl")
PAYOUT = {"title": 350, "second": 150, "third": 50, "pf_leader": 50}
MAX_WIN_GAP = 1   # further apart than this and they weren't really on the same bubble
MAX_ROOKIE_ROUNDS = 6
BOOT = 500
HORIZON = 5   # the corpus runs 2020-2025


def points(settings: dict, key: str) -> float:
    return (settings.get(key) or 0) + (settings.get(f"{key}_decimal") or 0) / 100


def season_row(d: dict) -> dict | None:
    luck = season_luck(d)
    bracket = d.get("winners_bracket") or []
    if not luck or not bracket:
        return None
    owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
    made = {owner.get(g.get(k)) for g in bracket for k in ("t1", "t2") if isinstance(g.get(k), int)}
    dollars = defaultdict(float)
    for g in bracket:
        if g.get("p") == 1:
            dollars[owner.get(g.get("w"))] += PAYOUT["title"]
            dollars[owner.get(g.get("l"))] += PAYOUT["second"]
        elif g.get("p") == 3:
            dollars[owner.get(g.get("w"))] += PAYOUT["third"]
    teams = {r.get("owner_id"): r["settings"] for r in d["rosters"] if r.get("owner_id") in luck}
    if not teams:
        return None
    dollars[max(teams, key=lambda o: points(teams[o], "fpts"))] += PAYOUT["pf_leader"]
    return {"prev": d["league"].get("previous_league_id"), "made": made, "dollars": dollars,
            "allplay": {o: luck[o]["allplay"] for o in teams}, "settings": teams,
            "slots": rookie_slots(d), "champ": owner.get(next((g["w"] for g in bracket if g.get("p") == 1), None))}


def rookie_slots(d: dict) -> dict:
    """The draft HELD in this league's offseason, so it was earned by the season before."""
    for dr in d.get("drafts") or []:
        rounds = (dr.get("settings") or {}).get("rounds") or 99
        if dr.get("status") == "complete" and rounds <= MAX_ROOKIE_ROUNDS and dr.get("draft_order"):
            return dr["draft_order"]
    return {}


def bubble_pair(s: dict) -> tuple | None:
    rank = lambda o: (s["settings"][o].get("wins") or 0, points(s["settings"][o], "fpts"))
    inside = [o for o in s["settings"] if o in s["made"]]
    outside = [o for o in s["settings"] if o not in s["made"]]
    if not inside or not outside:
        return None
    last_in, first_out = min(inside, key=rank), max(outside, key=rank)
    if abs(rank(last_in)[0] - rank(first_out)[0]) > MAX_WIN_GAP:
        return None
    return last_in, first_out


def main():
    seasons = {}
    for f in sorted(CRAWL_DIR.glob("*.json")):
        row = season_row(json.load(open(f, encoding="utf-8")))
        if row:
            seasons[f.stem] = row
    next_of = {s["prev"]: lid for lid, s in seasons.items() if s["prev"]}
    print(f"{len(seasons)} completed seasons")

    pairs = defaultdict(list)
    for lid, s in seasons.items():
        pair = bubble_pair(s)
        if not pair:
            continue
        a, b = pair     # a made it, b didn't
        row = {"allplay": s["allplay"][a] - s["allplay"][b], "d0": s["dollars"][a] - s["dollars"][b]}
        later = lid
        for h in range(1, HORIZON + 1):
            later = next_of.get(later)
            if not later or a not in seasons[later]["settings"] or b not in seasons[later]["settings"]:
                break
            t = seasons[later]
            row[f"d{h}"] = t["dollars"][a] - t["dollars"][b]
            row[f"ap{h}"] = t["allplay"][a] - t["allplay"][b]
            if h == 1 and a in t["slots"] and b in t["slots"]:
                row["slot"] = t["slots"][a] - t["slots"][b]
        pairs[lid].append(row)

    rng = random.Random(0)
    keys = list(pairs)
    flat = [r for v in pairs.values() for r in v]
    print(f"{len(flat)} bubble pairs, mean all-play gap {statistics.mean(r['allplay'] for r in flat):+.4f}")
    slots = [r["slot"] for r in flat if "slot" in r]
    print(f"next rookie slot, last in minus first out: {statistics.mean(slots):+.2f} (n={len(slots)})")

    def effect(rows, y):
        """The gap at equal all-play: the intercept, so a stronger team can't pass as the rule."""
        rows = [r for r in rows if y in r]
        x = np.column_stack([np.ones(len(rows)), [r["allplay"] for r in rows]])
        return float(np.linalg.lstsq(x, np.array([r[y] for r in rows]), rcond=None)[0][0]), len(rows)

    for y in [f"d{h}" for h in range(HORIZON + 1)] + [f"ap{h}" for h in range(1, HORIZON + 1)]:
        est, n = effect(flat, y)
        boot = sorted(effect([r for k in (rng.choice(keys) for _ in keys) for r in pairs[k]], y)[0]
                      for _ in range(BOOT))
        print(f"{y}: made-it minus missed {est:+8.3f} n={n:5} 90% [{boot[int(.05 * BOOT)]:+.3f}, {boot[int(.95 * BOOT)]:+.3f}]")

    # Same pairs only: a running total can't mix leagues that lasted with ones that didn't.
    for far in range(1, HORIZON + 1):
        later = [f"d{h}" for h in range(1, far + 1)]
        def total(rows):
            rows = [{**r, "sum": sum(r[y] for y in later)} for r in rows if f"d{far}" in r]
            return effect(rows, "sum")
        kept = {k: [r for r in v if f"d{far}" in r] for k, v in pairs.items()}
        kept = {k: v for k, v in kept.items() if v}
        est, n = total(flat)
        ks = list(kept)
        boot = sorted(total([r for k in (rng.choice(ks) for _ in ks) for r in kept[k]])[0] for _ in range(BOOT))
        print(f"later seasons 1-{far} together: made-it minus missed {est:+8.2f} n={n:5} 90% "
              f"[{boot[int(.05 * BOOT)]:+.2f}, {boot[int(.95 * BOOT)]:+.2f}]")

    # What straight max PF would do: who it would hand the top of the draft to.
    corr, champ_rank, top3_ap, worst3_ap = [], [], [], []
    for s in seasons.values():
        ppts = {o: points(x, "ppts") for o, x in s["settings"].items()}
        if len(ppts) < 8 or min(ppts.values()) <= 0:
            continue
        order = sorted(ppts, key=ppts.get)
        corr.append(np.corrcoef([ppts[o] for o in order], [s["allplay"][o] for o in order])[0, 1])
        by_ap = sorted(order, key=s["allplay"].get)
        top3_ap.append(statistics.mean(s["allplay"][o] for o in order[:3]))
        worst3_ap.append(statistics.mean(s["allplay"][o] for o in by_ap[:3]))
        if s["champ"] in ppts:
            champ_rank.append(order.index(s["champ"]) / len(order))
    print(f"max PF vs all-play within a league: r {statistics.mean(corr):.2f} ({len(corr)} seasons)")
    print(f"all-play of the three lowest max PF {statistics.mean(top3_ap):.3f}, of the three truly weakest {statistics.mean(worst3_ap):.3f}")
    print(f"champion: in the lower half of max PF {statistics.mean(r < .5 for r in champ_rank):.1%}, "
          f"bottom quarter {statistics.mean(r < .25 for r in champ_rank):.1%} (n={len(champ_rank)})")


if __name__ == "__main__":
    main()
