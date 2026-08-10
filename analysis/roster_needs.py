"""Which positions a team is actually thin at - not total value, but real depth. A team
can be fine at RB in total value while only having one RB worth starting; that's a need
even if the aggregate number looks okay. "Usable" is relative to the league's own format:
the Nth-best player at a position leaguewide, where N = how many starting slots the whole
league has at that position, sets the bar - not a hardcoded value cutoff.

Smoke test: python -m analysis.roster_needs <league_id>
"""

import sys

from sources import sleeper
from .team_values import NUM_QBS, get_players_with_roles

POSITIONS = ["QB", "RB", "WR", "TE"]


def dedicated_slots(roster_positions: list[str], is_superflex: bool) -> dict[str, int]:
    return {
        "QB": roster_positions.count("QB") + (1 if is_superflex else 0),
        "RB": roster_positions.count("RB"),
        "WR": roster_positions.count("WR"),
        "TE": roster_positions.count("TE"),
    }


def replacement_thresholds(players: dict[str, dict], slots: dict[str, int], num_teams: int) -> dict[str, float]:
    thresholds = {}
    for pos, starters_needed in slots.items():
        pos_values = sorted((info["value"] for info in players.values() if info["position"] == pos), reverse=True)
        rank = min(num_teams * starters_needed, len(pos_values)) - 1
        thresholds[pos] = pos_values[max(rank, 0)]
    return thresholds


def _usable_by_position(roster: dict, players: dict[str, dict], thresholds: dict[str, float]) -> dict[str, list[dict]]:
    """Every rostered player at each position that clears this league's replacement
    level, best to worst. The one shared walk find_needs and find_surplus both read
    from, so "usable" means exactly the same thing in a need (too few of them) as it
    does in a surplus (more than the starting slots require)."""
    by_pos = {pos: [] for pos in POSITIONS}
    for pid in roster["players"] or []:
        info = players.get(pid)
        if info and info["position"] in by_pos and info["value"] >= thresholds[info["position"]]:
            by_pos[info["position"]].append({"name": info["name"], "position": info["position"], "value": info["value"]})
    for entries in by_pos.values():
        entries.sort(key=lambda e: -e["value"])
    return by_pos


def find_needs(roster: dict, players: dict[str, dict], slots: dict[str, int], thresholds: dict[str, float]) -> dict:
    usable = _usable_by_position(roster, players, thresholds)
    needs = {}
    for pos in POSITIONS:
        count = len(usable[pos])
        required = slots[pos]
        if count < required:
            needs[pos] = "critical"
        elif count == required:
            needs[pos] = "thin"
    return needs


def find_surplus(roster: dict, players: dict[str, dict], slots: dict[str, int], thresholds: dict[str, float]) -> dict:
    """Mirror of find_needs: positions where a team has MORE usable players than its
    starting slots need, and which players specifically are the spare ones. Only
    players beyond the required starter count count as surplus - the top `slots[pos]`
    are the actual starting group and never get offered here. This is what makes a
    win-now-to-win-now swap real: a team's true extra depth at a position it doesn't
    need, not just any valuable player on the roster."""
    usable = _usable_by_position(roster, players, thresholds)
    return {pos: usable[pos][slots[pos]:] for pos in POSITIONS if len(usable[pos]) > slots[pos]}


