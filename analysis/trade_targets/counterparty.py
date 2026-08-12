"""The other side of the table: why an owner who isn't selling might still move a player,
who would want a player this team is moving, and the persuasion tier built on both.
History: LOGIC.md, "Persuasion targets" and "Joining facts the tool already had".
"""

from .. import team_state, roster_needs
from ..team_values import MIN_MEANINGFUL_RUNWAY
from .board import (Board, NOT_SELLER, _best_chip, _buy_friction, _friction, _others,
                    _sells_him, _with_trade_note)

# "Still there later" is the same question `team_state` asks of a cornerstone, so it uses
# the same answer - see team_values.MIN_MEANINGFUL_RUNWAY.
MIN_RUNWAY_FOR_LATER = MIN_MEANINGFUL_RUNWAY

PERSUASION_NOTE = (
    "These are held by teams that are NOT shopping them, so none is available the way a "
    "rebuilding team's pieces are. Each carries why that owner might listen and what it "
    "costs to ask. Two different asks sit here and the difference decides whether to make "
    "the call: where the owner has a hole this roster can fill, the trade serves his "
    "existing plan and is nearer a fit than a pitch, and those come first. Where he has no "
    "such hole it is marked PIVOT - you are asking him to change direction, which is a "
    "commitment on his part and prices above market. Within each group, ranked by current "
    "production per unit of trade value, the right order for a team buying for this season: "
    "the cheapest name is often better than the most valuable one, because the market "
    "discounts age the buyer isn't paying for. Where a decline argument is made it is "
    "LEAGUE-RELATIVE - a trajectory tertile - so read the two percentages it quotes: a "
    "narrow gap means this league is young, not that this roster is old."
)


def _seller_case(other: dict, prior: dict | None) -> str | None:
    """Why this non-selling *team* might listen: its roster is falling, or this core missed
    the playoffs with the roster it still has. None means nothing about the team says so -
    which is no longer the end of the search, see `_cliff_case`."""
    same_team = bool(prior and prior["describes_this_team"])
    if other["trajectory"] == "falling":
        base = (f"Their roster is among this league's least improving - {other['trajectory_rank']} "
                f"of {other['of_teams']} on trajectory, where 1 is the most ascending - with "
                f"{other['declining_pct']}% of their current production coming from declining "
                f"players against {other['ascending_pct']}% ascending. Aging out is the one "
                f"thing that turns a team that isn't selling into one that will.")
        if same_team and not prior["made_playoffs"]:
            return (f"{base} And it hasn't delivered: {prior['note']}")
        return base
    if same_team and not prior["made_playoffs"]:
        return (f"This core hasn't won with them. {prior['note']} A team that missed with "
                f"the same roster it still has is more open to changing course than the "
                f"standings alone suggest.")
    return None


def _cliff_case(player: dict, other: dict, ratio: float, discounted: bool = True,
                team_case: str | None = None) -> str | None:
    """Why an owner whose team looks fine might move one aging starter: their window and
    the player's don't line up. Requires a starter on a clock (runway, not bucket - the
    bucket missed a 1.2-year receiver reading `prime`) on a roster tilting ascending.
    `discounted` picks one clause, never whether the case exists - as a gate it made the
    answer turn on the fourth decimal place. `team_case` composes with this rather than
    silencing the more actionable player-level argument."""
    if not (player["is_starter"]
            and (player.get("years_to_decline") or 0) < MIN_MEANINGFUL_RUNWAY):
        return None
    if other["ascending_pct"] <= other["declining_pct"]:
        return None
    discount = (f" He produces {ratio:.2f}x his own trade value - still starting, priced as "
                f"though his remaining years are gone." if discounted else
                f" He is not discounted for it ({ratio:.2f}x his own trade value), so this is "
                f"about whose window he fits rather than a price to harvest.")
    opener = (f"{team_case} Separately, their window and {player['name']}'s don't line up"
              if team_case else
              f"Nothing about {other['owner']}'s team says seller, but their window and "
              f"{player['name']}'s don't line up")
    return (f"{opener}: {other['ascending_pct']}% of their "
            f"production is ascending against {other['declining_pct']}% declining, so "
            f"they're built for seasons he won't be part of.{discount} Keeping him may well "
            f"tip this season for them, which is exactly why it's a real decision for them "
            f"rather than a giveaway.")


