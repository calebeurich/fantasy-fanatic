"""Surface obvious trade fits: a team needing a position looks at Rebuilding teams'
sellable players there (their sell candidates), from owners who've actually made
trades before. This is a discovery tool, not a fairness calculator - it finds *who*
to call, not whether a specific package is fair (see CLAUDE.md/prior discussion on why
a real value calculator is a separate, harder problem: roster construction means bench
depth isn't fungible with a starter's value).

Smoke test: python -m analysis.trade_targets <league_id> <owner_name>
"""

import sys

from sources import sleeper, fantasycalc

from . import team_state, roster_needs, trade_activity
from .team_values import NUM_QBS, pick_equivalent

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

# How much of a lineup player's *current* production a replacement must retain before
# swapping them is worth considering. 90% is deliberately strict: this suggests giving up
# real production for trade value, so the production loss has to be close to noise.
MIN_PRODUCTION_RETAINED = 0.90

# And how much dynasty value the swap has to free up to be worth mentioning at all -
# below this it's churn, not arbitrage.
MIN_VALUE_FREED = 300


def _with_trade_note(entry: dict, other: dict, trade_counts: dict[str, int]) -> dict:
    return {**entry, "from_owner": other["owner"], "from_owner_trades": trade_counts.get(other["owner_id"], 0)}


def _my_offer_pool(me: dict, thresholds: dict[str, float], needs: dict[str, str],
                   projected: set[str] | None = None,
                   pick_values: dict[str, int] | None = None) -> list[dict]:
    """What you could realistically offer: bench value that isn't elite enough to be a
    cornerstone but also isn't part of your actual lineup (e.g. a 3rd QB in a 2-QB-max
    format), plus young surplus - never a valuable *starter*, even a non-cornerstone
    one, since that's not surplus, that's your team. Also never a position you
    yourself have a need at - trading away a WR while WR is your own critical need
    just moves the shortage, it doesn't fix anything. Cheapest give-up cost first."""
    # `projected` (value-derived, from roster_needs.projected_starters) is preferred over
    # each entry's `is_starter`, which comes from Sleeper's current-week snapshot and is
    # meaningless before Week 1. With the snapshot alone, a superflex team's QB2 was
    # being offered away as surplus because the preseason lineup listed only one QB.
    def is_lineup(entry: dict) -> bool:
        return entry["name"] in projected if projected is not None else entry["is_starter"]

    bench_sellable = [e for e in me["sellable"]
                       if not is_lineup(e) and e["position"] not in needs
                       and team_state.clears_relevance_floor(e, thresholds)]
    surplus = [e for e in me["tradeable_surplus"]
               if not is_lineup(e) and e["position"] not in needs
               and team_state.clears_relevance_floor(e, thresholds)]
    offers = bench_sellable + surplus

    # Trade value is not linear in raw value, and presenting it as if it were produced a
    # bad recommendation: a real offer list led with Christian McCaffrey (+1,783 over
    # replacement) and then listed Ollie Gordon (947 raw, but *1,637 below* replacement)
    # as though both were comparable pieces. Value above replacement is scarce and hard
    # to acquire; value below it is replaceable off waivers, so the raw number badly
    # overstates what it fetches in a trade.
    #
    # Depth is *not* worthless, though, and the label says so deliberately: injuries and
    # byes are real, and a cheap backup can spike in value overnight (see the handcuff
    # note under "Known limitations"). It's discounted, not zero - a sweetener that
    # shouldn't anchor an offer, rather than a name to be embarrassed about including.
    for e in offers:
        e["value_over_replacement"] = round(e["value"] - thresholds[e["position"]])
        e["tier"] = ("core piece - above replacement, scarce" if e["value_over_replacement"] > 0
                     else "depth - real but discounted, a sweetener not a centerpiece")
        # A pick equivalent makes the tier concrete. "Worth 947" means nothing to a
        # manager; "about a 2027 3rd (Late)" is immediately legible, and lands on the
        # right intuition - a depth piece is a late-pick-shaped asset, not a centerpiece.
        if pick_values:
            e["pick_equivalent"] = pick_equivalent(e["value"], pick_values)
    offers.sort(key=lambda e: -e["value_over_replacement"])
    return offers


