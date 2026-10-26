"""What converts to wins: every manager-score channel as a feature of season t, one linear
model predicting season t+1, each target season held out in turn. Tests M1-M4
pre-registered in research/README.md "What converts to wins".

Run: python -m research.trade_grades && python -m research.what_wins
"""

import json
import statistics
from collections import defaultdict
from pathlib import Path

from research.draft_skill import Values, sleeper_to_fp
from research.manager_habits import moves
from research.roster_strength import fit, predict, season_rosters
from research.schedule_luck import season_luck
from research.waiver_skill import waiver_shares

CRAWL_DIR = Path("data/crawl")
GRADES = Path("data/research/trade_grades.jsonl")
BASE = ["allplay", "luck", "starter", "bench", "old_share"]
BEHAVIOR = ["moves", "trades", "waiver_share", "trade_now", "trade_next", "picks_bought"]


def auc(scores: list, hits: list) -> float:
    """Chance a random champion out-ranks a random non-champion."""
    pos = [s for s, h in zip(scores, hits) if h]
    neg = [s for s, h in zip(scores, hits) if not h]
    return sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))


def load_grades() -> dict:
    """(league, manager) -> summed trade grades for that league-season."""
    grades = defaultdict(lambda: {"trade_now": 0.0, "trade_next": 0.0, "picks_bought": 0.0})
    for line in open(GRADES, encoding="utf-8"):
        r = json.loads(line)
        g = grades[(r["league"], r["manager"])]
        g["trade_now"] += r["now_titles"]
        g["trade_next"] += r["next_titles"]
        g["picks_bought"] += r["pick_margin"]
    return grades


def season_features(d: dict, values, xwalk, grades: dict) -> dict:
    """owner_id -> every channel for one completed season (BASE + BEHAVIOR + champion)."""
    luck = season_luck(d)
    if not luck:
        return {}
    lid = d["league"]["league_id"]
    rosters, waiver = season_rosters(d, values, xwalk), waiver_shares(d)
    trades, total = moves(d)
    owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
    champ = owner.get(next((m["w"] for m in d.get("winners_bracket") or [] if m.get("p") == 1), None))
    return {o: {**r, "luck": luck[o]["luck"], "moves": total[o], "trades": trades[o],
                "waiver_share": waiver.get(o, {}).get("share", 0.0), **grades[(lid, o)],
                "champion": o == champ} for o, r in rosters.items()}


def all_pairs(values, xwalk, grades) -> list:
    """(season t features, season t+1 features, season t+1) over the whole corpus."""
    seasons = {}
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        feats = season_features(d, values, xwalk, grades)
        if feats:
            seasons[d["league"]["league_id"]] = (d["league"].get("previous_league_id"),
                                                 int(d["league"]["season"]), feats)
    return [(seasons[prev][2][o], now[o], season) for prev, season, now in seasons.values()
            if prev in seasons for o in now if o in seasons[prev][2]]


def main():
    values, xwalk = Values(), sleeper_to_fp()
    pairs = all_pairs(values, xwalk, load_grades())
    print(f"{len(pairs)} manager pairs, {sum(n['champion'] for _, n, _ in pairs)} next-season champions\n")
    held_out = sorted({s for *_, s in pairs})
    rows = [(x, n["allplay"]) for x, n, _ in pairs]
    for name, cols in (("all-play only", ["allplay"]), ("results + roster (baseline)", BASE),
                       ("+ behavior + trade grades", BASE + BEHAVIOR)):
        preds, ys, champs = [], [], []
        for s in held_out:
            train = [(x, n["allplay"]) for x, n, t in pairs if t != s]
            test = [(x, n) for x, n, t in pairs if t == s]
            preds += predict(fit(train, cols), [(x, n["allplay"]) for x, n in test], cols)
            ys += [n["allplay"] for _, n in test]
            champs += [n["champion"] for _, n in test]
        print(f"  {name:28} next all-play r = {statistics.correlation(preds, ys):+.3f}   "
              f"next title AUC = {auc(preds, champs):.3f}")
    coef = fit(rows, BASE + BEHAVIOR)
    print("\nfull-data weights, next-season all-play per SD of each feature:")
    for c, b in sorted(zip(BASE + BEHAVIOR, coef[1:]), key=lambda cb: -abs(cb[1] * statistics.pstdev(x[cb[0]] for x, _ in rows))):
        print(f"  {c:14} {b * statistics.pstdev(x[c] for x, _ in rows):+.4f}")


if __name__ == "__main__":
    main()
