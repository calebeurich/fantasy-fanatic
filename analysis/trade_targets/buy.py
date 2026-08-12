"""The buy side: what this team can offer, who to ring for each need, and the cheap
depth underneath it all. History: LOGIC.md, "Trade target matching" and "Depth as a
third state".
"""

from .. import team_state, roster_needs
from ..team_values import age_bucket, pick_equivalent
from .board import (Board, CORNERSTONE_ASK, MIGHT_SELL, NOISE_RETAINED, _best_chip,
                    _buy_friction, _friction, _others, _sells_him, _with_trade_note)
from .counterparty import PERSUASION_NOTE, _persuasion_targets

LONG_SHOT_NOTE = (
    "LONG SHOTS - real fits, but something structural is in the way, and `friction` says what. "
    "These are separated from the buy list rather than ranked below it because the reason is "
    "not price: an owner who has never traded may not answer, a cornerstone's owner will answer "
    "and say no, and a player costing more than your biggest single chip cannot be reached "
    "one-for-one at all. Ring the buy list first. Raise one of these only when you already "
    "know something this tool doesn't - that the owner is about to sell, or that you are "
    "willing to open a negotiation with no single piece that covers it."
)

DEPTH_NOTE = (
    "DEPTH, NOT NEEDS. Each of these is a player who does not crack this lineup today but "
    "would step straight into it if one starter at his position were out - which byes "
    "guarantee and injuries make likely. They are listed because every one of them sits "
    "BELOW replacement level - startable quality - meaning they are cheap by definition and "
    "not who the buy targets above are for. Price them as sweeteners: worth a late pick or a "
    "spare body, never worth a real asset. But below LEAGUE replacement is not the same as "
    "unable to help THIS lineup - each line says whether the player actually outproduces your "
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
    future a closing window exists to spend. Out: any position this team itself needs
    (trading it just moves the shortage), and prime/declining starters, who ARE the
    current production. A starter's `lineup_cost` is stated, never used as a veto."""
    thresholds, pick_values = board.thresholds, board.pick_values
    covered = covered or {}
    backfills = backfills or {}

    def offerable(e):
        if not e["is_starter"] or e.get("is_cornerstone"):
            return True
        return covered.get(e["name"]) == 0 or (
            me.get("window") == "Push" and e["bucket"] == "ascending")

    offers = [{**e, "lineup_cost": round(covered[e["name"]])} if e["name"] in covered else e
              for e in me["sellable"] + me["tradeable_surplus"]
              if offerable(e) and e["position"] not in needs
              and team_state.clears_relevance_floor(e, thresholds)]

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
                                      f"moving him costs {cost_now:,.0f} of production out of your "
                                      f"own lineup{share}, after it refills itself"))
        e["friction"] = friction
        # The trade-off in one line, in both currencies - naming the replacement is what
        # turns the cost from an arbitrary number into the argument.
        if backfills.get(e["name"]):
            bf = backfills[e["name"]]
            e["backfill"] = bf
            e["trade_off"] = (f"frees {e['value']:,} of dynasty value for "
                              f"{cost_now:,.0f} of production, because {bf['name']} "
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
        upgrade_bar = need["weakest_starter"] if need["level"] == "weak" else None
        pos_targets = []
        for other in _others(states, me, MIGHT_SELL):
            for player in other["sellable"]:
                if player["position"] != pos or not team_state.clears_relevance_floor(player, thresholds):
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
                pos_targets.append({"position": pos, "need_level": need["level"],
                                     "need_note": need["note"],
                                     "over_weakest_starter": over_weakest,
                                     "sells_because": ("rebuilding" if other["window"] == "Rebuild"
                                                       else "rising, so selling age not youth"),
                                     "production_per_cost": round(ratio, 2) if ratio else None,
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
    Whether he also outproduces the asking team's weakest starter is stated per line,
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
                                                  info.get("usage_role"))}
            if (info.get(metric) or 0) >= bars[info["position"]]:
                continue  # a real fix, not depth - belongs to the buy path
            if not roster_needs.would_start_if_one_out(me_roster, ctx.players, player_id,
                                                      my_starters, ctx.lineup_dedicated,
                                                      ctx.lineup_flex):
                continue
            edge = ((info.get("redraft_value") or 0)
                    - weakest_starter.get(info["position"], 0)) if filling_lineup else None
            if edge is None:
                verdict = ""
            elif edge > 0:
                verdict = (f" Also outproduces your weakest {info['position']} starter by "
                           f"{edge:,} right now, so he is a real if modest upgrade there, "
                           f"not only cover.")
            else:
                verdict = (f" Does not outproduce your weakest {info['position']} starter, "
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
