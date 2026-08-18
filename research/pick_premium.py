"""Do 1sts buy more than their FantasyCalc number? Owner (2026-08-18): "we may just be
undervaluing 1sts - in reality they come off as golden trade pieces. I'd probably never
send one for some pseudo bums. Last year shivvv traded one for Kelce and Henry."

Test, scale-free: for every crawled trade where ONE side is exactly one pick (a lone 1st,
or a lone 2nd), what does it fetch - the best returning player's PERCENTILE in the DP
snapshot at the time, the summed value as a multiple of that best piece, and how old the
pieces are. Then: where does FantasyCalc price a 1st TODAY in its own percentile ranking?
If a lone 1st typically fetches a top-15% player and FC prices it like a top-30% one, that
gap is the premium - in units both scales share (rank), not in a constant.

Run: python -m research.pick_premium
"""

import bisect
import statistics
from collections import Counter

from research.stud_returns import load_dp, sleeper_to_fp, league_is_sf
from research.trade_ledger import trades
from sources import fantasycalc


def med(xs):
    return statistics.median(xs) if xs else float("nan")


def q(xs, p):
    xs = sorted(xs)
    return xs[int(p * (len(xs) - 1))] if xs else float("nan")


def main():
    dates, snaps = load_dp()
    s2fp = sleeper_to_fp()
    sf = league_is_sf()
    rows = []
    for t in trades():
        i = bisect.bisect_right(dates, t["date"]) - 1
        if i < 0 or len(t["sides"]) != 2:
            continue
        snap = snaps[i]
        key = "value_2qb" if sf.get(t["league_id"]) else "value_1qb"
        ranked = sorted((int(r[key]) for r in snap.values() if r.get(key)), reverse=True)
        if not ranked:
            continue
        pct = lambda v: bisect.bisect_left([-x for x in ranked], -v) / len(ranked)   # 0 = best
        (ra, a), (rb, b) = t["sides"].items()
        for pick_side, other in ((a, b), (b, a)):
            if pick_side["players"] or len(pick_side["picks"]) != 1 or other["picks"] or not other["players"]:
                continue
            pk = pick_side["picks"][0]
            got = []
            for pid in other["players"]:
                r = snap.get(s2fp.get(pid))
                if not (r and r.get(key)):
                    got = None
                    break
                got.append((r["player"], int(r[key]), float(r["age"]) if str(r.get("age", "")).replace(".", "").isdigit() else None, r.get("pos")))
            if not got:
                continue
            best = max(got, key=lambda g: g[1])
            rows.append({
                "round": pk["round"], "years_out": int(pk["season"]) - int(t["season"]),
                "date": t["date"], "league": t["league_name"],
                "n": len(got), "best": best[0], "best_pct": pct(best[1]),
                "best_age": best[2], "sum_mult": sum(g[1] for g in got) / best[1],
                "desc": " + ".join(f"{g[0]}({g[1]:,}, {g[2]})" for g in sorted(got, key=lambda g: -g[1])),
            })

    for rnd, label in ((1, "a lone 1st"), (2, "a lone 2nd")):
        grp = [r for r in rows if r["round"] == rnd]
        print(f"\n{label.upper()} as one whole side: {len(grp)} trades")
        if not grp:
            continue
        bp = [r["best_pct"] for r in grp]
        print(f"  best returning player: median top-{100 * med(bp):.0f}% (quartiles top-{100 * q(bp, .25):.0f}% .. top-{100 * q(bp, .75):.0f}%)")
        print(f"  pieces back: {dict(Counter(r['n'] for r in grp))}; summed / best piece: median {med([r['sum_mult'] for r in grp]):.2f}x")
        ages = [r["best_age"] for r in grp if r["best_age"]]
        print(f"  best piece's age: median {med(ages):.1f}; {100 * sum(1 for a in ages if a >= 28) / max(len(ages), 1):.0f}% are 28+")
        by_out = {}
        for r in grp:
            by_out.setdefault(r["years_out"], []).append(r["best_pct"])
        print("  by how far out the pick is: " + "; ".join(f"{k}y: top-{100 * med(v):.0f}% (n={len(v)})" for k, v in sorted(by_out.items())))
        print("  examples (best piece, its percentile):")
        for r in sorted(grp, key=lambda r: r["best_pct"])[:: max(1, len(grp) // 8)][:8]:
            print(f"    {r['date']} {r['league'][:22]:22} {pk_desc(r)} -> {r['desc']}  [top-{100 * r['best_pct']:.0f}%]")

    # Where FantasyCalc prices a 1st today, in FC's own ranking (12-team superflex).
    print("\nFANTASYCALC TODAY (12-team superflex): where a 1st / 2nd sits among players")
    players = fantasycalc.get_players(num_qbs=2, num_teams=12, ppr=1.0)
    vals = sorted((p["value"] for p in players.values()), reverse=True)
    picks = fantasycalc.get_pick_values(num_qbs=2, num_teams=12, ppr=1.0)
    for name in ("2027 1st", "2028 1st", "2027 2nd", "2028 2nd"):
        v = next((val for pk, val in picks.items() if pk.startswith(name)), None)
        if v is None:
            continue
        p = bisect.bisect_left([-x for x in vals], -v) / len(vals)
        print(f"  {name}: {v:,} = top-{100 * p:.0f}% of players")


def pk_desc(r):
    return f"{r['years_out']}y-out {'1st' if r['round'] == 1 else '2nd'}"


if __name__ == "__main__":
    main()
