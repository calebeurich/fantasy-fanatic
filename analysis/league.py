"""One shared league context, built once and reused.

Every analysis module used to open with the same four lines - fetch the league, describe
the format, derive num_qbs, load the player pool - in `roster_detail`, `roster_needs`,
`team_state`, `team_values` and `waiver_wire`. That's the duplication CLAUDE.md warns
about, and it had already cost real effort: adding `redraft_value` to the player pool
meant finding every one of those sites, and `find_targets` had grown an eight-line
preamble that fetched the league twice.

Caching (`sources/cache.py`) already made the repetition cheap, so this is a
maintainability fix rather than a performance one - the point is that there's now a
single place to add a field, and a single place that knows which of the several
similar-looking slot and threshold concepts is which.

**Two slot concepts, deliberately both kept:**
- `needs_slots` folds SUPER_FLEX into an extra QB. Right for "how many of this position
  must I own", which is what replacement level and positional needs are asking.
- `lineup_dedicated` + `lineup_flex` model the real lineup, where SUPER_FLEX takes any
  position and FLEX slots exist at all. Right for "who actually starts".

**Two threshold concepts, likewise:**
- `start_thresholds` (redraft): can this player start? A current-production question.
- `trade_thresholds` (dynasty): is this a real trade chip? A value question.
  Conflating these made a team with three startable WRs read as critically short - see
  LOGIC.md's "Positional needs".
"""

from dataclasses import dataclass, field

from sources import sleeper
from sources.cache import ttl_cache, LEAGUE_CONFIG_TTL

from . import roster_needs
from .team_values import NUM_QBS, get_players_with_roles


@dataclass
class LeagueContext:
    league_id: str
    league: dict
    fmt: dict
    players: dict
    rosters: list
    owner_names: dict
    needs_slots: dict
    lineup_dedicated: dict
    lineup_flex: list
    start_thresholds: dict
    trade_thresholds: dict

    @property
    def num_teams(self) -> int:
        return self.fmt["num_teams"]

    def roster_for(self, owner_query: str) -> dict:
        """Rosters are looked up by owner substring everywhere in this project, so the
        matching rule (and its error message listing the real options) lives here rather
        than being re-implemented per module."""
        query = owner_query.lower()
        for roster in self.rosters:
            if query in self.owner_names.get(roster["owner_id"], "").lower():
                return roster
        raise ValueError(f"no owner matching '{owner_query}' - options: {list(self.owner_names.values())}")


@ttl_cache(LEAGUE_CONFIG_TTL)
def context(league_id: str) -> LeagueContext:
    league = sleeper.get_league(league_id)
    fmt = sleeper.describe_format(league)
    players = get_players_with_roles(NUM_QBS[fmt["is_superflex"]], fmt["num_teams"],
                                     fmt["ppr"], fmt["is_dynasty"])
    needs_slots = roster_needs.dedicated_slots(league["roster_positions"], fmt["is_superflex"])
    lineup_dedicated, lineup_flex = roster_needs.lineup_slots(league["roster_positions"])
    return LeagueContext(
        league_id=league_id,
        league=league,
        fmt=fmt,
        players=players,
        rosters=sleeper.get_rosters(league_id),
        owner_names={u["user_id"]: u["display_name"] for u in sleeper.get_users(league_id)},
        needs_slots=needs_slots,
        lineup_dedicated=lineup_dedicated,
        lineup_flex=lineup_flex,
        start_thresholds=roster_needs.replacement_thresholds(
            players, needs_slots, fmt["num_teams"], metric="redraft_value"),
        trade_thresholds=roster_needs.replacement_thresholds(
            players, needs_slots, fmt["num_teams"], metric="value"),
    )
