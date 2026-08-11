"""Surface obvious trade fits: a team needing a position looks at what other teams can
part with, shaped by which window each side is in. This is a discovery tool, not a
fairness calculator - it finds *who* to call, not whether a specific package is fair (see CLAUDE.md/prior discussion on why
a real value calculator is a separate, harder problem: roster construction means bench
depth isn't fungible with a starter's value).

Smoke test: python -m analysis.trade_targets <league_id> <owner_name>
"""

import sys

from sources import fantasycalc

from . import team_state, roster_needs, trade_activity, prior_season
from .league import context
from .team_values import (owned_picks, pick_equivalent, now_premium_bar, age_bucket,
                          MIN_MEANINGFUL_RUNWAY)

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

# The choice an Ascend team is actually facing, stated rather than left implicit. Showing
# both paths without it - which is what the old "Middling" mode did - hands over two lists
# and no basis for choosing between them, when the whole reason this window exists is that
# one of them is cheaper.
ASCEND_TIMING_NOTE = (
    "TIMING: both paths are shown because both are live, but they cost differently. "
    "Pushing now means buying current production at market price - and this roster's own "
    "ascending players are scheduled to supply that production next season for free, so a "
    "push is paying a premium for one extra year of contention. Waiting is the cheaper "
    "default. Push anyway when the price is below market (a seller who has to move a "
    "piece), when the gap to the top team is small enough that one addition closes it, or "
    "when a need is count-shaped rather than quality-shaped - an empty starting slot costs "
    "points every week and no amount of patience fills it."
)

# The same mechanic means different things depending on whether there's a clock.
SWAP_FRAMING = {
    "Push": ("Your window is closing, so converting future premium into trade capital is "
             "the point: the seasons you'd be selling are further out than the ones you're "
             "playing for. Spend what this frees on production now."),
    "Contend": ("You're good and not declining, so there's no clock forcing this - it's "
                "profit-taking rather than a conversion. The lineup is unchanged either "
                "way, so the only question is whether the freed value is worth more to you "
                "than holding the pricier player. No urgency; take a good offer, don't "
                "chase one."),
}

# A contender whose production is tilting ascending has two live plays, and the window
# label alone hides that. It contends either way - which is exactly why `window` stays
# "Contend" and this is additive: the choice is about *how* it contends, not whether. A
# team aging into its own window has no such choice, and neither does one already falling.
CONTEND_CHOICE_NOTE = (
    "TWO LIVE PATHS. This roster contends now and its production is still tilting "
    "ascending, so it is not choosing whether to compete - it is choosing how. STACK: buy "
    "more current production. Already the strongest lineup, so the marginal win is cheaper "
    "here than for anyone else, and nothing has to be given up on. CONVERT: move the aging "
    "starters listed in `conversion_candidates` for value that matches the seasons the rest "
    "of the roster is built for. Both are defensible; the cost is that stacking spends "
    "future value on a lead this team already has, while converting gives up real "
    "production this season for a roster that stays strong longer. Neither is urgent - a "
    "contender with no clock can wait for a good price rather than chase one."
)

STRANDED_NOTE = (
    "STRANDED PRODUCTION - the most valuable thing this roster owns that it cannot use. "
    "Each of these out-produces the WEAKEST player in the starting lineup and is kept out "
    "of it purely by positional capacity, not by being worse. That makes their entire value "
    "to this team whatever they fetch in a trade, which is true whichever direction the team "
    "is heading: a contender should convert one into the position it is short at, a "
    "rebuilder into futures. Lead with these before anything else in the sell lists - "
    "holding them costs a starting slot's worth of production every week and fixes nothing."
)

PERSUASION_NOTE = (
    "These are held by teams that are NOT currently sellers, so none of them is available "
    "the way a rebuilding team's pieces are. Each carries why that owner might listen and "
    "what it costs to ask. Ranked by current production per unit of trade value, which is "
    "the right order for a team buying for this season - the cheapest name here is often "
    "better than the most valuable one, because the market discounts age the buyer isn't "
    "paying for."
)

# A persuasion target has to be *age-discounted*, which is the entire rationale for asking
# a non-seller: you want production the market prices down for seasons you aren't buying.
# The bar is a percentile of `redraft_value / value` **within the player's own position**
# (team_values.now_premium_bar), not an absolute number: top-decile now-weighting for his
# position, i.e. the market is pricing him for this season and writing off the rest.
#
# This was an absolute 1.0 ("below this he costs more in dynasty value than he delivers in
# current production"), which quietly treated two unnormalized scales as comparable. 1.0 is
# not neutral: it sits near the 90th percentile for RB and *above the entire TE pool*, whose
# maximum is 1.01. The tier was closed to tight ends and nearly closed to receivers, in
# every league, for reasons no reader could have seen. team_values.py records the same
# mistake being made once before with a raw dynasty/redraft ratio; treat an absolute
# threshold on these two scales as a bug on sight.
#
# One bar, not two. A separate looser floor for team-level reasons was tried and dropped:
# at the median it admitted players who are merely typical (a WR at 0.43 against a 0.37
# position median), which is not "age-discounted" in any sense a manager would recognise.
# `_cliff_case` needs no bar of its own - every candidate has already cleared this one, so
# what distinguishes that path is solely the window mismatch.
NOW_PREMIUM_PERCENTILE = 0.9

DEFAULT_MAX_PER_POSITION = 3  # a parameter, not a hard limit - "give me more" means call again with a higher number

# How much of a lineup player's *current* production a replacement must retain before
# swapping them is worth considering. 90% is deliberately strict: this suggests giving up
# real production for trade value, so the production loss has to be close to noise.
MIN_PRODUCTION_RETAINED = 0.90

# And how much dynasty value the swap has to free up to be worth mentioning at all -
# below this it's churn, not arbitrage.
MIN_VALUE_FREED = 300

# "Still there later" is the same question `team_state` asks of a cornerstone, so it uses the
# same answer - see team_values.MIN_MEANINGFUL_RUNWAY.
MIN_RUNWAY_FOR_LATER = MIN_MEANINGFUL_RUNWAY

# Windows where a team is still trying to field a winning lineup this season.
SWAP_ELIGIBLE_WINDOWS = ("Push", "Contend", "Ascend")


def _others(states: list[dict], me: dict, window_test) -> list[dict]:
    """Every team but this one whose window passes `window_test`.

    The same two-part condition - not me, and the right side of the market - was written
    inline in five places with three different window tests, which is how it came to read
    `== "Rebuild"` in one path and `!= "Rebuild"` in the one right below it. Naming it makes
    the direction of each search explicit at the call site."""
    return [o for o in states if o["owner_id"] != me["owner_id"] and window_test(o["window"])]


def IS_SELLER(window: str) -> bool:
    """Rebuilding teams - the ones actually trying to move current value."""
    return window == "Rebuild"


def NOT_SELLER(window: str) -> bool:
    """Everyone else, who has to be talked into it."""
    return window != "Rebuild"


