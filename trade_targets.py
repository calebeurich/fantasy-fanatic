"""Surface obvious trade fits: a team needing a position looks at Rebuilding teams'
sellable players there (their sell candidates), from owners who've actually made
trades before. This is a discovery tool, not a fairness calculator - it finds *who*
to call, not whether a specific package is fair (see CLAUDE.md/prior discussion on why
a real value calculator is a separate, harder problem: roster construction means bench
depth isn't fungible with a starter's value).

Smoke test: python trade_targets.py <league_id> <owner_name>
"""

import sys

import sleeper
import team_state
import roster_needs
import trade_activity

# Same VALUE_BASIS classification (team_state.py) drives both sides of a trade, just
# phrased for who's on which side of it.
BUY_PRICE_NOTE = {
    "production": "production-priced",
    "mixed": "upside-priced, may cost more than the fit justifies",
    "upside": "mostly future value - likely a real overpay for current-year fit",
}
OFFER_GIVE_UP_COST = {
    "production": "low - value is mostly already-realized production",
    "mixed": "moderate - some future value baked in too",
    "upside": "high - real future value you won't get back",
}


def _with_trade_note(entry: dict, other: dict, trade_counts: dict[str, int]) -> dict:
    return {**entry, "from_owner": other["owner"], "from_owner_trades": trade_counts.get(other["owner_id"], 0)}


def _my_offer_pool(me: dict, thresholds: dict[str, float]) -> list[dict]:
    """What you could realistically offer: bench value that isn't elite enough to be a
    cornerstone but also isn't part of your actual lineup (e.g. a 3rd QB in a 2-QB-max
    format), plus young surplus - never a valuable *starter*, even a non-cornerstone
    one, since that's not surplus, that's your team. Cheapest give-up cost first."""
    bench_sellable = [e for e in me["sellable"] if not e["is_starter"] and team_state.clears_relevance_floor(e, thresholds)]
    surplus = [e for e in me["tradeable_surplus"] if team_state.clears_relevance_floor(e, thresholds)]
    return bench_sellable + surplus


DEFAULT_MAX_PER_POSITION = 3  # a parameter, not a hard limit - "give me more" means call again with a higher number


def find_targets(league_id: str, owner_query: str, max_per_position: int = DEFAULT_MAX_PER_POSITION) -> dict:
    states = team_state.classify_league(league_id)
    needs_by_owner_id = roster_needs.league_needs(league_id)
    thresholds = roster_needs.league_thresholds(league_id)
    trade_counts = trade_activity.get_trade_counts(league_id)

    me = next((r for r in states if owner_query.lower() in r["owner"].lower()), None)
    if me is None:
        raise ValueError(f"no owner matching '{owner_query}' - options: {[r['owner'] for r in states]}")

    # A rebuilding team (especially one tanking for a pick) isn't trying to fill
    # starting-lineup needs with proven vets - it wants to sell what age value it has
    # left and stockpile youth/picks instead. Buy-target-by-need only makes sense for
    # a team actually trying to win now.
    if me["effective_strategy"] == "Rebuilding":
        sell_candidates = [e for e in me["sellable"] if team_state.clears_relevance_floor(e, thresholds)]
        acquire_targets = []
        for other in states:
            if other["owner_id"] == me["owner_id"] or other["effective_strategy"] not in ("Win-Now", "Middling"):
                continue
            for player in other["tradeable_surplus"]:
                if not team_state.clears_relevance_floor(player, thresholds):
                    continue
                acquire_targets.append(_with_trade_note(player, other, trade_counts))
        acquire_targets.sort(key=lambda t: (-t["from_owner_trades"], -t["value"]))
        return {"me": me, "mode": "rebuild", "sell_candidates": sell_candidates, "acquire_targets": acquire_targets}

    my_needs = needs_by_owner_id.get(me["owner_id"], {})
    # Critical needs (can't fill the position at all) get searched before thin ones
    # (fine, just no depth cushion) - a real recommendation should exhaust the urgent
    # gap before suggesting extra names for a position that isn't actually a problem.
    ordered_positions = sorted(my_needs, key=lambda p: 0 if my_needs[p] == "critical" else 1)

    targets = []
    for pos in ordered_positions:
        pos_targets = []
        for other in states:
            if other["owner_id"] == me["owner_id"] or other["effective_strategy"] != "Rebuilding":
                continue
            for player in other["sellable"]:
                if player["position"] != pos or not team_state.clears_relevance_floor(player, thresholds):
                    continue
                pos_targets.append({"position": pos, "need_level": my_needs[pos],
                                     **_with_trade_note(player, other, trade_counts)})
        pos_targets.sort(key=lambda t: (-t["from_owner_trades"], -t["value"]))
        targets += pos_targets[:max_per_position]

    return {"me": me, "mode": "buy", "needs": my_needs, "targets": targets,
            "my_offers": _my_offer_pool(me, thresholds)}


