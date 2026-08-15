"""The other side of the table: why an owner who isn't selling might still move a player,
who would want a player this team is moving, and the persuasion tier built on both.
History: LOGIC.md, "The counterparty".
"""

from .. import team_state, roster_needs
from ..team_values import INSIDE_FINAL_YEAR, MIN_MEANINGFUL_RUNWAY
from .board import (Board, NOT_SELLER, _best_chip, _buy_friction, _friction, _others,
                    acquires_by_default, _sells_him, _with_trade_note)

# "Still there later" is the same question `team_state` asks of a cornerstone, so it uses
# the same answer - see team_values.MIN_MEANINGFUL_RUNWAY.
MIN_RUNWAY_FOR_LATER = MIN_MEANINGFUL_RUNWAY

PERSUASION_NOTE = (
    "These are held by teams that are NOT shopping them, so none is available the way a "
    "rebuilding team's pieces are. Each carries why that owner might listen and what it "
    "costs to ask. Three different asks sit here and the difference decides whether to make "
    "the call: where the owner has a hole this roster can fill, the trade serves his "
    "existing plan and is nearer a fit than a pitch, and those come first. Where he has no "
    "such hole and is mid-table or rebuilding it is marked PIVOT - you are asking him to "
    "change direction, which is a commitment on his part and prices above market. Where he "
    "has no such hole and is CONTENDING it is marked HOLDS TO WIN - not a direction "
    "question at all: he could sell the piece and stay a contender, which is exactly why "
    "he probably won't, and only an overwhelming offer opens it. `offer_any_one_of` is a list "
    "of ALTERNATIVES, never a bundle: each name is one thing that owner would find "
    "interesting on its own, and combining two of them into a package is the one move this "
    "tool cannot support - dynasty value does not add across players, so nothing here prices "
    "'A plus B'. Name a single piece against a single piece. Within each group, ranked by current "
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
    serves_their_plan = [] if fills_a_hole else [_no_hole_friction(other)]
    if case:
        return {"their_reason": case, "friction": serves_their_plan}
    return {"their_reason": (f"Nothing about {other['owner']}'s team says seller, and their "
                             f"window does not argue for moving him either. They could do it "
                             f"and have no reason to, so this needs them to want your side "
                             f"more than they want him."),
            "friction": serves_their_plan}


def _no_hole_friction(other: dict) -> dict:
    """The no-hole ask, named by the seller's own window. "Change direction" is only the
    honest claim when there is a direction to change; for a contender it never was - the
    owner's own words, twice, months apart: "shiv is win now and could choose to move off
    the aging value but doesn't have to", and of a #1 lineup carrying the pivot tag, "I
    would not say it needs a pivot... probably hangs onto them to win now"."""
    if other["window"] in ("Push", "Contend"):
        return _friction(
            "holds_to_win",
            f"{other['owner']} has no hole you can fill and is winning now - this asks him "
            f"to sell production he is winning WITH, not to change direction. He could move "
            f"an aging piece and stay a contender, which is exactly why he probably won't: "
            f"expect a no unless the offer overwhelms him, and a premium if it does")
    return _friction(
        "needs_a_pivot",
        f"{other['owner']} has no hole you can fill, so this asks him to change direction "
        f"rather than take a fair offer - a wait-and-see, not a call you make once")


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
        # The direction gate: a rebuild does not buy this-season value by default, so a
        # positional hole never makes it "want" a rental ("Stafford to a rebuilder"
        # was suggested off a QB-shortage match; the shortage was real, the direction
        # was nonsense). Future-weighted pieces still reach them via the second reason.
        if need and not acquires_by_default(other["window"], player):
            need = None
        # The trajectory reason below is about future value and stands on its own, so the
        # production test gates only the positional one.
        their_roster = rosters_by_owner.get(other["owner_id"])
        helps = their_roster is None or _would_actually_help(player, their_roster, ctx)
        if need and helps:
            # Self-defending, because "short at QB (critical)" about a Mahomes-plus-nobody
            # superflex room read as "their QBs are bad" - the manager pushed back, and the
            # model apologised for advice that was right. The why now carries the shape of
            # the need, so it survives the pushback it is guaranteed to get.
            if need["startable"] < need["slots"]:
                good = (" - what they start is good, the slot is the problem"
                        if need.get("body_solid") else "")
            else:
                good = " - wants an upgrade, not another body"
            reasons.append(f"short at {player['position']} ({need['level']}: "
                           f"{need['startable']} startable for {need['slots']} "
                           f"slot{'' if need['slots'] == 1 else 's'}{good})")
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


def wanted_line(wanting: list[dict]) -> str:
    """The payload form of `wanted_by`: one string instead of a list of dicts. The dicts
    repeated near-identically on every same-position entry - measured at 21-26% of a sell
    report's tokens - and no reader used `rank` or `reason_count` at all. The
    contender-premium clause used to be composed only in the CLI printer, so the agent
    never saw it; built here, both readers get the same sentence."""
    # A Middling buyer's interest is real but conditional: he hasn't committed to
    # contending, so this buy would BE the commitment. Without the clause the model
    # presented an undecided team's need with a contender's urgency.
    return " | ".join(
        f"{w['owner']} [{w['window']}] {w['why']}"
        + (" (a contender - pays a premium for production, worth more there than here)"
           if w["window"] in ("Push", "Contend") else
           " (undecided window - buying is what would push them in, so expect interest "
           "without a contender's urgency or price)"
           if w["window"] == "Middling" else "")
        for w in wanting)


# How far above the target's own price a piece can be before offering it is an overpay
# rather than an offer. One player against one player is the only comparison this project
# makes, and it has to run in BOTH directions: the buy side already refuses targets above
# the asking team's best single chip (`beyond_your_best_chip`), while the give side had no
# ceiling at all and proposed a 7,321 cornerstone QB for a 2,006 back.
OVERPAY_LIMIT = 1.5


def _counterparty_fit(other: dict, their_needs: dict, my_offers: list[dict],
                      target: dict | None = None) -> dict | None:
    """What *I* hold that would interest this particular owner, or None. Two ways he is
    interested: he is short at a position I can offer, or he should be converting aging
    production and I hold now-and-later value he'd convert into. Annotation, not ranking -
    re-sorting by fit would push low-friction options down. `fills_a_hole` is returned as
    data because it is the single definition of the `needs_a_pivot` flavor."""
    # Position match alone is not want: an owner ACCUMULATING (tilting ascending) is the
    # same owner `_sells_him` says sells his own final-year pieces, so handing him one is
    # the trade backwards. Live: a 0.3-runway 28-year-old WR offered to a 55%-ascending
    # team because they were "short at WR" - the owner's read: "buttboi would not want
    # DK Metcalf". The clock is INSIDE_FINAL_YEAR, the same bar `_sells_him` uses.
    accumulating = other.get("ascending_pct", 0) > other.get("declining_pct", 0)
    ceiling = (target or {}).get("value", 0) * OVERPAY_LIMIT
    # The direction gate on the offer side: never dangle a rental at a rebuild - their
    # positional hole is real, but this-season value is the one thing their default
    # direction does not buy.
    fitting = [e for e in my_offers
               if e["position"] in their_needs
               and acquires_by_default(other.get("window", ""), e)
               and not (accumulating
                        and (e.get("years_to_decline") or 0) < INSIDE_FINAL_YEAR)]
    # The ceiling picks the SENTENCE, never whether a piece is mentioned: dropping
    # over-ceiling pieces silently made "why Goff instead of Hurts?" unanswerable - Hurts
    # sat 13 units (0.25%) over a hard 1.5x line he flaps across with every refresh.
    # A piece worth more than the target covers is not unofferable, it is a DIFFERENT
    # trade, and saying so is the answer.
    offers = [e for e in fitting if not (ceiling and e["value"] > ceiling)]
    bigger = [e for e in fitting if ceiling and e["value"] > ceiling]
    if offers or bigger:
        positions = sorted({e["position"] for e in fitting})
        # Parity opens spend the FEWEST years that get the deal done - keep-the-years is
        # the seller's side of the same doctrine, so among fitting pieces the shortest
        # runway leads. Pool order led with the 6.1-year piece over the 4.8.
        offers.sort(key=lambda e: (bool(e.get("friction")),
                                   e.get("years_to_decline") or 99))
        timeline = (" These are the ones that fit his timeline as well as his lineup - he "
                    "is accumulating, so anything of yours inside its final year is a "
                    "piece he is trying to move, not acquire." if accumulating else "")
        bigger_note = ""
        if bigger:
            names = ", ".join(f"{e['name']} ({e['value']:,}, "
                              f"{e.get('years_to_decline')} yrs)" for e in bigger[:3])
            bigger_note = (f" {names}: also fills this hole but is worth more than "
                           f"{(target or {}).get('name', 'this target')} covers - a sale "
                           f"in its own right at its own price, a different conversation "
                           f"than this acquisition, and nothing here prices the "
                           f"difference. For a seller keeping years, the shortest-runway "
                           f"piece among these is the one to be shopping hardest anyway.")
        return {"offer_any_one_of": [e["name"] for e in offers[:3]],
                "fills_a_hole": True,
                "why_it_fits": (f"{other['owner']} has a "
                                f"{their_needs[positions[0]]['level']} need at "
                                f"{'/'.join(positions)}, which you can fill from your own "
                                f"spare pieces - so this is a two-way conversation rather "
                                f"than asking him to do you a favour. Ordered by fewest "
                                f"years spent: the cheapest piece in remaining seasons "
                                f"that still gets the deal done.{timeline}{bigger_note}")}

    if accumulating:
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
            return {"offer_any_one_of": [e["name"] for e in offered],
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
        # The direction gate: contenders part with future, not production - by default
        # their rentals are simply not on anyone's board ("shivvv might sell Henry" is
        # nonsense with or without a holds_to_win label). A user asking about a named
        # player still gets the honest holds-to-win read from get_player_outlook.
        if other["window"] in ("Push", "Contend"):
            continue
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
            # Three different asks, not two: filling his hole serves his plan; a pivot
            # prices above market; and a CONTENDER with no hole is not a direction
            # question at all - he holds to win, and only an overwhelming offer opens it.
            # Describing the first two identically once contradicted `why_it_fits`;
            # describing the third as a pivot misread the league's best team.
            if is_fit:
                cost_note = (
                    f"{other['owner']} has a need you can fill, so this need not be a change "
                    f"of direction for him - it can be a straight trade that serves both "
                    f"plans. He is still not shopping {player['name']}, so you are opening "
                    f"the conversation and should expect to pay something for that, but this "
                    f"is nearer a fit than a pitch.")
            elif other["window"] in ("Push", "Contend"):
                cost_note = (
                    f"{other['owner']} is winning with {player['name']} and not currently a "
                    f"seller - and this is not a direction question: he could move him and "
                    f"stay a contender, which is exactly why he probably holds. Expect a no "
                    f"unless the offer overwhelms him; you are buying production out of a "
                    f"winning lineup, and that prices above market.")
            else:
                cost_note = (
                    f"{other['owner']} is not currently a seller, so this is a conversation "
                    f"rather than a fit: acquiring {player['name']} means persuading them to "
                    f"change direction, which is a commitment on their part and prices above "
                    f"market. Treat it as an option worth opening, not a deal that's there.")
            plausible.append({
                "position": pos,
                "need_level": need["level"],
                **(fit or {}),
                **_with_trade_note(player, other, trade_counts),
                "seller_window": other["window"],
                "production_per_cost": round(ratio, 2),
                "why_they_might_listen": why,
                "friction": friction,
                "needs_a_pivot": not is_fit,
                "cost_note": cost_note,
            })
            if not is_fit:
                friction.append(_friction(
                    "holds_to_win" if other["window"] in ("Push", "Contend")
                    else "needs_a_pivot", cost_note))
    plausible.sort(key=lambda t: -t["production_per_cost"])
    return plausible
