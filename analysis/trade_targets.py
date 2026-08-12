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

# The choice a Middling team is actually facing, stated rather than left implicit. Showing
# both paths without it - which is what the old "Middling" mode did - hands over two lists
# and no basis for choosing between them.
#
# **Two versions, because the middle tier is no longer only rising teams.** The original text
# is an argument for patience built entirely on the roster supplying next season's production
# by itself. Handed to a falling team it contradicted its own window note two lines above -
# "pushing later will not be cheaper than pushing now" followed immediately by "waiting is the
# cheaper default" - which is the drift this file keeps producing when one string is written
# for one case and then reused for another.
MIDDLING_TIMING_NOTE_RISING = (
    "TIMING: both paths are shown because both are live, but they cost differently. "
    "Pushing now means buying current production at market price - and this roster's own "
    "ascending players are scheduled to supply that production next season for free, so a "
    "push is paying a premium for one extra year of contention. Waiting is the cheaper "
    "default. Push anyway when the price is below market (a seller who has to move a "
    "piece), when the gap to the top team is small enough that one addition closes it, or "
    "when a need is count-shaped rather than quality-shaped - an empty starting slot costs "
    "points every week and no amount of patience fills it."
)

MIDDLING_TIMING_NOTE = (
    "TIMING: both paths are shown because both are live, and neither is free. This roster is "
    "not scheduled to improve on its own, so waiting does not lower the price of contending - "
    "it just spends a season. What waiting does buy is information: a few weeks of real "
    "results settle whether this team is closer to the top than the standings currently say, "
    "and that is a legitimate reason to hold. Push when the price is below market (a seller "
    "who has to move a piece) or when a need is count-shaped rather than quality-shaped - an "
    "empty starting slot costs points every week and no amount of patience fills it. Pivot if "
    "the season opens badly, while the aging production still prices well."
)

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

