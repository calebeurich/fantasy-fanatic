"""The buy side: what this team can offer, who to ring for each need, and the cheap
depth underneath it all. History: LOGIC.md, "Trade target matching" and "Depth as a
third state".
"""

from .. import team_state, roster_needs
from ..team_values import age_bucket, pick_equivalent, years_to_decline
from .board import (Board, CORNERSTONE_ASK, MIGHT_SELL, NOISE_RETAINED, _best_chip,
                    _rental,
                    _buy_friction, _friction, _others, _sells_him, _with_trade_note)
from .counterparty import PERSUASION_NOTE, _counterparty_fit, _persuasion_targets

LONG_SHOT_NOTE = (
    "LONG SHOTS - real fits, but something structural is in the way, and `friction` says what. "
    "These are separated from the buy list rather than ranked below it because the reason is "
    "not price: an owner who has never traded may not answer, a cornerstone's owner will answer "
    "and say no, and a player costing more than your biggest single chip cannot be reached "
    "one-for-one at all. Ring the buy list first. Raise one of these only when you already "
    "know something this tool doesn't - that the owner is about to sell, or that you are "
    "willing to open a negotiation with no single piece that covers it."
)

# The list of what this team can give up shipped with no note at all, and a bare list of
# names invites the one construction this project forbids: a live answer bundled two of
# them ("Fannin 3,650 + Shough 3,379") against one 4,473 target, which is precisely the
# additive-value error the tools refuse to make.
MY_OFFERS_NOTE = (
    "WHAT THIS TEAM CAN GIVE UP, one piece at a time. Every name here is offerable on its "
    "own; the list is ALTERNATIVES, not a package, and nothing in this result prices a "
    "bundle. Dynasty value does not add across players - two 3,500s are not a 7,000, because "
    "the receiving lineup can only start so many - so pairing two of these into one offer is "
    "not a bigger version of the same trade, it is a claim no tool here can support. Say "
    "which single piece goes for which single piece. `give_up_cost` says what each one "
    "actually costs YOU: production already realized (cheap to move) versus future years you "
    "won't get back (expensive), which is a different question from what he fetches."
)

TARGETS_NOTE = (
    "WHO TO RING FIRST - players whose owner is already selling this kind of piece, at a "
    "position this team is short of. `offer_any_one_of`, where present, is what THAT owner "
    "would have interest in receiving, filtered to his timeline as well as his lineup: a "
    "team accumulating youth is never offered a piece inside its final year, because that "
    "is exactly what he is trying to move. It is a starting point for the conversation, "
    "NOT a priced or fair-value offer - nothing here says these two pieces are worth the "
    "same, only that each side wants what the other has. Name one piece, let him counter."
)

DEPTH_NOTE = (
    "DEPTH, NOT NEEDS. Each of these is a player who does not crack this lineup today but "
    "would step straight into it if one starter at his position were out - which byes "
    "guarantee and injuries make likely. They are listed because every one of them sits "
    "BELOW replacement level - startable quality - meaning they are cheap by definition and "
    "not who the buy targets above are for. Price them as sweeteners: worth a late pick or a "
    "spare body, never worth a real asset. But below LEAGUE replacement is not the same as "
    "unable to help THIS lineup - each line says whether the market prices the player over your "
    "weakest starter at the position or only covers an absence, and on a thin roster several "
    "will. Cheapest first, because at this tier price is the entire point."
)

# Same cheap bodies, a different reason to want them: a rebuilder is not protecting a
# lineup, it is holding assets that become sellable when a role opens.
DEPTH_NOTE_REBUILD = (
    "LOTTERY TICKETS, NOT INSURANCE. Each of these would start for this team if a player "
    "above him were out - but this team is not protecting a lineup, so that is not the "
    "point. The point is that a body who inherits a starting role becomes a genuinely "
    "sellable asset, and at this price he costs a late pick to hold. Cheap upside on a "
    "roster whose whole plan is accumulating it. Still never worth a real asset."
)

DEPTH_LIMIT = 6