def STILL_COMPETING(window: str) -> bool:
    """Teams trying to field a winning lineup this season."""
    return window in SWAP_ELIGIBLE_WINDOWS


def _with_trade_note(entry: dict, other: dict, trade_counts: dict[str, int]) -> dict:
    # `is_starter` is a claim about *value* - the best lineup this roster could field - and
    # on a rebuilding team it is not a claim about intent. A team openly tanking is not
    # trying to start anybody; its "starter" is just its least-bad player at the position.
    # Without saying so, a buy target reads as "you'd have to prise away someone he's
    # relying on", which inverts the actual conversation: those are the players he most
    # wants to convert into picks.
    starter_note = ({"starter_caveat": (
        f"Listed as a starter for {other['owner']}, but they are rebuilding - that reflects "
        f"his value on that roster, not that the owner is trying to win with him.")}
        if entry.get("is_starter") and other["window"] == "Rebuild" else {})
    trades = trade_counts.get(other["owner_id"], 0)
    # A bare `0` is an unlabelled number, and this project has already shipped one of those:
    # `{"diff": -11}` reliably got a meaning invented for it. The CLI printed "NEVER TRADES"
    # while the dict an agent actually reads carried only the integer.
    never = ({"never_trades": (
        f"{other['owner']} has made no trades in this league's history. Not a reason to skip "
        f"him - it may just mean nobody has asked - but expect a harder conversation than "
        f"with an active trader, and weigh that against how much this player helps.")}
        if trades == 0 else {})
    return {**entry, "from_owner": other["owner"], "from_owner_trades": trades,
            **never, **starter_note}


def _my_offer_pool(me: dict, thresholds: dict[str, float], needs: dict[str, dict],
                   pick_values: dict[str, int] | None = None,
                   covered: dict[str, float] | None = None) -> list[dict]:
    """What you could realistically offer: bench value that isn't elite enough to be a
    cornerstone but also isn't part of your actual lineup (e.g. a 3rd QB in a 2-QB-max
    format), plus young surplus, plus any starter the roster **covers from the bench for
    free**. Never a position you yourself have a need at - trading away a WR while WR is
    your own critical need just moves the shortage, it doesn't fix anything. Most value over
    replacement first - which is printed, because raw value alone makes the order look wrong
    (a WR at 1,792 outranks a TE at 1,815, since WR replacement level is higher). Give-up
    cost labels each name, it doesn't order them.

    `covered` maps player name -> current production lost if he leaves and the lineup
    refills optimally (`roster_needs.production_lost_without`). Two ways a starter gets in:

    1. **The bench covers him for free** (`covered == 0`). He's in the lineup only because
       somebody has to be; the next man up scores the same. True in any window.
    2. **He's ascending and this team is `Push`.** A closing window sells future value and
       buys present - that is the module's whole premise - and an ascending starter is
       future value occupying a lineup slot. Declining and prime starters stay protected:
       they *are* the current production a pushing team is trying to keep.

    **"Is he a starter" was the wrong question and it hid the best asset on the board.** A
    real Push team's two biggest trade chips were an ascending TE (3,660 dynasty against
    1,035 redraft) and a backup QB. The TE was excluded for occupying a lineup slot, though
    his owner named him first when asked what he'd move - correctly, since a bench TE
    covers most of it and the rest is a future that team is trying to spend. Rule 1 alone
    would still have missed him: replacing him costs 420 of current production, which is
    real. That cost is now stated (`lineup_cost`) rather than used as a veto, because
    whether it's worth paying depends on what comes back, which this module deliberately
    doesn't price.

    The same test protects the players it should. That roster's best WR is declining with
    3,961 redraft against 3,773 dynasty - nearly all present value - so he is never offered,
    while the TE at 1,035 against 3,660 is."""
    # `is_starter` is the value-derived lineup (LeagueContext.starters), so this is just
    # a field read now. It used to be Sleeper's current-week snapshot, which is
    # meaningless before Week 1 - a superflex team's QB2 was offered away as surplus
    # because the preseason lineup listed only one QB - and the fix was a `projected` set
    # of names threaded through five functions. Fixing the flag at its source deleted all
    # of that.
    covered = covered or {}

    def offerable(e):
        if not e["is_starter"]:
            return True
        return covered.get(e["name"]) == 0 or (
            me.get("window") == "Push" and e["bucket"] == "ascending")

    offers = [{**e, "lineup_cost": round(covered[e["name"]])} if e["name"] in covered else e
              for e in me["sellable"] + me["tradeable_surplus"]
              if offerable(e) and e["position"] not in needs
              and team_state.clears_relevance_floor(e, thresholds)]

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


def find_efficiency_swaps(roster_entries: list[dict]) -> list[dict]:
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
        lineup = [e for e in entries if e["is_starter"]]
        bench = [e for e in entries if not e["is_starter"]]
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


def _counterparty_fit(other: dict, their_needs: dict, my_offers: list[dict]) -> dict | None:
    """What *I* hold that would interest this particular owner, or None if nothing obvious.

    **The gap this closes.** Persuasion targets were ranked purely on production-per-cost and
    never looked at the other side of the table. On a live roster that put a 1.54x back at the
    top, held by the one team in the league with **no needs at all** - unattainable - above a
    1.37x back whose owner had a *critical* need for the exact quarterback the asking team
    could not play. Every fact was computed; nothing joined them.

    Two ways an owner is interested, and the second matters as much as the first:

    1. **He is short at a position I can offer.** The obvious case.
    2. **He should be converting aging production, and I hold what he'd convert into.** A team
       contending now *and* tilting ascending (the `_cliff_case` shape) does not need a
       position - it needs value that scores this season *and* is still there later. Players
       carrying both a real redraft price and a non-declining future are exactly that. Missing
       this reads "he needs nothing, so there's no deal", when the deal is the whole reason
       his aging starter showed up on the list.

    Deliberately **annotation, not ranking.** Cheap targets from teams already selling need no
    persuasion at all, and re-sorting this tier by fit would push those low-friction options
    down in favour of a bigger ask. Two orderings inside one list is a mistake this module has
    already made once."""
    offers = [e for e in my_offers if e["position"] in their_needs]
    if offers:
        positions = sorted({e["position"] for e in offers})
        return {"you_could_offer": [e["name"] for e in offers[:3]],
                "why_it_fits": (f"{other['owner']} has a "
                                f"{their_needs[positions[0]]['level']} need at "
                                f"{'/'.join(positions)}, which you can fill from your own "
                                f"spare pieces - so this is a two-way conversation rather "
                                f"than asking him to do you a favour.")}

    if other["ascending_pct"] > other["declining_pct"]:
        # Scores now and is still there later. "Not declining" was the wrong test for the
        # second half: it passed a 28.7-year-old receiver whose runway to his own decline
        # cutoff was **0.3 years**, offered as a piece that would "still be there in two".
        # `years_to_decline` is the number the sentence is actually claiming, so use it.
        # Above replacement, not merely non-zero. `_my_offer_pool` already separates "core
        # piece - above replacement, scarce" from "depth - a sweetener not a centerpiece",
        # and only the first is a piece worth restructuring a roster around. Without this the
        # list padded itself out to three names with a 33-redraft tight end, offered as value
        # that "scores this season".
        both = sorted((e for e in my_offers
                       if e.get("value_over_replacement", 0) > 0
                       and (e.get("redraft_value") or 0) > 0
                       and (e.get("years_to_decline") or 0) >= MIN_RUNWAY_FOR_LATER),
                      key=lambda e: -(e.get("redraft_value") or 0))
        if both:
            return {"you_could_offer": [e["name"] for e in both[:3]],
                    "why_it_fits": (f"{other['owner']} has no positional hole, so there is "
                                    f"nothing to fill - but his roster is tilting ascending "
                                    f"({other['ascending_pct']}% against "
                                    f"{other['declining_pct']}% declining) while he starts "
                                    f"aging players, so what he wants is value that scores "
                                    f"this season and is still there in two. These carry both "
                                    f"a current price and a future, which is the trade he "
                                    f"should be making anyway.")}
    return None


