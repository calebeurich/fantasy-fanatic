"""Full player-by-player breakdown for one team. The league view shows the aggregate;
this is what actually makes up that aggregate. Usage: python roster_detail.py <league_id> <owner_name>
"""

import sys

import sleeper
import contracts
from team_values import NUM_QBS, age_bucket, get_players_with_roles


def find_roster(owner_name: str, rosters: list[dict], owner_names: dict[str, str]) -> dict:
    query = owner_name.lower()
    for roster in rosters:
        name = owner_names.get(roster["owner_id"], "")
        if query in name.lower():
            return roster
    raise ValueError(f"no owner matching '{owner_name}' - options: {list(owner_names.values())}")


def build_rows(roster: dict, players: dict[str, dict], contract_data: dict[str, dict]) -> list[dict]:
    starter_ids = {pid for pid in (roster["starters"] or []) if pid != "0"}
    rows = []
    for player_id in roster["players"] or []:
        info = players.get(player_id)
        lineup_role = "starter" if player_id in starter_ids else "bench"
        if info is None:
            rows.append({"name": f"(unvalued player_id {player_id})", "position": "?", "value": 0,
                         "age": None, "bucket": "n/a", "usage_role": None, "contract": None,
                         "lineup_role": lineup_role})
            continue
        rows.append({
            "name": info["name"],
            "position": info["position"],
            "value": info["value"],
            "age": info["age"],
            "bucket": age_bucket(info["position"], info["age"], info.get("usage_role")),
            "usage_role": info.get("usage_role"),
            "contract": contract_data.get(player_id),
            "lineup_role": lineup_role,
        })
    rows.sort(key=lambda r: r["value"], reverse=True)
    return rows


def get_roster_rows(league_id: str, owner_name: str) -> dict:
    """One team's full player-by-player breakdown. Reused by both the CLI smoke test
    and the MCP tool wrapper."""
    league = sleeper.get_league(league_id)
    fmt = sleeper.describe_format(league)
    num_qbs = NUM_QBS[fmt["is_superflex"]]

    players = get_players_with_roles(num_qbs, fmt["num_teams"], fmt["ppr"], fmt["is_dynasty"])
    contract_data = contracts.get_contracts()

    rosters = sleeper.get_rosters(league_id)
    users = sleeper.get_users(league_id)
    owner_names = {user["user_id"]: user["display_name"] for user in users}

    roster = find_roster(owner_name, rosters, owner_names)
    owner = owner_names[roster["owner_id"]]
    rows = build_rows(roster, players, contract_data)
    return {"owner": owner, "league_name": league["name"], "rows": rows}


def main(league_id: str, owner_name: str) -> None:
    result = get_roster_rows(league_id, owner_name)
    print(f"{result['owner']}'s roster in {result['league_name']}:")
    for row in result["rows"]:
        age = f"{row['age']:.1f}" if row["age"] is not None else "?"
        bucket = row["bucket"] + (f" ({row['usage_role']})" if row["usage_role"] else "")
        line = f"  [{row['lineup_role']:<7}] {row['name']:<22} {row['position']:<3} value={row['value']:<6} age={age:<5} {bucket}"
        if row["contract"]:
            c = row["contract"]
            line += f"  ({c['years_remaining']}yr/${c['guaranteed']:.1f}M gtd)"
        print(line)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
