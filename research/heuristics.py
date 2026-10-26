"""Situational heuristics: when a manager faced a situation a sharp manager responds to - a
hole while contending, a starter on injured reserve, a bubble at the deadline, aging RBs on a
rebuild - did responding pay, and do some managers respond consistently? Tests H1-H5
pre-registered in research/README.md "Situational heuristics". Every trigger and response
uses only weeks before the decision; outcomes come after.

Run: python -m research.trade_grades && python -m research.heuristics
"""

import json
import random
import statistics
from collections import defaultdict
from pathlib import Path

import nflreadpy as nfl

from research.alignment import weekly_allplay
from research.bias_checks import placements
from research.draft_skill import Values, is_superflex, sleeper_to_fp
from research.roster_strength import fit
from research.schedule_luck import season_luck
from research.trade_grades import OUT as GRADE_FILE
from sources.nflverse_ids import sleeper_names

CRAWL_DIR = Path("data/crawl")
POSITIONS = ("QB", "RB", "WR", "TE")
CONTENDER, REBUILD = 0.6, 0.4
REGULAR = 0.6
RESPOND_WEEKS = 3          # the trigger week and the next two
DEFAULT_DEADLINE = 11
AGING_RB = 27
BOOT = 300


def ir_weeks() -> tuple[set, set, dict]:
    """(season, week, sleeper_id) on injured reserve; (season, week, team) that played; and each
    player's team that week. IR alone: at the time it is known to be multi-week."""
    seasons = list(range(2020, 2026))
    weekly = nfl.load_rosters_weekly(seasons)
    ir, team = set(), {}
    for season, week, sid, status, tm in weekly.select(["season", "week", "sleeper_id", "status", "team"]).iter_rows():
        if sid:
            team[(season, week, str(sid))] = tm
            if status == "RES":
                ir.add((season, week, str(sid)))
    played = set()
    for season, week, home, away, kind in nfl.load_schedules(seasons).select(
            ["season", "week", "home_team", "away_team", "game_type"]).iter_rows():
        if kind == "REG":
            played.update({(season, week, home), (season, week, away)})
    return ir, played, team


class Season:
    """One completed season's week-by-week facts, indexed for the triggers."""

    def __init__(self, d: dict, position: dict):
        lg = d["league"]
        self.lid, self.season = lg["league_id"], int(lg["season"])
        self.prev = lg.get("previous_league_id")
        self.weeks = (lg["settings"].get("playoff_week_start") or 15) - 1
        dl = lg["settings"].get("trade_deadline") or 0
        self.deadline = dl if 0 < dl <= self.weeks else min(DEFAULT_DEADLINE, self.weeks)
        self.slots = lg["settings"].get("playoff_teams") or 0
        self.sf = is_superflex(lg)
        self.owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
        self.position = position
        self.mu = {w: d["matchups"].get(str(w)) or [] for w in range(1, self.weeks + 1)}
        self.allplay = weekly_allplay(d)          # owner -> {week: share outscored}
        bracket = d.get("winners_bracket") or []
        self.field = {self.owner.get(m[k]) for m in bracket for k in ("t1", "t2") if isinstance(m.get(k), int)}
        self.top3 = {self.owner.get(rid) for rid, pl in placements(d).items() if pl <= 3}
        self.acquired = defaultdict(list)         # roster -> [(week, player)]
        self.traded_away = defaultdict(set)       # roster -> players sent in trades
        for txs in (d.get("transactions") or {}).values():
            for t in txs or []:
                if t.get("status") != "complete" or t.get("type") not in ("trade", "waiver", "free_agent"):
                    continue
                for pid, rid in (t.get("adds") or {}).items():
                    self.acquired[rid].append((t.get("leg") or 1, pid, t["type"]))
                if t.get("type") == "trade":
                    for pid, rid in (t.get("drops") or {}).items():
                        self.traded_away[rid].add(pid)
        self.starts = defaultdict(set)            # (roster, player) -> weeks started
        self.rostered = defaultdict(set)          # (roster, player) -> weeks on the roster
        self.points = {}                          # (week, player) -> points
        for w, ms in self.mu.items():
            for m in ms:
                rid = m["roster_id"]
                for p in m.get("players") or []:
                    self.rostered[(rid, p)].add(w)
                for p in m.get("starters") or []:
                    self.starts[(rid, p)].add(w)
                for p, pts in (m.get("players_points") or {}).items():
                    self.points[(w, p)] = pts or 0

    def to_date(self, o, w) -> float | None:
        xs = [v for k, v in self.allplay.get(o, {}).items() if k < w]
        return statistics.mean(xs) if xs else None

    def rest(self, o, w) -> float | None:
        xs = [v for k, v in self.allplay.get(o, {}).items() if k >= w]
        return statistics.mean(xs) if xs else None

    def ppg_before(self, p, w) -> float:
        xs = [self.points.get((k, p), 0) for k in range(1, w) if self.points.get((k, p))]
        return statistics.mean(xs) if xs else 0.0

    def responded(self, rid, pos, w, via=None) -> bool:
        """Acquired a player at `pos` in weeks w..w+2 who started for this team in that span
        (`via`: "trade" for paying for him, None for any route)."""
        span = range(w, w + RESPOND_WEEKS)
        return any(leg in span and self.position.get(p) == pos and self.starts[(rid, p)] & set(span)
                   and (via is None or kind == via) for leg, p, kind in self.acquired[rid])

    def position_production(self, w) -> dict:
        """(roster, pos) -> starter points per week at that position, weeks before w."""
        out = defaultdict(float)
        for k in range(1, w):
            for m in self.mu[k]:
                for p, pts in zip(m.get("starters") or [], m.get("starters_points") or []):
                    pos = self.position.get(p)
                    if pos in POSITIONS:
                        out[(m["roster_id"], pos)] += (pts or 0) / (w - 1)
        return out


