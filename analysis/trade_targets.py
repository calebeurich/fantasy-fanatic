"""Surface obvious trade fits: a team needing a position looks at Rebuilding teams'
sellable players there (their sell candidates), from owners who've actually made
trades before. This is a discovery tool, not a fairness calculator - it finds *who*
to call, not whether a specific package is fair (see CLAUDE.md/prior discussion on why
a real value calculator is a separate, harder problem: roster construction means bench
depth isn't fungible with a starter's value).

Smoke test: python -m analysis.trade_targets <league_id> <owner_name>
"""

import sys

from . import team_state, roster_needs, trade_activity

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

DEFAULT_MAX_PER_POSITION = 3  # a parameter, not a hard limit - "give me more" means call again with a higher number


def _with_trade_note(entry: dict, other: dict, trade_counts: dict[str, int]) -> dict:
    return {**entry, "from_owner": other["owner"], "from_owner_trades": trade_counts.get(other["owner_id"], 0)}


def _my_offer_pool(me: dict, thresholds: dict[str, float], needs: dict[str, str]) -> list[dict]:
    """What you could realistically offer: bench value that isn't elite enough to be a
    cornerstone but also isn't part of your actual lineup (e.g. a 3rd QB in a 2-QB-max
    format), plus young surplus - never a valuable *starter*, even a non-cornerstone
    one, since that's not surplus, that's your team. Also never a position you
    yourself have a need at - trading away a WR while WR is your own critical need
    just moves the shortage, it doesn't fix anything. Cheapest give-up cost first."""
    bench_sellable = [e for e in me["sellable"]
                       if not e["is_starter"] and e["position"] not in needs
                       and team_state.clears_relevance_floor(e, thresholds)]
    surplus = [e for e in me["tradeable_surplus"]
               if e["position"] not in needs and team_state.clears_relevance_floor(e, thresholds)]
    return bench_sellable + surplus


def _buy_path(me: dict, states: list[dict], needs_by_owner_id: dict, thresholds: dict[str, float],
              trade_counts: dict[str, int], max_per_position: int) -> dict:
    """The push case: fill needs with sellable value from Rebuilding teams."""
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

    return {"needs": my_needs, "targets": targets, "my_offers": _my_offer_pool(me, thresholds, my_needs)}


def _pivot_path(me: dict, states: list[dict], thresholds: dict[str, float], trade_counts: dict[str, int]) -> dict:
    """The sell case: cash in declining/non-core value for youth from teams that
    don't need it, same logic a Rebuilding team uses.

    Split by VALUE_BASIS rather than one flat list - a declining piece only loses
    value from here, real urgency to move it. A prime piece below the cornerstone bar
    (a genuinely good player, just not elite enough to be this team's long-term core -
    e.g. a real starting-caliber WR on an already-loaded corps) isn't losing value on
    a clock, so it's a situational, take-a-fair-offer piece, not an urgent sell -
    presenting both the same way overstates how clear-cut the prime ones are."""
    real_sellable = [e for e in me["sellable"] if team_state.clears_relevance_floor(e, thresholds)]
    sell_candidates = [e for e in real_sellable if e["bucket"] == "declining"]
    situational = [e for e in real_sellable if e["bucket"] != "declining"]
    acquire_targets = []
    for other in states:
        if other["owner_id"] == me["owner_id"] or other["effective_strategy"] not in ("Win-Now", "Middling"):
            continue
        for player in other["tradeable_surplus"]:
            if not team_state.clears_relevance_floor(player, thresholds):
                continue
            acquire_targets.append(_with_trade_note(player, other, trade_counts))
    acquire_targets.sort(key=lambda t: (-t["from_owner_trades"], -t["value"]))
    return {"sell_candidates": sell_candidates, "situational": situational, "acquire_targets": acquire_targets}


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
        return {"me": me, "mode": "rebuild", **_pivot_path(me, states, thresholds, trade_counts)}

    if me["effective_strategy"] == "Middling":
        # Hasn't committed to a direction - show what pushing looks like AND what
        # pivoting looks like, rather than silently picking one. Whichever path
        # actually makes sense usually depends on something we don't have yet (the
        # season record - a Middling team two games out of a playoff spot should push,
        # one that's clearly out should pivot even mid-season) - logged in LOGIC.md.
        return {"me": me, "mode": "middling",
                "push": _buy_path(me, states, needs_by_owner_id, thresholds, trade_counts, max_per_position),
                "pivot": _pivot_path(me, states, thresholds, trade_counts)}

    return {"me": me, "mode": "buy",
            **_buy_path(me, states, needs_by_owner_id, thresholds, trade_counts, max_per_position)}


def _print_pivot(me: dict, pivot: dict) -> None:
    sell = ", ".join(e["name"] for e in pivot["sell_candidates"]) or "none"
    print(f"sell candidates (declining - value only goes down from here, real urgency to move it): {sell}")
    situational = ", ".join(e["name"] for e in pivot["situational"]) or "none"
    print(f"situational pieces (good players, just not your long-term core - take a fair offer, no urgency): {situational}")
    if not pivot["acquire_targets"]:
        print("no obvious acquire targets found")
        return
    print("acquire targets (young ascending surplus sitting on Win-Now/Middling rosters):")
    for t in pivot["acquire_targets"]:
        trade_note = f"{t['from_owner_trades']} trade(s) made" if t["from_owner_trades"] else "NEVER TRADES - unlikely"
        print(f"  {t['name']} ({t['position']}, value={t['value']}) from {t['from_owner']} - {trade_note}")


def _print_push(push: dict) -> None:
    if push["my_offers"]:
        print("you could offer (cheapest give-up cost first):")
        for e in push["my_offers"]:
            cost = OFFER_GIVE_UP_COST[team_state.VALUE_BASIS[e["bucket"]]]
            print(f"  {e['name']} ({e['position']}, value={e['value']}) - give-up cost: {cost}")
    else:
        print("you could offer: no obvious surplus")
    print()
    if not push["targets"]:
        print("no obvious targets found (no needs, or no Rebuilding team has a sell candidate there)")
        return
    print("buy targets (from Rebuilding teams, at a position you need):")
    for t in push["targets"]:
        trade_note = f"{t['from_owner_trades']} trade(s) made" if t["from_owner_trades"] else "NEVER TRADES - unlikely"
        price_note = BUY_PRICE_NOTE[team_state.VALUE_BASIS[t["bucket"]]]
        print(f"  {t['name']} ({t['position']}, value={t['value']}, {price_note}) from {t['from_owner']} "
              f"- need: {t['need_level']} - {trade_note}")


def _print_report(result: dict) -> None:
    me = result["me"]

    if result["mode"] == "rebuild":
        tank_note = "" if me["owns_next_first"] else " (doesn't own next 1st, so tanking for a pick wouldn't help)"
        print(f"{me['owner']}: Rebuilding{tank_note} - playing for future value, not starting-lineup needs")
        _print_pivot(me, result)
        return

    if result["mode"] == "middling":
        print(f"{me['owner']}: Middling - hasn't committed to a direction, here's both paths")
        print(f"\n-- if pushing (needs: {result['push']['needs'] or 'none'}) --")
        _print_push(result["push"])
        print("\n-- if pivoting --")
        _print_pivot(me, result["pivot"])
        return

    print(f"{me['owner']}: {me['effective_strategy']}, needs: {result['needs'] or 'none'}")
    _print_push(result)


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
