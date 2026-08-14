"""Production clock vs value clock, measured side by side.

Joins the dynasty-value cohorts (age_curve_study) to historical POSITIONAL redraft
ECR from FantasyPros (db_fpecr via DynastyProcess, weekly since 2019) by FantasyPros
id. Two questions: per age, where does a player actually rank as a PRODUCER while
his market value moves - and for players past the production breakpoint ("red"), how
chopped are they really: do they still rank startable, and do they survive a year?

Run: python -m research.redraft_curves   (needs data/db_fpecr.csv.gz, ~100MB:
https://raw.githubusercontent.com/dynastyprocess/data/master/files/db_fpecr.csv.gz)
"""

import statistics
from collections import defaultdict
from datetime import date

import pandas as pd

from analysis.team_values import AGE_CURVE
from research.age_curve_study import obs

# Startable positional depth in a 12-team lineup, superflex-ish for QB.
STARTABLE = {"QB": 24, "RB": 24, "WR": 36, "TE": 12}
RELEVANT = 200

df = pd.read_csv("data/db_fpecr.csv.gz",
                 usecols=["page_type", "ecr_type", "id", "ecr", "scrape_date"],
                 dtype={"id": str})
df = df[df["page_type"].isin([f"redraft-{p.lower()}" for p in STARTABLE])
        & (df["ecr_type"] == "rp")]
df["scrape_date"] = pd.to_datetime(df["scrape_date"]).dt.date
snap_dates = sorted(df["scrape_date"].unique())
by_date = {dt: dict(zip(g["id"], g["ecr"])) for dt, g in df.groupby("scrape_date")}
print(f"{len(snap_dates)} redraft snapshots {snap_dates[0]} .. {snap_dates[-1]}")


def rank_at(pid, target, tol=16):
    dt = min(snap_dates, key=lambda x: abs((x - target).days))
    if abs((dt - target).days) > tol:
        return None
    return by_date[dt].get(pid)


rows = []
for o in obs:
    if o["v1"] < RELEVANT:
        continue
    rows.append({**o,
                 "r1": rank_at(o["fp_id"], date.fromisoformat(o["start"])),
                 "r2": rank_at(o["fp_id"], date.fromisoformat(o["end"]))})
print(f"{len(rows)} relevant observations, "
      f"{sum(1 for r in rows if r['r1'] is not None)} with a redraft rank at start")

pos_base = {p: statistics.median(r["ratio"] for r in rows if r["pos"] == p)
            for p in STARTABLE}

print("\n== the two clocks, per position and age ==")
print("prod rank = median positional redraft ECR (lower is better) | "
      "value = dynasty ratio vs position baseline")
print(f"{'pos':4} {'age':>3} {'n':>4} {'%ranked':>7} {'prod rank':>9} {'%startable':>10} {'value rel':>9}")
grouped = defaultdict(list)
for r in rows:
    grouped[(r["pos"], r["age"])].append(r)
for (pos, age) in sorted(grouped):
    g = grouped[(pos, age)]
    if len(g) < 15:
        continue
    ranked = [r["r1"] for r in g if r["r1"] is not None]
    pct_ranked = len(ranked) / len(g)
    med_rank = statistics.median(ranked) if ranked else None
    startable = sum(1 for r in ranked if r <= STARTABLE[pos]) / len(ranked) if ranked else 0
    val = statistics.median(r["ratio"] for r in g) / pos_base[pos]
    print(f"{pos:4} {age:>3} {len(g):>4} {pct_ranked:>7.0%} "
          f"{med_rank if med_rank else float('nan'):>9.0f} {startable:>10.0%} {val:>9.2f}")

print("\n== how chopped is red, really: players PAST the production breakpoint ==")
print("(startable-now players only for survival - does he still rank startable a year on)")
print(f"{'pos':4} {'yrs past':>8} {'n':>4} {'prod rank':>9} {'%startable':>10} "
      f"{'still startable 1yr':>19}")
for pos in ("QB", "RB", "WR", "TE"):
    old = AGE_CURVE[pos][1]
    buckets = defaultdict(list)
    for r in rows:
        if r["pos"] != pos or r["age"] < old:
            continue
        past = r["age"] - old
        buckets[min(past, 3)].append(r)
    for past in sorted(buckets):
        g = buckets[past]
        ranked = [r for r in g if r["r1"] is not None]
        if len(ranked) < 10:
            continue
        med_rank = statistics.median(r["r1"] for r in ranked)
        startable_now = [r for r in ranked if r["r1"] <= STARTABLE[pos]]
        with_end = [r for r in startable_now if r["r2"] is not None or r["exited"]]
        survive = (sum(1 for r in with_end
                       if r["r2"] is not None and r["r2"] <= STARTABLE[pos]) / len(with_end)
                   if with_end else None)
        label = f"{past}" if past < 3 else "3+"
        print(f"{pos:4} {label:>8} {len(ranked):>4} {med_rank:>9.0f} "
              f"{len(startable_now)/len(ranked):>10.0%} "
              f"{survive if survive is not None else float('nan'):>19.0%}")