def _my_offer_pool(me: dict, board: Board, needs: dict[str, dict],
                   covered: dict[str, float] | None = None,
                   backfills: dict[str, dict] | None = None) -> list[dict]:
    """What you could realistically offer, most value over replacement first, friction
    last. In: bench value, young surplus, any starter the bench covers for free
    (`covered == 0`), and - for a Push team only - ascending starters, whose value is the
    future a closing window exists to spend. Out: STARTERS at a position this team itself
    needs (trading one just moves the shortage - bench surplus there is still an offer:
    rjl's QB3 Stroud behind Mayfield/Darnold at a weak QB room costs the lineup nothing
    and is exactly what a QB-critical seller wants; owner, 2026-08-17), and
    prime/declining starters, who ARE the current production. A starter's `lineup_cost`
    is stated, never used as a veto."""
    thresholds, pick_values = board.thresholds, board.pick_values
    covered = covered or {}
    backfills = backfills or {}

    def offerable(e):
        if not e["is_starter"] or e.get("is_cornerstone"):
            return True
        # A contender's ascending, non-cornerstone starter is future-priced value - the
        # currency a contender spends (owner: "KB would trade BTJ, a non-cornerstone
        # ascending asset buttboi wants, and refill the spot with someone contending-priced
        # like Evans"). Was Push-only; any contender qualifies. lineup_cost still says
        # what the slot costs to refill.
        return covered.get(e["name"]) == 0 or (
            me.get("window") in ("Push", "Contend") and e["bucket"] == "ascending")

    # The junk tier is cut (owner: "I don't think anyone cares about the sell list of
    # Nailor, Washington or Dulcich at all"): an offer must be market-relevant in at
    # least ONE currency - above the position's trade-value replacement, or carrying
    # startable production (the Tony Pollard depth shape: cheap in dynasty, real this
    # season). Below both bars is waiver fodder, and listing it wastes the reader.
    start_bars = getattr(board.ctx, "start_thresholds", None)

    def worth_offering(e):
        if not start_bars:  # minimal test boards carry no lineup context
            return True
        return (e["value"] > thresholds[e["position"]]
                or (e.get("redraft_value") or 0) >= start_bars.get(e["position"], 0))

    offers = [{**e, "lineup_cost": round(covered[e["name"]], 1)} if e["name"] in covered else e
              for e in me["sellable"] + me["tradeable_surplus"]
              if offerable(e) and (e["position"] not in needs or not e["is_starter"]
                                   or (me.get("window") in ("Push", "Contend") and e["bucket"] == "ascending"))
              and team_state.clears_relevance_floor(e, thresholds)
              and worth_offering(e)]

    for e in offers:
        # The same friction vocabulary the buy side uses, read from my side of the table.
        friction = []
        if e.get("is_cornerstone"):
            friction.append(_friction("cornerstone", CORNERSTONE_ASK))
        # A cost is only friction if the lineup actually notices - the same "barely moves"
        # band as everywhere else, rather than a new threshold.
        produced = me.get("starting_production") or 0
        cost_now = e.get("lineup_cost") or 0
        notices = ((produced - cost_now) / produced < NOISE_RETAINED
                   if produced else bool(cost_now))
        if cost_now and notices:
            share = (f" - {round(100 * cost_now / produced)}% of what it scores now"
                     if produced else "")
            friction.append(_friction("costs_you_production",
                                      f"moving him costs {cost_now:.1f} points a game out of your "
                                      f"own lineup{share}, after it refills itself"))
        e["friction"] = friction
        # The trade-off in one line, in both currencies - naming the replacement is what
        # turns the cost from an arbitrary number into the argument.
        if backfills.get(e["name"]):
            bf = backfills[e["name"]]
            e["backfill"] = bf
            e["trade_off"] = (f"frees {e['value']:,} of dynasty value for "
                              f"{cost_now:.1f} points a game, because {bf['name']} "
                              f"({bf['position']}, {bf['redraft_value']:,}) steps in")
        # Trade value is not linear in raw value: above replacement is scarce, below it is
        # replaceable off waivers - discounted, not zero.
        e["value_over_replacement"] = round(e["value"] - thresholds[e["position"]])
        e["tier"] = ("core piece - above replacement, scarce" if e["value_over_replacement"] > 0
                     else "depth - real but discounted, a sweetener not a centerpiece")
        # "About a 2027 3rd" is legible where "worth 947" is not.
        if pick_values:
            e["pick_equivalent"] = pick_equivalent(e["value"], pick_values)
    # Friction decides order, then value over replacement - "who is biggest" is not "who
    # should I move".
    offers.sort(key=lambda e: (bool(e["friction"]), -e["value_over_replacement"]))
    return offers


