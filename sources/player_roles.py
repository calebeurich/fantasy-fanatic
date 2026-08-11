"""Role tags from real usage and production, used to adjust the dynasty age curve: mobile
QBs decline earlier (their ceiling depends on athleticism, not just arm talent), *good*
pocket passers decline much later (arm talent and processing outlast legs), and
receiving-down RBs decline later than early-down backs (pass-catching survives longer than
between-the-tackles work). Thresholds sit on the natural gap in each real distribution.

**These tags are deliberately hard to earn, and most players get none.** Each one moves a
player onto a different age curve - a claim about how he will hold up over *years* - so the
cost of a wrong tag is much higher than the cost of no tag. An untagged player falls back to
his position's default, which is the honest middle rather than a failure to classify.

That principle was learned from a live miss. A quarterback whose owner rated him a pocket
passer got tagged `rushing_qb` off 5.47 carries a game in one season, against 4.46 across
three, and was moved onto a curve declining three years earlier on the strength of one noisy
autumn. Every tag now shares one long window (`ROLE_SEASONS`) and one games floor. He is
untagged today, which is the right answer: the tool has no strong evidence either way and
should not manufacture some.

This is also where the project stops. Anything finer - projecting an individual's decline,
weighting archetypes by expected value - is sports modelling, and there are people who do
that properly. These tags exist to stop the age curve being obviously wrong about broad
archetypes, not to out-predict them.

Smoke test: python -m sources.player_roles
"""

from datetime import date

import nflreadpy as nfl
import polars as pl

from .cache import ttl_cache, REFERENCE_TTL
from .nflverse_ids import gsis_to_sleeper

RUSHING_QB_CARRIES_PER_GAME = 5.0
PASS_CATCHING_RB_TARGETS_PER_GAME = 4.0
# Every role tag is measured over the same window, and it is a long one. These tags feed an
# **age curve** - a claim about how a player will hold up over years - so the question is
# always what kind of player he is, never what he did last autumn.
#
# One season was demonstrably too thin. A quarterback ran 5.47 times a game in a single
# season and 4.46 across three, which flipped him across the rushing bar and onto a curve
# that declines three years earlier - a strong claim about a player's future built on one
# noisy year. His own manager called it wrong on sight. Widening the window fixed it and
# removed a second window at the same time.
ROLE_SEASONS = 3
MIN_GAMES = 20  # ~1.2 seasons of starts; below this a per-game rate is noise, not a trait

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




def _seasons() -> list[int]:
    """The shared measurement window for every role tag - see ROLE_SEASONS."""
    latest = most_recent_season()
    return list(range(latest - ROLE_SEASONS + 1, latest + 1))


def most_recent_season() -> int:
    """NFL season named Y runs Sep Y - Feb (Y+1); before March, Y's season may still be
    finishing its playoffs, so treat Y-1 as the last fully completed one."""
    today = date.today()
    return today.year - 1 if today.month >= 3 else today.year - 2


@ttl_cache(REFERENCE_TTL)
def get_roles() -> dict[str, str]:
    """Sleeper player_id -> 'rushing_qb', 'pocket_passer' or 'pass_catching_rb'. Absent =
    no adjustment.

    The three quarterback tags come from two independent measurements - does he run, and does
    he throw well - which gives a genuine third case rather than a tie to break. A QB who runs
    *and* passes at an elite level is `dual_threat_qb`: when the legs go he is still a good
    passer, so the rushing discount that suits a mobility-only quarterback does not fit him.
    An earlier version made the two exclusive with rushing winning, which put the league's
    best run-and-throw QBs on the most pessimistic curve available."""
    stats = nfl.load_player_stats(seasons=_seasons())
    id_map = gsis_to_sleeper()
    roles = {}

    qb_usage = (
        stats.filter((stats["position"] == "QB") & (stats["attempts"] > 0))
        .group_by("player_id")
        .agg(pl.col("carries").sum().alias("carries"), pl.len().alias("games"))
        .filter(pl.col("games") >= MIN_GAMES)
        .with_columns((pl.col("carries") / pl.col("games")).alias("rate"))
    )
    rushers = {id_map.get(row["player_id"]) for row in qb_usage.iter_rows(named=True)
               if row["rate"] >= RUSHING_QB_CARRIES_PER_GAME}
    rushers.discard(None)

    passers = (
        stats.filter((pl.col("position") == "QB") & (pl.col("attempts") >= 10))
        .group_by("player_id")
        .agg(pl.col("passing_epa").sum().alias("epa"), pl.len().alias("games"))
        .filter(pl.col("games") >= MIN_GAMES)
        .with_columns((pl.col("epa") / pl.col("games")).alias("epa_per_game"))
    )
    rates = sorted(r["epa_per_game"] for r in passers.iter_rows(named=True)
                   if r["epa_per_game"] is not None)
    elite = set()
    if rates:
        bar = rates[int(ELITE_PASSER_PERCENTILE * (len(rates) - 1))]
        elite = {id_map.get(row["player_id"]) for row in passers.iter_rows(named=True)
                 if row["epa_per_game"] is not None and row["epa_per_game"] >= bar}
        elite.discard(None)

    # Three quarterback archetypes, from the two measurements above. The earlier version made
    # rushing and passing mutually exclusive with rushing winning, which forced the league's
    # best run-and-throw QBs onto the curve for players whose game is *only* mobility - and
    # the market plainly disagrees, paying 10,415 for one of them at 29.5.
    for sleeper_id in rushers:
        roles[sleeper_id] = "dual_threat_qb" if sleeper_id in elite else "rushing_qb"
    for sleeper_id in elite - rushers:
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
    for tag in ("rushing_qb", "dual_threat_qb", "pocket_passer", "pass_catching_rb"):
        count = sum(1 for r in roles.values() if r == tag)
        print(f"{tag}: {count} players")