def _persuasion_targets(me: dict, states: list[dict], my_needs: dict, thresholds: dict[str, float],
                        trade_counts: dict[str, int], prior: dict[str, dict],
                        premium_bars: dict[str, float],
                        needs_by_owner_id: dict | None = None,
                        my_offers: list[dict] | None = None) -> list[dict]:
    """Aging production held by teams that **aren't sellers yet** but could be talked into
    it - the tier `_buy_path` structurally cannot see, because it only searches `Rebuild`
    teams.

    That blind spot is large. A real Push team needing RB was offered Rachaad White (449
    redraft) and Tony Pollard (697), while Jonathan Taylor (6,649) and Saquon Barkley
    (5,081) sat on a contender that is the most steeply falling team in the league. A 15x
    gap in current production, hidden by a binary seller/non-seller split.

    Three deliberate choices, each of which was a trap when scoping this:

    1. **Sourced from `sellable`, not `win_now_core`.** The latter is gated on the
       cornerstone threshold (4,289 in that league), so it holds Taylor (5,240) and drops
       Barkley (3,746) - the same roster's *better* target. The output would have looked
       perfectly reasonable while missing the best name available.
    2. **Ranked by current production per unit of trade cost**, not by value. The normal
       buy path sorts by dynasty value descending, which is backwards for a win-now buyer:
       it puts Taylor (1.27x) above Barkley (1.36x) when Barkley delivers more production
       per unit paid *and* costs 1,494 less outright. At 29.5 the market discounts Barkley
       for seasons a pushing team isn't buying, and that discount is the entire point -
       the same arbitrage `find_efficiency_swaps` exploits within a roster, applied to
       acquisitions.
    3. **Implausible sellers are excluded, not ranked last.** The two best ratios in that
       league (Derrick Henry 1.55x, Christian McCaffrey 1.48x) sit on a reigning champion
       and the league's best team. Listing them would put unattainable names at the top and
       make the feature worse than no feature.
    """
    plausible = []
    for other in _others(states, me, NOT_SELLER):  # sellers are the normal buy path's job
        team_why = _seller_case(other, prior.get(other["owner_id"]))
        for player in other["sellable"]:
            pos, need = player["position"], my_needs.get(player["position"])
            if need is None or not player.get("redraft_value"):
                continue
            if not team_state.clears_relevance_floor(player, thresholds):
                continue
            if need["level"] == "weak" and player["redraft_value"] <= need["weakest_starter"]:
                continue
            ratio = player["redraft_value"] / player["value"]
            if ratio < premium_bars.get(pos, float("inf")):
                continue
            # The team's reason if it has one, otherwise a mismatch between the owner's
            # window and this player's. A team-level trajectory is an average, and an
            # average hides the individual: the case that forced this was a Contend/steady
            # team reading 16% declining - diluted by a genuinely young core - while
            # starting a 32-year-old RB the market prices at 1.54x. Without a per-player
            # fallback that name is unreachable, because the team gate rejects the whole
            # roster before any player on it is examined.
            why = team_why or _cliff_case(player, other, ratio)
            if why is None:
                continue
            fit = _counterparty_fit(other, (needs_by_owner_id or {}).get(other["owner_id"], {}),
                                    my_offers or [])
            plausible.append({
                "position": pos,
                "need_level": need["level"],
                **(fit or {}),
                **_with_trade_note(player, other, trade_counts),
                "production_per_cost": round(ratio, 2),
                "why_they_might_listen": why,
                # Two different asks, and they were being described identically. Where the
                # owner has a hole this team can fill, the trade *serves* his existing plan -
                # calling that "persuading them to change direction" contradicted the
                # `why_it_fits` line printed beside it. Where there is no hole, the pivot
                # framing is right and the price really is above market.
                "cost_note": (
                    f"{other['owner']} has a need you can fill, so this need not be a change "
                    f"of direction for him - it can be a straight trade that serves both "
                    f"plans. He is still not shopping {player['name']}, so you are opening "
                    f"the conversation and should expect to pay something for that, but this "
                    f"is nearer a fit than a pitch."
                    if fit and "need at" in (fit.get("why_it_fits") or "") else
                    f"{other['owner']} is not currently a seller, so this is a conversation "
                    f"rather than a fit: acquiring {player['name']} means persuading them to "
                    f"change direction, which is a commitment on their part and prices above "
                    f"market. Treat it as an option worth opening, not a deal that's there."
                ),
            })
    plausible.sort(key=lambda t: -t["production_per_cost"])
    return plausible


DEPTH_NOTE = (
    "DEPTH, NOT NEEDS. Each of these is a player who does not crack this lineup today but "
    "would step straight into it if one starter at his position were out - which byes "
    "guarantee and injuries make likely. They are listed because every one of them sits "
    "BELOW the trade-relevance floor, meaning they are cheap by definition and invisible to "
    "the buy targets above. Treat them as sweeteners and insurance: worth a late pick or a "
    "spare body, never worth a real asset, and never a substitute for filling an actual "
    "need. Cheapest first, because at this tier price is the entire point."
)

# Same cheap bodies, a different reason to want them. For a contender they are insurance
# against a lineup he is trying to protect; for a rebuilder there is no lineup worth
# protecting, and the value is that a back who inherits a starting job becomes a real asset
# to sell. The manager who made this point put it as "one injury to a starter away from
# relevance" - which is an upside argument, not a depth argument, and the note has to say
# which or it recommends the right players for a reason that doesn't apply.
DEPTH_NOTE_REBUILD = (
    "LOTTERY TICKETS, NOT INSURANCE. Each of these would start for this team if a player "
    "above him were out - but this team is not protecting a lineup, so that is not the "
    "point. The point is that a body who inherits a starting role becomes a genuinely "
    "sellable asset, and at this price he costs a late pick to hold. Cheap upside on a "
    "roster whose whole plan is accumulating it. Still never worth a real asset."
)