def _buy_path(me: dict, board: Board, max_per_position: int,
              my_picks: list[dict] | None = None,
              covered: dict[str, float] | None = None,
              backfills: dict[str, dict] | None = None) -> dict:
    """The push case: fill needs with sellable value, worst-shaped need first. Targets are
    ranked on the metric the window is buying (production for Push, value otherwise; age
    only breaks ties; trade activity is a last-resort tiebreak, never a ranking), and split
    into reachable `targets` and blocked `long_shots` so attainability never competes with
    quality for one ordering."""
    states, thresholds, trade_counts = board.states, board.thresholds, board.trade_counts
    my_needs = board.needs_by_owner_id.get(me["owner_id"], {})
    ordered_positions = sorted(
        my_needs, key=lambda p: roster_needs.NEED_PRIORITY[my_needs[p]["level"]])

    my_pool = _my_offer_pool(me, board, my_needs, covered, backfills)
    best_chip = _best_chip(my_pool)
    others_have_traded = board.others_have_traded(me["owner_id"])

    targets, long_shots = [], []
    for pos in ordered_positions:
        need = my_needs[pos]
        # A `weak` position has its slots filled - anyone who wouldn't displace the worst
        # starter is not a fix. Count-shaped needs have an empty slot, so any relevant
        # body helps (and a player without a redraft price isn't excluded for lacking one).
        # The FLEX is the exception both ways: its occupant is a real (bad) player, so the
        # displacement bar always applies - and ANY eligible position clears it, which is
        # how a team reading ok at RB still gets shown an RB (the flex is an open upgrade
        # slot). Positions that are needs in their own right are skipped here: their
        # candidates already appear under the real need.
        if pos == "FLEX":
            wanted = tuple(p for p in need["eligible"] if p not in my_needs)
            upgrade_bar = need["weakest_starter"]
        else:
            wanted = (pos,)
            upgrade_bar = need["weakest_starter"] if need["level"] == "weak" else None
        pos_targets = []
        for other in _others(states, me, MIGHT_SELL):
            for player in other["sellable"]:
                if player["position"] not in wanted or not team_state.clears_relevance_floor(player, thresholds):
                    continue
                if not _sells_him(other, player):
                    continue
                if upgrade_bar is not None and (player.get("redraft_value") or 0) <= upgrade_bar:
                    continue
                ratio = ((player.get("redraft_value") or 0) / player["value"]
                         if player.get("value") else None)
                # What he beats YOUR weakest starter by - the bar the asking manager
                # actually has, not league replacement level.
                over_weakest = ((player.get("redraft_value") or 0) - need["weakest_starter"]
                                if need.get("weakest_starter") is not None else None)
                # `need_level` rides, the need's full note does NOT: it is the asker's own
                # need, identical on every target at the position, and ships once in
                # result["needs"] - it was 14% of a live buy payload at 90% duplication.
                # WHAT TO SEND BACK, computed per counterparty - the half of the trade
                # this block used to leave out. Buy targets shipped "here is who to ring"
                # beside a separate list of everything this team could give up, with no
                # join, so the pairing was left to the reader: a live answer offered a
                # 0.3-runway 28-year-old to a 55%-ascending team. `_counterparty_fit` is
                # the same test the persuasion tier already used; it just never ran here.
                fit = _counterparty_fit(
                    other, board.needs_by_owner_id.get(other["owner_id"], {}), my_pool,
                    target=player) or {}
                pos_targets.append({"position": pos, "for_slot": pos,
                                     "need_level": need["level"],
                                     "over_weakest_starter": over_weakest,
                                     "sells_because": ("rebuilding" if other["window"] == "Rebuild"
                                                       else "rising, so selling age not youth"),
                                     "season_price_per_cost": round(ratio, 2) if ratio else None,
                                     **{k: v for k, v in fit.items() if k != "fills_a_hole"},
                                     **_buy_friction(player, other, best_chip,
                                                     trade_counts.get(other["owner_id"], 0),
                                                     others_have_traded),
                                     **_with_trade_note(player, other, trade_counts)})
        # Rank on what the window is buying. A Push team wants production, and among equal
        # production the cheaper, shorter (declining) asset; everyone else has no reason
        # to prefer aging players, so value leads. Activity is a tiebreak only.
        prefer_production = me["window"] == "Push"
        pos_targets.sort(key=lambda t: (
            -(t.get("redraft_value") or 0) if prefer_production else -t["value"],
            0 if (prefer_production and t["bucket"] == "declining") else 1,
            -t["from_owner_trades"],
        ))
        reachable = [t for t in pos_targets if not t["friction"]]
        blocked = [t for t in pos_targets if t["friction"]]
        targets += reachable[:max_per_position]
        long_shots += blocked[:max_per_position]

    result = {"needs": my_needs, "targets": targets, "my_offers": my_pool}
    if targets:
        result["targets_note"] = TARGETS_NOTE
    if my_pool:
        result["my_offers_note"] = MY_OFFERS_NOTE
    if long_shots:
        result["long_shots"] = long_shots
        result["long_shot_note"] = LONG_SHOT_NOTE

    # Sellers-only search misses the best available production - see _persuasion_targets.
    stretch = _persuasion_targets(me, board, my_needs, result["my_offers"])
    if stretch:
        result["persuasion_targets"] = stretch[:max_per_position * 2]
        result["persuasion_note"] = PERSUASION_NOTE

    # Picks are currency, not production: a first becomes a rookie, another upside asset,
    # so its value to a contender is entirely in what it can be traded FOR.
    if my_picks is not None:
        result["picks_to_trade_away"] = my_picks
    return result


