"""The CLI rendering of a find_targets result. Everything computed must print here -
`audit.check_everything_computed_is_printed` renders through `_print_report` and checks,
because six blocks have shipped attached-but-invisible. Note the check cannot catch an
early `return` that skips a populated block; the picks block was once lost that way.
"""

from .. import team_state
from ..team_values import MIN_MEANINGFUL_RUNWAY, ordinal
from .upgrades import UPGRADE_KIND_TAG

# The VALUE_BASIS classification (team_state.value_basis) phrased for each side of a trade.
# "mixed" must not read as "upside-priced" - that word belongs to the upside basis, and
# calling a prime receiver upside-priced overclaimed the classification (the owner: "I
# don't think Smith and Waddle are necessarily upside priced").
BUY_PRICE_NOTE = {
    "production": "production-priced",
    "mixed": "priced on production and remaining years both - some future in the cost",
    "upside": "mostly future value - likely a real overpay for current-year fit",
}
OFFER_GIVE_UP_COST = {
    "production": "low - value is mostly already-realized production",
    "mixed": "moderate - some future value baked in too",
    "upside": "high - real future value you won't get back",
}


def _print_pivot(me: dict, pivot: dict) -> None:
    def names(entries: list[dict]) -> str:
        # A cornerstone's friction is the whole reason he is listed, so it prints inline.
        return ", ".join(e["name"] + (" [CORNERSTONE]" if e.get("friction") else "")
                         for e in entries) or "none"

    def buyers(entries: list[dict]) -> None:
        for e in entries:
            if e.get("wanted_by"):
                print(f"      {e['name']} wanted by: {e['wanted_by']}")

    print(f"sell candidates (under {MIN_MEANINGFUL_RUNWAY:g} years before decline): "
          f"{names(pivot['sell_candidates'])}")
    print(f"  {pivot['sell_clock_note']}")
    buyers(pivot["sell_candidates"])
    for entry in pivot["sell_candidates"]:
        if entry.get("price_note"):
            print(f"  {entry['name']}: {entry['price_note']}")
    print(f"situational sells: {names(pivot['situational'])}")
    print(f"  {pivot['situational_note']}")
    buyers(pivot["situational"])
    for entry in pivot["sell_candidates"] + pivot["situational"]:
        if entry.get("runway_inversion"):
            print(f"  {entry['runway_inversion']}")
    # Said once, not once per cornerstone.
    cornerstones = [e for e in pivot["sell_candidates"] + pivot["situational"] if e.get("friction")]
    if cornerstones:
        print(f"  {cornerstones[0]['friction'][0]['why']}")
    # Picks print BEFORE the empty-players guard - an early return once took them down
    # with it, hiding the cleaner currency exactly when it was the whole plan.
    if pivot.get("picks_to_acquire"):
        print("picks to ask about (worth less to a contender than to you):")
        for t in pivot["picks_to_acquire"][:8]:
            trade_note = f"{t['from_owner_trades']} trade(s)" if t["from_owner_trades"] else "NEVER TRADES"
            print(f"  {t['pick']} (value={t['value']}) from {t['from_owner']} - {trade_note}")
        print(f"  {pivot['picks_note']}")
    if not pivot["acquire_targets"]:
        print("no reachable young players found - which makes the picks above the whole plan, "
              "not a consolation")
        return
    print("acquire targets (cleanest first, capped per position):")
    for t in pivot["acquire_targets"]:
        trade_note = f"{t['from_owner_trades']} trade(s) made" if t["from_owner_trades"] else "NEVER TRADES - unlikely"
        print(f"  {t['name']} ({t['position']}, value={t['value']}) from {t['from_owner']} "
              f"[{t['seller_state']}] - {trade_note}")
        for f in t["friction"]:
            print(f"      - [{f['flavor']}] {f['why']}")
    print(f"  {pivot['acquire_note']}")


def _needs_summary(needs: dict) -> str:
    return ", ".join(f"{pos} ({e['level']}, {e['rank']}/{e['of']})" for pos, e in needs.items()) or "none"