def find_efficiency_swaps(roster_entries: list[dict], projected: set[str]) -> list[dict]:
    """Win-now arbitrage *within* a position: a lineup player whose bench alternative
    produces nearly as much this season for meaningfully less dynasty value. Sell the
    expensive one, start the cheap one, pocket the difference.

    **Pairwise within a position on purpose.** The first attempt at this used an absolute
    threshold on dynasty/redraft ratio and was wrong: the two value scales aren't
    normalized to each other (a real example - McCaffrey is 4,345 dynasty against 6,505
    redraft, while a mid-tier RB runs 2x the other way), so the raw ratio flagged
    26-year-old veterans as "100% future potential". Comparing two players at the same
    position, with values from the same two scales, cancels that distortion out.

    Real case this exists for: a superflex roster's QB2 (C.J. Stroud, 3,288 dynasty /
    2,744 redraft) and QB3 (Sam Darnold, 2,735 / 2,704) produce within 1.5% of each other
    this season, but Stroud costs 553 more in trade value. Ranking by dynasty value alone
    can never see that.
    """
    by_pos: dict[str, list[dict]] = {}
    for e in roster_entries:
        if e.get("redraft_value"):  # no redraft price = no current-production read
            by_pos.setdefault(e["position"], []).append(e)

    swaps = []
    for pos, entries in by_pos.items():
        lineup = [e for e in entries if e["name"] in projected]
        bench = [e for e in entries if e["name"] not in projected]
        for starter in lineup:
            for alt in bench:
                retained = alt["redraft_value"] / starter["redraft_value"]
                freed = starter["value"] - alt["value"]
                if retained >= MIN_PRODUCTION_RETAINED and freed >= MIN_VALUE_FREED:
                    swaps.append({
                        "position": pos,
                        "sell": starter["name"],
                        "start_instead": alt["name"],
                        "production_retained_pct": round(retained * 100),
                        "dynasty_value_freed": round(freed),
                        "note": (
                            f"{alt['name']} produces {round(retained * 100)}% of what "
                            f"{starter['name']} does this season but costs {round(freed)} less "
                            f"in dynasty value - selling {starter['name']} converts future "
                            f"premium into trade capital without losing much now. At this "
                            f"margin neither is clearly the better start week to week, so "
                            f"this is a value decision, not a lineup upgrade"
                        ),
                    })
    swaps.sort(key=lambda s: -s["dynasty_value_freed"])
    return swaps


def _buy_path(me: dict, states: list[dict], needs_by_owner_id: dict, thresholds: dict[str, float],
              trade_counts: dict[str, int], max_per_position: int,
              projected: set[str] | None = None,
              pick_values: dict[str, int] | None = None) -> dict:
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
        # Window fit before raw value. A Win-Now buyer wants *current production*, and
        # this project's own pricing model says declining players are "production-priced"
        # while prime ones are "upside-priced, may cost more than the fit justifies" -
        # their value bakes in future growth a win-now team isn't buying. Sorting only by
        # (trade activity, value) contradicted that: a real Win-Now team was handed six
        # buy targets, every one of them prime, and none of the cheaper production it
        # actually needed. Rebuilding/Middling buyers keep the old ordering, since they
        # have no reason to prefer aging players.
        prefer_production = me["effective_strategy"] == "Win-Now"
        pos_targets.sort(key=lambda t: (
            0 if (prefer_production and t["bucket"] == "declining") else 1,
            -t["from_owner_trades"],
            -t["value"],
        ))
        targets += pos_targets[:max_per_position]

    result = {"needs": my_needs, "targets": targets,
              "my_offers": _my_offer_pool(me, thresholds, my_needs, projected, pick_values)}
    # Only meaningful for a team actually trying to win now - a rebuilding team wants
    # the future premium it would be selling.
    if me["effective_strategy"] == "Win-Now" and projected:
        swaps = find_efficiency_swaps(me["sellable"] + me["tradeable_surplus"], projected)
        if swaps:
            result["efficiency_swaps"] = swaps
    return result


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
    projected_by_owner = roster_needs.league_projected_starters(league_id)
    fmt = sleeper.describe_format(sleeper.get_league(league_id))
    pick_values = fantasycalc.get_pick_values(NUM_QBS[fmt['is_superflex']], fmt['num_teams'],
                                              fmt['ppr'], fmt['is_dynasty'])

    me = next((r for r in states if owner_query.lower() in r["owner"].lower()), None)
    if me is None:
        raise ValueError(f"no owner matching '{owner_query}' - options: {[r['owner'] for r in states]}")

    # A rebuilding team (especially one tanking for a pick) isn't trying to fill
    # starting-lineup needs with proven vets - it wants to sell what age value it has
    # left and stockpile youth/picks instead. Buy-target-by-need only makes sense for
    # a team actually trying to win now.
    projected = projected_by_owner.get(me["owner_id"], set())

    if me["effective_strategy"] == "Rebuilding":
        return {"me": me, "mode": "rebuild", **_pivot_path(me, states, thresholds, trade_counts)}

    if me["effective_strategy"] == "Middling":
        # Hasn't committed to a direction - show what pushing looks like AND what
        # pivoting looks like, rather than silently picking one. Whichever path
        # actually makes sense usually depends on something we don't have yet (the
        # season record - a Middling team two games out of a playoff spot should push,
        # one that's clearly out should pivot even mid-season) - logged in LOGIC.md.
        return {"me": me, "mode": "middling",
                "push": _buy_path(me, states, needs_by_owner_id, thresholds, trade_counts,
                                  max_per_position, projected, pick_values),
                "pivot": _pivot_path(me, states, thresholds, trade_counts)}

    return {"me": me, "mode": "buy",
            **_buy_path(me, states, needs_by_owner_id, thresholds, trade_counts,
                        max_per_position, projected, pick_values)}


