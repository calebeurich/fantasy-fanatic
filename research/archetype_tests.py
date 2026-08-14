"""Archetype tests: do the role overrides (pass-catching RB, rushing QB) and the
contract-year question hold up in realized value history?

No hindsight: each observation's tags come from the season COMPLETED before its
cohort started (usage in season t-1 is what a manager knew at time t), and contract
state is the contract actually covering year t. Joined to the dynasty cohorts via
the DP id crosswalk (fantasypros_id <-> gsis_id).

Run: python -m research.archetype_tests   (needs data/db_playerids.csv)
"""

import csv
import statistics
from collections import defaultdict
from datetime import date

import nflreadpy as nfl
import polars as pl

from research.age_curve_study import obs

fp_to_gsis = {}
with open("data/db_playerids.csv", encoding="utf-8") as f:
    for r in csv.DictReader(f):
        if r.get("fantasypros_id") and r.get("gsis_id"):
            fp_to_gsis[r["fantasypros_id"]] = r["gsis_id"]
print(f"{len(fp_to_gsis)} fp->gsis mappings")

SEASONS = range(2019, 2025)
rb_pc_by_season: dict[int, set] = {}
qb_rush_by_season: dict[int, set] = {}
for season in SEASONS:
    stats = nfl.load_player_stats(seasons=[season])
    rb = (stats.filter(pl.col("position") == "RB")
          .group_by("player_id")
          .agg(pl.col("targets").sum().alias("t"), pl.len().alias("g"))
          .filter(pl.col("g") >= 8))
    rb_pc_by_season[season] = {r["player_id"] for r in rb.iter_rows(named=True)
                               if r["t"] / r["g"] >= 4.0}
    qb = (stats.filter((pl.col("position") == "QB") & (pl.col("attempts") > 0))
          .group_by("player_id")
          .agg(pl.col("carries").sum().alias("c"), pl.len().alias("g"))
          .filter(pl.col("g") >= 8))
    qb_rush_by_season[season] = {r["player_id"] for r in qb.iter_rows(named=True)
                                 if r["c"] / r["g"] >= 5.0}
    print(f"{season}: {len(rb_pc_by_season[season])} pass-catching RBs, "
          f"{len(qb_rush_by_season[season])} rushing QBs")

# Contract history: every contract, so year-t state is reconstructable.
contracts = nfl.load_contracts()
by_gsis = defaultdict(list)
for r in contracts.iter_rows(named=True):
    if r.get("gsis_id") and r.get("year_signed") and r.get("years"):
        by_gsis[r["gsis_id"]].append((int(r["year_signed"]), int(r["years"])))


def contract_state(gsis, year):
    """'final' / 'not_final' for the contract covering `year`, None if unknown."""
    covering = [(ys, yrs) for ys, yrs in by_gsis.get(gsis, [])
                if ys <= year < ys + yrs]
    if not covering:
        return None
    ys, yrs = max(covering)
    return "final" if year == ys + yrs - 1 else "not_final"


RELEVANT = 200
rows = []
for o in obs:
    if o["v1"] < RELEVANT:
        continue
    gsis = fp_to_gsis.get(o["fp_id"])
    if not gsis:
        continue
    usage_season = int(o["start"][:4]) - 1
    rows.append({**o, "gsis": gsis,
                 "pc_rb": gsis in rb_pc_by_season.get(usage_season, set()),
                 "rush_qb": gsis in qb_rush_by_season.get(usage_season, set()),
                 "contract": contract_state(gsis, int(o["start"][:4]))})
print(f"{len(rows)} observations joined to nflverse identities\n")


def compare(label, pool, flag, bands):
    print(f"== {label} ==")
    for band_label, ages in bands:
        yes = [o["ratio"] for o in pool if o["age"] in ages and o[flag]]
        no = [o["ratio"] for o in pool if o["age"] in ages and not o[flag]]
        if min(len(yes), len(no)) >= 8:
            print(f"  age {band_label}: {flag} n={len(yes)} med "
                  f"{statistics.median(yes):.2f} | others n={len(no)} med "
                  f"{statistics.median(no):.2f}")
    print()


compare("pass-catching RB (>=4 targets/game prior season) vs other RBs",
        [o for o in rows if o["pos"] == "RB"], "pc_rb",
        [("22-24", range(22, 25)), ("25-26", range(25, 27)), ("27+", range(27, 40))])

compare("rushing QB (>=5 carries/game prior season) vs other QBs",
        [o for o in rows if o["pos"] == "QB"], "rush_qb",
        [("23-26", range(23, 27)), ("27-30", range(27, 31)), ("31+", range(31, 45))])

print("== contract year: final year of deal vs not, same position and age band ==")
for pos in ("RB", "WR", "QB", "TE"):
    pool = [o for o in rows if o["pos"] == pos and o["contract"]]
    for band_label, ages in (("<=25", range(20, 26)), ("26-28", range(26, 29)),
                             ("29+", range(29, 45))):
        fin = [o["ratio"] for o in pool if o["age"] in ages and o["contract"] == "final"]
        not_fin = [o["ratio"] for o in pool if o["age"] in ages and o["contract"] == "not_final"]
        if min(len(fin), len(not_fin)) >= 10:
            print(f"  {pos} {band_label}: final-year n={len(fin)} med "
                  f"{statistics.median(fin):.2f} | mid-contract n={len(not_fin)} med "
                  f"{statistics.median(not_fin):.2f}")
