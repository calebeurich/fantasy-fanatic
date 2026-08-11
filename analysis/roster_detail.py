"""Full player-by-player breakdown for one team. The league view shows the aggregate;
this is what actually makes up that aggregate. Usage: python -m analysis.roster_detail <league_id> <owner_name>
"""

import sys

from sources import contracts, injuries
from .team_values import age_bucket


def build_rows(roster: dict, players: dict[str, dict], contract_data: dict[str, dict],
               starter_ids: set[str], miss_rates: dict[str, dict] | None = None) -> list[dict]:
    """`starter_ids` is the value-derived lineup, not Sleeper's current-week snapshot -
    this is the roster view a user actually reads, so a preseason snapshot listing one QB
    in a superflex league labelled a real starter as bench right on the screen.

    `miss_rate` is the share of roster weeks this player has actually missed over the last
    three seasons (`sources.injuries`), or **None for unknown** - which is not the same as
    zero and matters most for the youngest players, who are exactly the ones with too little
    history to judge. Two seasons is the minimum sample, so most rookies carry None here."""
    rows = []
    for player_id in roster["players"] or []:
        info = players.get(player_id)
        lineup_role = "starter" if player_id in starter_ids else "bench"
        if info is None:
            rows.append({"name": f"(unvalued player_id {player_id})", "position": "?", "value": 0,
                         "age": None, "bucket": "n/a", "usage_role": None, "contract": None,
                         "lineup_role": lineup_role,
            "miss_rate": (miss_rates or {}).get(player_id, {}).get("miss_rate"), "miss_rate": None})
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
            "miss_rate": (miss_rates or {}).get(player_id, {}).get("miss_rate"),
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
    rows = build_rows(roster, players, contract_data, ctx.starters_for(roster),
                      injuries.player_miss_rates())
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


def optimal_lineup(league_id: str, owner_name: str, without: list[str] | None = None) -> dict:
    """This team's best legal lineup, and what it becomes with `without` players removed.

    Exists because filling FLEX and SUPER_FLEX is a small deterministic optimisation that a
    language model will confidently approximate. Asked "what if my RB2 goes down", the
    honest answer involves a cascade - the FLEX slides back into RB2 and something else
    fills the FLEX - and the something else is often not what a manager assumes. On a real
    roster the vacated FLEX went to a *tight end* (627) rather than the obvious backup WR
    (259), because FLEX accepts RB/WR/TE and the bench TE simply produced more.

    `without` takes player names (substring, case-insensitive) so a question can be phrased
    the way it's asked - "if Jonathan Taylor gets hurt" - rather than in player ids.
    """
    from .league import context
    from . import roster_needs

    ctx = context(league_id)
    roster = ctx.roster_for(owner_name)
    dropped, keep = [], list(roster["players"] or [])
    for query in without or []:
        match = next((p for p in keep if query.lower() in ctx.players.get(p, {}).get("name", "").lower()), None)
        if match is None:
            raise ValueError(f"no player matching '{query}' on this roster")
        keep.remove(match)
        dropped.append(ctx.players[match]["name"])

    def build(player_ids):
        filled = roster_needs.fill_lineup({**roster, "players": player_ids}, ctx.players,
                                          ctx.lineup_dedicated, ctx.lineup_flex)
        rows = [{"slot": slot, "name": ctx.players[pid]["name"],
                 "position": ctx.players[pid]["position"],
                 "redraft_value": ctx.players[pid].get("redraft_value") or 0} for slot, pid in filled]
        return rows, sum(r["redraft_value"] for r in rows)

    before_rows, before_total = build(roster["players"] or [])
    if not dropped:
        return {"owner": ctx.owner_names.get(roster["owner_id"]), "lineup": before_rows,
                "total_production": before_total}

    after_rows, after_total = build(keep)
    before_names = {r["name"] for r in before_rows}
    after_names = {r["name"] for r in after_rows}
    moved = [r for r in after_rows
             if r["name"] in before_names
             and r["slot"] != next(b["slot"] for b in before_rows if b["name"] == r["name"])]
    return {
        "owner": ctx.owner_names.get(roster["owner_id"]),
        "without": dropped,
        "lineup": after_rows,
        "total_production": after_total,
        "production_lost": before_total - after_total,
        "promoted": [r for r in after_rows if r["name"] not in before_names],
        "moved_slots": moved,
        "note": (f"Removing {', '.join(dropped)} costs {before_total - after_total:,} of "
                 f"current production. This is the league's real slot rules applied "
                 f"exactly - flex and superflex slots refill from every eligible position, "
                 f"so the replacement is often not the same position as the player lost."),
    }