# The same runway means different things depending on whether the team has picked this
# direction. A committed seller should move the piece; a Middling team may still wait, and
# for it the clock bounds how long waiting stays free instead of ordering a sale. One string
# so the CLI line and the agent's JSON can't drift apart the way they did on `mgibbons612`.
SELL_CLOCK_COMMITTED = "value only goes down from here, real urgency to move it"
SELL_CLOCK_OPTIONAL = (
    "value only goes down from here - so these are what waiting costs, and the deadline on "
    "deciding. This team has NOT committed to selling: the clock is a reason to pick a "
    "direction before the price decays, not an instruction to sell now"
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

# A parameter, not a hard limit - "give me more" means call again with a higher number.
# 5, not 3: on a real critical RB need, three slots went to two players on a team flagged
# NEVER TRADES plus one live option, and the two genuinely gettable names - an active
# trader's back and the cheap production-priced fix - fell off the end. Raising the default
# is the blunt version; scaling it by `roster_needs.NEED_PRIORITY` is the precise one, and
# wasn't worth the machinery for one number.
DEFAULT_MAX_PER_POSITION = 5

# Two bars, because "give up a little production for a lot of value" splits into two decisions
# a manager makes differently, and one threshold cannot say which is which.
#
# Above NOISE_RETAINED the lineup genuinely does not move - Smith-Njigba -> St. Brown retains
# 98.5% and releases 1,440 - so the only question is whether you want the value.
#
# Between the two bars the production loss is REAL and the trade is a conversion: Josh Allen
# -> Lamar Jackson gives up 994 to free 3,159. That is a defensible dynasty play for a team
# with no clock and the wrong answer for one pushing, so it is labelled `conversion`, kept out
# of Push, and never described as leaving the lineup alone. 0.90 is the floor because below it
# the loss stops being a conversion and is simply a worse team.
NOISE_RETAINED = 0.98
MIN_PRODUCTION_RETAINED = 0.90

UPGRADE_KIND_TAG = {
    "upgrade": "",
    "value_decision": " [value decision - lineup unchanged]",
    "conversion": " [CONVERSION - gives up real production for value]",
}

# And how much dynasty value has to come back for the trade to be worth mentioning at all -
# below this it's churn, not arbitrage.
MIN_VALUE_FREED = 300

# "Still there later" is the same question `team_state` asks of a cornerstone, so it uses the
# same answer - see team_values.MIN_MEANINGFUL_RUNWAY.
MIN_RUNWAY_FOR_LATER = MIN_MEANINGFUL_RUNWAY

# Windows where a team is still trying to field a winning lineup this season.
SWAP_ELIGIBLE_WINDOWS = ("Push", "Contend", "Middling")


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


# Difficulty, not availability - the same job `from_owner_trades`/"NEVER TRADES" does for a
# counterparty, one notch softer. An owner who never trades may genuinely not answer; a
# cornerstone's owner will, and will want more than market.
CORNERSTONE_ASK = ("cornerstone - the runway this roster is built around, so expect to pay "
                   "over market or be told no. Moveable, just the hardest ask here")


# ONE vocabulary for "how hard is this, and why", used on both sides of the table. An entry
# with no friction is easy; each friction entry names a `flavor` and says `why` in the reader's
# terms. Flavors rather than a difficulty score because they call for different responses - an
# inactive owner may never answer, a cornerstone's owner answers and says no, and a player above
# your best single chip is a different conversation entirely - and because they group: "these
# are good but cornerstones" is a sub-list the reader asked for, and grouping needs a label.
#
# Deliberately none of these is a price. `beyond_your_best_chip` is a one-against-one
# comparison, the only kind this project can make: it says no single piece covers him, not what
# the deal would be.
BUY_FRICTION = ("cornerstone", "beyond_your_best_chip", "never_trades", "needs_a_pivot")
SELL_FRICTION = ("cornerstone", "costs_you_production")


def _friction(flavor: str, why: str) -> dict:
    return {"flavor": flavor, "why": why}


def _why_they_would_move_him(player: dict, other: dict, prior: dict | None,
                             premium_bars: dict[str, float]) -> dict:
    """Whether the OTHER owner has a reason to part with this player - the half that was
    missing. `value_upgrades` said who would want the player I'd move and stopped there, so a
    tight end held by a contender read exactly like one held by a seller.

    Three answers, and only the first is easy. A `Rebuild` owner is already selling him. A
    non-seller might still listen, and `_seller_case`/`_cliff_case` are the same two arguments
    the persuasion tier makes - reused, not restated. Otherwise nothing about their side says
    seller, which is the honest answer and belongs in the output: *"shiv is win now and could
    choose to move off the aging value but doesn't have to."*"""
    if other["window"] == "Rebuild":
        return {"their_reason": (f"{other['owner']} is rebuilding - this is exactly the kind of "
                                 f"production they should be converting, so no persuasion needed.")}
    ratio = ((player.get("redraft_value") or 0) / player["value"]) if player.get("value") else 0
    # Same now-weighted gate `_persuasion_targets` puts in front of `_cliff_case`. Without it
    # that argument asserts he is "priced as though his remaining years are gone" about a player
    # who may not be discounted at all - a claim the entry's own numbers would contradict.
    cliff = (_cliff_case(player, other, ratio)
             if ratio >= (premium_bars or {}).get(player["position"], float("inf")) else None)
    case = _seller_case(other, (prior or {}).get(other["owner_id"])) or cliff
    if case:
        return {"their_reason": case,
                "friction": [_friction("needs_a_pivot",
                                       f"{other['owner']} is not a seller today, so this asks "
                                       f"them to change direction rather than take a fair "
                                       f"offer - a wait-and-see, not a call you make once")]}
    return {"their_reason": (f"Nothing about {other['owner']}'s team says seller, and their "
                             f"window does not argue for moving him either. They could do it "
                             f"and have no reason to, so this needs them to want your side "
                             f"more than they want him."),
            "friction": [_friction("needs_a_pivot",
                                   f"{other['owner']} has no reason to sell him at all - the "
                                   f"least likely of these to happen")]}


def _buy_friction(player: dict, other: dict, best_chip: dict | None, trades: int,
                  trades_are_informative: bool) -> dict:
    """What stands between this team and this target, or an empty list if nothing does."""
    friction = []
    if player.get("is_cornerstone"):
        friction.append(_friction("cornerstone",
                                  f"a cornerstone for {other['owner']} - they are building "
                                  f"around him, so expect a no rather than a price"))
    if best_chip and player["value"] > best_chip["value"]:
        friction.append(_friction("beyond_your_best_chip",
                                  f"costs more than your biggest single chip "
                                  f"({best_chip['name']}, {best_chip['value']:,}), so no "
                                  f"one-for-one reaches him"))
    # Only when somebody in this league HAS traded. In a league with no trade history at all,
    # a zero says nothing about this owner - it describes the league - and treating it as
    # friction would empty the buy list for all twelve teams. Same reasoning as the
    # `no_trade_history` flag `team_state` already carries.
    if trades_are_informative and not trades:
        friction.append(_friction("never_trades",
                                  f"{other['owner']} has never made a trade, so the call may "
                                  f"not be returned at all"))
    return {"friction": friction}


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
        if not e["is_starter"] or e.get("is_cornerstone"):
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
        # The same friction vocabulary the buy side uses, read from my side of the table.
        # `ask_difficulty` was a one-off string for the cornerstone case; the two reasons a
        # sale is hard are that his owner (me) is building around him, or that the lineup
        # actually loses production - and `lineup_cost` was already measuring the second.
        friction = []
        if e.get("is_cornerstone"):
            friction.append(_friction("cornerstone", CORNERSTONE_ASK))
        if e.get("lineup_cost"):
            friction.append(_friction("costs_you_production",
                                      f"moving him costs {e['lineup_cost']:,.0f} of production "
                                      f"out of your own lineup, after it refills itself"))
        e["friction"] = friction
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


LONG_SHOT_NOTE = (
    "LONG SHOTS - real fits, but something structural is in the way, and `friction` says what. "
    "These are separated from the buy list rather than ranked below it because the reason is "
    "not price: an owner who has never traded may not answer, a cornerstone's owner will answer "
    "and say no, and a player costing more than your biggest single chip cannot be reached "
    "one-for-one at all. Ring the buy list first. Raise one of these only when you already "
    "know something this tool doesn't - that the owner is about to sell, or that you are "
    "willing to open a negotiation with no single piece that covers it."
)

VALUE_UPGRADE_NOTE = (
    "BETTER TO HOLD, not a priced offer. Every one of these costs LESS in dynasty value than "
    "the starter named above him, and they come in three flavours that must not be blurred "
    "together. Unmarked: he produces MORE this season too, so this raises the lineup and frees "
    "trade value at once and the man he replaces drops to depth. `value decision`: he produces "
    "very slightly less, the lineup is effectively unchanged, and what you are buying is the "
    "value released - do it at a good price, never chase it. `CONVERSION`: he produces "
    "MEANINGFULLY less and the value freed is the entire point, which is a real trade-off, "
    "defensible with no clock and wrong if you are trying to win this season. Anything from "
    "`your own bench` needs no trade at all - promote him and sell the starter above him - so "
    "it is listed first however small the production line looks. What it takes to get him is a "
    "separate question this tool does not answer: value is not additive across players, so "
    "there is no package price here, only the observation that one holding beats another. "
    "These are usually older, which is exactly why they are cheaper - check the age against "
    "your own timeline before paying."
)


RETURNS_PER_MOVE = 4  # a shortlist of who to ask about, not the league


def _would_actually_help(player: dict, roster: dict, ctx) -> bool:
    """Does he beat what they already start at his position? An empty slot takes anybody.

    **A positional need is not the same as wanting THIS player**, and reading it that way
    listed four counterparties for a quarterback none of them would have started. He produces
    780 against a replacement level of 2,579; of the five teams short at QB, the only lineup
    he improves is the one whose second starter produces 518. His owner put it better than
    the tool did: no contender would start him and no rebuilder sees him rising - he is injury
    cover for a core, and that is the whole of it."""
    theirs = [ctx.players[pid].get("redraft_value") or 0
              for pid in ctx.starters_for(roster)
              if pid in ctx.players and ctx.players[pid]["position"] == player["position"]]
    return not theirs or (player.get("redraft_value") or 0) > min(theirs)


def wanted_by(player: dict, me_roster: dict, states: list[dict],
              needs_by_owner_id: dict, ctx=None) -> list[dict]:
    """Which other owners would want THIS player, and in one line why.

    Lifted to module level so `find_value_upgrades` and the stranded block share one
    definition. `stranded` correctly said "the whole value of this player is what he fetches"
    and then left the reader to work out who would give anything for him, while
    `league_needs` had the answer in the next tool result.

    **Two reasons, not one, and checking only the first missed the most obvious counterparty
    on a live board.** Positional need is the easy half. The other is trajectory: a roster
    whose production is falling wants *ascending* value at any position. The owner holding
    both of the best returns available to one manager - and the most active trader in that
    league - needed no tight end and no running back. He needed youth, at 4% ascending
    against 40% declining and 11th of 12 in dynasty value, and this function could not say
    so, because it only knew about needs."""
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


def _holding_kind(produced: float, costs: float, mine: dict) -> str | None:
    """Is this player a better thing to own than one of my starters at his position, and in
    which of the three senses? `None` if he isn't.

    Costing less in dynasty value is required by all three - that is what keeps this from
    collapsing into "go buy someone better", which is the buy path's question."""
    if costs >= mine["value"]:
        return None
    mine_produced = mine.get("redraft_value") or 0
    if produced > mine_produced:
        return "upgrade"
    if not mine_produced or mine["value"] - costs < MIN_VALUE_FREED:
        return None
    retained = produced / mine_produced
    if retained >= NOISE_RETAINED:
        return "value_decision"
    return "conversion" if retained >= MIN_PRODUCTION_RETAINED else None


def _upgrade_note(kind: str, produced: float, replaced: dict, freed: float, mine_side: bool) -> str:
    theirs = replaced.get("redraft_value") or 0
    action = (f"Selling {replaced['name']} and starting him instead costs nothing to arrange - "
              f"he is already yours." if mine_side else
              f"{replaced['name']} becomes depth rather than a starter.")
    if kind == "upgrade":
        return (f"Produces {produced:,} this season against {replaced['name']}'s {theirs:,}, and "
                f"costs {freed:,} less in dynasty value - strictly the better holding for a team "
                f"trying to win now. {action}")
    pct = round(produced / theirs * 100)
    if kind == "value_decision":
        return (f"Produces {produced:,} against {replaced['name']}'s {theirs:,} - {pct}% of it, so "
                f"the lineup barely moves - while costing {freed:,} less in dynasty value. A value "
                f"decision, not a lineup upgrade: neither is clearly the better start week to "
                f"week, and what you are buying is the {freed:,} released, not the points. {action}")
    return (f"Produces {produced:,} against {replaced['name']}'s {theirs:,} - {pct}%, so this "
            f"GIVES UP {theirs - produced:,} of real production this season - to free {freed:,} in "
            f"dynasty value. A conversion, not an upgrade: worth it only if you would rather own "
            f"the value than score the points, which is a defensible call with no clock and the "
            f"wrong one if you are pushing. {action}")


def find_value_upgrades(me_roster: dict, ctx, states: list[dict], my_starters: set[str],
                        trade_counts: dict[str, int], needs_by_owner_id: dict,
                        window: str = "Contend", prior: dict | None = None,
                        premium_bars: dict[str, float] | None = None) -> list[dict]:
    """Which single holding beats one of my starters at his own position, for less dynasty
    value? Candidates come from every roster in the league INCLUDING my own bench, because
    "who is a better thing to own" does not care where he currently sits - only the action
    differs, and a man already mine needs no trade at all. Three `kind`s:

    - `upgrade` - strictly more production for strictly less value. Nothing to tune.
    - `value_decision` - at least `NOISE_RETAINED` of the production. The lineup does not
      really move; the value released is the gain.
    - `conversion` - down to `MIN_PRODUCTION_RETAINED`. Real production given up for real
      value back. Defensible with no clock, wrong while pushing, so `Push` never sees these.

    Costing less in dynasty value is required by all three, which is what stops this becoming
    "go get someone better" - that question is the buy path's.

    **This absorbed a within-roster version** (`find_efficiency_swaps`, deleted) that compared
    a starter only against my own bench, and was gated to Push/Contend. Measured before
    deleting it: ~160 same-position starter/bench pairs across 12 Push/Contend teams in three
    leagues, zero qualifying. Two things were wrong and only one was the search space. Within a
    position dynasty and redraft value are correlated, so a bench player retaining the
    production is priced the same and frees nothing - which is why 673 pairs across full
    rosters yield only two. But both of those two sat on windows the gate excluded, so the one
    live case it existed for was unreachable: BradTheInhaler starts a tight end producing 353
    while T.J. Hockenson produces 331 on his bench for 1,293 less. Folding the bench in here
    reaches it, and asking the same question of eleven other rosters finds hundreds more.

    Comparisons stay one player against one player at the same position: the two value scales
    are not normalized to each other (McCaffrey runs 4,345 dynasty against 6,505 redraft while
    a mid-tier back runs 2x the other way), and only a same-position pair cancels that out.
    It deliberately does not price a package - it says which single holding beats which, and
    leaves what it takes to get him to a human.

    A candidate is matched against the *weakest* starter he beats, which is both the slot he
    would actually take and the pairing with the largest gain.

    **Organised around the player being moved, not the player being acquired.** A flat list of
    strictly-better names reads as a straight one-for-one swap, and that is not how a trade
    happens: *"I'm not trading LaPorta for Kittle alone, so I need more context to work with."*
    Each entry is a **move** - the upside-priced starter, several win-now returns that beat
    him, and which teams are short at his position and would therefore want him. That last
    join is the difference between a fact and a phone call.

    Returns are ranked within a move rather than capped across all of them. This is a
    two-dimensional finding - production gained and dynasty value freed - and one global
    ranking on either axis hides winners on the other: sorted by production and capped at six,
    it dropped the exact swap this roster's owner had already named, a tight end worth +233 of
    production but the largest value release on the board at 1,073."""
    mine_by_pos = {}
    for pid in my_starters:
        info = ctx.players.get(pid)
        if info:
            mine_by_pos.setdefault(info["position"], []).append(info)

    by_owner_id = {s["owner_id"]: s for s in states}
    upgrades = []
    for roster in ctx.rosters:
        other = by_owner_id.get(roster["owner_id"])
        if other is None:
            continue
        mine_side = roster["owner_id"] == me_roster["owner_id"]
        their_starters = ctx.starters_for(roster)
        for pid in roster["players"] or []:
            # My own starters are what's being compared against; my own BENCH is a live
            # candidate, and the cheapest one there is - no trade required, just a lineup
            # change and a sale.
            if mine_side and pid in their_starters:
                continue
            info = ctx.players.get(pid)
            if not info:
                continue
            produced = info.get("redraft_value") or 0
            kinds = {}
            for m in mine_by_pos.get(info["position"], []):
                kind = _holding_kind(produced, info["value"], m)
                if kind and not (kind == "conversion" and window == "Push"):
                    kinds[m["name"]] = (m, kind)
            if not kinds:
                continue
            # The weakest starter beaten is both the slot he'd actually take and the pairing
            # with the largest gain, so one `min` serves both.
            replaced, kind = min(kinds.values(), key=lambda mk: mk[0].get("redraft_value") or 0)
            gained = produced - (replaced.get("redraft_value") or 0)
            freed = replaced["value"] - info["value"]
            entry = {
                "name": info["name"], "position": info["position"],
                "value": info["value"], "redraft_value": produced,
                "is_starter": pid in their_starters,
                "upgrades_over": replaced["name"],
                "production_gained": gained, "value_freed": freed,
                "kind": kind, "already_mine": mine_side,
                "note": _upgrade_note(kind, produced, replaced, freed, mine_side),
                # A player already on my bench needs nobody's permission.
                **({"their_reason": "already yours - no counterparty at all"} if mine_side else
                   _why_they_would_move_him(
                       {**info, "is_starter": pid in their_starters,
                        "bucket": age_bucket(info["position"], info.get("age"),
                                             info.get("usage_role"))},
                       other, prior, premium_bars)),
            }
            # A counterparty's trade history is meaningless for a player already on my roster,
            # and `_with_trade_note` would stamp him "NEVER TRADES" off my own zero count.
            upgrades.append({**entry, "from_owner": "your own bench"} if mine_side
                            else _with_trade_note(entry, other, trade_counts))
    by_starter = {}
    for u in upgrades:
        by_starter.setdefault(u["upgrades_over"], []).append(u)

    moves = []
    for pid in my_starters:
        mine = ctx.players.get(pid)
        if not mine or mine["name"] not in by_starter:
            continue
        # A man already on my bench leads and is never truncated away. He ranks last on
        # production gained by construction - that is what makes him a conversion - but he is
        # the only option here costing no trade at all, so the shortlist cap must not be what
        # decides whether it gets mentioned. Live: BradTheInhaler starts a tight end producing
        # 353 while T.J. Hockenson produces 331 on his bench for 1,293 less dynasty value, and
        # four better external returns pushed it off the list entirely.
        ranked = sorted(by_starter[mine["name"]], key=lambda u: -u["production_gained"])
        free = [u for u in ranked if u.get("already_mine")]
        returns = free + [u for u in ranked if not u.get("already_mine")][:RETURNS_PER_MOVE]
        produced = mine.get("redraft_value") or 0
        profile = {**mine, "bucket": age_bucket(mine["position"], mine.get("age"),
                                                mine.get("usage_role"))}
        moves.append({
            "move_off": mine["name"], "position": mine["position"],
            "value": mine["value"], "redraft_value": produced,
            "bucket": profile["bucket"],
            "now_share": round(produced / mine["value"], 2) if mine["value"] else None,
            "wanted_by": wanted_by(profile, me_roster, states, needs_by_owner_id, ctx),
            "returns": returns,
            "best_gain": returns[0]["production_gained"],
        })
    return sorted(moves, key=lambda m: -m["best_gain"])


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
       the same arbitrage `find_value_upgrades` exploits, applied to ranking rather than
       to picking a single better holding.
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
    "BELOW replacement level - startable quality - meaning they are cheap by definition and "
    "not who the buy targets above are for. Price them as sweeteners: worth a late pick or a "
    "spare body, never worth a real asset. But below LEAGUE replacement is not the same as "
    "unable to help THIS lineup - each line says whether the player actually outproduces your "
    "weakest starter at the position or only covers an absence, and on a thin roster several "
    "will. Cheapest first, because at this tier price is the entire point."
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


def _depth_adds(me_roster: dict, ctx, states: list[dict], filling_lineup: bool,
                my_starters: set[str], already: set[str]) -> list[dict]:
    """Cheap bodies on rebuilding rosters who would start for me if one player above them
    went down. The complement of `_buy_path`, not an extension of it.

    **Which bar makes someone "only depth" depends on what the asking team is doing**, and
    it is the two-metric split `roster_needs.replacement_thresholds` documents: filling a
    lineup is a *redraft* question, holding a lottery ticket is a *dynasty* one.

    - Filling a lineup (Push/Contend/Middling): above replacement-level **production** and he
      is a real fix, not insurance. David Montgomery - 2,145 dynasty, 1,779 redraft against
      RB replacement of 1,708 - was filed as "never worth a real asset" on a roster whose
      second starting RB produces 633. He is a +1,146 upgrade to the weakest slot in that
      lineup. Testing dynasty value there answered a question nobody asked.
    - Rebuilding: production now is beside the point (`DEPTH_NOTE_REBUILD` - the value is a
      body who inherits a job and *becomes* sellable), so cheap-by-dynasty is the right bar.

    **Not `clears_relevance_floor` either way.** That floor is *tiered* - a production-priced
    player clears it at half - so testing it here opened a crack between the two lists rather
    than partitioning them. Tony Pollard (1,493) and Jaylen Warren (1,948) both cleared half
    of RB's 2,576 and were dropped as "the buy path already owns him", while `_buy_path`'s cap
    of three ranked them 4th and 5th on production and never showed them either. The cheapest,
    most obviously gettable help in the league was invisible in both lists - the same cap that
    once hid Chris Olave for being prime, hiding these two for being cheap.

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
    metric = "redraft_value" if filling_lineup else "value"
    bars = ctx.start_thresholds if filling_lineup else ctx.trade_thresholds
    bar_label = "replacement-level production" if filling_lineup else "the trade-value floor"
    # Below *league* replacement is not the same as "can't help me". On a roster whose second
    # RB produces 633 against a replacement level of 1,708, three players called insurance
    # were all upgrades to that slot - Warren by 511. Stated per line rather than used as a
    # bar: excluding them would push them into the buy path, where they rank below the
    # per-position cap on production and vanish entirely, which is how they were hidden
    # before. The reader needs the number, not a different list.
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
                         "note": (f"Would start for you if your weakest {info['position']} "
                                  f"were out. Below {bar_label} "
                                  f"({round(bars[info['position']]):,} at "
                                  f"{info['position']}), so the price should be nominal."
                                  + verdict)})
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
    replacement behind him - "should they do this?" is answered from their own side of the
    table, where this tier only answers "is this worth asking?", and `cost_note` already
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

    # The BIGGEST SINGLE thing this team could put on the table, not the sum of everything.
    # `cost_share` used to divide a target's price by that sum, which is the additive
    # assumption this module rejects everywhere else - it implied a roster could be stacked
    # into one offer, and 64%-of-capital read as merely expensive rather than out of reach.
    # One player against one player is the only comparison available, so that is the test:
    # a target priced above your best chip cannot be reached by any single-piece deal, and
    # what it would actually take is a negotiation this tool does not price.
    my_pool = _my_offer_pool(me, thresholds, my_needs, pick_values, covered)
    best_chip = max(my_pool, key=lambda e: e["value"], default=None)

    # `never_trades` is only ever a fact about a COUNTERPARTY - the asking team's own trade
    # history says nothing about whether it can sell, and the app's user may well have never
    # traded. So exclude self from the "does a zero mean anything here" test too: if I am the
    # only person in the league who has traded, everyone else's zero describes the league, and
    # counting it would turn every target in the league into a long shot.
    others_have_traded = any(count for owner_id, count in trade_counts.items()
                             if owner_id != me["owner_id"])

    targets, long_shots = [], []
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
                # `production_per_cost` is the same efficiency measure the persuasion tier
                # reports - without it an elite player and a cheap one look equivalent.
                ratio = ((player.get("redraft_value") or 0) / player["value"]
                         if player.get("value") else None)
                pos_targets.append({"position": pos, "need_level": need["level"],
                                     "need_note": need["note"],
                                     "production_per_cost": round(ratio, 2) if ratio else None,
                                     **_buy_friction(player, other, best_chip,
                                                     trade_counts.get(other["owner_id"], 0),
                                                     others_have_traded),
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
        # and an `Middling` team least of all.
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
        # Split, not merely sorted. One ranked list forced attainability to compete with
        # quality for the same ordering, and quality won by design: a real RB list read
        # Gibbs (a cornerstone priced above this roster's best chip), then three names from an
        # owner who has never traded, and only then Jaylen Warren - who was the one realistic
        # call on the board. The reader's question is "who do I ring first", and that is a
        # different question from "who is best", so it gets its own list. Both halves keep the
        # production-first ordering above; the cap applies per half so a blocked target can
        # never displace a reachable one.
        reachable = [t for t in pos_targets if not t["friction"]]
        blocked = [t for t in pos_targets if t["friction"]]
        targets += reachable[:max_per_position]
        long_shots += blocked[:max_per_position]

    result = {"needs": my_needs, "targets": targets, "my_offers": my_pool}
    if long_shots:
        result["long_shots"] = long_shots
        result["long_shot_note"] = LONG_SHOT_NOTE

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
    return result


def _pivot_path(me: dict, states: list[dict], thresholds: dict[str, float], trade_counts: dict[str, int],
                picks_by_owner: dict[int, list[dict]] | None = None,
                stranded: list[dict] | None = None, committed: bool = True) -> dict:
    """The sell case: cash in declining/non-core value for youth from teams that
    don't need it, same logic a Rebuilding team uses.

    Split by runway rather than one flat list - a piece inside MIN_MEANINGFUL_RUNWAY of its
    decline cutoff only loses value from here, real urgency to move it. A piece below the
    cornerstone bar with years still on it (a genuinely good player, just not elite enough to
    be this team's long-term core - e.g. a real starting-caliber WR on an already-loaded
    corps) isn't losing value on a clock, so it's a situational, take-a-fair-offer piece,
    not an urgent sell - presenting both the same way overstates how clear-cut it is.

    **Runway, not bucket** - the correction `MIN_MEANINGFUL_RUNWAY` already made in
    `team_state.classify`, which this path missed. Splitting on `bucket == "declining"` put
    Justin Jefferson - 6,828, the most valuable asset on a rebuilding roster, and 1.8 years
    from his cutoff - under "no urgency", while a 2,145 back at -2.2 read as urgent."""
    # Cornerstones are in `sellable` because they are askable (see CORNERSTONE_ASK), but they
    # do not belong in *these* two lists: `situational` means "years still on them, just not
    # your long-term core", and a cornerstone is by definition the long-term core. Putting
    # them here would print a label that contradicts itself. Where a cornerstone is genuinely
    # the piece to move, `my_offers` and `value_upgrades` are the surfaces that say so.
    real_sellable = [e for e in me["sellable"]
                     if team_state.clears_relevance_floor(e, thresholds)
                     and not e.get("is_cornerstone")]

    def on_a_clock(e):
        return (e["years_to_decline"] or 0) < MIN_MEANINGFUL_RUNWAY

    sell_candidates = [e for e in real_sellable if on_a_clock(e)]
    situational = [e for e in real_sellable if not on_a_clock(e)]
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
              "acquire_targets": acquire_targets,
              "sell_clock_note": SELL_CLOCK_COMMITTED if committed else SELL_CLOCK_OPTIONAL}
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

    # Depth applies in every window - it used to be computed inside the buy branch only, so a
    # Rebuild team got none of it, and cheap bodies are arguably worth MORE to a rebuilder
    # since a moonshot back is one injury from being a real asset. It is built in
    # `with_extras` rather than here, because it has to know what the buy path surfaced.
    stranded_ids = roster_needs.stranded_starters(my_roster, ctx.players, my_starters)
    by_name = {e["name"]: e for e in me["sellable"] + me["tradeable_surplus"]}
    stranded = []
    for player_id in stranded_ids:
        info = ctx.players[player_id]
        entry = by_name.get(info["name"], {"name": info["name"], "position": info["position"],
                                           "value": info["value"],
                                           "redraft_value": info.get("redraft_value")})
        wanted = wanted_by(entry, my_roster, states, needs_by_owner_id, ctx)
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
        # Computed here, not earlier, so the buy path's actual output can be excluded. The
        # two lists are documented as partitioning the space and stopped doing it once they
        # started testing different metrics - depth against replacement-level *production*,
        # the buy path against the *dynasty* relevance floor. Tony Pollard and Jaylen Warren
        # cleared one and failed the other, so they came back in both lists at once: named as
        # live targets at a critical need, and simultaneously as bodies "never worth a real
        # asset". `already` existed for this and was being handed an empty set.
        surfaced = {t["name"] for t in result.get("targets") or []}
        surfaced |= {t["name"] for t in (result.get("push") or {}).get("targets") or []}
        # Not for a rebuilder: "strictly better to hold if you're winning now" is advice for
        # someone who is. Every other window can buy production, including Middling, whose
        # whole push half is about whether to.
        if me["window"] != "Rebuild":
            upgrades = find_value_upgrades(my_roster, ctx, states, my_starters, trade_counts,
                                           needs_by_owner_id, me["window"], prior, premium_bars)
            if upgrades:
                result["value_upgrades"] = upgrades
                result["value_upgrade_note"] = VALUE_UPGRADE_NOTE
        depth = _depth_adds(my_roster, ctx, states, me["window"] != "Rebuild",
                            my_starters, surfaced)
        if depth:
            result["depth_adds"] = depth
            result["depth_note"] = (DEPTH_NOTE_REBUILD if me["window"] == "Rebuild"
                                    else DEPTH_NOTE)
        return result

    if me["window"] == "Rebuild":
        return with_extras({"me": me, "mode": "rebuild",
                            **_pivot_path(me, states, thresholds, trade_counts, picks_by_owner,
                                          stranded)})

    if me["window"] == "Middling":
        # Hasn't committed to a direction - show what pushing looks like AND what
        # pivoting looks like, rather than silently picking one. Whichever path
        # actually makes sense usually depends on something we don't have yet (the
        # season record - a Middling team two games out of a playoff spot should push,
        # one that's clearly out should pivot even mid-season) - logged in LOGIC.md.
        timing = (MIDDLING_TIMING_NOTE_RISING if me["trajectory"] == "rising"
                  else MIDDLING_TIMING_NOTE)
        return with_extras({"me": me, "mode": "middling", "timing_note": timing,
                "push": _buy_path(me, states, needs_by_owner_id, thresholds, trade_counts,
                                  max_per_position, pick_values, my_picks, prior,
                                  premium_bars, covered),
                "pivot": _pivot_path(me, states, thresholds, trade_counts, picks_by_owner,
                                     stranded, committed=False)})

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
    print(f"sell candidates (under {MIN_MEANINGFUL_RUNWAY:g} years before decline): {sell}")
    print(f"  {pivot['sell_clock_note']}")
    situational = ", ".join(e["name"] for e in pivot["situational"]) or "none"
    print(f"situational pieces (years still on them, just not your long-term core - take a fair "
          f"offer, no urgency): {situational}")
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


def _print_push(push: dict, extras: dict) -> None:
    """`extras` is the top-level result, which is where `depth_adds` lives - for Middling,
    `push` is only the pushing half and doesn't carry it."""
    for pos, entry in push["needs"].items():
        print(f"  need at {pos}: {entry['note']}")
    if push["my_offers"]:
        print("you could offer (most value over replacement first):")
        for e in push["my_offers"]:
            cost = OFFER_GIVE_UP_COST[team_state.VALUE_BASIS[e["bucket"]]]
            flavors = (" [" + ", ".join(f["flavor"] for f in e["friction"]) + "]"
                       if e["friction"] else "")
            print(f"  {e['name']} ({e['position']}, value={e['value']}, "
                  f"{e['value_over_replacement']:+} vs replacement) - "
                  f"give-up cost: {cost}{flavors}")
            for f in e["friction"]:
                print(f"      - {f['why']}")
    else:
        print("you could offer: no obvious surplus")
    if push.get("picks_to_trade_away"):
        picks = ", ".join(f"{p['pick']} ({p['value']})" for p in push["picks_to_trade_away"][:4])
        print(f"picks to pay with (currency for buying production, not production itself): {picks}")
    print()
    # Cheapest and most gettable first, escalating to the long shots. The old order led with
    # "harder asks" - teams that aren't selling - and buried the nominal-price depth below two
    # targets stamped NEVER TRADES. Read top-down, it recommended the least likely moves first.
    _print_value_upgrades(extras)
    _print_depth(extras)
    if push["targets"]:
        print("BUY TARGETS - ring these first (from Rebuilding teams, at a position you need):")
        for t in push["targets"]:
            trade_note = f"{t['from_owner_trades']} trade(s) made" if t["from_owner_trades"] else "no trades yet"
            price_note = BUY_PRICE_NOTE[team_state.VALUE_BASIS[t["bucket"]]]
            print(f"  {t['name']} ({t['position']}, value={t['value']}, {price_note}) from "
                  f"{t['from_owner']} - need: {t['need_level']} - {trade_note}")
    else:
        print("no reachable targets found (no needs, no Rebuilding team is selling there, or "
              "everything available is a long shot below)")
    if push.get("long_shots"):
        print("\nlong shots (real fits, but something is in the way - see why on each):")
        for t in push["long_shots"]:
            price_note = BUY_PRICE_NOTE[team_state.VALUE_BASIS[t["bucket"]]]
            print(f"  {t['name']} ({t['position']}, value={t['value']}, {price_note}) from "
                  f"{t['from_owner']} - need: {t['need_level']}")
            for f in t["friction"]:
                print(f"      - [{f['flavor']}] {f['why']}")
        print(f"  {push['long_shot_note']}")
    if push.get("persuasion_targets"):
        print()
        print("harder asks (aging production on teams that are NOT selling yet - each of these "
              "is asking a team to change direction, not take a fair offer):")
        for t in push["persuasion_targets"]:
            print(f"  {t['name']} ({t['position']}, {t['production_per_cost']}x production "
                  f"per unit of cost - dyn {t['value']:,} / redraft {t['redraft_value']:,}) "
                  f"from {t['from_owner']}")
            print(f"      why they might listen: {t['why_they_might_listen']}")


def _print_report(result: dict) -> None:
    me = result["me"]

    if result["mode"] == "rebuild":
        tank_note = "" if me["owns_next_first"] else " (doesn't own next 1st, so tanking for a pick wouldn't help)"
        print(f"{me['owner']}: Rebuilding{tank_note} - playing for future value, not starting-lineup needs")
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


def _print_value_upgrades(result: dict) -> None:
    if not result.get("value_upgrades"):
        return
    print("\nbetter things to own than what you start now (more or equal production, less "
          "value tied up):")
    for m in result["value_upgrades"]:
        # "only 0.52 of his price is now" is the point for an upside-priced starter and
        # nonsense above 1.0, where the market is already paying for the present.
        pricing = (f"upside-priced, only {m['now_share']} of his dynasty price is production now"
                   if (m["now_share"] or 0) < 1 else
                   f"already now-priced at {m['now_share']}, so this is a straight upgrade")
        print(f"  move off {m['move_off']} ({m['position']}, {m['value']:,} dynasty / "
              f"{m['redraft_value']:,} this season - {pricing}):")
        if m["wanted_by"]:
            print("      who would want him:")
            for w in m["wanted_by"][:3]:
                print(f"        {w['owner']} [{w['window']}] - {w['why']}")
        else:
            print("      who would want him: nobody obvious - no team is short here and "
                  "nobody's roster is crying out for this profile")
        for u in m["returns"]:
            if u.get("already_mine"):
                trades = "NO TRADE NEEDED - promote him and sell the man above"
            else:
                trades = (f"{u['from_owner_trades']} trade(s)" if u["from_owner_trades"]
                          else "NEVER TRADES")
            print(f"      <- {u['name']} ({u['redraft_value']:,} this season, "
                  f"{u['production_gained']:+,} production, {u['value_freed']:,} dynasty "
                  f"freed){UPGRADE_KIND_TAG[u['kind']]} from {u['from_owner']} - {trades}")
            # Half the trade was missing: who wants MY guy was printed, why THEIRS would be
            # available was computed nowhere. A tight end held by a contender read exactly
            # like one held by a seller.
            if u.get("their_reason"):
                print(f"           why they'd move him: {u['their_reason']}")
    print(f"  {result['value_upgrade_note']}")


def _print_depth(result: dict) -> None:
    """Was computed for every window and printed for none - `depth_adds` reached the agent
    over MCP but never the CLI the author spot-checks with."""
    if not result.get("depth_adds"):
        return
    print("\ncheap depth (cheapest first - nominal price, never worth a real asset):")
    for a in result["depth_adds"]:
        edge = a.get("over_weakest_starter")
        edge_note = ("" if edge is None else
                     f", {edge:+,} vs your weakest {a['position']} starter")
        print(f"  {a['name']} ({a['position']}, value={a['value']}, "
              f"{a['redraft_value'] or 0:,} this season{edge_note}) from {a['from_owner']}")
    print(f"  {result['depth_note']}")


def main(league_id: str, owner_query: str = None, max_per_position: int = DEFAULT_MAX_PER_POSITION) -> None:
    if owner_query:
        result = find_targets(league_id, owner_query, max_per_position)
        _print_report(result)
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