def _depth_adds(me_roster: dict, board: Board, filling_lineup: bool,
                my_starters: set[str], already: set[str]) -> list[dict]:
    """Cheap bodies on rebuilding rosters who would start for me if one player above them
    went down - the complement of `_buy_path`, cheapest first. The "only depth" bar depends
    on what the asking team is doing: filling a lineup is a redraft question, holding a
    lottery ticket is a dynasty one. Deliberately not `clears_relevance_floor`, whose
    tiered fractions opened a crack between the two lists that hid players from both.
    Whether the market also prices him over the asking team's weakest starter is stated per line,
    never used as a bar."""
    ctx, states, trade_counts = board.ctx, board.states, board.trade_counts
    # Excluding my own roster is not incidental: a rebuilding team searching rebuilding
    # teams includes itself, and once advised its owner to acquire two players he had.
    rebuilders = {s["owner_id"]: s["owner"] for s in states
                  if s["window"] == "Rebuild" and s["owner_id"] != me_roster["owner_id"]}
    states_by_id = {s["owner_id"]: s for s in states}
    others_have_traded = board.others_have_traded(me_roster["owner_id"])
    metric = "redraft_value" if filling_lineup else "value"
    bars = ctx.start_thresholds if filling_lineup else ctx.trade_thresholds
    bar_label = "replacement-level production" if filling_lineup else "the trade-value floor"
    weakest_starter = {}
    for pid in my_starters:
        info = ctx.players.get(pid)
        if info:
            produced = info.get("redraft_value") or 0
            pos = info["position"]
            if pos not in weakest_starter or produced < weakest_starter[pos]:
                weakest_starter[pos] = produced
    adds = []
    for roster in ctx.rosters:
        owner = rebuilders.get(roster["owner_id"])
        if owner is None:
            continue
        for player_id in roster["players"] or []:
            info = ctx.players.get(player_id)
            if not info or not info.get("value") or info["name"] in already:
                continue
            entry = {**info, "bucket": age_bucket(info["position"], info.get("age"),
                                                  info.get("usage_role")),
                     "years_to_decline": years_to_decline(info["position"],
                                                          info.get("age"),
                                                          info.get("usage_role"))}
            if (info.get(metric) or 0) >= bars[info["position"]]:
                continue  # a real fix, not depth - belongs to the buy path
            # The direction gate reaches the lottery tier too: a rebuild holds tickets
            # with a future, not rentals - Kelce and Kirk Cousins were being offered
            # to rebuilds as "cheap bodies" because these entries carried no runway.
            if not filling_lineup and _rental(entry):
                continue
            if not roster_needs.would_start_if_one_out(me_roster, ctx.players, player_id,
                                                      my_starters, ctx.lineup_dedicated,
                                                      ctx.lineup_flex):
                continue
            edge = ((info.get("redraft_value") or 0)
                    - weakest_starter.get(info["position"], 0)) if filling_lineup else None
            if edge is None:
                verdict = ""
            elif edge > 0:
                verdict = (f" Also priced {edge:,} over your weakest {info['position']} starter's "
                           f"season, so he is a real if modest upgrade there, "
                           f"not only cover.")
            else:
                verdict = (f" Priced under your weakest {info['position']} starter's season, "
                           f"so he is cover for an absence and nothing more.")
            adds.append({"name": info["name"], "position": info["position"],
                         "value": info["value"], "redraft_value": info.get("redraft_value"),
                         "age": info.get("age"), "bucket": entry["bucket"],
                         "from_owner": owner, "over_weakest_starter": edge,
                         # Friction travels with the (player, counterparty) pair, not with
                         # the list he happens to be in.
                         "friction": _buy_friction(info, states_by_id[roster["owner_id"]],
                                                   None, trade_counts.get(roster["owner_id"], 0),
                                                   others_have_traded)["friction"],
                         "note": (f"Would start for you if your weakest {info['position']} "
                                  f"were out. Below {bar_label} "
                                  f"({round(bars[info['position']]):,} at "
                                  f"{info['position']}), so the price should be nominal."
                                  + verdict)})
    adds.sort(key=lambda a: a["value"])
    return adds[:DEPTH_LIMIT]


