"""Five strategy claims, tested: QB investment in superflex (P1), depth when a starter misses
(D1), early waiver adds (E1), trading with weaker partners (T1), and roster age and balance
(A1, A2). Pre-registered in research/README.md "Strategy tests".

Run: python -m research.trade_grades && python -m research.strategy_tests
"""

import json
import statistics
from collections import defaultdict
from pathlib import Path

from research.draft_skill import Values, is_superflex, sleeper_to_fp
from research.roster_strength import fit
from research.schedule_luck import season_luck
from sources.nflverse_ids import sleeper_names

CRAWL_DIR = Path("data/crawl")
GRADES = Path("data/research/trade_grades.jsonl")
REGULAR = 0.6     # started this share of his rostered weeks
EARLY_WEEKS = 4


def roster_profile(d, values, xwalk, position, luck) -> dict:
    """owner_id -> value, starter value, QB share, value-weighted age and its spread at the
    end of the regular season, each relative to the league that season."""
    lg = d["league"]
    sf, weeks = is_superflex(lg), (lg["settings"].get("playoff_week_start") or 15) - 1
    owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
    out = {}
    for m in d["matchups"].get(str(weeks)) or []:
        o = owner.get(m["roster_id"])
        if o not in luck:
            continue
        priced = []
        for pid in m.get("players") or []:
            row = values.rows_at(xwalk.get(str(pid)), f"{lg['season']}-12-10")
            if row and row["age"] not in ("", "NA", None):
                priced.append((float(row["value_2qb" if sf else "value_1qb"] or 0), float(row["age"]),
                               position.get(pid)))
        total = sum(v for v, _, _ in priced)
        if not total:
            continue
        n = len(m.get("starters") or [])
        age = sum(v * a for v, a, _ in priced) / total
        out[o] = {"total": total, "starter": sum(sorted((v for v, _, _ in priced), reverse=True)[:n]),
                  "qb_share": sum(v for v, _, p in priced if p == "QB") / total, "age": age,
                  "age_spread": (sum(v * (a - age) ** 2 for v, a, _ in priced) / total) ** 0.5,
                  "allplay": luck[o]["allplay"], "superflex": sf}
    for k in ("total", "starter"):
        mean = statistics.mean(r[k] for r in out.values()) if out else 1
        for r in out.values():
            r[k] /= mean
    for k in ("qb_share", "age", "age_spread"):
        mean = statistics.mean(r[k] for r in out.values()) if out else 0
        for r in out.values():
            r[k] -= mean
    return out


def depth_events(d, position) -> list:
    """(team's points that week minus its season mean, backup's season PPG, missing starter's
    season PPG) for each week a regular starter was out of the lineup or scored 0."""
    weeks = (d["league"]["settings"].get("playoff_week_start") or 15) - 1
    mus = {w: d["matchups"].get(str(w)) or [] for w in range(1, weeks + 1)}
    rostered, started, scored = defaultdict(int), defaultdict(int), defaultdict(list)
    team_pts = defaultdict(list)
    for w, ms in mus.items():
        for m in ms:
            rid = m["roster_id"]
            team_pts[rid].append(m.get("points") or 0)
            pp = m.get("players_points") or {}
            for p in m.get("players") or []:
                rostered[(rid, p)] += 1
                if pp.get(p):
                    scored[p].append(pp[p])
            for p in m.get("starters") or []:
                started[(rid, p)] += 1
    ppg = {p: statistics.mean(s) for p, s in scored.items()}
    regular = {k for k, n in rostered.items() if n >= 4 and started[k] / n >= REGULAR}
    events = []
    for w, ms in mus.items():
        for m in ms:
            rid, pp = m["roster_id"], m.get("players_points") or {}
            starters, mean = set(m.get("starters") or []), statistics.mean(team_pts[rid])
            for p in m.get("players") or []:
                if (rid, p) in regular and (p not in starters or not pp.get(p)):
                    bench = [ppg.get(q, 0) for q in m.get("players") or []
                             if q not in starters and q != p and position.get(q) == position.get(p)]
                    events.append(((m.get("points") or 0) - mean, max(bench, default=0), ppg.get(p, 0)))
    return events