def _print_push(push: dict, extras: dict) -> None:
    """`extras` is the top-level result, which is where `depth_adds` lives - for Middling,
    `push` is only the pushing half and doesn't carry it."""
    for pos, entry in push["needs"].items():
        print(f"  need at {pos}: {entry['note']}")
    _print_stranded(extras)
    if push["my_offers"]:
        print("you could offer, ONE AT A TIME - not as a package (cleanest first, "
              "anything with friction is listed last, with why):")
        if push.get("my_offers_note"):
            print(f"  {push['my_offers_note']}")
        for e in push["my_offers"]:
            cost = OFFER_GIVE_UP_COST[team_state.value_basis(e)]
            flavors = (" [" + ", ".join(f["flavor"] for f in e["friction"]) + "]"
                       if e["friction"] else "")
            print(f"  {e['name']} ({e['position']}, value={e['value']}, "
                  f"{e['value_over_replacement']:+} vs replacement) - "
                  f"give-up cost: {cost}{flavors}")
            if e.get("trade_off"):
                print(f"      {e['trade_off']}")
            for f in e["friction"]:
                print(f"      - {f['why']}")
    else:
        print("you could offer: no obvious surplus")
    if push.get("picks_to_trade_away"):
        picks = ", ".join(f"{p['pick']} ({p['value']})" for p in push["picks_to_trade_away"][:4])
        print(f"picks to pay with (currency for buying production, not production itself): {picks}")
    print()
    # Cheapest and most gettable first, escalating to the long shots.
    _print_value_upgrades(extras)
    _print_depth(extras)
    if push["targets"]:
        print("BUY TARGETS - ring these first (from owners who are selling THIS player, at a "
              "position you need):")
        for t in push["targets"]:
            trade_note = f"{t['from_owner_trades']} trade(s) made" if t["from_owner_trades"] else "no trades yet"
            price_note = BUY_PRICE_NOTE[team_state.value_basis(t)]
            ow = t.get("over_weakest_starter")
            beats = "" if ow is None else f", {ow:+,} vs your weakest {t['position']} starter"
            # A body at a count-shaped need still fills a slot - label, don't hide.
            kind = " [DEPTH - does not beat who you start there]" if (ow is not None and ow <= 0) else ""
            print(f"  {t['name']} ({t['position']}, value={t['value']}, {price_note}{beats}) from "
                  f"{t['from_owner']} [{t['sells_because']}] - need: {t['need_level']} - "
                  f"{trade_note}{kind}")
    else:
        why = ("everything available is a long shot, listed below" if push.get("long_shots")
               else "no need for the buy path to fill in the first place")
        print(f"no reachable targets found ({why})")
    if push.get("long_shots"):
        print("\nlong shots (real fits, but something is in the way - see why on each):")
        for t in push["long_shots"]:
            price_note = BUY_PRICE_NOTE[team_state.value_basis(t)]
            beats = ("" if t.get("over_weakest_starter") is None else
                     f", {t['over_weakest_starter']:+,} vs your weakest {t['position']} starter")
            print(f"  {t['name']} ({t['position']}, value={t['value']}, {price_note}{beats}) from "
                  f"{t['from_owner']} - need: {t['need_level']}")
            for f in t["friction"]:
                print(f"      - [{f['flavor']}] {f['why']}")
        print(f"  {push['long_shot_note']}")
    if push.get("persuasion_targets"):
        print()
        # Nearly-fits first, which is also the order to make the calls in.
        print("harder asks (aging production on teams that are NOT shopping it - most are still "
              "a fit for both sides; PIVOT needs a change of direction, HOLDS TO WIN needs an "
              "overwhelming offer):")
        for t in sorted(push["persuasion_targets"], key=lambda t: t["needs_a_pivot"]):
            marker = ""
            if t["needs_a_pivot"]:
                marker = (" [HOLDS TO WIN]" if t.get("seller_window") in ("Push", "Contend")
                          else " [PIVOT]")
            print(f"  {t['name']} ({t['position']}, {t['production_per_cost']}x production "
                  f"per unit of cost - dyn {t['value']:,} / redraft {t['redraft_value']:,}) "
                  f"from {t['from_owner']}{marker}")
            print(f"      why they might listen: {t['why_they_might_listen']}")
            if t.get("offer_any_one_of"):
                print(f"      they'd be interested in any ONE of (not a bundle): "
                      f"{', '.join(t['offer_any_one_of'])}")
            if t.get("cost_note"):
                print(f"      what it costs: {t['cost_note']}")
            for f in t.get("friction") or []:
                # needs_a_pivot / holds_to_win are already said by cost_note above.
                if f["flavor"] not in ("needs_a_pivot", "holds_to_win"):
                    print(f"      - [{f['flavor']}] {f['why']}")
        print(f"  {push['persuasion_note']}")


def _print_report(result: dict) -> None:
    me = result["me"]

    if result["mode"] == "rebuild":
        tank_note = "" if me["owns_next_first"] else " (doesn't own next 1st, so tanking for a pick wouldn't help)"
        print(f"{me['owner']}: Rebuilding{tank_note} - playing for future value, not starting-lineup needs")
        _print_stranded(result)
        _print_pivot(me, result)
        _print_depth(result)
    elif result["mode"] == "middling":
        print(f"{me['owner']}: {me['state']} ({me['flavor']}) - both directions are open")
        print(f"  {me['window_note']}")
        print(f"\n  {result['timing_note']}")
        print(f"\n-- if pushing (needs: {_needs_summary(result['push']['needs'])}) --")
        _print_push(result["push"], result)
        print("\n-- if pivoting --")
        _print_pivot(me, result["pivot"])
    else:
        print(f"{me['owner']}: {me['state']} ({me['flavor']}), needs: {_needs_summary(result['needs'])}")
        print(f"  {me['window_note']}")
        _print_push(result, result)
        _print_conversion_candidates(result)


