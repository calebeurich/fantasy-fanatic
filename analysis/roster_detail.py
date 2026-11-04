"""Full player-by-player breakdown for one team. The league view shows the aggregate;
this is what actually makes up that aggregate. Usage: python -m analysis.roster_detail <league_id> <owner_name>
"""

import sys

from sources import contracts, injuries, nflverse_ids, sleeper
from .team_values import age_bucket, eppg, years_to_decline


def build_rows(roster: dict, players: dict[str, dict], contract_data: dict[str, dict],
               starter_ids: set[str], miss_rates: dict[str, dict] | None = None) -> list[dict]:
    """`starter_ids` is the value-derived lineup, not Sleeper's current-week snapshot -
    this is the roster view a user actually reads, so a preseason snapshot listing one QB
    in a superflex league labelled a real starter as bench right on the screen.

    `miss_rate` is the share of roster weeks this player has actually missed over the last
    three seasons (`sources.injuries`), or **None for unknown** - which is not the same as
    zero and matters most for the youngest players, who are exactly the ones with too little
    history to judge. Two seasons is the minimum sample, so most rookies carry None here.

    `weeks_suspended` is kept *out* of `miss_rate` and reported alongside it. A suspended
    player was unavailable but not fragile, and merging the two told a manager his receiver
    was injury-prone when six of those weeks were served in full health. It is still a real
    availability fact, and arguably predicts itself, so it is surfaced rather than dropped."""
    rows = []
    for player_id in roster["players"] or []:
        info = players.get(player_id)
        lineup_role = "starter" if player_id in starter_ids else "bench"
        if info is None:
            known = nflverse_ids.sleeper_names().get(player_id, {})
            rows.append({"name": known.get("name") or f"(unknown player_id {player_id})",
                         "position": known.get("position") or "?", "value": 0,
                         "age": None, "bucket": "unvalued", "usage_role": None, "contract": None,
                         "years_to_decline": None,
                         "lineup_role": lineup_role,
                         "miss_rate": (miss_rates or {}).get(player_id, {}).get("miss_rate"),
                         "weeks_suspended": (miss_rates or {}).get(player_id, {}).get("weeks_suspended")})
            continue
        rows.append({
            "injury": info.get("injury"),
            "name": info["name"],
            "position": info["position"],
            "value": info["value"],
            "redraft_value": info.get("redraft_value"),
            "projected_ppg": eppg(info),
            "age": info["age"],
            "bucket": age_bucket(info["position"], info["age"], info.get("usage_role")),
            # The distance to the boundary, not just which side of it he's on - two players
            # can both read "prime" with years between them. See team_values.years_to_decline.
            "years_to_decline": years_to_decline(info["position"], info["age"],
                                                 info.get("usage_role")),
            "usage_role": info.get("usage_role"),
            "contract": contract_data.get(player_id),
            "lineup_role": lineup_role,
            "miss_rate": (miss_rates or {}).get(player_id, {}).get("miss_rate"),
            "weeks_suspended": (miss_rates or {}).get(player_id, {}).get("weeks_suspended"),
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
        line = f"  [{row['lineup_role']:<7}] {row['name']:<24} {row['position']:<3} value={row['value']:<6} age={age:<5} {bucket}"
        if row["contract"]:
            c = row["contract"]
            line += f"  ({c['years_remaining']}yr/${c['guaranteed']:.1f}M gtd)"
        print(line)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])


def at_healthy_rates(players: dict) -> dict:
    """Everyone at his rate when back, so a lineup can be filled as if the team were whole."""
    return {pid: {**p, "projected_ppg": p["healthy_ppg"]} if p.get("healthy_ppg") is not None else p
            for pid, p in players.items()}


def _back_week(pid: str, league: dict) -> int | None:
    """The first week left that projects him to score; None = no return projected."""
    from .league import REGULAR_SEASON_WEEKS

    now = max(int(sleeper.get_nfl_state().get("week") or 1), 1)
    for week in range(now, REGULAR_SEASON_WEEKS + 1):
        for row in sleeper.projection_rows(league["season"], week):
            if row["player_id"] == pid and sleeper.score(row.get("stats") or {}, league["scoring_settings"]) > 0:
                return week
    return None


def _injury_hole(ctx, roster: dict, total: float) -> dict:
    """What injuries already cost this lineup. A player out projects 0, so today's lineup has
    already routed around him and "without him" reads 0.0 - the cost is against full health."""
    from . import roster_needs

    current = {pid for _, pid in roster_needs.fill_lineup(roster, ctx.players, ctx.lineup_dedicated, ctx.lineup_flex)}
    healthy = at_healthy_rates(ctx.players)
    whole = roster_needs.fill_lineup(roster, healthy, ctx.lineup_dedicated, ctx.lineup_flex)
    out = [pid for _, pid in whole if pid not in current]
    if not out:
        return {}
    whole_total = sum(eppg(healthy[pid]) for _, pid in whole)
    return {"out_now": [{"name": ctx.players[pid]["name"], "position": ctx.players[pid]["position"],
                         "healthy_ppg": healthy[pid]["projected_ppg"],
                         "injury": ctx.players[pid].get("injury"),
                         "back_week": _back_week(pid, ctx.league)} for pid in out],
            "full_health_total": round(whole_total, 1),
            "injury_cost": round(whole_total - total, 1)}


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
                 "redraft_value": ctx.players[pid].get("redraft_value") or 0,
                 "projected_ppg": eppg(ctx.players[pid])} for slot, pid in filled]
        return rows, round(sum(r["projected_ppg"] for r in rows), 1)

    before_rows, before_total = build(roster["players"] or [])
    hole = _injury_hole(ctx, roster, before_total)
    if not dropped:
        return {"owner": ctx.owner_names.get(roster["owner_id"]), "lineup": before_rows,
                "total_production": before_total, **hole}

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
        "production_lost": round(before_total - after_total, 1),
        "promoted": [r for r in after_rows if r["name"] not in before_names],
        "moved_slots": moved,
        **hole,
        "note": (f"Removing {', '.join(dropped)} costs {before_total - after_total:.1f} "
                 f"projected points a game. This is the league's real slot rules applied "
                 f"exactly - flex and superflex slots refill from every eligible position, "
                 f"so the replacement is often not the same position as the player lost."),
    }