def projected_starters(roster: dict, players: dict[str, dict], slots: dict[str, int]) -> set[str]:
    """Names of the players a team would actually start, derived from value and the
    league's own slot counts.

    Deliberately *not* Sleeper's `starters` field. That reflects whatever the current
    week's lineup happens to be, which is meaningless before Week 1 - in a real
    superflex league (2 QB slots) the snapshot listed exactly one QB as a starter, so
    the team's obvious QB2 (C.J. Stroud, ascending, 3,288 value) was classed as bench
    and offered up as spare parts. In superflex especially, a second QB is among the
    most valuable things on a roster, not dead weight.

    **Ranked by redraft value, not dynasty value.** A lineup is purely "who scores most
    this week", which is what redraft prices measure; dynasty value governs who you keep
    or trade, not who you start. Ranking by dynasty put the wrong player in the lineup -
    a real league showed a TE whose bench alternative produced *102%* of his current
    output, i.e. the better current player was sitting. This holds for every window, not
    just Win-Now: a rebuilding team still starts its best scorers.

    Players with no redraft price sort last. That's safe rather than lossy: redraft
    covers the top ~200, and the highest-dynasty rostered player missing one across a
    real 12-team league was 1,350 - far below every positional replacement level, so
    nobody actually startable is affected.

    Top `slots[pos]` at each position. Flex slots aren't modelled (same approximation as
    `dedicated_slots`), so this is conservative: it can under-count starters, never
    over-count them.
    """
    by_pos: dict[str, list[tuple[float, str]]] = {pos: [] for pos in POSITIONS}
    for pid in roster["players"] or []:
        info = players.get(pid)
        if info and info["position"] in by_pos:
            by_pos[info["position"]].append((info.get("redraft_value") or 0, info["name"]))
    starters = set()
    for pos, entries in by_pos.items():
        entries.sort(reverse=True)
        starters.update(name for _, name in entries[:slots[pos]])
    return starters


def _league_setup(league_id: str) -> tuple[dict[str, dict], dict[str, int], dict[str, float]]:
    """Shared setup every per-team function below needs: the player value pool,
    starter slot counts, and replacement-level thresholds for this league's format -
    computed once instead of re-fetched by every league_* function."""
    league = sleeper.get_league(league_id)
    fmt = sleeper.describe_format(league)
    num_qbs = NUM_QBS[fmt["is_superflex"]]

    players = get_players_with_roles(num_qbs, fmt["num_teams"], fmt["ppr"], fmt["is_dynasty"])
    slots = dedicated_slots(league["roster_positions"], fmt["is_superflex"])
    thresholds = replacement_thresholds(players, slots, fmt["num_teams"])
    return players, slots, thresholds


def league_projected_starters(league_id: str) -> dict[str, set[str]]:
    """Projected starting lineup per roster, keyed by owner_id - the value-derived
    version of "who's actually in the lineup", used by trade_targets so it never
    offers away a real starter on the strength of a stale preseason snapshot."""
    players, slots, _ = _league_setup(league_id)
    return {
        roster["owner_id"]: projected_starters(roster, players, slots)
        for roster in sleeper.get_rosters(league_id)
    }


def league_thresholds(league_id: str) -> dict[str, float]:
    """Replacement-level value per position for this league's format - the bar a player
    needs to clear to plausibly fill a need there, reused by trade_targets.py so it
    doesn't suggest a near-zero-value player as the fix for a real roster hole."""
    _, _, thresholds = _league_setup(league_id)
    return thresholds


def league_needs(league_id: str) -> dict[str, dict]:
    """Positional needs for every roster, keyed by owner_id."""
    players, slots, thresholds = _league_setup(league_id)
    return {
        roster["owner_id"]: find_needs(roster, players, slots, thresholds)
        for roster in sleeper.get_rosters(league_id)
    }


def league_surplus(league_id: str) -> dict[str, dict]:
    """Positional surplus for every roster, keyed by owner_id - the mirror of
    league_needs, reused by trade_targets.find_mutual_swaps to match one team's
    spare depth against another's need."""
    players, slots, thresholds = _league_setup(league_id)
    return {
        roster["owner_id"]: find_surplus(roster, players, slots, thresholds)
        for roster in sleeper.get_rosters(league_id)
    }


def main(league_id: str) -> None:
    needs_by_owner_id = league_needs(league_id)
    owner_names = {u["user_id"]: u["display_name"] for u in sleeper.get_users(league_id)}

    for owner_id, needs in needs_by_owner_id.items():
        owner = owner_names.get(owner_id, "Unknown")
        if needs:
            summary = ", ".join(f"{pos} ({level})" for pos, level in needs.items())
            print(f"  {owner}: {summary}")
        else:
            print(f"  {owner}: no positional needs")


if __name__ == "__main__":
    main(sys.argv[1])