def _why_they_would_move_him(player: dict, other: dict, prior: dict | None,
                             premium_bars: dict[str, float], never_trades: bool = False,
                             fills_a_hole: bool = False) -> dict:
    """Whether the OTHER owner has a reason to part with this player. A Rebuild owner is
    already selling; a non-seller gets the same `_seller_case`/`_cliff_case` arguments the
    persuasion tier makes; otherwise the honest answer is that nothing says seller.
    `never_trades` overrides all three - it is evidence about what he does, not an argument
    about what he should want. `fills_a_hole` must come from the caller (it is a fact about
    MY roster against theirs); `_counterparty_fit` is the single test."""
    if never_trades:
        base = (f"{other['owner']} is rebuilding and should be converting exactly this"
                if other["window"] == "Rebuild" else
                f"{other['owner']}'s window does not obviously argue for keeping him")
        return {"their_reason": (f"{base} - BUT they have never made a trade, which outweighs "
                                 f"any of that. Treat him as unavailable until they show "
                                 f"otherwise."),
                "friction": [_friction("never_trades",
                                       f"{other['owner']} has never made a trade, so the call "
                                       f"may not be returned at all")]}
    if other["window"] == "Rebuild":
        return {"their_reason": (f"{other['owner']} is rebuilding - this is exactly the kind of "
                                 f"production they should be converting, so no persuasion needed.")}
    ratio = ((player.get("redraft_value") or 0) / player["value"]) if player.get("value") else 0
    team_case = _seller_case(other, (prior or {}).get(other["owner_id"]))
    case = _cliff_case(player, other, ratio, team_case=team_case,
                       discounted=ratio >= (premium_bars or {}).get(player["position"],
                                                                    float("inf"))) or team_case
    serves_their_plan = [_friction(
        "needs_a_pivot", f"{other['owner']} has no hole you can fill, so this asks him to "
                         f"change direction rather than take a fair offer - a wait-and-see, "
                         f"not a call you make once")] if not fills_a_hole else []
    if case:
        return {"their_reason": case, "friction": serves_their_plan}
    return {"their_reason": (f"Nothing about {other['owner']}'s team says seller, and their "
                             f"window does not argue for moving him either. They could do it "
                             f"and have no reason to, so this needs them to want your side "
                             f"more than they want him."),
            "friction": serves_their_plan}


def _would_actually_help(player: dict, roster: dict, ctx) -> bool:
    """Does he beat what they already start at his position? An empty slot takes anybody -
    a positional need is not the same as wanting THIS player."""
    theirs = [ctx.players[pid].get("redraft_value") or 0
              for pid in ctx.starters_for(roster)
              if pid in ctx.players and ctx.players[pid]["position"] == player["position"]]
    return not theirs or (player.get("redraft_value") or 0) > min(theirs)


def wanted_by(player: dict, me_roster: dict, board: Board) -> list[dict]:
    """Which other owners would want THIS player, and in one line why. Two reasons, not
    one: a positional need he'd actually improve, or a falling roster short of ascending
    value at any position."""
    ctx, states, needs_by_owner_id = board.ctx, board.states, board.needs_by_owner_id
    rosters_by_owner = {r["owner_id"]: r for r in ctx.rosters} if ctx else {}
    wanting = []
    for other in states:
        if other["owner_id"] == me_roster["owner_id"]:
            continue
        reasons = []
        need = needs_by_owner_id.get(other["owner_id"], {}).get(player["position"])
        # The trajectory reason below is about future value and stands on its own, so the
        # production test gates only the positional one.
        their_roster = rosters_by_owner.get(other["owner_id"])
        helps = their_roster is None or _would_actually_help(player, their_roster, ctx)
        if need and helps:
            reasons.append(f"short at {player['position']} ({need['level']})")
        ascending_pct, declining_pct = other.get("ascending_pct", 0), other.get("declining_pct", 0)
        if player.get("bucket") == "ascending" and declining_pct > ascending_pct:
            reasons.append(f"falling roster ({ascending_pct}% ascending against "
                           f"{declining_pct}% declining) - ascending value is what it is short "
                           f"of, whatever the position")
        if not reasons:
            continue
        wanting.append({"owner": other["owner"], "window": other["window"],
                        "need_level": need["level"] if need else None,
                        "rank": need.get("rank") if need else None,
                        "reason_count": len(reasons), "why": "; ".join(reasons)})
    return sorted(wanting, key=lambda w: (roster_needs.NEED_PRIORITY.get(w["need_level"], 3),
                                          -w["reason_count"]))


def _counterparty_fit(other: dict, their_needs: dict, my_offers: list[dict]) -> dict | None:
    """What *I* hold that would interest this particular owner, or None. Two ways he is
    interested: he is short at a position I can offer, or he should be converting aging
    production and I hold now-and-later value he'd convert into. Annotation, not ranking -
    re-sorting by fit would push low-friction options down. `fills_a_hole` is returned as
    data because it is the single definition of the `needs_a_pivot` flavor."""
    offers = [e for e in my_offers if e["position"] in their_needs]
    if offers:
        positions = sorted({e["position"] for e in offers})
        return {"you_could_offer": [e["name"] for e in offers[:3]],
                "fills_a_hole": True,
                "why_it_fits": (f"{other['owner']} has a "
                                f"{their_needs[positions[0]]['level']} need at "
                                f"{'/'.join(positions)}, which you can fill from your own "
                                f"spare pieces - so this is a two-way conversation rather "
                                f"than asking him to do you a favour.")}

    if other["ascending_pct"] > other["declining_pct"]:
        # Above replacement, with a real current price, and not past his own cliff. Runway
        # RANKS this pool rather than emptying it - 24% of starters sit within a year of the
        # two-season bar - and the bar decides what the sentence claims instead.
        both = sorted((e for e in my_offers
                       if e.get("value_over_replacement", 0) > 0
                       and (e.get("redraft_value") or 0) > 0
                       and (e.get("years_to_decline") or 0) >= 0),
                      # Friction last, then the longest runway, then most current production.
                      # Runway leads because it is what this branch is selling.
                      key=lambda e: (bool(e.get("friction")),
                                     -(e.get("years_to_decline") or 0),
                                     -(e.get("redraft_value") or 0)))
        if both:
            offered = both[:3]
            reach = min((e.get("years_to_decline") or 0) for e in offered)
            future = (f"These carry both a current price and a future, which is the trade he "
                      f"should be making anyway."
                      if reach >= MIN_RUNWAY_FOR_LATER else
                      f"These score now, but the nearest of them is {reach:.1f} years from his "
                      f"own decline cutoff - so this is the weaker version of that trade, and "
                      f"he should want a real future piece more than he wants these.")
            return {"you_could_offer": [e["name"] for e in offered],
                    "fills_a_hole": False,
                    "why_it_fits": (f"{other['owner']} has no positional hole, so there is "
                                    f"nothing to fill - but his roster is tilting ascending "
                                    f"({other['ascending_pct']}% against "
                                    f"{other['declining_pct']}% declining) while he starts "
                                    f"aging players, so what he wants is value that scores "
                                    f"this season and is still there in two. {future}")}
    return None