DEPTH_LIMIT = 6


def _depth_adds(me_roster: dict, ctx, states: list[dict], thresholds: dict[str, float],
                my_starters: set[str], already: set[str]) -> list[dict]:
    """Cheap bodies on rebuilding rosters who would start for me if one player above them
    went down. The complement of `_buy_path`, not an extension of it.

    **Sourced from below the relevance floor on purpose.** That floor is what makes a player
    a real trade target, so everything here failed it and is therefore invisible to the buy
    path by construction - the two lists cannot overlap or compete. The live case that
    forced this missed by *3 dynasty points* on a roster only two deep at his position, and
    a rule that answers "not worth trading for" to a body that would start next week is
    wrong in a way no threshold tuning fixes.

    Needs are binary and that is the gap: a position is a hole or it is fine, so a team
    starting five receivers and a team starting three look identical at WR once both are
    filled, even though only one of them is a single absence from an empty slot. Depth is a
    third state, and deliberately a weak one - the note tells the caller not to overpay,
    because the failure mode here is paying real value for insurance."""
    # Excluding my own roster is not incidental: a rebuilding team searching rebuilding teams
    # includes itself, and the first live run cheerfully advised its owner to acquire two
    # players he already had. It only surfaced once the *asking* team was a rebuilder, which
    # is exactly the case neither of the development leagues could produce.
    rebuilders = {s["owner_id"]: s["owner"] for s in states
                  if s["window"] == "Rebuild" and s["owner_id"] != me_roster["owner_id"]}
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
            if team_state.clears_relevance_floor(entry, thresholds):
                continue  # a real trade target - the buy path already owns him
            if not roster_needs.would_start_if_one_out(me_roster, ctx.players, player_id,
                                                      my_starters, ctx.lineup_dedicated,
                                                      ctx.lineup_flex):
                continue
            adds.append({"name": info["name"], "position": info["position"],
                         "value": info["value"], "redraft_value": info.get("redraft_value"),
                         "age": info.get("age"), "bucket": entry["bucket"],
                         "from_owner": owner,
                         "note": (f"Would start for you if your weakest {info['position']} "
                                  f"were out. Below the trade-relevance floor "
                                  f"({round(thresholds[info['position']]):,} at "
                                  f"{info['position']}), so the price should be nominal.")})
    adds.sort(key=lambda a: a["value"])
    return adds[:DEPTH_LIMIT]


def _conversion_candidates(me: dict, premium_bars: dict[str, float]) -> list[dict]:
    """`_cliff_case` turned around and pointed at your own roster: the aging starters whose
    remaining seasons don't reach the ones your roster is built for.

    Deliberately the same rule read from the other side, not a second heuristic. If the
    league's other managers are told your 32-year-old RB is the one piece worth calling you
    about, you should be told the same thing about him, in the same terms - two rules would
    guarantee they eventually disagreed."""
    return [{**player,
             "production_per_cost": round(player["redraft_value"] / player["value"], 2),
             "note": (f"Still starting for you and still producing, but priced at "
                      f"{player['redraft_value'] / player['value']:.2f}x his own trade "
                      f"value - the market is paying for this season and writing off the "
                      f"rest, which is the season your roster is least short of.")}
            for player in me["sellable"]
            if player.get("redraft_value") and player.get("value")
            and player["redraft_value"] / player["value"] >= premium_bars.get(player["position"], float("inf"))
            and _cliff_case(player, me, player["redraft_value"] / player["value"])]


def _seller_case(other: dict, prior: dict | None) -> str | None:
    """Why this non-selling *team* might listen, or None if nothing about the team says it
    would. A None here is no longer the end of the search - see `_cliff_case`."""
    same_team = bool(prior and prior["describes_this_team"])
    if other["trajectory"] == "falling":
        base = (f"Their roster is falling - {other['declining_pct']}% of their current "
                f"production comes from declining players, against {other['ascending_pct']}% "
                f"ascending. Aging out is the one thing that turns a contender into a seller.")
        if same_team and not prior["made_playoffs"]:
            return (f"{base} And it hasn't delivered: {prior['note']}")
        return base
    if same_team and not prior["made_playoffs"]:
        return (f"This core hasn't won with them. {prior['note']} A team that missed with "
                f"the same roster it still has is more open to changing course than the "
                f"standings alone suggest.")
    return None


def _cliff_case(player: dict, other: dict, ratio: float) -> str | None:
    """Why an owner whose team looks fine might still move one aging starter: **their
    window and the player's don't line up**. Not "he's old" - old is only half of it.

    A team whose production is tilting ascending is set up to contend for years, and the
    seasons this player still has aren't the seasons it's built for. His current output is
    real, so keeping him isn't a mistake - it might be what tips this year. But it's the
    one asset whose price is highest to a team that needs *now* and lowest to its owner's
    actual plan, which is the whole basis for the ask.

    The tilt is what makes this specific rather than universal, and the live pair proves
    it. Two Contend teams, same 32-year-old-RB profile: one at 26% ascending against 16%
    declining, the other at 21% against 23%. The first is contending now *and* later, so
    the RB is surplus to a future arriving without him. The second is contending now and
    aging into it - that RB is aligned with its window, and it would be right to keep him.

    The other two conditions keep it honest:

    - **Declining and starting.** A declining player on the bench is just a bad asset, not
      a conversation - his owner has already stopped relying on him, so there's nothing to
      talk him out of.
    - **Now-weighted for his own position** (`team_values.now_premium_bar`). The first
      version of this used an absolute 1.25 on the raw ratio, which was a bug rather than a
      strict setting: TE and WR pools top out at 1.01 and 1.07, so no TE or WR could ever
      clear it in any league. Ranked within position instead, a 36.9-year-old TE at 0.83
      raw is the second most now-weighted declining starter in the league.

    Note this bar is about *shape*, not quality - it says the market prices him for now
    rather than later, not that he's any good. "Too far gone to want" is an absolute
    question, and `clears_relevance_floor` has already answered it above.

    Two things this deliberately does NOT do. It doesn't check whether the owner has a
    replacement behind him - that's `find_efficiency_swaps`, which answers "should they do
    this?", where this tier only answers "is this worth asking?", and `cost_note` already
    says an ask is all it is. And it no longer special-cases a reigning champion: that veto
    existed to stop exactly the aging-contender case the tilt now rejects on its merits,
    and a title says less about whether an owner should sell than the shape of their roster
    does. A champion tilting ascending is a team that can afford to sell, trophy or not."""
    if not (player["bucket"] == "declining" and player["is_starter"]):
        return None
    if other["ascending_pct"] <= other["declining_pct"]:
        return None
    return (f"Nothing about {other['owner']}'s team says seller, but their window and "
            f"{player['name']}'s don't line up: {other['ascending_pct']}% of their "
            f"production is ascending against {other['declining_pct']}% declining, so "
            f"they're built for seasons he won't be part of. He produces {ratio:.2f}x his "
            f"own trade value - still starting, priced as though his remaining years are "
            f"gone. Keeping him may well tip this season for them, which is exactly why "
            f"it's a real decision for them rather than a giveaway.")


