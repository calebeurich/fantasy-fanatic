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

# Neither side of a suggested trade should be roster filler indistinguishable from the
# waiver wire. Declining/prime "sellable" value needs to clear more of replacement
# level (it's being offered as real current production) than ascending "surplus" value
# does (its appeal is future upside, which is legitimately priced lower right now) -
# both floors were calibrated against real examples of value too low to be worth listing.
MIN_SELLABLE_RELEVANCE_FRACTION = 0.5
MIN_SURPLUS_RELEVANCE_FRACTION = 0.25


def _with_trade_note(entry: dict, other: dict, trade_counts: dict[str, int]) -> dict:
    return {**entry, "from_owner": other["owner"], "from_owner_trades": trade_counts.get(other["owner_id"], 0)}


def _clears_floor(entry: dict, thresholds: dict[str, float], fraction: float) -> bool:
    return entry["value"] >= thresholds[entry["position"]] * fraction


def _my_offer_pool(me: dict, thresholds: dict[str, float]) -> list[dict]:
    """What you could realistically offer: young surplus, plus bench value that isn't
    elite enough to be a cornerstone but also isn't part of your actual lineup (e.g. a
    3rd QB in a 2-QB-max format) - never a valuable *starter*, even a non-cornerstone
    one, since that's not surplus, that's your team."""
    surplus = [e for e in me["tradeable_surplus"] if _clears_floor(e, thresholds, MIN_SURPLUS_RELEVANCE_FRACTION)]
    bench_sellable = [e for e in me["sellable"]
                       if not e["is_starter"] and _clears_floor(e, thresholds, MIN_SELLABLE_RELEVANCE_FRACTION)]
    return surplus + bench_sellable


def find_targets(league_id: str, owner_query: str) -> dict:
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
        sell_candidates = [e for e in me["sellable"] if _clears_floor(e, thresholds, MIN_SELLABLE_RELEVANCE_FRACTION)]
        acquire_targets = []
        for other in states:
            if other["owner_id"] == me["owner_id"] or other["effective_strategy"] not in ("Win-Now", "Middling"):
                continue
            for player in other["tradeable_surplus"]:
                if not _clears_floor(player, thresholds, MIN_SURPLUS_RELEVANCE_FRACTION):
                    continue
                acquire_targets.append(_with_trade_note(player, other, trade_counts))
        acquire_targets.sort(key=lambda t: (-t["from_owner_trades"], -t["value"]))
        return {"me": me, "mode": "rebuild", "sell_candidates": sell_candidates, "acquire_targets": acquire_targets}

    my_needs = needs_by_owner_id.get(me["owner_id"], {})
    targets = []
    for pos in my_needs:
        for other in states:
            if other["owner_id"] == me["owner_id"] or other["effective_strategy"] != "Rebuilding":
                continue
            for player in other["sellable"]:
                if player["position"] != pos or not _clears_floor(player, thresholds, MIN_SELLABLE_RELEVANCE_FRACTION):
                    continue
                targets.append({"position": pos, "need_level": my_needs[pos],
                                 **_with_trade_note(player, other, trade_counts)})
    targets.sort(key=lambda t: (-t["from_owner_trades"], -t["value"]))

    return {"me": me, "mode": "buy", "needs": my_needs, "targets": targets,
            "my_offers": _my_offer_pool(me, thresholds)}


def main(league_id: str, owner_query: str) -> None:
    result = find_targets(league_id, owner_query)
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
    offers = ", ".join(e["name"] for e in result["my_offers"]) or "no obvious surplus"
    print(f"you could offer: {offers}")
    print()

    if not result["targets"]:
        print("no obvious targets found (no needs, or no Rebuilding team has a sell candidate there)")
        return

    print("buy targets (from Rebuilding teams, at a position you need):")
    for t in result["targets"]:
        trade_note = f"{t['from_owner_trades']} trade(s) made" if t["from_owner_trades"] else "NEVER TRADES - unlikely"
        # Declining = you're paying mostly for this year's production. Prime and
        # (more so) ascending bake in future growth a win-now buyer doesn't need, so
        # they cost more per unit of current-year fit than the raw value suggests -
        # the earlier in the age curve, the larger that gap gets.
        if t["bucket"] == "declining":
            price_note = "production-priced"
        elif t["bucket"] == "prime":
            price_note = "upside-priced, may cost more than the fit justifies"
        else:
            price_note = "mostly future value - likely a real overpay for current-year fit"
        print(f"  {t['name']} ({t['position']}, value={t['value']}, {price_note}) from {t['from_owner']} "
              f"- need: {t['need_level']} - {trade_note}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
