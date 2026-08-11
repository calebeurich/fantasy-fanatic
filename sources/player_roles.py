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

from .cache import ttl_cache, REFERENCE_TTL
from .nflverse_ids import gsis_to_sleeper

RUSHING_QB_CARRIES_PER_GAME = 5.0
PASS_CATCHING_RB_TARGETS_PER_GAME = 4.0
MIN_GAMES = 5  # below this, per-game rates are too noisy to classify on

# A **good** pocket passer ages differently from an ordinary one, which is the missing tier
# in the QB curve. The argument, from a sports-modelling data scientist reviewing this tool:
# a pure pocket passer trades on arm talent and processing, both of which hold into the late
# thirties, while a rushing QB's value leans on legs that slow near 30. Quality is the part
# that matters - a *mediocre* pocket passer doesn't age gracefully, he just gets replaced.
#
# Measured over 3 seasons of passing EPA per game, the top tier is exactly who you would
# name: Goff 6.87, Purdy 6.62, Stafford 5.33, Burrow 4.51, Mahomes 4.30 - and all of them
# throw from the pocket. Two QBs on one live roster split cleanly: Goff at 6.87 with 1.7
# carries a game against Herbert at 1.66 with 4.5, despite Herbert carrying the higher
# dynasty price.
#
# **EPA, not CPOE, despite CPOE being the better-isolated stat.** Completion percentage over
# expected penalises aggressive downfield throwing, and Stafford posts -0.47 CPOE alongside
# 5.33 EPA per game - requiring positive CPOE would drop one of the clearest examples of the
# archetype this tag exists to capture.
#
# Top third rather than a fixed EPA number, so it recalibrates as the league's passing
# environment moves - the same reasoning behind every other percentile in this project.
ELITE_PASSER_PERCENTILE = 0.67

# Quality is measured over more seasons than usage, deliberately. "Is he a good passer" is a
# durable trait and wants a long window; "does he run" is a fact about his current role and
# can flip with one coordinator change, so it stays on the most recent season.
QUALITY_SEASONS = 3
MIN_QUALITY_GAMES = 20


def most_recent_season() -> int:
    """NFL season named Y runs Sep Y - Feb (Y+1); before March, Y's season may still be
    finishing its playoffs, so treat Y-1 as the last fully completed one."""
    today = date.today()
    return today.year - 1 if today.month >= 3 else today.year - 2


@ttl_cache(REFERENCE_TTL)
def get_roles() -> dict[str, str]:
    """Sleeper player_id -> 'rushing_qb', 'pocket_passer' or 'pass_catching_rb'. Absent =
    no adjustment.

    `rushing_qb` and `pocket_passer` are mutually exclusive **by construction**, and the
    rushing test wins: a quarterback who runs enough to clear that bar carries the rushing
    decline risk whatever his arm does, so an elite dual-threat stays on the earlier curve.
    That is the conservative reading and it is a real judgement call - it puts the league's
    best passers-who-also-run on a curve that may be too pessimistic for them."""
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

    # The quality half, on its own longer window - see QUALITY_SEASONS.
    quality = nfl.load_player_stats(seasons=list(range(most_recent_season() - QUALITY_SEASONS + 1,
                                                      most_recent_season() + 1)))
    passers = (
        quality.filter((pl.col("position") == "QB") & (pl.col("attempts") >= 10))
        .group_by("player_id")
        .agg(pl.col("passing_epa").sum().alias("epa"), pl.len().alias("games"))
        .filter(pl.col("games") >= MIN_QUALITY_GAMES)
        .with_columns((pl.col("epa") / pl.col("games")).alias("epa_per_game"))
    )
    rates = sorted(r["epa_per_game"] for r in passers.iter_rows(named=True)
                   if r["epa_per_game"] is not None)
    if rates:
        bar = rates[int(ELITE_PASSER_PERCENTILE * (len(rates) - 1))]
        for row in passers.iter_rows(named=True):
            sleeper_id = id_map.get(row["player_id"])
            # `not in roles` is the mutual-exclusion rule: a rushing QB keeps that tag.
            if (sleeper_id and sleeper_id not in roles
                    and row["epa_per_game"] is not None and row["epa_per_game"] >= bar):
                roles[sleeper_id] = "pocket_passer"

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
    for tag in ("rushing_qb", "pocket_passer", "pass_catching_rb"):
        count = sum(1 for r in roles.values() if r == tag)
        print(f"{tag}: {count} players")