def _buy_path(me: dict, states: list[dict], needs_by_owner_id: dict, thresholds: dict[str, float],
              trade_counts: dict[str, int], max_per_position: int,
              pick_values: dict[str, int] | None = None,
              my_picks: list[dict] | None = None,
              prior: dict[str, dict] | None = None,
              premium_bars: dict[str, float] | None = None,
              covered: dict[str, float] | None = None) -> dict:
    """The push case: fill needs with sellable value from Rebuilding teams."""
    my_needs = needs_by_owner_id.get(me["owner_id"], {})
    # Worst-shaped need first (roster_needs.NEED_PRIORITY): a position you can't field at
    # all outranks one you can field but only badly. A real recommendation should exhaust
    # the urgent gap before suggesting upgrades somewhere merely mediocre.
    ordered_positions = sorted(
        my_needs, key=lambda p: roster_needs.NEED_PRIORITY[my_needs[p]["level"]])

    # Everything this team could realistically put on the table - the denominator for
    # `cost_share`. Offers plus the picks it should be moving anyway.
    my_capital = (sum(e["value"] for e in _my_offer_pool(me, thresholds, my_needs, pick_values, covered))
                  + sum(p["value"] for p in (my_picks or [])))

    targets = []
    for pos in ordered_positions:
        need = my_needs[pos]
        # A `weak` position already has the slots filled - the problem is that the group
        # is bad. Anyone who wouldn't displace the current worst starter is not a fix for
        # it, however cheap. Count-shaped needs (critical/top-heavy) have an empty slot to
        # fill, so any relevant body helps there.
        # None for count-shaped needs: there's an empty slot, so any relevant body helps
        # and a player without a redraft price shouldn't be excluded for lacking one.
        upgrade_bar = need["weakest_starter"] if need["level"] == "weak" else None
        pos_targets = []
        for other in _others(states, me, IS_SELLER):
            for player in other["sellable"]:
                if player["position"] != pos or not team_state.clears_relevance_floor(player, thresholds):
                    continue
                if upgrade_bar is not None and (player.get("redraft_value") or 0) <= upgrade_bar:
                    continue
                # The two things a reader uses to tell a realistic target from an
                # aspirational one, and neither was here. `production_per_cost` is the same
                # efficiency measure the persuasion tier already reports - a buy list that
                # omitted it left an elite player and a cheap one looking equivalent.
                # `cost_share` is blunter and answers "can I actually pay": his price as a
                # fraction of everything this team could put on the table. A player at 64% of
                # a roster's entire tradeable value is technically available and practically
                # not, which is a different statement from "expensive".
                ratio = ((player.get("redraft_value") or 0) / player["value"]
                         if player.get("value") else None)
                pos_targets.append({"position": pos, "need_level": need["level"],
                                     "need_note": need["note"],
                                     "production_per_cost": round(ratio, 2) if ratio else None,
                                     "cost_share": (round(100 * player["value"] / my_capital)
                                                    if my_capital else None),
                                     **_with_trade_note(player, other, trade_counts)})
        # Window fit before raw value. A Win-Now buyer wants *current production*, and
        # this project's own pricing model says declining players are "production-priced"
        # while prime ones are "upside-priced, may cost more than the fit justifies" -
        # their value bakes in future growth a win-now team isn't buying. Sorting only by
        # (trade activity, value) contradicted that: a real Win-Now team was handed six
        # buy targets, every one of them prime, and none of the cheaper production it
        # actually needed. Rebuilding/Middling buyers keep the old ordering, since they
        # have no reason to prefer aging players.
        # Only a *closing* window justifies preferring aging production. A `Contend`
        # team is good and not declining, so it has no reason to buy the shorter asset,
        # and an `Ascend` team least of all.
        # Trade history is a *flag*, not a ranking. It used to sort ahead of value, which
        # meant how often an owner trades decided which players you were shown: a real
        # league's #1 RB recommendation produced 738 redraft, from the most active trader,
        # while the second-best current production available (1,883) sat 5th and off the
        # end of the default list because its owner had never made a trade. Activity says
        # something about whether a call gets returned - it says nothing about whether the
        # player helps - so it drops to a last-resort tiebreak and stays visible as
        # `from_owner_trades` ("NEVER TRADES" in the text output).
        #
        # And rank on the metric the window is actually buying. Sorting a Push team's
        # targets by dynasty value contradicts the line above it, which puts declining
        # players first *because* current production is the point; dynasty value then
        # reorders them by the future years that team isn't buying.
        # Production first, age second. "Declining" used to be the *hard first key* for a
        # pushing team, on the reasoning that declining players are production-priced while
        # prime ones carry an upside premium. That reasoning is about **price per unit of
        # production**, and implementing it as an absolute ordering meant any declining player
        # outranked every prime one however little he produced: a real Push team with a WR
        # need was shown Jauan Jennings at 70 redraft above Chris Olave at 3,439 - a 49x gap -
        # and the default cap of three then hid Olave entirely.
        #
        # A buyer wants production; among equal production he should prefer the cheaper,
        # shorter asset. So age drops to a tiebreak beneath the thing actually being bought.
        prefer_production = me["window"] == "Push"
        pos_targets.sort(key=lambda t: (
            -(t.get("redraft_value") or 0) if prefer_production else -t["value"],
            0 if (prefer_production and t["bucket"] == "declining") else 1,
            -t["from_owner_trades"],
        ))
        targets += pos_targets[:max_per_position]

    result = {"needs": my_needs, "targets": targets,
              "my_offers": _my_offer_pool(me, thresholds, my_needs, pick_values, covered)}

    # Sellers-only search misses the best available production - see _persuasion_targets.
    stretch = _persuasion_targets(me, states, my_needs, thresholds, trade_counts, prior or {},
                                  premium_bars or {}, needs_by_owner_id, result["my_offers"])
    if stretch:
        result["persuasion_targets"] = stretch[:max_per_position * 2]
        result["persuasion_note"] = PERSUASION_NOTE

    # Picks are *currency*, not production. A first doesn't help you win - it becomes a
    # rookie at the next offseason's draft, and a rookie is another upside asset, which is
    # the opposite of what a contender needs. The value to a win-now team is entirely in
    # what a pick can be traded *for*: it's the cleanest thing to pay with, since it costs
    # no roster spot and carries no injury risk, and it's worth more to the rebuilder
    # receiving it than to the contender holding it. Framing this as "converting future
    # into now" was wrong - the conversion only happens in a trade.
    if my_picks is not None:
        result["picks_to_trade_away"] = my_picks
    # Only meaningful for a team actually trying to win now - a rebuilding team wants
    # the future premium it would be selling.
    # Push and Contend both, for different reasons - see SWAP_FRAMING. Not Ascend or
    # Rebuild: those want the future premium this converts away.
    #
    # Worth knowing what this does NOT catch. It requires a replacement already on the
    # roster producing >=90%, so it finds "sell the premium, promote the backup" and is
    # blind to "sell an aging starter you have no replacement for". A real Contend team
    # starts a 33-year-old RB1 and a 37-year-old TE with nothing behind either; both are
    # obvious sell-high candidates and neither can surface here. Logged in LOGIC.md as a
    # separate age-cliff path rather than stretched to fit.
    if me["window"] in ("Push", "Contend"):
        swaps = find_efficiency_swaps(me["sellable"] + me["tradeable_surplus"])
        # Suppressed only at *count-shaped* needs (critical / top-heavy), where there's an
        # empty starting slot: promoting the backup fills it but spends the last body you
        # had, so you end up no deeper and one asset lighter.
        #
        # Allowed at a `weak` need, which an earlier blanket rule got wrong. A weak
        # position has its slots covered and wants a better starter - and that is exactly
        # what this raises capital for, at flat production, since the swap retains >=90% by
        # construction. Suppressing it there silenced the only two swaps in a real league:
        # a Contend team weak at QB and TE could free 564 and 380 while its lineup barely
        # moved, which is capital pointed straight at the upgrade it needs.
        #
        # The offer pool excludes need positions as a blanket rule, so a swap target at a
        # `weak` need does re-enter `my_offers` below - carrying `swap_note`, which is the
        # documented exception rather than a contradiction.
        swaps = [s for s in swaps
                 if my_needs.get(s["position"], {}).get("level") not in ("critical", "top-heavy")]
        if swaps:
            result["efficiency_swaps"] = swaps
            result["efficiency_swap_framing"] = SWAP_FRAMING[me["window"]]

            # These contradicted each other before: the swap named a player to sell while
            # the offer list excluded him for being a starter, so the single most
            # efficient chip on the roster never appeared among the things to offer. A
            # swap target *is* offerable by definition - that's the whole finding - so it
            # joins the pool, flagged with what it costs and what replaces him.
            offered = {e["name"] for e in result["my_offers"]}
            by_name = {e["name"]: e for e in me["sellable"] + me["tradeable_surplus"]}
            for swap in swaps:
                entry = by_name.get(swap["sell"])
                if entry is None or swap["sell"] in offered:
                    continue
                result["my_offers"].append({
                    **entry,
                    "value_over_replacement": round(entry["value"] - thresholds[entry["position"]]),
                    "tier": "core piece - startable, but replaceable at little cost this season",
                    "pick_equivalent": pick_equivalent(entry["value"], pick_values) if pick_values else None,
                    "swap_note": swap["note"],
                    # Same field the offer pool attaches, so both routes into this list
                    # answer "what does moving him cost my lineup" in the same units. The
                    # swap's own >=90% guarantee is measured pairwise within a position;
                    # this is measured against the whole refilled lineup, so they can differ
                    # and the concrete number is the more useful one to state.
                    **({"lineup_cost": round(covered[entry["name"]])}
                       if covered and entry["name"] in covered else {}),
                })
            result["my_offers"].sort(key=lambda e: -e["value_over_replacement"])
    return result


