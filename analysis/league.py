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

**One starter concept, deliberately only one.** `starters` is the single answer to "who
is in this team's lineup", derived from value and the league's own slots. Sleeper's
`roster["starters"]` is a snapshot of whatever the current week's lineup happens to be,
which is meaningless in the preseason - in this league it listed one QB for a superflex
team and eight starters for a ten-slot lineup. That was known and fixed on one code path
while three others kept reading the snapshot, including the one computing the starter
value the whole league is ranked by. Now nothing reads it.
"""

from dataclasses import dataclass, field

from sources import sleeper
from sources.cache import ttl_cache, LEAGUE_CONFIG_TTL

from . import roster_needs
from .team_values import get_players_with_roles


@dataclass
class LeagueContext:
    league_id: str
    league: dict
    fmt: dict
    players: dict
    rosters: list
    owner_names: dict
    team_names: dict
    needs_slots: dict
    lineup_dedicated: dict
    lineup_flex: list
    start_thresholds: dict
    trade_thresholds: dict
    starters: dict          # owner_id -> set of player_ids actually in the lineup

    @property
    def num_teams(self) -> int:
        return self.fmt["num_teams"]

    def starters_for(self, roster: dict) -> set[str]:
        return self.starters.get(roster["owner_id"], set())

    def aliases_for(self, owner_id: str) -> list[str]:
        """Every name a manager might be called by: their Sleeper handle and their team
        name. Both are how people actually refer to a team - this project only ever
        matched on the handle, so a user asking about "Where's the Lamb Sauce???" got told
        no such owner existed while the tool happily listed twelve handles nobody uses in
        conversation."""
        return [n for n in (self.owner_names.get(owner_id), self.team_names.get(owner_id)) if n]

    def roster_for(self, owner_query: str) -> dict:
        """Rosters are looked up by owner substring everywhere in this project, so the
        matching rule (and its error message listing the real options) lives here rather
        than being re-implemented per module."""
        return self.rosters[self._match(owner_query, [r["owner_id"] for r in self.rosters])]

    def pick_owner(self, owner_query: str, rows: list[dict]) -> dict:
        """The same match against an already-computed list of per-team rows (e.g.
        `team_state.classify_league` output), which is what the trade paths hold rather
        than raw rosters. Shares `_match` so the rule and the error message can't drift
        from `roster_for`'s - they were separately hand-rolled in four places."""
        return rows[self._match(owner_query, [row["owner_id"] for row in rows])]

    def _match(self, owner_query: str, owner_ids: list[str]) -> int:
        query = _normalize(owner_query)
        for i, owner_id in enumerate(owner_ids):
            if any(query in _normalize(a) for a in self.aliases_for(owner_id)):
                return i
        options = [" / ".join(self.aliases_for(o)) for o in owner_ids]
        raise ValueError(f"no owner matching '{owner_query}' - options: {options}")


def _normalize(name: str) -> str:
    """Lowercase, letters and digits only - spaces and punctuation dropped entirely.

    Team names are free text, full of characters nobody retypes. The real one that broke
    this is "Where's the Lamb Sauce???", where Sleeper stores a curly apostrophe (U+2019):
    a user typing the obvious "wheres the lamb sauce" matched nothing. Dropping punctuation
    to a *space* doesn't fix it either - that yields "where s the lamb sauce", which the
    query still doesn't sit inside. Removing separators altogether makes the comparison
    about the letters, which is what someone typing a team name from memory gets right."""
    return "".join(c for c in name.lower() if c.isalnum())


@ttl_cache(LEAGUE_CONFIG_TTL)
def context(league_id: str) -> LeagueContext:
    league = sleeper.get_league(league_id)
    fmt = sleeper.describe_format(league)
    players = get_players_with_roles(fmt["num_qbs"], fmt["num_teams"],
                                     fmt["ppr"], fmt["is_dynasty"], fmt["tep_tier"])
    needs_slots = roster_needs.dedicated_slots(league["roster_positions"])
    lineup_dedicated, lineup_flex = roster_needs.lineup_slots(league["roster_positions"])
    rosters = sleeper.get_rosters(league_id)
    users = sleeper.get_users(league_id)
    return LeagueContext(
        league_id=league_id,
        league=league,
        fmt=fmt,
        players=players,
        rosters=rosters,
        owner_names={u["user_id"]: u["display_name"] for u in users},
        team_names={u["user_id"]: (u.get("metadata") or {}).get("team_name")
                    for u in users},
        needs_slots=needs_slots,
        lineup_dedicated=lineup_dedicated,
        lineup_flex=lineup_flex,
        start_thresholds=roster_needs.replacement_thresholds(
            players, needs_slots, fmt["num_teams"], metric="redraft_value"),
        trade_thresholds=roster_needs.replacement_thresholds(
            players, needs_slots, fmt["num_teams"], metric="value"),
        # lineup_* rather than needs_slots: the real lineup has FLEX slots and a
        # SUPER_FLEX that takes any position, where needs_slots folds SUPER_FLEX into a
        # second QB.
        starters={r["owner_id"]: roster_needs.projected_starters(
            r, players, lineup_dedicated, lineup_flex) for r in rosters},
    )
