"""How are studs actually acquired? For every crawled trade whose best piece was a
top-tier player at the time (DP point-in-time value), describe the RETURN: how many
pieces, how many of them picks, the best returning piece as a share of the stud, and
the (approximate) summed multiple.

Owner's prior (2026-08-16): "the packages really never work like that [3 RBs for
Jeanty, 4-for-1 for JSN] - trading for studs like those would require a good player
like Drake London and 2 first round picks." This tests that against real trades.

Values: DP archive (data/dp_values.jsonl, GPL - fetched, never vendored), the snapshot
at or before the trade date, 2QB values for superflex leagues else 1QB. Picks are NOT in
the player archive, so they are priced at FantasyCalc's CURRENT flat round averages for
a 12-team superflex - a stated approximation, fine for shape, not for precision.

Run: python -m research.stud_returns
"""

import bisect
import csv
import json
import statistics
from collections import Counter
from pathlib import Path

from research.trade_ledger import trades

DP = Path("data/dp_values.jsonl")
IDS = Path("data/db_playerids.csv")
CRAWL = Path("data/crawl")

# FantasyCalc 12-team superflex flat round averages, 2026-08 (sources.fantasycalc
# get_pick_values, "2027 1st" etc.). Approximation - see module docstring.
PICK_VALUE = {1: 3200, 2: 1400, 3: 800, 4: 500}
STUD_PCT = 0.05   # top 5% of the snapshot's valued players = "stud"


def load_dp():
    snaps = []
    for line in DP.open(encoding="utf-8"):
        s = json.loads(line)
        rows = {r["fp_id"]: r for r in s["rows"] if r.get("fp_id") and r.get("player")}
        snaps.append((s["scrape_date"], rows))
    snaps.sort(key=lambda s: s[0])
    return [d for d, _ in snaps], [r for _, r in snaps]


def sleeper_to_fp():
    out = {}
    with IDS.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("sleeper_id") and row.get("fantasypros_id"):
                out[row["sleeper_id"]] = row["fantasypros_id"]
    return out


def league_is_sf():
    sf = {}
    for f in CRAWL.glob("*.json"):
        s = json.loads(f.read_text(encoding="utf-8"))
        sf[s["league"]["league_id"]] = "SUPER_FLEX" in s["league"].get("roster_positions", [])
    return sf


def main():
    dates, snaps = load_dp()
    s2fp = sleeper_to_fp()
    sf = league_is_sf()
    rows = []
    for t in trades():
        i = bisect.bisect_right(dates, t["date"]) - 1
        if i < 0:
            continue
        snap = snaps[i]
        key = "value_2qb" if sf.get(t["league_id"]) else "value_1qb"
        vals = sorted((int(r[key]) for r in snap.values() if r.get(key)), reverse=True)
        if not vals:
            continue
        stud_bar = vals[int(len(vals) * STUD_PCT)]
        sides = []
        for rid, side in t["sides"].items():
            pieces = []
            for pid in side["players"]:
                r = snap.get(s2fp.get(pid))
                if r and r.get(key):
                    pieces.append(("P", r["player"], int(r[key])))
                else:
                    pieces.append(("P?", pid, 0))
            for pk in side["picks"]:
                pieces.append(("K", f"{pk['season']} {pk['round']}", PICK_VALUE.get(pk["round"], 300)))
            sides.append(pieces)
        if len(sides) != 2:
            continue
        allp = sides[0] + sides[1]
        if not allp or not sides[0] or not sides[1]:
            continue
        best = max(allp, key=lambda p: p[2])
        if best[0] != "P" or best[2] < stud_bar:
            continue
        # the return = the OTHER side's pieces (what the stud's holder received)
        stud_side = 0 if best in sides[0] else 1
        ret = sides[1 - stud_side]
        with_stud = [p for p in sides[stud_side] if p != best]
        unknown = sum(1 for p in ret if p[0] == "P?")
        if unknown:
            continue  # can't shape a return we can't price
        ret_players = [p for p in ret if p[0] == "P"]
        ret_picks = [p for p in ret if p[0] == "K"]
        best_ret = max((p[2] for p in ret), default=0)
        rows.append({
            "date": t["date"], "league": t["league_name"], "stud": best[1], "stud_v": best[2],
            "n_ret": len(ret), "n_players": len(ret_players), "n_picks": len(ret_picks),
            "n_firsts": sum(1 for p in ret_picks if p[1].endswith(" 1")),
            "best_ret_share": best_ret / best[2],
            "sum_mult": sum(p[2] for p in ret) / best[2],
            "stud_side_extra": len(with_stud),
            "ret_desc": " + ".join(f"{p[1]}({p[2]:,})" for p in sorted(ret, key=lambda p: -p[2])),
        })

    print(f"{len(rows)} trades where the best piece was a top-{int(STUD_PCT*100)}% player at the time\n")
    clean = [r for r in rows if r["stud_side_extra"] == 0]
    print(f"{len(clean)} of them with the stud alone on his side (a true 'what does he fetch')\n")

    def med(xs): return statistics.median(xs) if xs else float("nan")
    print("RETURN SHAPE (stud alone on his side):")
    print(f"  pieces in return: median {med([r['n_ret'] for r in clean]):.0f}; distribution "
          f"{dict(sorted(Counter(r['n_ret'] for r in clean).items()))}")
    print(f"  returns containing a 1st: {sum(1 for r in clean if r['n_firsts'])} / {len(clean)}"
          f"  (two+ firsts: {sum(1 for r in clean if r['n_firsts'] >= 2)})")
    print(f"  returns with NO picks at all: {sum(1 for r in clean if not r['n_picks'])} / {len(clean)}")
    print(f"  best returning piece as share of stud: median {med([r['best_ret_share'] for r in clean]):.2f}"
          f" (q1 {statistics.quantiles([r['best_ret_share'] for r in clean], n=4)[0]:.2f},"
          f" q3 {statistics.quantiles([r['best_ret_share'] for r in clean], n=4)[2]:.2f})")
    print(f"  summed multiple (picks approximated): median {med([r['sum_mult'] for r in clean]):.2f}")
    print("\n  by number of pieces coming back:")
    for n in sorted(set(r["n_ret"] for r in clean)):
        grp = [r for r in clean if r["n_ret"] == n]
        if len(grp) < 5:
            continue
        print(f"    {n}-for-1 (n={len(grp):>3}): best piece share median {med([r['best_ret_share'] for r in grp]):.2f}, "
              f"summed {med([r['sum_mult'] for r in grp]):.2f}x, contains a 1st {sum(1 for r in grp if r['n_firsts'])}/{len(grp)}, "
              f"all-players (no picks) {sum(1 for r in grp if not r['n_picks'])}/{len(grp)}")

    print("\n  the SHAPE question - what came back for the biggest studs (top 15 by value):")
    for r in sorted(clean, key=lambda r: -r["stud_v"])[:15]:
        print(f"    {r['date']} {r['stud']} ({r['stud_v']:,}) <- {r['ret_desc']}")