def _pivot_path(me: dict, states: list[dict], thresholds: dict[str, float], trade_counts: dict[str, int],
                picks_by_owner: dict[int, list[dict]] | None = None,
                stranded: list[dict] | None = None) -> dict:
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
    # Most now-weighted first, not most valuable first. A seller is converting present into
    # future, so the right order is how much of a player's price is present - the same
    # `redraft / dynasty` reading `_persuasion_targets` buys on, read from the selling side.
    # Sorting by dynasty value put a 31-year-old QB priced at 1.36x (i.e. the market paying
    # for this season and writing off the rest, on a team with no this-season) *below* a
    # 25-year-old receiver who is exactly the kind of asset a rebuild should keep.
    # Players with no redraft price sort last: unknown, not zero.
    situational.sort(key=lambda e: -((e.get("redraft_value") or 0) / e["value"]) if e["value"] else 0)
    acquire_targets = []
    for other in _others(states, me, NOT_SELLER):
        for player in other["tradeable_surplus"]:
            if not team_state.clears_relevance_floor(player, thresholds):
                continue
            acquire_targets.append(_with_trade_note(player, other, trade_counts))
    # Value first, activity as the tiebreak - see the buy path for why trade history
    # ranking ahead of value hid the best available player behind a chatty owner.
    acquire_targets.sort(key=lambda t: (-t["value"], -t["from_owner_trades"]))
    result = {"sell_candidates": sell_candidates, "situational": situational,
              "acquire_targets": acquire_targets}
    if stranded:
        result["stranded"] = stranded
        result["stranded_note"] = STRANDED_NOTE

    # The mirror: a rebuilding team wants picks, and the teams holding them that
    # *shouldn't* want them are the contenders - a Win-Now team's future 1st is worth
    # more to a rebuilder than to its owner. Listed with who holds it and how often that
    # owner actually trades, same as player targets.
    if picks_by_owner:
        pick_targets = []
        for other in _others(states, me, NOT_SELLER):
            for pick in picks_by_owner.get(other["roster_id"], []):
                pick_targets.append({
                    **pick, "from_owner": other["owner"],
                    "from_owner_trades": trade_counts.get(other["owner_id"], 0),
                    "note": f"{other['owner']} is in {other['window']} mode - future picks are worth less to them than to you",
                })
        pick_targets.sort(key=lambda t: (-t["value"], -t["from_owner_trades"]))
        if pick_targets:
            result["picks_to_acquire"] = pick_targets[:8]
    return result


