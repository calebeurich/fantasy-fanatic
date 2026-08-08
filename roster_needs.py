"""Which positions a team is actually thin at - not total value, but real depth. A team
can be fine at RB in total value while only having one RB worth starting; that's a need
even if the aggregate number looks okay. "Usable" is relative to the league's own format:
the Nth-best player at a position leaguewide, where N = how many starting slots the whole
league has at that position, sets the bar - not a hardcoded value cutoff.

Smoke test: python roster_needs.py <league_id>
"""

import sys

import sleeper
from team_values import NUM_QBS, get_players_with_roles

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


def find_needs(roster: dict, players: dict[str, dict], slots: dict[str, int], thresholds: dict[str, float]) -> dict:
    needs = {}
    for pos in POSITIONS:
        usable = sum(
            1 for pid in (roster["players"] or [])
            if (info := players.get(pid)) and info["position"] == pos and info["value"] >= thresholds[pos]
        )
        required = slots[pos]
        if usable < required:
            needs[pos] = "critical"
        elif usable == required:
            needs[pos] = "thin"
    return needs


def league_needs(league_id: str) -> dict[str, dict]:
    """Positional needs for every roster, keyed by owner_id."""
    league = sleeper.get_league(league_id)
    fmt = sleeper.describe_format(league)
    num_qbs = NUM_QBS[fmt["is_superflex"]]

    players = get_players_with_roles(num_qbs, fmt["num_teams"], fmt["ppr"], fmt["is_dynasty"])
    slots = dedicated_slots(league["roster_positions"], fmt["is_superflex"])
    thresholds = replacement_thresholds(players, slots, fmt["num_teams"])

    return {
        roster["owner_id"]: find_needs(roster, players, slots, thresholds)
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
