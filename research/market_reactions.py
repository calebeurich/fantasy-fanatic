"""Does the dynasty market overreact? Injured players (M1), breakouts (M2) and down years (M3),
each against MATCHED players - same position, same value decile, same snapshot - 12 months on,
so the market's general drift nets out. Plus H6: does rostering an RB's NFL teammate soften his
injury? Pre-registered in research/README.md "Market reactions and handcuffs".

Run: python -m research.market_reactions
"""

import bisect
import datetime
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path

import nflreadpy as nfl

from research.draft_skill import Values, sleeper_to_fp
from research.heuristics import Season, ir_weeks
from research.roster_strength import fit
from research.schedule_luck import season_luck
from sources.nflverse_ids import gsis_to_sleeper, sleeper_names

CRAWL_DIR = Path("data/crawl")
MIN_VALUE = 500
BOOT = 500


class Market:
    """Superflex DP values by snapshot, with a matched-control return for any player-date."""

    def __init__(self, values: Values):
        self.values = values
        self.cache = {}

    def snap(self, date: str) -> int | None:
        i = bisect.bisect_left(self.values.dates, date)
        return i if i < len(self.values.dates) else None

    def value(self, i, fp) -> float:
        row = self.values.rows[i].get(fp)
        return float(row["value_2qb"] or 0) if row else 0.0

    def ret(self, fp, date) -> tuple | None:
        """(player's 12-month log return, matched controls' mean) or None."""
        i = self.snap(date)
        j = self.snap((datetime.date.fromisoformat(date) + datetime.timedelta(days=365)).isoformat())
        if i is None or j is None:
            return None
        row = self.values.rows[i].get(fp)
        if not row or self.value(i, fp) < MIN_VALUE:
            return None
        key = (i, row["pos"])
        if key not in self.cache:
            peers = sorted((self.value(i, f), f) for f, r in self.values.rows[i].items()
                           if r["pos"] == row["pos"] and self.value(i, f) >= MIN_VALUE)
            self.cache[key] = peers
        peers = self.cache[key]
        k = bisect.bisect_left(peers, (self.value(i, fp), fp))
        band = peers[max(0, k - len(peers) // 20): k + len(peers) // 20 + 1]   # +/- 5% of the position = a decile
        r = lambda f: math.log((self.value(j, f) + 50) / (self.value(i, f) + 50))
        controls = [r(f) for _, f in band if f != fp]
        return (r(fp), statistics.mean(controls)) if controls else None


def excess(events: list, rng) -> tuple:
    xs = [a - b for a, b in events]
    est = sorted(statistics.mean(rng.choice(xs) for _ in xs) for _ in range(BOOT))
    return statistics.mean(xs), est[int(.05 * BOOT)], est[int(.95 * BOOT)], len(xs)


def main():
    values, xwalk = Values(), sleeper_to_fp()
    market = Market(values)
    rng = random.Random(0)
    ir, played, team = ir_weeks()

    onset = {}
    for season, week, sid in sorted(ir):
        if (season, week - 1, sid) not in ir and (season, sid) not in onset:
            onset[(season, sid)] = week
    m1 = [e for (season, sid), week in onset.items()
          if (e := market.ret(xwalk.get(sid), (datetime.date(season, 9, 5) + datetime.timedelta(weeks=week - 1)).isoformat()))]
    w, lo, hi, n = excess(m1, rng)
    print(f"M1 injured reserve onsets n={n}: 12-month log return vs matched {w:+.3f} [{lo:+.3f}, {hi:+.3f}] "
          f"(~{100 * (math.exp(w) - 1):+.0f}% value)")

    stats = nfl.load_player_stats(list(range(2018, 2026)))
    games, pts = defaultdict(int), defaultdict(float)
    for pid, season, kind, fp in stats.select(["player_id", "season", "season_type", "fantasy_points_ppr"]).iter_rows():
        if kind == "REG":
            games[(pid, season)] += 1
            pts[(pid, season)] += fp or 0
    to_sleeper = gsis_to_sleeper()
    m2, m3 = [], []
    for (pid, season), g in games.items():
        prev = (pid, season - 1)
        if g < 8 or games.get(prev, 0) < 8 or pid not in to_sleeper:
            continue
        now, before = pts[(pid, season)] / g, pts[prev] / games[prev]
        if before < 5:
            continue
        e = market.ret(xwalk.get(to_sleeper[pid]), f"{season + 1}-02-15")
        if e and now >= 1.4 * before:
            m2.append(e)
        elif e and before >= 8 and now <= 0.7 * before:
            m3.append(e)
    for name, ev in (("M2 breakouts (ppg +40%)", m2), ("M3 down years (ppg -30%)", m3)):
        w, lo, hi, n = excess(ev, rng)
        print(f"{name:26} n={n}: 12-month log return vs matched {w:+.3f} [{lo:+.3f}, {hi:+.3f}] "
              f"(~{100 * (math.exp(w) - 1):+.0f}% value)")

    position = {p: v["position"] for p, v in sleeper_names().items()}
    rows = []
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        if not season_luck(d) or int(d["league"]["season"]) > 2025:
            continue
        s = Season(d, position)
        seen = set()
        for wk in range(3, s.weeks - 1):
            for m in s.mu[wk]:
                rid, o = m["roster_id"], s.owner.get(m["roster_id"])
                for p in m.get("players") or []:
                    before = {k for k in s.rostered[(rid, p)] if k < wk}
                    started = {k for k in s.starts[(rid, p)] if k < wk}
                    if (p in seen or position.get(p) != "RB" or len(before) < 3 or len(started) / len(before) < 0.6
                            or (s.season, wk, p) not in ir or (s.season, wk - 1, p) in ir):
                        continue
                    seen.add(p)
                    nfl_team = team.get((s.season, wk, p))
                    cuff = any(q != p and position.get(q) == "RB" and team.get((s.season, wk, q)) == nfl_team
                               for q in m.get("players") or [])
                    td, rest = s.to_date(o, wk), s.rest(o, wk)
                    if td is not None and rest is not None and nfl_team:
                        rows.append({"league": s.lid, "to_date": td, "lift": rest - td, "cuff": float(cuff)})
    by = defaultdict(list)
    for r in rows:
        by[r["league"]].append((r, r["lift"]))
    keys = list(by)
    est = sorted(fit([p for k in (rng.choice(keys) for _ in keys) for p in by[k]], ["to_date", "cuff"])[2]
                 for _ in range(300))
    c = fit([(r, r["lift"]) for r in rows], ["to_date", "cuff"])
    print(f"H6 regular-starter RBs to IR n={len(rows)} ({100 * statistics.mean(r['cuff'] for r in rows):.0f}% "
          f"handcuffed): rest-of-season lift from the handcuff {100 * c[2]:+.1f} pts [{100 * est[15]:+.1f}, {100 * est[284]:+.1f}]")


if __name__ == "__main__":
    main()
