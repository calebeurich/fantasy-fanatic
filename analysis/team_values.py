"""Dynasty value + win-window breakdown per team. Usage: python -m analysis.team_values <league_id>"""

import sys

from sources import sleeper, fantasycalc, contracts, player_roles

NUM_QBS = {True: 2, False: 1}  # superflex counts as a 2nd "QB" slot for value purposes

# (ascending_below, declining_at_or_above), by position. Dynasty community heuristics,
# not a model: RBs decline earliest, QBs/TEs age gracefully.
AGE_CURVE = {
    "QB": (26, 34),
    "RB": (24, 27),
    "WR": (25, 29),
    "TE": (25, 30),
}

# Usage-based overrides (see player_roles.py): mobile QBs lean on athleticism, so their
# decline pulls forward; receiving-down RBs age more like WRs, so theirs pushes back.
AGE_CURVE_OVERRIDES = {
    "rushing_qb": (26, 31),
    "pass_catching_rb": (24, 29),
}

# A "declining" player still on a multi-year deal is a weaker sell than the age curve
# alone suggests - a team is still paying for the role, not just letting it expire.
SECURE_YEARS_REMAINING = 2

# How many future draft classes to count as pick capital. Beyond this, picks are too
# speculative to value meaningfully and dynasty traders rarely deal that far out anyway.
FUTURE_DRAFT_YEARS = 2


def age_bucket(position: str, age: float | None, role: str | None = None) -> str:
    if age is None:
        return "unknown"
    if role in AGE_CURVE_OVERRIDES:
        young_cutoff, old_cutoff = AGE_CURVE_OVERRIDES[role]
    elif position in AGE_CURVE:
        young_cutoff, old_cutoff = AGE_CURVE[position]
    else:
        return "unknown"
    if age < young_cutoff:
        return "ascending"
    if age >= old_cutoff:
        return "declining"
    return "prime"


ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = ORDINAL_SUFFIXES.get(n % 10, "th")
    return f"{n}{suffix}"


def team_breakdown(player_ids: list[str], players: dict[str, dict]) -> dict:
    totals = {"ascending": 0, "prime": 0, "declining": 0, "unknown": 0}
    for player_id in player_ids:
        info = players.get(player_id)
        if info is None:
            continue
        bucket = age_bucket(info["position"], info["age"], info.get("usage_role"))
        totals[bucket] += info["value"]
    return totals


def find_outliers(player_ids: list[str], players: dict[str, dict], contract_data: dict[str, dict]) -> list[str]:
    """Declining-by-age players whose contract says otherwise."""
    outliers = []
    for player_id in player_ids:
        info = players.get(player_id)
        contract = contract_data.get(player_id)
        if info is None or contract is None:
            continue
        if age_bucket(info["position"], info["age"], info.get("usage_role")) != "declining":
            continue
        if contract["years_remaining"] >= SECURE_YEARS_REMAINING:
            outliers.append(f"{info['name']} ({contract['years_remaining']}yr/${contract['guaranteed']:.1f}M gtd)")
    return outliers


def get_players_with_roles(num_qbs: int, num_teams: int, ppr: float, is_dynasty: bool) -> dict[str, dict]:
    players = fantasycalc.get_players(num_qbs, num_teams, ppr, is_dynasty)
    for player_id, role in player_roles.get_roles().items():
        if player_id in players:
            players[player_id]["usage_role"] = role

    # Redraft values from the same API with one flipped parameter - free, and previously
    # unused. Dynasty value prices *current production plus future years*; redraft prices
    # current production alone. The ratio between them is the share of a player's price
    # that's future potential, which is exactly what a win-now team is overpaying for.
    #
    # Real case that motivated this: a superflex team's QB2 (C.J. Stroud, dynasty 3,288,
    # redraft 2,744, premium 1.20) and QB3 (Sam Darnold, dynasty 2,735, redraft 2,704,
    # premium 1.01) produce within 1.5% of each other *this season*, but Stroud costs 553
    # more in trade value. For a win-now roster that's arbitrage - sell the premium, keep
    # the production. Ranking by dynasty value alone can't see it.
    #
    # Deliberately NOT exposing a dynasty/redraft *ratio*. A first version did, and it
    # was the `diff` mistake again: 1.0 reads as neutral, but the measured median ratio
    # across the 200 players in both pools is 2.22 (p10 0.93, p90 18.1). So a 2.01 looked
    # like "100% future premium" while actually sitting *below* typical - it flagged
    # production-oriented veterans as speculative assets. Raw redraft_value is
    # unambiguous (a price on a known scale, both pools topping out near 10,400);
    # comparisons are made pairwise within a position by find_efficiency_swaps, where the
    # skew cancels.
    #
    # Coverage is partial by nature of the source: redraft carries ~200 players against
    # dynasty's ~400, since deep dynasty-only assets (rookies, prospects) have no redraft
    # market. Missing entries get redraft_value=None and future_premium=None rather than a
    # fabricated number - callers must handle absence, not silently treat it as zero.
    if is_dynasty:
        redraft = fantasycalc.get_players(num_qbs, num_teams, ppr, is_dynasty=False)
        for player_id, info in players.items():
            r = redraft.get(player_id)
            info["redraft_value"] = r["value"] if r else None
    return players