def add_payoff(d) -> list:
    """(week added, starter points the add then scored for that team) per in-season add."""
    weeks = (d["league"]["settings"].get("playoff_week_start") or 15) - 1
    adds = []
    for txs in (d.get("transactions") or {}).values():
        for t in txs or []:
            if t.get("type") in ("waiver", "free_agent") and t.get("status") == "complete" \
                    and 1 <= (t.get("leg") or 0) <= weeks:
                adds += [(t["leg"], pid, rid) for pid, rid in (t.get("adds") or {}).items()]
    out = []
    for leg, pid, rid in adds:
        pts = 0.0
        for w in range(leg, weeks + 1):
            for m in d["matchups"].get(str(w)) or []:
                if m["roster_id"] == rid:
                    pts += sum(x or 0 for s, x in zip(m.get("starters") or [], m.get("starters_points") or [])
                               if s == pid)
        out.append((leg, pts, weeks - leg + 1))
    return out


def main():
    values, xwalk = Values(), sleeper_to_fp()
    position = {p: v["position"] for p, v in sleeper_names().items()}
    seasons, events, adds = {}, [], []
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        luck = season_luck(d)
        if not luck:
            continue
        for m in d["matchups"].get("1") or []:
            for p in m.get("players") or []:
                position.setdefault(p, "DEF" if not p.isdigit() else None)
        seasons[d["league"]["league_id"]] = (d["league"].get("previous_league_id"),
                                             roster_profile(d, values, xwalk, position, luck))
        events += depth_events(d, position)
        adds += add_payoff(d)
    next_of = {prev: lid for lid, (prev, _) in seasons.items()}

    def pairs(h):
        out = []
        for lid, (_, rs) in seasons.items():
            later = lid
            for _ in range(h):
                later = next_of.get(later)
            if later:
                out += [(x, seasons[later][1][o]["allplay"]) for o, x in rs.items() if o in seasons[later][1]]
        return out

    def weight(data, cols, k):
        coef = fit(data, cols)
        return coef[cols.index(k) + 1] * statistics.pstdev(x[k] for x, _ in data)

    one = pairs(1)
    for sf in (True, False):
        data = [p for p in one if p[0]["superflex"] == sf]
        print(f"P1 {'superflex' if sf else '1QB':9} n={len(data):4}: QB share's weight on next all-play "
              f"per SD {weight(data, ['allplay', 'starter', 'qb_share'], 'qb_share'):+.4f}")

    reg = statistics.linear_regression([b for _, b, _ in events], [x for x, _, _ in events])
    print(f"D1 {len(events)} missed-starter weeks: team points vs its average, per backup PPG: "
          f"slope {reg.slope:+.2f} (mean drop {statistics.mean(x for x, _, _ in events):+.1f})")

    early = [p for w, p, _ in adds if w <= EARLY_WEEKS]
    late = [p for w, p, _ in adds if w > EARLY_WEEKS]
    per_week = lambda xs: statistics.mean(p / n for _, p, n in xs)
    print(f"E1 adds: weeks 1-{EARLY_WEEKS} n={len(early)} {statistics.mean(early):.1f} starter pts per add "
          f"({per_week([a for a in adds if a[0] <= EARLY_WEEKS]):.2f}/week left) vs later n={len(late)} "
          f"{statistics.mean(late):.1f} ({per_week([a for a in adds if a[0] > EARLY_WEEKS]):.2f}/week left)")

    by_trade = defaultdict(list)
    for line in open(GRADES, encoding="utf-8"):
        r = json.loads(line)
        by_trade[r["trade"]].append(r)
    gap, margin = [], []
    for sides in by_trade.values():
        if len(sides) == 2:
            for me, them in (sides, sides[::-1]):
                gap.append(me["allplay_now"] - them["allplay_now"])
                margin.append(me["value_margin"])
    print(f"T1 {len(gap)} trade sides: value margin vs (my all-play - partner's) r = "
          f"{statistics.correlation(gap, margin):+.3f}")

    two = pairs(2)
    print(f"A1 n={len(two)}: older value-weighted age's weight on all-play two seasons out per SD "
          f"{weight(two, ['allplay', 'total', 'age'], 'age'):+.4f}; one season out "
          f"{weight(one, ['allplay', 'total', 'age'], 'age'):+.4f}")
    print(f"A2 n={len(one)}: age spread's weight on next all-play per SD "
          f"{weight(one, ['allplay', 'total', 'age', 'age_spread'], 'age_spread'):+.4f}")


if __name__ == "__main__":
    main()
