"""Claims from the fantasy literature, tested on the crawl: how much byes decide titles,
how many seasons it takes to know a manager, and whether dealing (or just being active)
predicts a better next season once the roster is accounted for. Tests H5-H8 pre-registered
in research/README.md "Manager habits".

Run: python -m research.manager_habits
"""

import json
import statistics
from collections import Counter
from pathlib import Path

from research.draft_skill import Values, sleeper_to_fp
from research.roster_strength import fit, predict, season_rosters
from research.schedule_luck import season_luck

CRAWL_DIR = Path("data/crawl")


def moves(d: dict) -> tuple[Counter, Counter]:
    """(trades, all moves) per owner_id over the league-season's completed transactions."""
    owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
    trades, total = Counter(), Counter()
    for week in (d.get("transactions") or {}).values():
        for t in week or []:
            if t.get("status") != "complete":
                continue
            if t.get("type") == "trade":
                for rid in t.get("roster_ids") or []:
                    trades[owner.get(rid)] += 1
                    total[owner.get(rid)] += 1
            elif t.get("type") in ("waiver", "free_agent"):
                for rid in set((t.get("adds") or {}).values()):
                    total[owner.get(rid)] += 1
    return trades, total


def byes(d: dict):
    """(bye seeds, other playoff seeds, champion) for a completed bracket with byes."""
    bracket = d.get("winners_bracket") or []
    field = {m[k] for m in bracket for k in ("t1", "t2") if isinstance(m.get(k), int)}
    first = {m[k] for m in bracket if m.get("r") == 1 for k in ("t1", "t2")}
    champ = next((m["w"] for m in bracket if m.get("p") == 1), None)
    return field - first, first, champ


def main():
    values, xwalk = Values(), sleeper_to_fp()
    seasons, bye_titles, other_titles, n_bye, n_other, brackets = {}, 0, 0, 0, 0, 0
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        if not season_luck(d):
            continue
        bye, rest, champ = byes(d)
        if bye and champ is not None:
            brackets += 1
            n_bye, n_other = n_bye + len(bye), n_other + len(rest)
            bye_titles += champ in bye
            other_titles += champ in rest
        rosters = season_rosters(d, values, xwalk)
        trades, total = moves(d)
        for o, r in rosters.items():
            r["trades"], r["moves"] = trades[o], total[o]
        seasons[d["league"]["league_id"]] = (d["league"].get("previous_league_id"),
                                             int(d["league"]["season"]), rosters)

    print(f"H5 byes: {brackets} brackets with byes - bye seeds won {bye_titles} titles "
          f"({100 * bye_titles / brackets:.0f}%); per team: bye seed {100 * bye_titles / n_bye:.1f}% "
          f"vs other playoff seed {100 * other_titles / n_other:.1f}% "
          f"({(bye_titles / n_bye) / (other_titles / n_other):.1f}x)")

    r1 = 0.412   # all-play year-over-year, research/schedule_luck.py
    print(f"\nH6 Spearman-Brown: seasons of all-play for 0.7 reliability = {0.7 * (1 - r1) / (r1 * 0.3):.1f}")
    triples = [(seasons[seasons[prev][0]][2][o]["allplay"], seasons[prev][2][o]["allplay"], now[o]["allplay"])
               for prev, _, now in seasons.values()
               if prev in seasons and seasons[prev][0] in seasons
               for o in now if o in seasons[prev][2] and o in seasons[seasons[prev][0]][2]]
    corr = statistics.correlation
    print(f"   {len(triples)} managers with three straight seasons: one season predicts the third "
          f"r = {corr([b for _, b, _ in triples], [c for *_, c in triples]):+.3f}, two-season average "
          f"r = {corr([(a + b) / 2 for a, b, _ in triples], [c for *_, c in triples]):+.3f}")

    pairs = [(seasons[prev][2][o], now[o]["allplay"], season) for prev, season, now in seasons.values()
             if prev in seasons for o in now if o in seasons[prev][2]]
    habit = [(seasons[prev][2][o]["trades"], now[o]["trades"]) for prev, _, now in seasons.values()
             if prev in seasons for o in now if o in seasons[prev][2]]
    print(f"\nH7 trades per season: median {statistics.median(x['trades'] for x, _, _ in pairs)}, "
          f"year-over-year r = {corr([a for a, _ in habit], [b for _, b in habit]):+.3f}")
    base = ["allplay", "starter"]
    for name, cols in (("base (all-play + starter value)", base), ("+ trades", base + ["trades"]),
                       ("+ all moves", base + ["moves"])):
        preds, ys = [], []
        for held in sorted({s for *_, s in pairs}):
            c = fit([(x, y) for x, y, s in pairs if s != held], cols)
            te = [(x, y) for x, y, s in pairs if s == held]
            preds += predict(c, te, cols)
            ys += [y for _, y in te]
        full = fit([(x, y) for x, y, _ in pairs], cols)
        sd = {c: statistics.pstdev(x[c] for x, _, _ in pairs) for c in cols}
        print(f"   {name:32} leave-one-season-out r = {corr(preds, ys):+.4f}   weight per SD: "
              + "  ".join(f"{c} {b * sd[c]:+.4f}" for c, b in zip(cols, full[1:])))


if __name__ == "__main__":
    main()
