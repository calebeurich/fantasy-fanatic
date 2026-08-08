"""Rushing-QB / pass-catching-RB tags from real season usage, used to adjust the dynasty
age curve: mobile QBs tend to decline earlier (their ceiling depends on athleticism, not
just arm talent), receiving-down RBs tend to decline later (pass-catching survives longer
than between-the-tackles work). Thresholds picked by inspecting the 2025 season's actual
carries/game and targets/game distributions - they land on the natural gap in each, e.g.
Lamar Jackson/Josh Allen/Jalen Hurts clear 5 carries/game while pure pocket passers don't
crack 3.

Smoke test: python -m sources.player_roles
"""

from datetime import date

import nflreadpy as nfl
import polars as pl

from .nflverse_ids import gsis_to_sleeper

RUSHING_QB_CARRIES_PER_GAME = 5.0
PASS_CATCHING_RB_TARGETS_PER_GAME = 4.0
MIN_GAMES = 5  # below this, per-game rates are too noisy to classify on


def most_recent_season() -> int:
    """NFL season named Y runs Sep Y - Feb (Y+1); before March, Y's season may still be
    finishing its playoffs, so treat Y-1 as the last fully completed one."""
    today = date.today()
    return today.year - 1 if today.month >= 3 else today.year - 2


def get_roles() -> dict[str, str]:
    """Sleeper player_id -> 'rushing_qb' or 'pass_catching_rb'. Absent = no adjustment."""
    stats = nfl.load_player_stats(seasons=[most_recent_season()])
    id_map = gsis_to_sleeper()
    roles = {}

    qb_usage = (
        stats.filter((stats["position"] == "QB") & (stats["attempts"] > 0))
        .group_by("player_id")
        .agg(pl.col("carries").sum().alias("carries"), pl.len().alias("games"))
        .filter(pl.col("games") >= MIN_GAMES)
        .with_columns((pl.col("carries") / pl.col("games")).alias("rate"))
    )
    for row in qb_usage.iter_rows(named=True):
        sleeper_id = id_map.get(row["player_id"])
        if sleeper_id and row["rate"] >= RUSHING_QB_CARRIES_PER_GAME:
            roles[sleeper_id] = "rushing_qb"

    rb_usage = (
        stats.filter((stats["position"] == "RB") & (stats["carries"] > 0))
        .group_by("player_id")
        .agg(pl.col("targets").sum().alias("targets"), pl.len().alias("games"))
        .filter(pl.col("games") >= MIN_GAMES)
        .with_columns((pl.col("targets") / pl.col("games")).alias("rate"))
    )
    for row in rb_usage.iter_rows(named=True):
        sleeper_id = id_map.get(row["player_id"])
        if sleeper_id and row["rate"] >= PASS_CATCHING_RB_TARGETS_PER_GAME:
            roles[sleeper_id] = "pass_catching_rb"

    return roles


if __name__ == "__main__":
    roles = get_roles()
    for tag in ("rushing_qb", "pass_catching_rb"):
        count = sum(1 for r in roles.values() if r == tag)
        print(f"{tag}: {count} players")
