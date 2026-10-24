"""Waivers: how much of a manager's lineup came off the wire? Track 3's third channel.
Waiver share = starter points from players added in-season (waiver or free agent),
counted only in weeks they started for the team that added them, over all of that team's
starter points. Tests the hypotheses pre-registered in research/README.md "Waivers".

Run: python -m research.waiver_skill
"""

import json
import statistics
from collections import defaultdict
from pathlib import Path

from research.schedule_luck import season_luck

CRAWL_DIR = Path("data/crawl")


def waiver_shares(d: dict) -> dict:
    """owner_id -> {"share", "allplay"} for one completed season, or {}."""
    allplay = season_luck(d)          # also the completed-season filter
    if not allplay:
        return {}
    st = d["league"]["settings"]
    weeks = (st.get("playoff_week_start") or 15) - 1
    owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
    added = defaultdict(dict)         # roster -> player -> first week he was added
    for week_txs in (d.get("transactions") or {}).values():
        for t in week_txs or []:
            if t.get("type") not in ("waiver", "free_agent") or t.get("status") != "complete":
                continue
            if not 1 <= (t.get("leg") or 0) <= weeks:
                continue
            for pid, rid in (t.get("adds") or {}).items():
                added[rid].setdefault(pid, t["leg"])
    from_wire, total = defaultdict(float), defaultdict(float)
    for w in range(1, weeks + 1):
        for m in d["matchups"].get(str(w)) or []:
            rid = m["roster_id"]
            for pid, pts in zip(m.get("starters") or [], m.get("starters_points") or []):
                total[rid] += pts or 0
                if added[rid].get(pid, 99) <= w:
                    from_wire[rid] += pts or 0
    out = {}
    for rid, o in owner.items():
        if o in allplay and total[rid]:
            out[o] = {"share": from_wire[rid] / total[rid], "allplay": allplay[o]["allplay"]}
    return out


def main():
    seasons = {}
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        shares = waiver_shares(d)
        if shares:
            seasons[d["league"]["league_id"]] = (d["league"].get("previous_league_id"), shares)
    rows = [r for _, s in seasons.values() for r in s.values()]
    q = statistics.quantiles([r["share"] for r in rows], n=10)
    print(f"{len(seasons)} seasons, {len(rows)} manager-seasons; waiver share median "
          f"{100 * statistics.median(r['share'] for r in rows):.1f}% (10th-90th {100 * q[0]:.1f}-{100 * q[-1]:.1f}%)")
    pairs = [(seasons[prev][1][o], s[o]) for prev, s in seasons.values() if prev in seasons
             for o in s if o in seasons[prev][1]]
    corr = statistics.correlation
    print(f"{len(pairs)} back-to-back pairs")
    print(f"W1 waiver share year-over-year r = {corr([a['share'] for a, _ in pairs], [b['share'] for _, b in pairs]):+.3f}")
    print(f"W2 waiver share vs same-season all-play r = {corr([r['share'] for r in rows], [r['allplay'] for r in rows]):+.3f}")
    print(f"W3 waiver share vs NEXT season's all-play r = {corr([a['share'] for a, _ in pairs], [b['allplay'] for _, b in pairs]):+.3f}")


if __name__ == "__main__":
    main()
