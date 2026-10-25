"""Roster strength now -> all-play win% next season: the future half of championship
equity. Every completed crawled season's end-of-regular-season rosters are rebuilt from
the lineup records and valued with DynastyProcess; a linear model learns how roster value
and SHAPE turn into next season's all-play win%. Fit on target seasons through 2024,
tested on 2025. Tests the hypotheses pre-registered in research/README.md "Roster strength".

Run: python -m research.roster_strength
"""

import json
import statistics
from pathlib import Path

import numpy as np

from research.draft_skill import Values, is_superflex, sleeper_to_fp
from research.schedule_luck import season_luck

CRAWL_DIR = Path("data/crawl")
OLD_AGE = 28
TEST_SEASON = 2025
FEATURES = ["allplay", "total", "star", "starter", "bench", "old_share"]


def season_rosters(d: dict, values: Values, xwalk: dict) -> dict:
    """owner_id -> league-relative roster features at the end of one completed regular
    season, plus that season's all-play win%."""
    luck = season_luck(d)
    if not luck:
        return {}
    lg = d["league"]
    sf = is_superflex(lg)
    weeks = (lg["settings"].get("playoff_week_start") or 15) - 1
    date = f"{lg['season']}-12-10"
    owner = {r["roster_id"]: r.get("owner_id") for r in d["rosters"]}
    raw = {}
    for m in d["matchups"].get(str(weeks)) or []:
        o = owner.get(m["roster_id"])
        if o not in luck:
            continue
        priced = []
        for pid in m.get("players") or []:
            fp = xwalk.get(str(pid))
            row = values.rows_at(fp, date)
            if row:
                age = row["age"] if row["age"] not in ("", "NA", None) else 0   # unknown: not old
                priced.append((float(row["value_2qb" if sf else "value_1qb"] or 0), float(age)))
        priced.sort(reverse=True)
        n = len(m.get("starters") or [])
        total = sum(v for v, _ in priced)
        raw[o] = {"total": total, "star": sum(v for v, _ in priced[:3]),
                  "starter": sum(v for v, _ in priced[:n]),
                  "bench": sum(v for v, _ in priced[n:]),
                  "old_share": sum(v for v, a in priced if a >= OLD_AGE) / total if total else 0,
                  "allplay": luck[o]["allplay"]}
    if len(raw) < 4:
        return {}
    for k in ("total", "star", "starter", "bench"):
        mean = statistics.mean(r[k] for r in raw.values()) or 1
        for r in raw.values():
            r[k] /= mean
    return raw


def fit(rows: list, cols: list) -> np.ndarray:
    x = np.column_stack([np.ones(len(rows))] + [[r[0][c] for r in rows] for c in cols])
    return np.linalg.lstsq(x, np.array([r[1] for r in rows]), rcond=None)[0]


def predict(coef, rows, cols) -> list:
    return [coef[0] + sum(b * r[0][c] for b, c in zip(coef[1:], cols)) for r in rows]


def main():
    values, xwalk = Values(), sleeper_to_fp()
    seasons = {}
    for f in sorted(CRAWL_DIR.glob("*.json")):
        d = json.load(open(f, encoding="utf-8"))
        rosters = season_rosters(d, values, xwalk)
        if rosters:
            seasons[d["league"]["league_id"]] = (d["league"].get("previous_league_id"),
                                                 int(d["league"]["season"]), rosters)
    # (features at the end of t, all-play win% in t+1, season t+1)
    pairs = [(seasons[prev][2][o], now[o]["allplay"], season)
             for prev, season, now in seasons.values() if prev in seasons
             for o in now if o in seasons[prev][2]]
    train = [(x, y) for x, y, s in pairs if s < TEST_SEASON]
    test = [(x, y) for x, y, s in pairs if s == TEST_SEASON]
    print(f"{len(pairs)} team pairs: {len(train)} train (next season <= {TEST_SEASON - 1}), "
          f"{len(test)} test (next season {TEST_SEASON})\n")
    models = {"all-play only": ["allplay"], "roster value only": ["total"],
              "all-play + value": ["allplay", "total"],
              "all-play + shape": ["allplay", "star", "starter", "bench", "old_share"]}
    for name, cols in models.items():
        coef = fit(train, cols)
        r = statistics.correlation(predict(coef, test, cols), [y for _, y in test])
        print(f"  {name:22} out-of-sample r = {r:+.3f}   "
              + "  ".join(f"{c} {b:+.3f}" for c, b in zip(cols, coef[1:])))


if __name__ == "__main__":
    main()