if __name__ == "__main__":
    main()


def spectrum():
    """The same shape read across the whole value spectrum and by position: does the
    return's shape change as the best piece gets less valuable, and does an RB stud
    fetch a different shape than a WR or QB stud?"""
    dates, snaps = load_dp()
    s2fp = sleeper_to_fp()
    sf = league_is_sf()
    rows = []
    for t in trades():
        i = bisect.bisect_right(dates, t["date"]) - 1
        if i < 0:
            continue
        snap = snaps[i]
        key = "value_2qb" if sf.get(t["league_id"]) else "value_1qb"
        vals = sorted((int(r[key]) for r in snap.values() if r.get(key)), reverse=True)
        if not vals:
            continue
        sides = []
        for rid, side in t["sides"].items():
            pieces = []
            for pid in side["players"]:
                r = snap.get(s2fp.get(pid))
                pieces.append(("P", r["player"], int(r[key]), r["pos"]) if r and r.get(key)
                              else ("P?", pid, 0, "?"))
            for pk in side["picks"]:
                pieces.append(("K", f"{pk['season']} {pk['round']}", PICK_VALUE.get(pk["round"], 300), "PICK"))
            sides.append(pieces)
        if len(sides) != 2 or not sides[0] or not sides[1]:
            continue
        if any(p[0] == "P?" for p in sides[0] + sides[1]):
            continue
        best = max(sides[0] + sides[1], key=lambda p: p[2])
        if best[0] != "P":
            continue
        stud_side = 0 if best in sides[0] else 1
        if len(sides[stud_side]) != 1:
            continue  # stud alone on his side
        ret = sides[1 - stud_side]
        rank = bisect.bisect_left([-v for v in vals], -best[2])  # 0-based rank among valued players
        pct = rank / len(vals)
        rows.append({"pct": pct, "pos": best[3], "v": best[2], "n": len(ret),
                     "n_picks": sum(1 for p in ret if p[0] == "K"),
                     "firsts": sum(1 for p in ret if p[0] == "K" and p[1].endswith(" 1")),
                     "share": max(p[2] for p in ret) / best[2],
                     "mult": sum(p[2] for p in ret) / best[2]})

    def med(xs): return statistics.median(xs) if xs else float("nan")
    def line(label, grp):
        if len(grp) < 8:
            return f"  {label:<26} n={len(grp):>3}  (too few)"
        return (f"  {label:<26} n={len(grp):>3}  pieces {med([r['n'] for r in grp]):.0f}  "
                f"centerpiece {med([r['share'] for r in grp]):.2f}  summed {med([r['mult'] for r in grp]):.2f}x  "
                f"has 1st {100*sum(1 for r in grp if r['firsts'])//len(grp):>2}%  "
                f"no picks {100*sum(1 for r in grp if not r['n_picks'])//len(grp):>2}%  "
                f"4+ pieces {100*sum(1 for r in grp if r['n'] >= 4)//len(grp):>2}%")

    print(f"\nSPECTRUM - {len(rows)} trades with the best piece alone on his side, by that piece's value percentile at the time:")
    bins = [(0, .02, "top 2%"), (.02, .05, "2-5%"), (.05, .10, "5-10%"), (.10, .20, "10-20%"),
            (.20, .35, "20-35%"), (.35, .60, "35-60%"), (.60, 1.01, "bottom 40%")]
    for lo, hi, label in bins:
        print(line(label, [r for r in rows if lo <= r["pct"] < hi]))
    print("\nBY POSITION of the best piece, top 10% only:")
    top = [r for r in rows if r["pct"] < .10]
    for pos in ("QB", "RB", "WR", "TE"):
        print(line(pos, [r for r in top if r["pos"] == pos]))
    print("\nBY POSITION, 10-35%:")
    mid = [r for r in rows if .10 <= r["pct"] < .35]
    for pos in ("QB", "RB", "WR", "TE"):
        print(line(pos, [r for r in mid if r["pos"] == pos]))


if __name__ == "__main__":
    spectrum()