def holes(s: Season) -> list:
    """H1: the first week each contender shows a bottom-quarter position."""
    rows, seen = [], set()
    for w in range(4, s.weeks - 1):
        prod = s.position_production(w)
        for pos in POSITIONS:
            league = sorted(prod.get((rid, pos), 0) for rid in s.owner)
            cut = league[len(league) // 4]
            for rid, o in s.owner.items():
                td = s.to_date(o, w)
                if (rid, pos) in seen or td is None or td < CONTENDER or prod.get((rid, pos), 0) > cut:
                    continue
                seen.add((rid, pos))
                rest = s.rest(o, w)
                if rest is not None:
                    rows.append({"league": s.lid, "season": s.season, "manager": o, "pos": pos, "to_date": td,
                                 "lift": rest - td, "resp": float(s.responded(rid, pos, w)),
                                 "resp_trade": float(s.responded(rid, pos, w, "trade")),
                                 "top3": float(o in s.top3), "playoffs": float(o in s.field), "has3": bool(s.top3)})
    return rows


def injuries(s: Season, ir: set, played: set, team: dict) -> list:
    """H2: a regular starter's first IR week, the team without a starter-caliber backup."""
    rows, seen = [], set()
    for w in range(3, s.weeks - 1):
        league_starter = defaultdict(list)
        for k in range(1, w):
            for m in s.mu[k]:
                for p, pts in zip(m.get("starters") or [], m.get("starters_points") or []):
                    if pts:
                        league_starter[s.position.get(p)].append(pts)
        median = {pos: statistics.median(v) for pos, v in league_starter.items() if v}
        for m in s.mu[w]:
            rid, o = m["roster_id"], s.owner.get(m["roster_id"])
            for p in m.get("players") or []:
                before = {k for k in s.rostered[(rid, p)] if k < w}
                started = {k for k in s.starts[(rid, p)] if k < w}
                pos = s.position.get(p)
                if (p in seen or pos not in POSITIONS or len(before) < 3 or len(started) / len(before) < REGULAR
                        or (s.season, w, p) not in ir or (s.season, w - 1, p) in ir
                        or (s.season, w, team.get((s.season, w, p))) not in played):
                    continue
                seen.add(p)
                backups = [s.ppg_before(q, w) for q in m.get("players") or []
                           if q != p and s.position.get(q) == pos and (s.season, w, q) not in ir]
                td, rest = s.to_date(o, w), s.rest(o, w)
                if td is None or rest is None:
                    continue
                rows.append({"league": s.lid, "season": s.season, "manager": o, "pos": pos, "sf": s.sf,
                             "to_date": td, "lift": rest - td, "resp": float(s.responded(rid, pos, w)),
                             "covered": max(backups, default=0) >= median.get(pos, 99),
                             "top3": float(o in s.top3), "has3": bool(s.top3)})
    return rows


def bubble(s: Season, trades: dict) -> list:
    """H3: teams near the playoff line in the three weeks before the deadline - bought, sold,
    or stood still."""
    w = max(s.deadline - 2, 3)
    ranked = sorted((o for o in s.owner.values() if s.to_date(o, w) is not None),
                    key=lambda o: -s.to_date(o, w))
    rows = []
    for i, o in enumerate(ranked, 1):
        if not s.slots or abs(i - s.slots) > 2:
            continue
        window = [t for t in trades.get((s.lid, o), []) if w <= t["week"] <= s.deadline]
        bought = any(t["now_titles"] >= 0.01 for t in window)
        sold = sum(t["pick_margin"] for t in window) > 0
        action = "bought" if bought else "sold" if sold else "stood" if not window else "other"
        rows.append({"league": s.lid, "season": s.season, "manager": o, "to_date": s.to_date(o, w),
                     "action": action, "playoffs": float(o in s.field)})
    return rows


def aging_rbs(s: Season, d: dict, values: Values, xwalk: dict) -> list:
    """H4: rebuilds that started the season holding RBs 27+ with real value - sold or held."""
    rows = []
    day = f"{s.season}-09-05"
    first = {m["roster_id"]: set(m.get("players") or []) for m in s.mu[1]}
    mean_value = statistics.mean(sum(values.at(xwalk.get(str(p)), day, s.sf) or 0 for p in ps)
                                 for ps in first.values()) if first else 0
    if mean_value < 5000:
        return rows
    for rid, players in first.items():
        o = s.owner.get(rid)
        final = statistics.mean(s.allplay.get(o, {}).values() or [0.5])
        if final >= REBUILD:
            continue
        aging = []
        for p in players:
            row = values.rows_at(xwalk.get(str(p)), day)
            if (row and s.position.get(p) == "RB" and row["age"] not in ("", "NA", None)
                    and float(row["age"]) >= AGING_RB and float(row["value_2qb" if s.sf else "value_1qb"] or 0) >= 1000):
                aging.append(p)
        if aging:
            start = sum(values.at(xwalk.get(str(p)), day, s.sf) or 0 for p in players) / mean_value
            rows.append({"league": s.lid, "season": s.season, "manager": o, "allplay": final, "start": start,
                         "sold": float(any(p in s.traded_away[rid] for p in aging))})
    return rows


def boot_weight(rows, cols, k, y, rng) -> tuple:
    by = defaultdict(list)
    for r in rows:
        by[r["league"]].append((r, r[y]))
    keys = list(by)
    est = sorted(fit([p for kk in (rng.choice(keys) for _ in keys) for p in by[kk]], cols)[cols.index(k) + 1]
                 for _ in range(BOOT))
    return fit([(r, r[y]) for r in rows], cols)[cols.index(k) + 1], est[int(.05 * BOOT)], est[int(.95 * BOOT)]


def main():
    values, xwalk = Values(), sleeper_to_fp()
    position = {p: v["position"] for p, v in sleeper_names().items()}
    ir, played, team = ir_weeks()
    trades = defaultdict(list)
    for line in open(GRADE_FILE, encoding="utf-8"):
        r = json.loads(line)
        if r["in_season"]:
            trades[(r["league"], r["manager"])].append(r)
    h1, h2, h3, h4 = [], [], [], []
    seasons = {}
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        if not season_luck(d):
            continue
        for m in d["matchups"].get("1") or []:
            for p in m.get("players") or []:
                position.setdefault(p, "DEF" if not p.isdigit() else None)
        s = Season(d, position)
        seasons[s.lid] = (s.prev, {o: statistics.mean(v.values()) for o, v in s.allplay.items() if v})
        h1 += holes(s)
        if s.season <= 2025:
            h2 += injuries(s, ir, played, team)
        h3 += bubble(s, trades)
        h4 += aging_rbs(s, d, values, xwalk)
    rng = random.Random(0)
    mean = lambda xs: statistics.mean(xs) if xs else float("nan")

    print(f"H1 holes on contenders: {len(h1)} ({100 * mean([r['resp'] for r in h1]):.0f}% responded)")
    w, lo, hi = boot_weight(h1, ["to_date", "resp"], "resp", "lift", rng)
    print(f"   rest-of-season all-play lift from responding: {100 * w:+.1f} pts [{100 * lo:+.1f}, {100 * hi:+.1f}]")
    t3 = [r for r in h1 if r["has3"]]
    w, lo, hi = boot_weight(t3, ["to_date", "resp"], "resp", "top3", rng)
    print(f"   top-3 finish from responding: {100 * w:+.1f} pts [{100 * lo:+.1f}, {100 * hi:+.1f}] (n={len(t3)})")
    # Exploratory (after the first run): the owner's heuristic is PAYING for a win-now player -
    # a trade - and "responded" also counted waiver fill-ins.
    paid = [dict(r, waiver_only=float(r["resp"] and not r["resp_trade"])) for r in h1]
    cols = ["to_date", "resp_trade", "waiver_only"]
    t3p = [r for r in paid if r["has3"]]
    for k, label in (("resp_trade", f"paid via trade ({100 * mean([r['resp_trade'] for r in h1]):.0f}%)"),
                     ("waiver_only", "waiver fill-in only")):
        lw, llo, lhi = boot_weight(paid, cols, k, "lift", rng)
        tw, tlo, thi = boot_weight(t3p, cols, k, "top3", rng)
        print(f"   exploratory - {label}: lift {100 * lw:+.1f} [{100 * llo:+.1f}, {100 * lhi:+.1f}], "
              f"top 3 {100 * tw:+.1f} [{100 * tlo:+.1f}, {100 * thi:+.1f}] pts")
    for pos in POSITIONS:
        g = [r for r in h1 if r["pos"] == pos]
        print(f"     {pos}: n={len(g):5} responded {100 * mean([r['resp'] for r in g]):3.0f}%  lift responders "
              f"{100 * mean([r['lift'] for r in g if r['resp']]):+.1f} vs {100 * mean([r['lift'] for r in g if not r['resp']]):+.1f}")

    exposed = [r for r in h2 if not r["covered"]]
    print(f"\nH2 regular starters to IR: {len(h2)}; no starter-caliber backup: {len(exposed)} "
          f"({100 * mean([r['resp'] for r in exposed]):.0f}% responded)")
    w, lo, hi = boot_weight(exposed, ["to_date", "resp"], "resp", "lift", rng)
    print(f"   lift from responding, exposed teams: {100 * w:+.1f} pts [{100 * lo:+.1f}, {100 * hi:+.1f}]")
    covered = [r for r in h2 if r["covered"]]
    if len(covered) > 50:
        w, *_ = boot_weight(covered, ["to_date", "resp"], "resp", "lift", rng)
        print(f"   (covered teams, for contrast: {100 * w:+.1f} pts, n={len(covered)})")
    for pos in POSITIONS:
        for sf in (True, False):
            g = [r for r in exposed if r["pos"] == pos and r["sf"] == sf]
            if len(g) >= 40:
                c = fit([(r, r["lift"]) for r in g], ["to_date", "resp"])
                print(f"     {pos} {'superflex' if sf else '1QB':9}: n={len(g):4} responded {100 * mean([r['resp'] for r in g]):3.0f}%  "
                      f"lift from responding {100 * c[2]:+.1f} pts")

    print(f"\nH3 bubble teams at the deadline: {len(h3)} - " + ", ".join(
        f"{a} {sum(r['action'] == a for r in h3)}" for a in ("bought", "sold", "stood", "other")))
    sub = [dict(r, bought=float(r["action"] == "bought")) for r in h3 if r["action"] in ("bought", "stood")]
    w, lo, hi = boot_weight(sub, ["to_date", "bought"], "bought", "playoffs", rng)
    print(f"   made the playoffs, bought vs stood (to-date held): {100 * w:+.1f} pts [{100 * lo:+.1f}, {100 * hi:+.1f}]")
    next_of = {prev: lid for lid, (prev, _) in seasons.items()}
    two = lambda r: (seasons.get(next_of.get(next_of.get(r["league"])), (None, {}))[1].get(r["manager"]))
    sub2 = [dict(r, sold=float(r["action"] == "sold"), t2=two(r)) for r in h3 if r["action"] in ("sold", "stood")]
    sub2 = [r for r in sub2 if r["t2"] is not None]
    if len(sub2) > 50:
        w, lo, hi = boot_weight(sub2, ["to_date", "sold"], "sold", "t2", rng)
        print(f"   all-play two seasons out, sold vs stood: {100 * w:+.1f} pts [{100 * lo:+.1f}, {100 * hi:+.1f}] (n={len(sub2)})")

    h4 = [dict(r, t2=two(r)) for r in h4]
    h4 = [r for r in h4 if r["t2"] is not None]
    print(f"\nH4 rebuilds holding 27+ RBs, with a season two out: {len(h4)} ({100 * mean([r['sold'] for r in h4]):.0f}% sold)")
    if len(h4) > 50:
        w, lo, hi = boot_weight(h4, ["allplay", "start", "sold"], "sold", "t2", rng)
        print(f"   all-play two seasons out, sold vs held: {100 * w:+.1f} pts [{100 * lo:+.1f}, {100 * hi:+.1f}]")

    rate = defaultdict(list)
    for r in h1 + [r for r in h2 if not r["covered"]]:
        rate[(r["league"], r["manager"])].append(r["resp"])
    pairs = [(mean(rate[(prev, o)]), mean(v)) for (lid, o), v in rate.items()
             for prev in [seasons.get(lid, (None,))[0]] if (prev, o) in rate]
    if len(pairs) > 30:
        print(f"\nH5 response rate (holes + exposed injuries) year over year: r = "
              f"{statistics.correlation([a for a, _ in pairs], [b for _, b in pairs]):+.3f} (n={len(pairs)})")


if __name__ == "__main__":
    main()
