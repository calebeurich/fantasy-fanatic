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

from sources import sleeper, degraded
from sources.cache import ttl_cache, LEAGUE_CONFIG_TTL

from . import roster_needs
from .team_values import get_players_with_roles

GAMES_PER_SEASON = 17
REGULAR_SEASON_WEEKS = 18   # NFL weeks, the bye included


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


def rest_of_season_ppg(weekly: dict[int, list[dict]], byes: dict[int, set[str]],
                       scoring: dict, preseason: dict[str, float]) -> dict[str, tuple[float, float]]:
    """player_id -> (expected, healthy) points a game over the weeks left. In-season the
    season-total projection goes stale (Sleeper froze it at preseason - verified week 3,
    2026: Lamar still exactly August's 18.7) while the weekly lines move, so ePPG is
    rebuilt from them. A bye is skipped (owner: "don't count bye weeks"); an injury week
    counts as the 0 it projects ("injuries are real"). `healthy` is his rate when back,
    only for a player OUT NOW (his next game projects 0 - A.J. Brown's five zeros, then
    back in week 8): the weeks he is projected to play, or the preseason rate when there
    are none (Sleeper projects IR with no games at all). A fill-in whose zeros come AFTER
    his starts (Kirk Cousins, 14 14 16 then 0s) has no healthy story: healthy = expected."""
    pts: dict[str, list[float]] = {}
    for week, rows in weekly.items():
        for r in rows:
            if r.get("team") and r["team"] not in byes.get(week, set()):
                pts.setdefault(r["player_id"], []).append(sleeper.score(r.get("stats") or {}, scoring))
    out = {}
    for pid, weeks in pts.items():
        expected = sum(weeks) / len(weeks)
        playing = [p for p in weeks if p > 0]
        if weeks[0] > 0:
            healthy = expected
        else:
            healthy = sum(playing) / len(playing) if playing else preseason.get(pid, 0)
        out[pid] = (round(expected, 1), round(healthy, 1))
    return out


def _projected_ppg(league: dict) -> dict[str, tuple[float, float | None]]:
    """player_id -> (projected_ppg, healthy_ppg). Preseason: the season total over 17
    games, and no healthy number. In-season: `rest_of_season_ppg`."""
    season, scoring = league["season"], league["scoring_settings"]
    preseason = {pid: round(sleeper.score(stats, scoring) / GAMES_PER_SEASON, 1)
                 for pid, stats in sleeper.get_projections(season).items()}
    try:
        state = sleeper.get_nfl_state()
        if state.get("season_type") != "regular" or str(state.get("season")) != str(season):
            return {pid: (v, None) for pid, v in preseason.items()}
        weeks = range(max(int(state.get("week") or 1), 1), REGULAR_SEASON_WEEKS + 1)
        return rest_of_season_ppg({w: sleeper.projection_rows(season, w) for w in weeks},
                                  {w: sleeper.bye_teams(season, w) for w in weeks},
                                  scoring, preseason)
    except Exception as e:
        degraded.record("weekly projections", f"ePPG fell back to the preseason projection ({type(e).__name__})")
        return {pid: (v, None) for pid, v in preseason.items()}


@ttl_cache(LEAGUE_CONFIG_TTL)
def context(league_id: str) -> LeagueContext:
    league = sleeper.get_league(league_id)
    fmt = sleeper.describe_format(league)
    players = get_players_with_roles(fmt["num_qbs"], fmt["num_teams"],
                                     fmt["ppr"], fmt["is_dynasty"], fmt["tep_tier"])
    # A copy per league: the market dict is shared across formats, the projection is
    # priced by THIS league's scoring.
    ppg = _projected_ppg(league)
    players = {pid: {**info, "projected_ppg": ppg.get(pid, (0.0, None))[0],
                     "healthy_ppg": ppg.get(pid, (0.0, None))[1]}
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
