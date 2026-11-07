"""Beating Sleeper's weekly projection with workload: does trailing expected fantasy points
(nflverse ffopportunity xFP) carry information the projection leaves out? Tests B1-B4,
pre-registered in research/README.md "Beating Sleeper's weekly projection with workload".

Run: python -m research.projection_blend
"""

import gzip
import json
from pathlib import Path

import numpy as np
import polars as pl

from sources import sleeper
from sources.nflverse_ids import gsis_to_sleeper

CACHE = Path("data/research/projection_blend")
SEASONS = range(2020, 2026)
TRAIN = range(2020, 2024)
WEEKS = range(1, 18)
MIN_PRIOR_GAMES = 3
EP_URL = "https://github.com/ffverse/ffopportunity/releases/download/latest-data/ep_weekly_{}.parquet"


# Sleeper's default full-PPR scoring. Raw stat lines are scored with it, not read from the
# `pts_ppr` field: in projections that field runs ~2 points above its own stat line for QBs.
PPR = {"pass_yd": 0.04, "pass_td": 4, "pass_int": -1, "pass_2pt": 2, "rush_yd": 0.1, "rush_td": 6,
       "rush_2pt": 2, "rec": 1, "rec_yd": 0.1, "rec_td": 6, "rec_2pt": 2, "fum_lost": -2}


def sleeper_week(kind: str, season: int, week: int) -> dict[str, float]:
    """player_id -> full-PPR points for one week, `kind` "projections" or "stats", cached on
    disk: past weeks don't change."""
    path = CACHE / f"{kind}_{season}_{week}.json.gz"
    if not path.exists():
        query = "season_type=regular&" + "&".join(f"position[]={p}" for p in sleeper.PROJECTION_POSITIONS)
        resp = sleeper._http.get(f"https://api.sleeper.app/{kind}/nfl/{season}/{week}?{query}")
        resp.raise_for_status()
        pts = {r["player_id"]: sleeper.score(r["stats"], PPR) for r in resp.json() if r.get("stats")}
        path.write_bytes(gzip.compress(json.dumps(pts).encode()))
    return json.loads(gzip.decompress(path.read_bytes()))


def workload(season: int) -> pl.DataFrame:
    """One row per player-game: sleeper_id, week, xfp, fp - ffopportunity's weekly file."""
    path = CACHE / f"ep_weekly_{season}.parquet"
    if not path.exists():
        resp = sleeper._http.get(EP_URL.format(season))
        resp.raise_for_status()
        path.write_bytes(resp.content)
    ids = gsis_to_sleeper()
    df = pl.read_parquet(path).filter(pl.col("position").is_in(["QB", "RB", "WR", "TE"]))
    return df.select(
        pl.col("player_id").replace_strict(ids, default=None).alias("sleeper_id"),
        pl.col("position"), pl.col("week").cast(pl.Int64),
        pl.col("total_fantasy_points_exp").alias("xfp"), pl.col("total_fantasy_points").alias("fp"),
    ).drop_nulls("sleeper_id")


def rows() -> pl.DataFrame:
    """Every player-week with a projection, a game played and 3+ earlier games that season,
    with trailing means from those earlier games only."""
    out = []
    for season in SEASONS:
        games = workload(season).sort("week")
        games = games.with_columns(
            pl.col("xfp").cum_sum().shift(1).over("sleeper_id").alias("xfp_sum"),
            pl.col("fp").cum_sum().shift(1).over("sleeper_id").alias("fp_sum"),
            pl.int_range(pl.len()).over("sleeper_id").alias("prior_games"),
        ).filter(pl.col("prior_games") >= MIN_PRIOR_GAMES)
        proj = [(pid, w, p) for w in WEEKS for pid, p in sleeper_week("projections", season, w).items()]
        actual = [(pid, w, p) for w in WEEKS for pid, p in sleeper_week("stats", season, w).items()]
        frame = lambda t, name: pl.DataFrame(t, schema=["sleeper_id", "week", name], orient="row")
        out.append(games.join(frame(proj, "proj"), on=["sleeper_id", "week"])
                   .join(frame(actual, "actual"), on=["sleeper_id", "week"])
                   .with_columns(pl.lit(season).alias("season"),
                                 (pl.col("xfp_sum") / pl.col("prior_games")).alias("xfp_avg"),
                                 (pl.col("fp_sum") / pl.col("prior_games")).alias("fp_avg"))
                   .drop_nulls(["proj", "actual"]))
    return pl.concat(out)


def fit_mae(train: pl.DataFrame, test: pl.DataFrame, features: list[str]) -> tuple[float, list]:
    """Out-of-sample MAE of a least-squares fit, and its coefficients (intercept first)."""
    X = lambda d: np.column_stack([np.ones(d.height)] + [d[f].to_numpy() for f in features])
    coef, *_ = np.linalg.lstsq(X(train), train["actual"].to_numpy(), rcond=None)
    return float(np.mean(np.abs(X(test) @ coef - test["actual"].to_numpy()))), coef.round(2).tolist()


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    df = rows()
    print(f"{df.height} player-weeks, {df['season'].min()}-{df['season'].max()}")
    for pos in ("QB", "RB", "WR", "TE"):
        d = df.filter(pl.col("position") == pos)
        train, test = d.filter(pl.col("season").is_in(TRAIN)), d.filter(~pl.col("season").is_in(TRAIN))
        raw = float((test["proj"] - test["actual"]).abs().mean())
        models = {name: fit_mae(train, test, f) for name, f in {
            "proj (recalibrated)": ["proj"], "points/game": ["fp_avg"], "xFP/game": ["xfp_avg"],
            "proj + xFP": ["proj", "xfp_avg"], "proj + xFP + points": ["proj", "xfp_avg", "fp_avg"]}.items()}
        base = models["proj (recalibrated)"][0]
        print(f"\n{pos}: train {train.height}, test {test.height} | raw projection MAE {raw:.2f}")
        for name, (mae, coef) in models.items():
            print(f"  {name:22} MAE {mae:.2f}  ({(mae - base) / base:+.1%} vs proj)  coef {coef}")
        hot = d.with_columns((pl.col("fp_avg") - pl.col("xfp_avg")).alias("over"), (pl.col("actual") - pl.col("proj")).alias("miss"))
        for label, cond in (("ran 3+ above xFP", pl.col("over") >= 3), ("within 3", pl.col("over").abs() < 3),
                            ("ran 3+ below xFP", pl.col("over") <= -3)):
            g = hot.filter(cond)
            print(f"  B4 {label:17} n {g.height:5}  actual - projection {g['miss'].mean():+.2f}")


if __name__ == "__main__":
    main()
