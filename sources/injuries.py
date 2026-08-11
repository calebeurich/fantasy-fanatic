"""How often players actually miss games, from nflverse weekly rosters and injury reports.

Exists because `roster_needs` could say how *bad* an absence would be and never how *likely*
one was - `drop_if_injured` carries an explicit disclaimer to that effect. Two rates come out
of one pull: per player (this specific back has missed a third of his weeks) and per position
(quarterbacks miss less than running backs, so equal exposure numbers are not equally
worrying).

Deliberately shallow. This measures *whether* a player was available, not the severity, type,
or recency of what kept him out, and it does not attempt to forecast. Weighting depth by
injury type and expected duration is a real modelling problem and is logged as future work
rather than half-built here.

Smoke test: python -m sources.injuries
"""

import nflreadpy as nfl

from .cache import ttl_cache, REFERENCE_TTL
from .nflverse_ids import gsis_to_sleeper
from .player_roles import most_recent_season

# How many completed seasons to measure over. Three is a compromise with a reason on each
# side: fewer and one freak year dominates a player's number, more and it describes a body
# that no longer exists - a running back's durability at 24 says little about him at 29.
SEASONS = 3

# Weeks a player was on the roster in a way that means he *could* have played, and so
# belongs in the denominator. Practice squad (DEV) is excluded - those players aren't
# expected to dress, so counting their weeks would read as availability nobody wanted.
# CUT/RET/TRD weeks are gone from the team entirely and mean nothing about health.
ELIGIBLE_STATUSES = {"ACT", "INA", "RES"}

# **Not every reserve week is an injury week, and conflating them was a real bug.** The
# first version counted all of status RES, which silently scored suspensions as fragility:
# a receiver's rate came out at 0.451, of which six weeks were a suspension served in full
# health. His owner spotted it immediately. `status_description_abbr` carries the reason,
# and the codes were classified empirically - by how often each one also appears on the
# weekly injury report, and by who is in it - rather than by guessing at the NFL's
# vocabulary:
#
#   R01 (12,309 wks) Reserve/Injured .................. injury
#   R48 ( 1,162)     IR, designated to return ......... injury (47% also on the report)
#   R04 (   945)     PUP .............................. injury
#   R05 (   383)     non-football injury .............. injury
#   I01 ( 1,872)     inactive - injury ................ injury
#   R40 (   177)     suspended ........................ NOT injury (0% on the report)
#   R30 (    51)     suspended, indefinite ............ NOT injury (0%)
#   R06 (    53)     did not report / left squad ...... NOT injury (0%)
#
# Reserve weeks matter most of all, which is why getting this right matters: season-ending
# injuries live on IR, and a player on IR often stops appearing on the weekly report
# altogether. Reading the report alone would *undercount* exactly the absences a manager
# most needs to plan for - note R01 is on the report only 5% of the time.
#
# An **allowlist**, so an unfamiliar or newly-introduced code counts as not-injury. That is
# the safe direction: understating a rate is a smaller error than telling someone a player
# is fragile when he was suspended.
INJURY_CODES = {"R01", "R48", "R04", "R05", "I01"}

# Absences that say nothing about durability. Dropped from the numerator *and* the
# denominator: a suspended or holding-out player was not available, but he also wasn't
# hurt, and leaving those weeks in the denominator would quietly reward being suspended
# with a lower miss rate. Counted and reported separately instead - whether suspension
# predicts future suspension is a real question, and one this rate should not answer by
# smuggling it in under an injury label.
NON_INJURY_ABSENCE_CODES = {"R40", "R30", "R06"}

RESERVE_STATUS = "RES"

# The report's own word for "will not play". "Questionable" and "Doubtful" are deliberately
# not counted - they describe uncertainty, not absence, and plenty of questionable players
# play a full game.
OUT = "Out"

# Below these a rate is noise rather than a trait, so it is reported as unknown instead of
# guessed. Both are needed and they catch different failures.
#
# The week floor is roughly one season - enough that a single missed game moves the number
# by a few points rather than swinging it.
#
# The season floor exists because one season is a sample of one body-year, and measuring
# only that produced nonsense at both ends. Players who spent a single year on injured
# reserve and were never otherwise rostered scored exactly 1.000 (17 of 17), reading as
# "always hurt" off one bad year; every rookie scored 0.000, reading as ironclad durability
# on the strength of having existed for four months. The second is the more dangerous
# error, since rookies are the assets a dynasty manager is most often asked to price.
# Two seasons is the minimum at which "he gets hurt" is a claim about the player rather
# than about one year.
MIN_ELIGIBLE_WEEKS = 17
MIN_SEASONS = 2


def _seasons() -> list[int]:
    latest = most_recent_season()
    return list(range(latest - SEASONS + 1, latest + 1))


