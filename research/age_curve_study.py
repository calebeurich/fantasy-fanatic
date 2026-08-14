"""The curve-validation study (spec: ROADMAP.md). Grades the product's aging claims
against DP's realized value history, survivorship handled by counting exits.

Method: quarterly cohorts of (snapshot, snapshot+1yr) pairs; players joined by fp_id;
outcome = value ratio a year later, exits (vanished from the priced list) counted at
the destination snapshot's floor value, never dropped. Medians + exit rates by
position x integer age. Pre-registered hypothesis: among same-age young RBs/WRs,
rookies behave more 'ascending' than experienced players of the same age.
"""

import statistics
from collections import defaultdict
from datetime import date, timedelta

from sources import dp_values

snaps = dp_values.load()
# Pre-late-2020 files use a different schema (no fp_id, different value columns) -
# the study runs on the consistent era rather than guessing at column archaeology.
snaps = [s for s in snaps if any(r.get("fp_id") for r in s["rows"][:5])]
snaps.sort(key=lambda s: s["scrape_date"])
print(f"{len(snaps)} snapshots {snaps[0]['scrape_date']} .. {snaps[-1]['scrape_date']}")

def d(s): return date.fromisoformat(s)

# Quarterly cohort starts, each paired with the snapshot nearest one year later.
by_date = {s["scrape_date"]: s for s in snaps}
dates = [d(s["scrape_date"]) for s in snaps]
cohorts = []
cursor = dates[0]
while cursor <= dates[-1] - timedelta(days=345):
    start = min(dates, key=lambda x: abs((x - cursor).days))
    target = start + timedelta(days=365)
    end = min(dates, key=lambda x: abs((x - target).days))
    if abs((end - target).days) <= 21 and (end - start).days > 300:
        cohorts.append((start, end))
    cursor += timedelta(days=91)
print(f"{len(cohorts)} cohort pairs, e.g. {cohorts[0]} .. {cohorts[-1]}")

def rows_of(dt):
    out = {}
    for r in by_date[dt.isoformat()]["rows"]:
        try:
            v = float(r["value_2qb"] or 0)
            age = float(r["age"]) if r["age"] else None
        except ValueError:
            continue
        if r.get("fp_id") and v > 0:
            out[r["fp_id"]] = {"pos": r["pos"], "age": age, "v": v,
                               "draft_year": r.get("draft_year"), "team": r.get("team"),
                               "name": r.get("player")}
    return out

# Observation table: one row per (cohort, player).
obs = []
for start, end in cohorts:
    a, b = rows_of(start), rows_of(end)
    floor_b = min(p["v"] for p in b.values())
    for pid, p in a.items():
        if p["age"] is None or p["pos"] not in ("QB", "RB", "WR", "TE"):
            continue
        exited = pid not in b
        v2 = b[pid]["v"] if not exited else floor_b
        try:
            exp_years = start.year - int(float(p["draft_year"])) if p["draft_year"] else None
        except ValueError:
            exp_years = None
        obs.append({"year": start.year, "pos": p["pos"], "age": int(p["age"]),
                    "v1": p["v"], "ratio": v2 / p["v"], "exited": exited,
                    "exp": exp_years, "name": p["name"],
                    "team_changed": (not exited and b[pid]["team"] != p["team"])})
print(f"{len(obs)} player-year observations")

RELEVANT = 200  # ignore end-of-list dust whose ratios are noise

def show(rows, label):
    print(f"\n== {label} (players with value >= {RELEVANT}) ==")
    print(f"{'pos':4} {'age':>3} {'n':>5} {'median ratio':>12} {'exit rate':>9}")
    grouped = defaultdict(list)
    for o in rows:
        if o["v1"] >= RELEVANT:
            grouped[(o["pos"], o["age"])].append(o)
    for (pos, age) in sorted(grouped):
        g = grouped[(pos, age)]
        if len(g) < 12:
            continue
        med = statistics.median(o["ratio"] for o in g)
        exit_rate = sum(o["exited"] for o in g) / len(g)
        print(f"{pos:4} {age:>3} {len(g):>5} {med:>12.2f} {exit_rate:>8.0%}")

show(obs, "value ratio one year later, by position and age")

# Pre-registered: same-age young players, rookies vs experienced.
print("\n== pre-registered: experience among same-age players ==")
for pos in ("RB", "WR"):
    for age in (22, 23, 24, 25):
        cohort_rows = [o for o in obs if o["pos"] == pos and o["age"] == age
                       and o["v1"] >= RELEVANT and o["exp"] is not None]
        rook = [o["ratio"] for o in cohort_rows if o["exp"] <= 0]
        soph = [o["ratio"] for o in cohort_rows if o["exp"] == 1]
        vets = [o["ratio"] for o in cohort_rows if o["exp"] >= 2]
        if min(len(rook), len(vets)) >= 10:
            print(f"{pos} age {age}: rookies n={len(rook)} med {statistics.median(rook):.2f} | "
                  f"yr2 n={len(soph)} med {statistics.median(soph) if soph else float('nan'):.2f} | "
                  f"3+yrs n={len(vets)} med {statistics.median(vets):.2f}")

# Exploratory (flagged: team change is outcome-entangled - cuts cause moves).
print("\n== exploratory: team change vs stayed (survivors only, flagged biased) ==")
for pos in ("RB", "WR", "TE", "QB"):
    rows = [o for o in obs if o["pos"] == pos and o["v1"] >= RELEVANT and not o["exited"]]
    moved = [o["ratio"] for o in rows if o["team_changed"]]
    stayed = [o["ratio"] for o in rows if not o["team_changed"]]
    if len(moved) >= 20:
        print(f"{pos}: moved n={len(moved)} med {statistics.median(moved):.2f} | "
              f"stayed n={len(stayed)} med {statistics.median(stayed):.2f}")
