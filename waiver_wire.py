"""Unrostered players with real dynasty value, and whether any of them are an
immediate upgrade over what's already on a roster. Not very impactful in a deep
12-team dynasty league today (there usually isn't a notable name sitting unrostered),
but it matters a lot more once Twitter/sportsbook signals can flag a name before its
dynasty value catches up - this establishes the "what's actually out there" baseline
that a future breakout-detection feature would compare against.

Smoke test: python waiver_wire.py <league_id> [owner_name]
"""

import sys

import sleeper
import team_state
import roster_needs
from team_values import NUM_QBS, age_bucket, get_players_with_roles

POSITIONS = ["QB", "RB", "WR", "TE"]


def get_available_players(league_id: str, players: dict[str, dict]) -> dict[str, dict]:
    """Players with a real dynasty value that nobody in this league has rostered."""
    rostered = {pid for r in sleeper.get_rosters(league_id) for pid in (r["players"] or [])}
    return {pid: info for pid, info in players.items() if pid not in rostered}


def get_waiver_budgets(league_id: str) -> dict[str, int]:
    """FAAB remaining per owner. Tradeable in most leagues (including this one) but
    rarely actually traded - still worth knowing who has the resources to actually
    act on a waiver claim."""
    league = sleeper.get_league(league_id)
    total_budget = league["settings"]["waiver_budget"]
    owner_names = {u["user_id"]: u["display_name"] for u in sleeper.get_users(league_id)}
    return {
        owner_names.get(r["owner_id"], "Unknown"): total_budget - r["settings"]["waiver_budget_used"]
        for r in sleeper.get_rosters(league_id)
    }


def find_upgrades(roster: dict, players: dict[str, dict], available: dict[str, dict],
                   needs: dict[str, str], thresholds: dict[str, float]) -> list[dict]:
    """Where an available player is either a straight upgrade over this team's worst
    rostered player at the position, or fills a real need even if it isn't."""
    upgrades = []
    for position in POSITIONS:
        my_values = sorted(
            players[pid]["value"] for pid in (roster["players"] or [])
            if pid in players and players[pid]["position"] == position
        )
        worst_mine = my_values[0] if my_values else 0

        candidates = []
        for info in available.values():
            if info["position"] != position:
                continue
            bucket = age_bucket(info["position"], info["age"], info.get("usage_role"))
            if team_state.clears_relevance_floor({**info, "bucket": bucket}, thresholds):
                candidates.append(info)
        if not candidates:
            continue

        best = max(candidates, key=lambda info: info["value"])
        if best["value"] > worst_mine:
            reason = "upgrade over your worst rostered player at the position"
        elif position in needs:
            reason = f"not better than your worst, but fills a {needs[position]} need"
        else:
            continue
        upgrades.append({"position": position, "name": best["name"], "value": best["value"], "reason": reason})
    return upgrades


def league_upgrades(league_id: str, owner_query: str = None) -> dict:
    """Available-player count plus per-owner waiver upgrades and FAAB budget. Reused
    by both the CLI smoke test and the MCP tool wrapper - one source of truth for the
    orchestration, not duplicated in each caller."""
    league = sleeper.get_league(league_id)
    fmt = sleeper.describe_format(league)
    num_qbs = NUM_QBS[fmt["is_superflex"]]

    players = get_players_with_roles(num_qbs, fmt["num_teams"], fmt["ppr"], fmt["is_dynasty"])
    available = get_available_players(league_id, players)
    thresholds = roster_needs.league_thresholds(league_id)
    needs_by_owner_id = roster_needs.league_needs(league_id)
    budgets = get_waiver_budgets(league_id)

    rosters = sleeper.get_rosters(league_id)
    owner_names = {u["user_id"]: u["display_name"] for u in sleeper.get_users(league_id)}

    teams = []
    for roster in rosters:
        owner = owner_names.get(roster["owner_id"], "Unknown")
        if owner_query and owner_query.lower() not in owner.lower():
            continue
        needs = needs_by_owner_id.get(roster["owner_id"], {})
        upgrades = find_upgrades(roster, players, available, needs, thresholds)
        teams.append({"owner": owner, "faab_remaining": budgets.get(owner), "upgrades": upgrades})

    return {"available_count": len(available), "teams": teams}


def main(league_id: str, owner_query: str = None) -> None:
    result = league_upgrades(league_id, owner_query)
    print(f"{result['available_count']} unrostered players with a real dynasty value")
    for team in result["teams"]:
        print(f"\n{team['owner']} (FAAB remaining: {team['faab_remaining']}):")
        if not team["upgrades"]:
            print("  no obvious waiver upgrades")
        for u in team["upgrades"]:
            print(f"  {u['name']} ({u['position']}, value={u['value']}) - {u['reason']}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