@ttl_cache(REFERENCE_TTL)
def player_miss_rates() -> dict[str, dict]:
    """Keyed by **Sleeper player_id**: how much of the time this player was unavailable.

        {"miss_rate": 0.31, "weeks_missed": 17, "weeks_eligible": 55,
         "weeks_suspended": 0, "seasons": [2023, 2024, 2025]}

    A missed week is one spent on an **injury** reserve list (see `INJURY_CODES`), one the
    club listed inactive with an injury, or one the injury report called `Out`. Suspensions
    and holdouts are excluded from both halves of the fraction and reported separately as
    `weeks_suspended` - being suspended is not being fragile. The
    denominator is weeks actually on an NFL roster - not a flat 17 per season - because the
    alternative silently rates a player who was never in the league as perfectly durable.

    Players below `MIN_ELIGIBLE_WEEKS` or `MIN_SEASONS` are **omitted entirely** rather than
    given a rate. Callers must treat a missing key as "unknown", not as zero: rookies are the
    most common absentees here, and reading their absence as durability would invert the
    finding for precisely the youngest, least-proven assets."""
    seasons = _seasons()
    weekly = nfl.load_rosters_weekly(seasons)
    reports = nfl.load_injuries(seasons)

    # (gsis_id, season, week) that the injury report flagged as out. A set, because a player
    # can appear on more than one report row for the same week.
    out_weeks = {
        (row["gsis_id"], row["season"], row["week"])
        for row in reports.iter_rows(named=True)
        if row["gsis_id"] and row["report_status"] == OUT and row["game_type"] == "REG"
    }

    tally: dict[str, dict] = {}
    for row in weekly.iter_rows(named=True):
        gsis, status, code = row["gsis_id"], row["status"], row["status_description_abbr"]
        if not gsis or status not in ELIGIBLE_STATUSES or row["game_type"] != "REG":
            continue
        entry = tally.setdefault(gsis, {"weeks_eligible": 0, "weeks_missed": 0,
                                        "weeks_suspended": 0, "seasons": set()})
        if code in NON_INJURY_ABSENCE_CODES:
            entry["weeks_suspended"] += 1
            continue  # not availability information either way - see NON_INJURY_ABSENCE_CODES
        entry["weeks_eligible"] += 1
        entry["seasons"].add(row["season"])
        if code in INJURY_CODES or (gsis, row["season"], row["week"]) in out_weeks:
            entry["weeks_missed"] += 1

    sleeper_ids = gsis_to_sleeper()
    return {
        sleeper_ids[gsis]: {
            "miss_rate": round(entry["weeks_missed"] / entry["weeks_eligible"], 3),
            "weeks_missed": entry["weeks_missed"],
            "weeks_eligible": entry["weeks_eligible"],
            "weeks_suspended": entry["weeks_suspended"],
            "seasons": sorted(entry["seasons"]),
        }
        for gsis, entry in tally.items()
        if gsis in sleeper_ids and entry["weeks_eligible"] >= MIN_ELIGIBLE_WEEKS
        and len(entry["seasons"]) >= MIN_SEASONS
    }


@ttl_cache(REFERENCE_TTL)
def position_miss_rates() -> dict[str, float]:
    """Position -> share of eligible weeks missed, pooled across every player at it.

    The number `drop_if_injured` has always been missing. That measures magnitude and says
    so loudly - "an equal number at QB and at RB is not equally worrying" - without ever
    supplying the rates that would let a caller act on the warning.

    Pooled over player-weeks rather than averaged over players on purpose: a per-player mean
    lets a fringe body who spent one season hurt count as much as a decade-long starter, and
    the question here is what happens to a *lineup slot*, not to the average résumé. Computed
    from the full weekly data, so it includes players too new for `player_miss_rates`."""
    seasons = _seasons()
    weekly = nfl.load_rosters_weekly(seasons)
    reports = nfl.load_injuries(seasons)
    out_weeks = {
        (row["gsis_id"], row["season"], row["week"])
        for row in reports.iter_rows(named=True)
        if row["gsis_id"] and row["report_status"] == OUT and row["game_type"] == "REG"
    }

    pooled: dict[str, list[int]] = {}
    for row in weekly.iter_rows(named=True):
        status, position, code = row["status"], row["position"], row["status_description_abbr"]
        if status not in ELIGIBLE_STATUSES or row["game_type"] != "REG":
            continue
        if code in NON_INJURY_ABSENCE_CODES:
            continue
        eligible, missed = pooled.setdefault(position, [0, 0])
        pooled[position][0] = eligible + 1
        if code in INJURY_CODES or (row["gsis_id"], row["season"], row["week"]) in out_weeks:
            pooled[position][1] = missed + 1
    return {position: round(missed / eligible, 3)
            for position, (eligible, missed) in pooled.items() if eligible}


if __name__ == "__main__":
    print(f"seasons: {_seasons()}")
    rates = position_miss_rates()
    print("\nby position (share of roster weeks missed):")
    for position in ("QB", "RB", "WR", "TE"):
        if position in rates:
            print(f"  {position:<3} {rates[position]:.3f}")

    players = player_miss_rates()
    print(f"\nper-player rates: {len(players)} players with >= {MIN_ELIGIBLE_WEEKS} eligible weeks")
    worst = sorted(players.items(), key=lambda kv: -kv[1]["miss_rate"])[:10]
    for player_id, entry in worst:
        print(f"  {player_id:<8} {entry['miss_rate']:.3f} "
              f"({entry['weeks_missed']}/{entry['weeks_eligible']} weeks)")