def pick_equivalent(value: float, pick_values: dict[str, int]) -> str | None:
    """The draft pick a player's value is closest to, e.g. "about a 2027 3rd (Late)".

    Exists because a raw number is hard to feel. Told that a bench piece is "worth 947",
    nobody knows whether that's a real asset; told it's "about a 2027 3rd", every dynasty
    manager immediately does - and the honest read of a low-value depth player is closer
    to a late pick than to a piece a trade is built around. FantasyCalc prices picks on
    the same scale as players, so this is a lookup, not a model.

    Future classes are priced as flat round averages (see get_pick_values), so the match
    is approximate by nature - hence "about".
    """
    if not pick_values or value <= 0:
        return None
    name, _ = min(pick_values.items(), key=lambda kv: abs(kv[1] - value))
    return name


def split_starters_bench(roster: dict, players: dict[str, dict]) -> tuple[int, int]:
    starter_ids = {pid for pid in (roster["starters"] or []) if pid != "0"}
    all_ids = roster["players"] or []
    starter_value = sum(players[pid]["value"] for pid in starter_ids if pid in players)
    bench_value = sum(players[pid]["value"] for pid in all_ids if pid in players and pid not in starter_ids)
    return starter_value, bench_value


def pick_capital(league_id: str, season: int, draft_rounds: int, roster_ids: list[int],
                  pick_values: dict[str, int]) -> dict[int, int]:
    """Total future pick value per roster_id, current owner after trades."""
    traded = sleeper.get_traded_picks(league_id)
    traded_map = {(int(t["season"]), t["round"], t["roster_id"]): t["owner_id"] for t in traded}

    totals = {rid: 0 for rid in roster_ids}
    for year_offset in range(1, FUTURE_DRAFT_YEARS + 1):
        pick_season = season + year_offset
        for round_num in range(1, draft_rounds + 1):
            value = pick_values.get(f"{pick_season} {ordinal(round_num)}", 0)
            for rid in roster_ids:
                current_owner = traded_map.get((pick_season, round_num, rid), rid)
                totals[current_owner] += value
    return totals


def main(league_id: str) -> None:
    league = sleeper.get_league(league_id)
    fmt = sleeper.describe_format(league)
    num_qbs = NUM_QBS[fmt["is_superflex"]]

    players = get_players_with_roles(num_qbs, fmt["num_teams"], fmt["ppr"], fmt["is_dynasty"])
    pick_values = fantasycalc.get_pick_values(num_qbs, fmt["num_teams"], fmt["ppr"], fmt["is_dynasty"])
    contract_data = contracts.get_contracts()

    rosters = sleeper.get_rosters(league_id)
    users = sleeper.get_users(league_id)
    owner_names = {user["user_id"]: user["display_name"] for user in users}

    pick_totals = pick_capital(
        league_id, int(league["season"]), league["settings"]["draft_rounds"],
        [r["roster_id"] for r in rosters], pick_values,
    )

    standings = []
    for roster in rosters:
        owner = owner_names.get(roster["owner_id"], "Unknown")
        player_ids = roster["players"] or []
        starter_value, bench_value = split_starters_bench(roster, players)
        pick_value = pick_totals[roster["roster_id"]]
        breakdown = team_breakdown(player_ids, players)
        outliers = find_outliers(player_ids, players, contract_data)
        total = starter_value + bench_value + pick_value
        standings.append((owner, total, starter_value, bench_value, pick_value, breakdown, outliers))

    standings.sort(key=lambda row: row[1], reverse=True)
    print(f"{league['name']} - dynasty value standings:")
    for rank, (owner, total, starters, bench, picks, breakdown, outliers) in enumerate(standings, start=1):
        player_total = starters + bench
        pct = {b: round(100 * v / player_total) for b, v in breakdown.items()} if player_total else breakdown
        print(f"  {rank}. {owner}: {total} total  (starters {starters} / bench {bench} / picks {picks})")
        print(f"       age mix: ascending {pct['ascending']}% / prime {pct['prime']}% / declining {pct['declining']}% / unknown {pct['unknown']}%")
        if outliers:
            print(f"       declining-by-age but contract-secure: {', '.join(outliers)}")


if __name__ == "__main__":
    main(sys.argv[1])