def _persuasion_targets(me: dict, board: Board, my_needs: dict,
                        my_offers: list[dict] | None = None) -> list[dict]:
    """Aging production held by teams that aren't sellers yet but could be talked into it -
    the tier the buy path structurally cannot see. Sourced from `sellable` (not the
    cornerstone-gated `win_now_core`), ranked by current production per unit of trade cost,
    with implausible sellers excluded rather than ranked last. Runway defines the tier
    (`MIN_MEANINGFUL_RUNWAY`), the floor applies at every need level, and the now-premium
    bar picks a clause inside `_cliff_case` - it gates nothing."""
    states, thresholds, trade_counts = board.states, board.thresholds, board.trade_counts
    prior, premium_bars, needs_by_owner_id = (board.prior, board.premium_bars,
                                              board.needs_by_owner_id)
    best_chip = _best_chip(my_offers)
    others_have_traded = board.others_have_traded(me["owner_id"])
    plausible = []
    for other in _others(states, me, NOT_SELLER):  # sellers are the normal buy path's job
        team_why = _seller_case(other, prior.get(other["owner_id"]))
        for player in other["sellable"]:
            pos, need = player["position"], my_needs.get(player["position"])
            if need is None or not player.get("redraft_value"):
                continue
            if not team_state.clears_relevance_floor(player, thresholds):
                continue
            # A rising Middling team's aging pieces are the buy path's job (`_sells_him`);
            # this tier is for players whose owner has to be talked into it.
            if _sells_him(other, player):
                continue
            # Asking an owner to change direction for a player who would not crack your
            # lineup is not a trade - bodies for a count-shaped need are `depth_adds`' job.
            if player["redraft_value"] <= need["weakest_starter"]:
                continue
            ratio = player["redraft_value"] / player["value"]
            # Runway defines the tier; the ratio only keeps the discount sentence honest.
            if (player.get("years_to_decline") or 0) >= MIN_MEANINGFUL_RUNWAY:
                continue
            why = _cliff_case(player, other, ratio, team_case=team_why,
                              discounted=ratio >= premium_bars.get(pos,
                                                                   float("inf"))) or team_why
            if why is None:
                continue
            fit = _counterparty_fit(other, (needs_by_owner_id or {}).get(other["owner_id"], {}),
                                    my_offers or [])
            is_fit = bool(fit and fit.get("fills_a_hole"))
            friction = _buy_friction(player, other, best_chip,
                                     trade_counts.get(other["owner_id"], 0),
                                     others_have_traded)["friction"]
            plausible.append({
                "position": pos,
                "need_level": need["level"],
                **(fit or {}),
                **_with_trade_note(player, other, trade_counts),
                "production_per_cost": round(ratio, 2),
                "why_they_might_listen": why,
                "friction": friction,
                "needs_a_pivot": not is_fit,
                # Two different asks: filling his hole serves his plan; a pivot prices
                # above market. Describing both identically contradicted `why_it_fits`.
                "cost_note": (
                    f"{other['owner']} has a need you can fill, so this need not be a change "
                    f"of direction for him - it can be a straight trade that serves both "
                    f"plans. He is still not shopping {player['name']}, so you are opening "
                    f"the conversation and should expect to pay something for that, but this "
                    f"is nearer a fit than a pitch."
                    if is_fit else
                    f"{other['owner']} is not currently a seller, so this is a conversation "
                    f"rather than a fit: acquiring {player['name']} means persuading them to "
                    f"change direction, which is a commitment on their part and prices above "
                    f"market. Treat it as an option worth opening, not a deal that's there."
                ),
            })
            if not is_fit:
                friction.append(_friction("needs_a_pivot", plausible[-1]["cost_note"]))
    plausible.sort(key=lambda t: -t["production_per_cost"])
    return plausible