PRODUCTION_ADDS_NOTE = (
    "PRODUCTION ADDS - the rental market, the win-harder list for a buyer with no hole to "
    "fill. Each of these is a PRODUCTION-PRICED piece (declining, or inside his final "
    "year) a seller is moving that would START for this team today - he beats the "
    "weakest starter at his position or the weakest flex occupant, and `over_weakest_starter` "
    "is by how much - ranked by that gain, cheapest in dynasty first among equals. Because "
    "they come from selling paths they are priced on production, not future: a 2nd-round "
    "pick for one of these is a directionally sound, ordinary trade (the direction gate is "
    "satisfied on both sides by construction). `runway` says which are rentals (this season "
    "only) and which are declining-not-done. This is also the natural SECOND LEG of a plan: "
    "after a consolidation trade opens a slot, one of these closes it. Not a priced offer."
)

PRODUCTION_ADDS_LIMIT = 6


def _production_adds(me_roster: dict, board: Board, my_starters: set[str],
                     already: set[str]) -> list[dict]:
    """Sellers' pieces that would start for me right now, ranked by production over the
    starter they displace. The complement of `_depth_adds` (bodies who start only if
    someone is out): these start today. Only sellers - a contender's producers are not
    on the table (the direction gate) - and only pieces that clear the relevance floor."""
    ctx, states, trade_counts = board.ctx, board.states, board.trade_counts
    others_have_traded = board.others_have_traded(me_roster["owner_id"])
    filled = roster_needs.fill_lineup(me_roster, ctx.players, ctx.lineup_dedicated,
                                      ctx.lineup_flex)
    # The bar a candidate must clear: the weakest occupant of any slot he could take -
    # his own position's dedicated slots, or a flex he is eligible for.
    def bar_for(pos):
        doors = [(slot, ctx.players[pid].get("redraft_value") or 0) for slot, pid in filled
                 if pid in ctx.players
                 and (slot == pos or pos in roster_needs.FLEX_ELIGIBILITY.get(slot, ()))]
        return min(doors, key=lambda d: d[1]) if doors else None
    adds = []
    for other in _others(states, me_roster, MIGHT_SELL):
        for player in other["sellable"] + other["tradeable_surplus"]:
            if player["name"] in already or not _sells_him(other, player):
                continue
            if not team_state.clears_relevance_floor(player, board.thresholds):
                continue
            # The rental market only: production-priced pieces (declining, or inside the
            # final year). Studs a seller holds belong to value_upgrades / persuasion -
            # ranked here they crowd out exactly the cheap production this list is for.
            if team_state.value_basis(player) != "production":
                continue
            door = bar_for(player["position"])
            produced = player.get("redraft_value") or 0
            if door is None or produced <= door[1]:
                continue
            adds.append({"name": player["name"], "position": player["position"],
                         "value": player["value"], "redraft_value": produced,
                         "years_to_decline": player.get("years_to_decline"),
                         "bucket": player.get("bucket"), "from_owner": other["owner"],
                         "seller_path": other.get("path", ""),
                         "slot": door[0], "over_weakest_starter": round(produced - door[1]),
                         "friction": _buy_friction(player, other, None,
                                                   trade_counts.get(other["owner_id"], 0),
                                                   others_have_traded)["friction"],
                         **_with_trade_note(player, other, trade_counts)})
    adds.sort(key=lambda a: (-a["over_weakest_starter"], a["value"]))
    return adds[:PRODUCTION_ADDS_LIMIT]