def _print_value_upgrades(result: dict) -> None:
    if not result.get("value_upgrades"):
        return
    print("\nbetter things to own than what you start now (more or equal production, less "
          "value tied up):")
    for m in result["value_upgrades"]:
        # Rank on each scale, not the raw ratio - see `team_values.priced_for`. A relative
        # measure prints a relative claim.
        pf = m.get("priced_for")
        if not pf:
            pricing = "no redraft price, so how he is priced cannot be read"
        elif pf["priced_for"] == "later":
            pricing = (f"UPSIDE-PRICED - {ordinal(pf['dynasty_rank'])} at {m['position']} in "
                       f"dynasty against {ordinal(pf['redraft_rank'])} in redraft, so moving him "
                       f"sells future years you are not playing for")
        elif pf["priced_for"] == "now":
            pricing = (f"already now-priced - {ordinal(pf['dynasty_rank'])} in dynasty against "
                       f"{ordinal(pf['redraft_rank'])} in redraft, so his price is this season")
        else:
            pricing = (f"priced like the rest of his position ({ordinal(pf['dynasty_rank'])} of "
                       f"{pf['of']} at {m['position']} in dynasty against "
                       f"{ordinal(pf['redraft_rank'])} in redraft) - he may still be young, this "
                       f"says only that the market is not paying him a premium other "
                       f"{m['position']}s don't get")
        print(f"  move off {m['move_off']} ({m['position']}, {m['value']:,} dynasty / "
              f"{m['redraft_value']:,} this season - {pricing}):")
        if m["wanted_by"]:
            print(f"      who would want him: {m['wanted_by']}")
        else:
            print("      who would want him: nobody obvious - no team is short here and "
                  "nobody's roster is crying out for this profile")
        for u in m["returns"]:
            if u.get("already_mine"):
                trades = "NO TRADE NEEDED - promote him and sell the man above"
            else:
                trades = (f"{u['from_owner_trades']} trade(s)" if u["from_owner_trades"]
                          else "NEVER TRADES")
            price = (f"{u['value_freed']:,} dynasty freed" if u["value_freed"] > 0
                     else "same dynasty price")
            print(f"      <- {u['name']} ({u['redraft_value']:,} this season, "
                  f"{u['production_gained']:+,} production, {price})"
                  f"{UPGRADE_KIND_TAG[u['kind']]} from {u['from_owner']} - {trades}")
            if u.get("their_reason"):
                print(f"           why they'd move him: {u['their_reason']}")
            for f in u.get("friction") or []:
                if f["flavor"] != "never_trades":   # the header line already shouts it
                    print(f"           - [{f['flavor']}] {f['why']}")
    print(f"  {result['value_upgrade_note']}")


def _print_conversion_candidates(result: dict) -> None:
    if not result.get("conversion_candidates"):
        return
    print("\nWHAT THEY WILL CALL YOU ABOUT - your own aging starters, priced for a season your "
          "roster is least short of:")
    for e in result["conversion_candidates"]:
        print(f"  {e['name']} ({e['position']}, {e['production_per_cost']}x production per unit "
              f"of cost - dyn {e['value']:,} / redraft {e['redraft_value']:,})")
        print(f"      {e['note']}")
    print(f"  {result['choice_note']}")


def _print_stranded(result: dict) -> None:
    if not result.get("stranded"):
        return
    print("STRANDED - the most valuable thing you own that you cannot use:")
    for e in result["stranded"]:
        margin = (f"{e['times_weakest']}x what your weakest starter produces"
                  if e.get("times_weakest") else "production your weakest starter has none of")
        print(f"  {e['name']} ({e['position']}, {e['redraft_value'] or 0:,} this season, "
              f"{e['value']:,} dynasty) - {margin}, and every {e['blocked_by']}-capable slot "
              f"is held by someone better"
              + (f"; wanted by {e['wanted_by']}" if e.get("wanted_by") else ""))
    print(f"  {result['stranded_note']}")


def _print_depth(result: dict) -> None:
    if not result.get("depth_adds"):
        return
    print("\ncheap depth (cheapest first - nominal price, never worth a real asset):")
    for a in result["depth_adds"]:
        edge = a.get("over_weakest_starter")
        edge_note = ("" if edge is None else
                     f", {edge:+,} vs your weakest {a['position']} starter")
        flavors = (" [" + ", ".join(f["flavor"] for f in a["friction"]) + "]"
                   if a.get("friction") else "")
        print(f"  {a['name']} ({a['position']}, value={a['value']}, "
              f"{a['redraft_value'] or 0:,} this season{edge_note}) from "
              f"{a['from_owner']}{flavors}")
        for f in a.get("friction") or []:
            print(f"      - {f['why']}")
    print(f"  {result['depth_note']}")
