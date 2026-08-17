"""One shared league context, built once and reused - the single place to add a field,
and the single place that knows which similar-looking concept is which:

**Two slot concepts:** `needs_slots` folds SUPER_FLEX into an extra QB ("how many must I
own"); `lineup_dedicated` + `lineup_flex` model the real lineup ("who actually starts").

**Two threshold concepts:** `start_thresholds` (redraft - can he start?) and
`trade_thresholds` (dynasty - is he a real chip?). Conflating them once marked a team
with three startable WRs as critically short.

**One starter concept:** `starters`, value-derived. Nothing reads Sleeper's current-week
snapshot, which is meaningless in the preseason.

**Two production concepts:** `redraft_value` is the MARKET's this-season price - what
buys, sells and clears a positional bar. `projected_ppg` is what a lineup PRODUCES -
Sleeper's season projection under this league's own scoring, per game. Every sum or
share of a lineup's production uses eppg; market prices are convex in points (Gibbs
priced 3.3x Chase Brown, projected 1.6x), so summing them made one star look like a
whole lineup. LOGIC.md, "Production is projected points".
"""

from dataclasses import dataclass, field

from sources import sleeper
from sources.cache import ttl_cache, LEAGUE_CONFIG_TTL

from . import roster_needs
from .team_values import get_players_with_roles

GAMES_PER_SEASON = 17


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
    # A copy per league: the market dict is shared across formats, the projection is
    # priced by THIS league's scoring.
    projections = sleeper.get_projections(league["season"])
    players = {pid: {**info, "projected_ppg": round(
        sleeper.score(projections.get(pid, {}), league["scoring_settings"]) / GAMES_PER_SEASON, 1)}
        for pid, info in players.items()}
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