def _print_report(result: dict) -> None:
    me = result["me"]

    if result["mode"] == "rebuild":
        tank_note = "" if me["owns_next_first"] else " (doesn't own next 1st, so tanking for a pick wouldn't help)"
        print(f"{me['owner']}: Rebuilding{tank_note} - playing for future value, not starting-lineup needs")
        sell = ", ".join(e["name"] for e in result["sell_candidates"]) or "none - nothing worth selling left"
        print(f"sell candidates (prime/declining value that isn't your long-term core): {sell}")
        print()
        if not result["acquire_targets"]:
            print("no obvious acquire targets found")
            return
        print("acquire targets (young ascending surplus sitting on Win-Now/Middling rosters):")
        for t in result["acquire_targets"]:
            trade_note = f"{t['from_owner_trades']} trade(s) made" if t["from_owner_trades"] else "NEVER TRADES - unlikely"
            print(f"  {t['name']} ({t['position']}, value={t['value']}) from {t['from_owner']} - {trade_note}")
        return

    print(f"{me['owner']}: {me['effective_strategy']}, needs: {result['needs'] or 'none'}")
    if result["my_offers"]:
        print("you could offer (cheapest give-up cost first):")
        for e in result["my_offers"]:
            cost = OFFER_GIVE_UP_COST[team_state.VALUE_BASIS[e["bucket"]]]
            print(f"  {e['name']} ({e['position']}, value={e['value']}) - give-up cost: {cost}")
    else:
        print("you could offer: no obvious surplus")
    print()

    if not result["targets"]:
        print("no obvious targets found (no needs, or no Rebuilding team has a sell candidate there)")
        return

    print("buy targets (from Rebuilding teams, at a position you need):")
    for t in result["targets"]:
        trade_note = f"{t['from_owner_trades']} trade(s) made" if t["from_owner_trades"] else "NEVER TRADES - unlikely"
        price_note = BUY_PRICE_NOTE[team_state.VALUE_BASIS[t["bucket"]]]
        print(f"  {t['name']} ({t['position']}, value={t['value']}, {price_note}) from {t['from_owner']} "
              f"- need: {t['need_level']} - {trade_note}")


def main(league_id: str, owner_query: str = None, max_per_position: int = DEFAULT_MAX_PER_POSITION) -> None:
    if owner_query:
        _print_report(find_targets(league_id, owner_query, max_per_position))
        return

    # No owner given - run the whole league in one pass so it's easy to eyeball every
    # team's recommendations together instead of spot-checking one at a time.
    owners = [row["owner"] for row in team_state.classify_league(league_id)]
    for i, owner in enumerate(owners):
        if i > 0:
            print("\n" + "=" * 60 + "\n")
        _print_report(find_targets(league_id, owner, max_per_position))


if __name__ == "__main__":
    owner_arg = sys.argv[2] if len(sys.argv) > 2 else None
    limit_arg = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_MAX_PER_POSITION
    main(sys.argv[1], owner_arg, limit_arg)