SWAP_ELIGIBLE_STRATEGIES = ("Win-Now", "Middling")


def find_mutual_swaps(league_id: str, owner_query: str) -> dict:
    """Two-way trades between teams both still trying to win: each side has a
    positional surplus (real spare depth, from roster_needs.league_surplus) that
    happens to be the other side's need, so both teams fix a hole without touching
    their own starters or their long-term core. The buy/pivot paths above only ever
    match a Win-Now/Middling team against a Rebuilding team's sell candidates - they
    never consider two competing teams trading with each other, which misses a common
    and realistic trade shape a pure rebuild-vs-contend model can't produce."""
    states = team_state.classify_league(league_id)
    needs_by_owner = roster_needs.league_needs(league_id)
    surplus_by_owner = roster_needs.league_surplus(league_id)

    me = next((r for r in states if owner_query.lower() in r["owner"].lower()), None)
    if me is None:
        raise ValueError(f"no owner matching '{owner_query}' - options: {[r['owner'] for r in states]}")

    if me["effective_strategy"] not in SWAP_ELIGIBLE_STRATEGIES:
        # A Rebuilding team isn't trying to fix a starting lineup right now - it's
        # selling current value for youth, which is the pivot path above, not this.
        return {"me": me, "swaps": []}

    my_needs = needs_by_owner.get(me["owner_id"], {})
    my_surplus = surplus_by_owner.get(me["owner_id"], {})

    swaps = []
    for other in states:
        if other["owner_id"] == me["owner_id"] or other["effective_strategy"] not in SWAP_ELIGIBLE_STRATEGIES:
            continue
        other_needs = needs_by_owner.get(other["owner_id"], {})
        other_surplus = surplus_by_owner.get(other["owner_id"], {})
        for need_pos, other_surplus_entries in other_surplus.items():
            if need_pos not in my_needs:
                continue
            for their_need_pos, my_surplus_entries in my_surplus.items():
                if their_need_pos in other_needs:
                    swaps.append({
                        "with_owner": other["owner"],
                        "fills_your_need_at": need_pos,
                        "you_receive": other_surplus_entries,
                        "fills_their_need_at": their_need_pos,
                        "you_send": my_surplus_entries,
                    })
    return {"me": me, "swaps": swaps}


def offerable_names(result: dict) -> set[str]:
    """Every player name this team could reasonably be told to trade away, across
    whichever path(s) find_targets returned for its mode. Single source of truth for
    "is this a real give-up piece" - used by agent.py's post-hoc grounding check so
    that check never has to re-derive the mode-specific logic above itself."""
    if result["mode"] == "rebuild":
        return {e["name"] for e in result["sell_candidates"] + result["situational"]}
    if result["mode"] == "middling":
        return ({e["name"] for e in result["push"]["my_offers"]}
                | {e["name"] for e in result["pivot"]["sell_candidates"] + result["pivot"]["situational"]})
    return {e["name"] for e in result["my_offers"]}


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


def _print_swaps(swaps: list[dict]) -> None:
    if not swaps:
        print("no mutual swap fits found")
        return
    print("mutual swaps (both sides fix a different need, no core piece touched):")
    for s in swaps:
        receive = ", ".join(e["name"] for e in s["you_receive"])
        send = ", ".join(e["name"] for e in s["you_send"])
        print(f"  with {s['with_owner']}: you get {receive} ({s['fills_your_need_at']} need) "
              f"for {send} ({s['fills_their_need_at']} need for them)")


def main(league_id: str, owner_query: str = None, max_per_position: int = DEFAULT_MAX_PER_POSITION) -> None:
    if owner_query:
        result = find_targets(league_id, owner_query, max_per_position)
        _print_report(result)
        if result["mode"] in ("buy", "middling"):
            print()
            _print_swaps(find_mutual_swaps(league_id, owner_query)["swaps"])
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