def find_targets(league_id: str, owner_query: str, max_per_position: int = DEFAULT_MAX_PER_POSITION) -> dict:
    states = team_state.classify_league(league_id)
    needs_by_owner_id = roster_needs.league_needs(league_id)
    ctx = context(league_id)
    thresholds = ctx.trade_thresholds
    # Per-position, because dynasty and redraft are unnormalized scales whose relationship
    # differs by position - see team_values.now_premium_bar for why an absolute bar is a bug.
    premium_bars = now_premium_bar(ctx.players, NOW_PREMIUM_PERCENTILE)
    trade_counts = trade_activity.get_trade_counts(league_id)
    prior = prior_season.results(league_id)
    pick_values = fantasycalc.get_pick_values(ctx.fmt["num_qbs"], ctx.num_teams,
                                              ctx.fmt["ppr"], ctx.fmt["is_dynasty"])
    # Keyed on the window, which is where a pick's slot actually comes from: how good the
    # originating team finishes. Contending teams pick late, rebuilders early.
    strategy_by_roster = {r["roster_id"]: r["window"] for r in states}
    picks_by_owner = owned_picks(league_id, int(ctx.league["season"]),
                                 ctx.league["settings"]["draft_rounds"],
                                 [r["roster_id"] for r in ctx.rosters], pick_values,
                                 strategy_by_roster)

    me = ctx.pick_owner(owner_query, states)

    # What each of my starters actually costs to lose, after the lineup refills itself.
    # A zero means the bench covers him for free, which is the real question behind the
    # old "never offer a starter" rule - see _my_offer_pool.
    my_roster = ctx.roster_for(owner_query)
    my_starters = ctx.starters_for(my_roster)
    covered = {ctx.players[pid]["name"]: roster_needs.production_lost_without(
                   my_roster, ctx.players, pid, my_starters,
                   ctx.lineup_dedicated, ctx.lineup_flex)
               for pid in my_starters if pid in ctx.players}

    # A rebuilding team (especially one tanking for a pick) isn't trying to fill
    # starting-lineup needs with proven vets - it wants to sell what age value it has
    # left and stockpile youth/picks instead. Buy-target-by-need only makes sense for
    # a team actually trying to win now.
    my_picks = picks_by_owner.get(me["roster_id"], [])

    # Computed once, before the window dispatch, because every one of these applies in every
    # window and the previous placement (inside the buy branch only) meant a Rebuild team got
    # neither. That team had the worst RB room in its league and six qualifying cheap bodies
    # available - and cheap bodies are arguably worth MORE to a rebuilder, since a moonshot
    # back is one injury away from being a real asset and costs a late pick to hold.
    depth = _depth_adds(my_roster, ctx, states, thresholds, my_starters,
                        set())
    def _wanted_by(position: str) -> list[dict]:
        """Which other teams are short at this position, worst shortage first.

        The mirror of `_counterparty_fit`, and the other half of the same missing join.
        `stranded` correctly said "the whole value of this player is what he fetches" and
        then left the reader to work out who would give anything for him - while
        `league_needs` had the answer sitting in the next tool result. On a live roster the
        stranded quarterback produced more than that team's entire starting RB room, and the
        one owner with a *critical* QB need also held the running back it wanted. Nothing
        connected the two."""
        wanting = []
        for other in _others(states, me, lambda w: True):
            need = needs_by_owner_id.get(other["owner_id"], {}).get(position)
            if need:
                wanting.append({"owner": other["owner"], "window": other["window"],
                                "need_level": need["level"], "rank": need.get("rank")})
        return sorted(wanting, key=lambda w: roster_needs.NEED_PRIORITY[w["need_level"]])

    stranded_ids = roster_needs.stranded_starters(my_roster, ctx.players, my_starters)
    by_name = {e["name"]: e for e in me["sellable"] + me["tradeable_surplus"]}
    stranded = []
    for player_id in stranded_ids:
        info = ctx.players[player_id]
        entry = by_name.get(info["name"], {"name": info["name"], "position": info["position"],
                                           "value": info["value"],
                                           "redraft_value": info.get("redraft_value")})
        wanted = _wanted_by(info["position"])
        stranded.append({**entry, "blocked_by": info["position"],
                         "wanted_by": wanted,
                         "note": (f"Produces {info.get('redraft_value') or 0:,} this season - more than "
                                  f"the weakest player in your lineup - but cannot be started: "
                                  f"this roster has no slot left for another {info['position']}."
                                  + (f" {len(wanted)} team(s) are short at {info['position']}: "
                                     + ", ".join(f"{w['owner']} ({w['need_level']})" for w in wanted[:3])
                                     + " - start there."
                                     if wanted else
                                     f" No team in this league currently needs a "
                                     f"{info['position']}, so he will be hard to move at "
                                     f"anything like his value."))})

    def with_extras(result: dict) -> dict:
        if depth:
            result["depth_adds"] = depth
            result["depth_note"] = (DEPTH_NOTE_REBUILD if me["window"] == "Rebuild"
                                    else DEPTH_NOTE)
        return result

    if me["window"] == "Rebuild":
        return with_extras({"me": me, "mode": "rebuild",
                            **_pivot_path(me, states, thresholds, trade_counts, picks_by_owner,
                                          stranded)})

    if me["window"] == "Ascend":
        # Hasn't committed to a direction - show what pushing looks like AND what
        # pivoting looks like, rather than silently picking one. Whichever path
        # actually makes sense usually depends on something we don't have yet (the
        # season record - a Middling team two games out of a playoff spot should push,
        # one that's clearly out should pivot even mid-season) - logged in LOGIC.md.
        return with_extras({"me": me, "mode": "ascend", "timing_note": ASCEND_TIMING_NOTE,
                "push": _buy_path(me, states, needs_by_owner_id, thresholds, trade_counts,
                                  max_per_position, pick_values, my_picks, prior,
                                  premium_bars, covered),
                "pivot": _pivot_path(me, states, thresholds, trade_counts, picks_by_owner,
                                     stranded)})

    result = {"me": me, "mode": "buy",
              **_buy_path(me, states, needs_by_owner_id, thresholds, trade_counts,
                          max_per_position, pick_values, my_picks, prior,
                          premium_bars, covered)}

    result = with_extras(result)
    if stranded:
        result["stranded"] = stranded
        result["stranded_note"] = STRANDED_NOTE

    # Additive on purpose - `window` is untouched. A contender tilting ascending contends
    # whichever path it takes, so the label is right either way and only the tactics differ.
    # Making `window` plural here would have been the more "honest" shape and the wrong one:
    # it reads as a decision about whether to compete, which this team has already made.
    conversions = _conversion_candidates(me, premium_bars)
    if conversions:
        result["choice_note"] = CONTEND_CHOICE_NOTE
        result["conversion_candidates"] = conversions
    return result




# How lopsided the two sides of a mutual swap may be before it stops being a realistic
# proposal. Both sides are startable-quality depth by construction, so this is a sanity
# bound, not a fairness calculator (that's a separate, harder problem - see the module
# docstring). 0.6 means the lighter side must be worth at least 60% of the heavier one:
# loose enough that a genuine need premium still goes through, tight enough that nobody
# gets shown "your RB3 for their QB5".
MIN_SWAP_BALANCE = 0.6


def _swap_is_balanced(receive: list[dict], send: list[dict]) -> bool:
    got, gave = sum(e["value"] for e in receive), sum(e["value"] for e in send)
    if not got or not gave:
        return False
    return min(got, gave) / max(got, gave) >= MIN_SWAP_BALANCE


def _fills(entries: list[dict], need: dict | None) -> list[dict]:
    """The subset of `entries` that actually addresses `need` - empty if it doesn't, or
    if there's no need there at all. A count-shaped need (critical/top-heavy) has an open
    slot, so any usable body fills it; a `weak` one is already covered and only improves
    if the incoming player beats the current worst starter."""
    if not need:
        return []
    if need["level"] != "weak":
        return entries
    return [e for e in entries if (e.get("redraft_value") or 0) > need["weakest_starter"]]


