"""Full player-by-player breakdown for one team. The league view shows the aggregate;
this is what actually makes up that aggregate. Usage: python -m analysis.roster_detail <league_id> <owner_name>
"""

import sys

from sources import contracts
from .team_values import age_bucket


def build_rows(roster: dict, players: dict[str, dict], contract_data: dict[str, dict],
               starter_ids: set[str]) -> list[dict]:
    """`starter_ids` is the value-derived lineup, not Sleeper's current-week snapshot -
    this is the roster view a user actually reads, so a preseason snapshot listing one QB
    in a superflex league labelled a real starter as bench right on the screen."""
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
    from .league import context
    ctx = context(league_id)
    league, players = ctx.league, ctx.players
    contract_data = contracts.get_contracts()
    rosters, owner_names = ctx.rosters, ctx.owner_names

    roster = ctx.roster_for(owner_name)
    owner = owner_names[roster["owner_id"]]
    rows = build_rows(roster, players, contract_data, ctx.starters_for(roster))
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