def find_mutual_swaps(league_id: str, owner_query: str) -> dict:
    """Two-way trades between teams both still trying to win: each side has a
    positional surplus (real spare depth, from roster_needs.league_surplus) that
    happens to be the other side's need, so both teams fix a hole without touching
    their own starters or their long-term core. The buy/pivot paths above only ever
    match a Win-Now/Middling team against a Rebuilding team's sell candidates - they
    never consider two competing teams trading with each other, which misses a common
    and realistic trade shape a pure rebuild-vs-contend model can't produce."""
    ctx = context(league_id)
    states = team_state.classify_league(league_id)
    needs_by_owner = roster_needs.league_needs(league_id)
    surplus_by_owner = roster_needs.league_surplus(league_id)

    me = ctx.pick_owner(owner_query, states)

    if me["window"] not in SWAP_ELIGIBLE_WINDOWS:
        # A Rebuilding team isn't trying to fix a starting lineup right now - it's
        # selling current value for youth, which is the pivot path above, not this.
        return {"me": me, "swaps": []}

    my_needs = needs_by_owner.get(me["owner_id"], {})
    my_surplus = surplus_by_owner.get(me["owner_id"], {})

    swaps = []
    for other in _others(states, me, STILL_COMPETING):
        other_needs = needs_by_owner.get(other["owner_id"], {})
        other_surplus = surplus_by_owner.get(other["owner_id"], {})
        for need_pos, other_surplus_entries in other_surplus.items():
            if need_pos not in my_needs:
                continue
            # Their spare depth only fixes a `weak` position if it actually beats what's
            # already starting there - a weak group has the slots filled and wants an
            # upgrade. Without this the swap list offered a fringe backup as the cure for
            # a bottom-third room, which is churn dressed up as a fit.
            incoming = _fills(other_surplus_entries, my_needs[need_pos])
            if not incoming:
                continue
            for their_need_pos, my_surplus_entries in my_surplus.items():
                outgoing = _fills(my_surplus_entries, other_needs.get(their_need_pos))
                if outgoing and _swap_is_balanced(incoming, outgoing):
                    swaps.append({
                        "with_owner": other["owner"],
                        "fills_your_need_at": need_pos,
                        "you_receive": incoming,
                        "fills_their_need_at": their_need_pos,
                        "you_send": outgoing,
                        "balance": {
                            "you_receive_value": sum(e["value"] for e in incoming),
                            "you_send_value": sum(e["value"] for e in outgoing),
                            "note": "Both sides are spare startable depth of comparable "
                                    "value - this is a shape that could work, not a "
                                    "priced offer. Check it against the market before sending.",
                        },
                    })
    swaps.sort(key=lambda s: -(s["balance"]["you_receive_value"] + s["balance"]["you_send_value"]))
    return {"me": me, "swaps": swaps}


def offerable_names(result: dict) -> set[str]:
    """Every player name this team could reasonably be told to trade away, across
    whichever path(s) find_targets returned for its mode. Single source of truth for
    "is this a real give-up piece" - used by agent.py's post-hoc grounding check so
    that check never has to re-derive the mode-specific logic above itself."""
    if result["mode"] == "rebuild":
        return {e["name"] for e in result["sell_candidates"] + result["situational"]}
    if result["mode"] == "ascend":
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
    if pivot.get("picks_to_acquire"):
        print("picks to ask about (worth less to a contender than to you):")
        for t in pivot["picks_to_acquire"][:5]:
            trade_note = f"{t['from_owner_trades']} trade(s)" if t["from_owner_trades"] else "NEVER TRADES"
            print(f"  {t['pick']} (value={t['value']}) from {t['from_owner']} - {trade_note}")
    print("acquire targets (young ascending surplus sitting on Win-Now/Middling rosters):")
    for t in pivot["acquire_targets"]:
        trade_note = f"{t['from_owner_trades']} trade(s) made" if t["from_owner_trades"] else "NEVER TRADES - unlikely"
        print(f"  {t['name']} ({t['position']}, value={t['value']}) from {t['from_owner']} - {trade_note}")


def _needs_summary(needs: dict) -> str:
    return ", ".join(f"{pos} ({e['level']}, {e['rank']}/{e['of']})" for pos, e in needs.items()) or "none"


def _print_push(push: dict) -> None:
    for pos, entry in push["needs"].items():
        print(f"  need at {pos}: {entry['note']}")
    if push["my_offers"]:
        print("you could offer (most value over replacement first):")
        for e in push["my_offers"]:
            cost = OFFER_GIVE_UP_COST[team_state.VALUE_BASIS[e["bucket"]]]
            print(f"  {e['name']} ({e['position']}, value={e['value']}, "
                  f"{e['value_over_replacement']:+} vs replacement) - give-up cost: {cost}")
    else:
        print("you could offer: no obvious surplus")
    if push.get("picks_to_trade_away"):
        picks = ", ".join(f"{p['pick']} ({p['value']})" for p in push["picks_to_trade_away"][:4])
        print(f"picks to pay with (currency for buying production, not production itself): {picks}")
    print()
    if not push["targets"]:
        print("no obvious targets found (no needs, or no Rebuilding team has a sell candidate there)")
        return
    if push.get("persuasion_targets"):
        print()
        print("harder asks (aging production on teams that are NOT selling yet):")
        for t in push["persuasion_targets"]:
            print(f"  {t['name']} ({t['position']}, {t['production_per_cost']}x production "
                  f"per unit of cost - dyn {t['value']:,} / redraft {t['redraft_value']:,}) "
                  f"from {t['from_owner']}")
            print(f"      why they might listen: {t['why_they_might_listen']}")
        print()
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

    if result["mode"] == "ascend":
        print(f"{me['owner']}: Ascend - can push now or arrive cheaper next season")
        print(f"  {me['window_note']}")
        print(f"\n  {result['timing_note']}")
        print(f"\n-- if pushing (needs: {_needs_summary(result['push']['needs'])}) --")
        _print_push(result["push"])
        print("\n-- if pivoting --")
        _print_pivot(me, result["pivot"])
        return

    print(f"{me['owner']}: {me['window']}, needs: {_needs_summary(result['needs'])}")
    print(f"  {me['window_note']}")
    _print_push(result)


def _print_swaps(swaps: list[dict]) -> None:
    if not swaps:
        print("no mutual swap fits found")
        return
    print("mutual swaps (both sides fix a different need, no core piece touched):")
    for s in swaps:
        receive = ", ".join(e["name"] for e in s["you_receive"])
        send = ", ".join(e["name"] for e in s["you_send"])
        b = s["balance"]
        print(f"  with {s['with_owner']}: you get {receive} ({s['fills_your_need_at']} need, "
              f"{b['you_receive_value']}) for {send} ({s['fills_their_need_at']} need for "
              f"them, {b['you_send_value']})")


def main(league_id: str, owner_query: str = None, max_per_position: int = DEFAULT_MAX_PER_POSITION) -> None:
    if owner_query:
        result = find_targets(league_id, owner_query, max_per_position)
        _print_report(result)
        if result["mode"] in ("buy", "ascend"):
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
