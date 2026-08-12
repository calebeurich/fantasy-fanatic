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
                          priced_for, ordinal,
                          MIN_MEANINGFUL_RUNWAY, INSIDE_FINAL_YEAR)

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
    "starters named above for value that matches the seasons the rest of the roster is built "
    "for - and note that this is the same list every other manager in the league is being "
    "handed as the reason to call you. Both are defensible; the cost is that stacking spends "
    "future value on a lead this team already has, while converting gives up real "
    "production this season for a roster that stays strong longer. Neither is urgent - a "
    "contender with no clock can wait for a good price rather than chase one."
)

# The same runway means different things depending on whether the team has picked this
# direction. A committed seller should move the piece; a Middling team may still wait, and
# for it the clock bounds how long waiting stays free instead of ordering a sale. One string
# so the CLI line and the agent's JSON can't drift apart the way they did on `mgibbons612`.
SELL_CLOCK_COMMITTED = "value only goes down from here, real urgency to move it"

# Selling your own cornerstone is one action with two entirely different meanings, and which
# one applies is the `committed` distinction this path already carries. On a team that has
# picked its direction it is a defined move - the hardest ask on the roster, but coherent. On a
# MIDDLING team it is not a move at all, it is the choice: converting the core is what going
# one way rather than the other consists of. That is the thing worth surfacing to a middling
# team above everything else, so it is stated as a decision rather than as a sell instruction.
CORNERSTONE_SELL = {
    True: ("cornerstone - the runway this roster is built around, so expect to pay over market "
           "or be told no if you are on the other side of it. Moveable, and the hardest ask "
           "here: only sell him for what actually shortens the rebuild, never for a fair price "
           "on paper"),
    False: ("cornerstone - and for a team that has NOT picked a direction this is not one move "
            "among others, it IS the choice. Converting him is what committing to the future "
            "consists of; keeping him is what committing to now consists of. Decide the "
            "direction first, then this answers itself - do not trade him to find out"),
}
SELL_CLOCK_OPTIONAL = (
    "value only goes down from here - so these are what waiting costs, and the deadline on "
    "deciding. This team has NOT committed to selling: the clock is a reason to pick a "
    "direction before the price decays, not an instruction to sell now"
)

STRANDED_NOTE = (
    "STRANDED PRODUCTION - the most valuable thing this roster owns that it cannot use. "
    "Each of these produces at least DOUBLE what the weakest player in the starting lineup "
    "does, while every slot he is eligible for is held by someone better still - so the gap "
    "is not depth waiting its turn, it is a starter's worth of scoring the lineup can never "
    "collect. A body only marginally better than the worst starter is ordinary depth and "
    "belongs in the depth list instead. That makes their entire value "
    "to this team whatever they fetch in a trade, which is true whichever direction the team "
    "is heading: a contender should convert one into the position it is short at, a "
    "rebuilder into futures. Lead with these before anything else in the sell lists - "
    "holding them costs a starting slot's worth of production every week and fixes nothing."
)

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

# NOISE_RETAINED as a band rather than a floor, so the same 2% means the same thing on both
# axes and in both directions. Three separate near-miss bugs came from having it on one side
# only:
#
# - **Value axis.** Requiring *strictly* less dynasty value made a real finding flicker between
#   refreshes: DeVonta Smith at 3,619 against Rashee Rice at 3,616 produces 535 more this season
#   for a price identical to three decimal places.
# - **Production axis, gains.** `NOISE_RETAINED` caught noise when production DROPPED and nothing
#   caught it when production rose, so Jaylen Waddle at **+3 of 2,572 (+0.12%)** was called
#   "strictly the better holding for a team trying to win now". The lineup does not move at +3;
#   the finding is the 408 of dynasty value freed, which is what `value_decision` already says.
#
# Only `upgrade` gets the band on the value side: where the released value is the entire point
# (`value_decision`, `conversion`) it has to actually be released, and MIN_VALUE_FREED says how
# much.
NOISE_BAND = 1 - NOISE_RETAINED

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


def MIGHT_SELL(window: str) -> bool:
    """Teams the buy path is allowed to look at - see `_sells_him` for which of their players."""
    return window in ("Rebuild", "Middling")


def _sells_him(other: dict, player: dict) -> bool:
    """Is this owner a seller **of this player**? Seller-ness is a property of the pair, not of
    the team, and treating it as a team fact hid the best target on the board.

    A `Rebuild` team is selling everything. A **Middling team that is rising** is selling its
    aging pieces and nothing else - it is accumulating the seasons those players will not be
    there for. That is not a new heuristic, it is the `rising` flavor already computed, read
    per player instead of per roster.

    Live: James Cook, 6,027 of production at 1.21x production per unit of cost - the best
    win-now RB reachable by a team whose critical need is RB - sat behind a `window == "Rebuild"`
    test because kbmckenna is Middling. He is 0.1 years from his cutoff on a roster 47%
    ascending against 0% declining. Travis Etienne is the same shape on bigbuttboi at 55%
    against 8%. Both read as "harder asks" needing persuasion, when the owner's own direction
    is the argument for the trade.

    The clock is what keeps this honest: a rising team's young core is emphatically NOT for
    sale, and without the runway test this would offer Cook's owner up for his own ascending
    players. `INSIDE_FINAL_YEAR` rather than `MIN_MEANINGFUL_RUNWAY`, because 2.0 years is a
    buyer's planning horizon and on the RB curve it means "any RB over 25"."""
    if other["window"] == "Rebuild":
        return True
    return (other["window"] == "Middling" and other.get("trajectory") == "rising"
            and (player.get("years_to_decline") or 0) < INSIDE_FINAL_YEAR)


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
                             premium_bars: dict[str, float], never_trades: bool = False,
                             fills_a_hole: bool = False) -> dict:
    """Whether the OTHER owner has a reason to part with this player - the half that was
    missing. `value_upgrades` said who would want the player I'd move and stopped there, so a
    tight end held by a contender read exactly like one held by a seller.

    Three answers, and only the first is easy. A `Rebuild` owner is already selling him. A
    non-seller might still listen, and `_seller_case`/`_cliff_case` are the same two arguments
    the persuasion tier makes - reused, not restated. Otherwise nothing about their side says
    seller, which is the honest answer and belongs in the output: *"shiv is win now and could
    choose to move off the aging value but doesn't have to."*

    **`never_trades` overrides all three**, because it is a different kind of fact. The other
    three are arguments about what an owner *should* want; this one is evidence about what he
    actually does. Reading only the window, this said "no persuasion needed" about a player
    whose owner has never made a trade - while the same report's long-shot list said the call
    might not be returned at all. One report, one player, opposite claims.

    **`fills_a_hole` is what decides the pivot, and it has to come from the caller** because
    it is a fact about *my* roster against theirs, not about this player. This used to tag
    `needs_a_pivot` on any non-rebuilder, while `_persuasion_targets` defined the same flavor
    as "no hole of theirs I can fill" - two definitions of one named concept in one module.
    They then disagreed in print about the same player in the same run: Travis Kelce on
    Paulyt101 read `needs_a_pivot: False` / "nearer a fit than a pitch" in the persuasion
    block and `needs_a_pivot` in the upgrade block. `_counterparty_fit` is the single test."""
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
    # The bar decides one clause inside the case, not whether the case exists - see `_cliff_case`.
    # Used as a gate it made the existence of any reason at all turn on the fourth decimal place.
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
                   covered: dict[str, float] | None = None,
                   backfills: dict[str, dict] | None = None) -> list[dict]:
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
        # A cost is only friction if the lineup actually notices. Reuses `NOISE_RETAINED` - the
        # same "the lineup barely moves" bar `find_value_upgrades` uses - rather than inventing a
        # threshold: moving Harold Fannin costs 122 of 38,467 and leaves 99.7% standing, while
        # Lamar Jackson costs 6,043 and leaves 84%. Grouping those as one kind of hard put the
        # cheapest real chip on the roster behind the most expensive.
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
        # The trade-off in one line, in both currencies, which is what the owner asked for:
        # "seeing if the prod lost is anywhere close to the value realized from moving an asset
        # priced for youth." Fannin frees 3,688 of dynasty value for 122 of production, because
        # Metcalf takes the FLEX. Naming the replacement is what turns 122 from an arbitrary
        # number into the argument.
        if backfills.get(e["name"]):
            bf = backfills[e["name"]]
            e["backfill"] = bf
            e["trade_off"] = (f"frees {e['value']:,} of dynasty value for "
                              f"{cost_now:,.0f} of production, because {bf['name']} "
                              f"({bf['position']}, {bf['redraft_value']:,}) steps in")
        e["value_over_replacement"] = round(e["value"] - thresholds[e["position"]])
        e["tier"] = ("core piece - above replacement, scarce" if e["value_over_replacement"] > 0
                     else "depth - real but discounted, a sweetener not a centerpiece")
        # A pick equivalent makes the tier concrete. "Worth 947" means nothing to a
        # manager; "about a 2027 3rd (Late)" is immediately legible, and lands on the
        # right intuition - a depth piece is a late-pick-shaped asset, not a centerpiece.
        if pick_values:
            e["pick_equivalent"] = pick_equivalent(e["value"], pick_values)
    # Friction decides order, then value over replacement. Sorted on value alone this list led
    # with Lamar Jackson - a cornerstone costing 6,119 of production - and buried Tyler Shough,
    # a bench QB3 with no friction at all, in third. The reader's eye landed on the one move the
    # owner had already ruled out. Same principle the buy list got when it split off long shots:
    # "who is biggest" is not "who should I move".
    offers.sort(key=lambda e: (bool(e["friction"]), -e["value_over_replacement"]))
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

    Not costing MORE in dynasty value is required by all three - that is what keeps this from
    collapsing into "go buy someone better", which is the buy path's question. For `upgrade`
    that allows `NOISE_BAND`, because two prices inside the band are the same price and a strict
    comparison made a real finding flicker between refreshes.

    **`upgrade` also has to CLEAR the band on production, not merely exceed it.** A gain of +3
    against 2,572 is not a better lineup, it is the same lineup, and calling it "strictly the
    better holding" while `value_decision` sat unused said something false about a real trade."""
    mine_produced = mine.get("redraft_value") or 0
    if (produced > mine_produced * (1 + NOISE_BAND)
            and costs <= mine["value"] * (1 + NOISE_BAND)):
        return "upgrade"
    if costs >= mine["value"]:
        return None
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
        # `freed` can now be very slightly negative - NOISE_BAND admits an upgrade priced inside
        # the noise band, and "costs -3 less" is not a sentence.
        price = (f"costs {freed:,} less in dynasty value" if freed > 0 else
                 f"costs the same in dynasty value, inside the {abs(freed):,} that separates them")
        return (f"Produces {produced:,} this season against {replaced['name']}'s {theirs:,}, and "
                f"{price} - strictly the better holding for a team trying to win now. {action}")
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
                        premium_bars: dict[str, float] | None = None,
                        already_surfaced: set[str] | None = None,
                        my_offers: list[dict] | None = None) -> list[dict]:
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
    # Rank within position on each scale, which is what decides whether a starter is really
    # upside-priced - see `team_values.priced_for` for why the raw ratio cannot answer it.
    pricing = priced_for(ctx.players)
    mine_by_pos = {}
    for pid in my_starters:
        info = ctx.players.get(pid)
        if info:
            mine_by_pos.setdefault(info["position"], []).append(info)

    by_owner_id = {s["owner_id"]: s for s in states}
    # Self excluded: `never_trades` is only ever a fact about a counterparty.
    others_have_traded = any(n for oid, n in trade_counts.items()
                             if oid != me_roster["owner_id"])
    upgrades = []
    for roster in ctx.rosters:
        other = by_owner_id.get(roster["owner_id"])
        if other is None:
            continue
        mine_side = roster["owner_id"] == me_roster["owner_id"]
        their_starters = ctx.starters_for(roster)
        # Whether an ask is a fit or a pivot is a fact about the OWNER, not about each player,
        # so it is resolved once per roster off the same `_counterparty_fit` the persuasion
        # tier uses rather than re-decided per candidate.
        hole = bool((_counterparty_fit(other, (needs_by_owner_id or {}).get(roster["owner_id"], {}),
                                      my_offers or []) or {}).get("fills_a_hole"))
        for pid in roster["players"] or []:
            # My own starters are what's being compared against; my own BENCH is a live
            # candidate, and the cheapest one there is - no trade required, just a lineup
            # change and a sale.
            if mine_side and pid in their_starters:
                continue
            info = ctx.players.get(pid)
            if not info:
                continue
            # The buy path already named him, at a position it knows this roster needs, and it
            # says the more useful thing: what he beats the weakest starter there by, plus the
            # need level. Re-deriving him here as "a better thing to own" is the same call in
            # weaker words. Live: Tony Pollard as a critical-need RB buy AND as the return for
            # a 638-production bench RB, worth +54 of production and 6% of his value.
            if not mine_side and info["name"] in (already_surfaced or set()):
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
                       other, prior, premium_bars,
                       never_trades=others_have_traded
                       and not trade_counts.get(other["owner_id"], 0),
                       fills_a_hole=hole)),
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
            **({"priced_for": pricing[pid]} if pid in pricing else {}),
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
                # The one fact that decides whether an ask is a fit or a pivot, so it is
                # returned as data. Callers used to recover it by sniffing `why_it_fits` for the
                # substring "need at" - a label test standing in for the thing itself.
                "fills_a_hole": True,
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
        # **Runway ranks this pool, it no longer empties it.** Requiring
        # `>= MIN_RUNWAY_FOR_LATER` to be offered at all made a hard line out of a continuous
        # measure, and 24% of the league's starters sit within a year of it: CeeDee Lamb at 1.6
        # was unofferable to a rising team, and DK Metcalf at 0.3 unofferable to anyone without
        # a positional hole, though both are real pieces a young roster would take. Past his own
        # cliff is still excluded - there the claim would be false rather than weaker.
        both = sorted((e for e in my_offers
                       if e.get("value_over_replacement", 0) > 0
                       and (e.get("redraft_value") or 0) > 0
                       and (e.get("years_to_decline") or 0) >= 0),
                      # Friction last, then the longest runway, then most current production.
                      # Runway leads because it is what this branch is selling - "still there
                      # later" - where `_my_offer_pool` sorts the same pool for a buyer of now.
                      key=lambda e: (bool(e.get("friction")),
                                     -(e.get("years_to_decline") or 0),
                                     -(e.get("redraft_value") or 0)))
        if both:
            # The bar now decides what the sentence claims. Offered a 0.3-runway receiver, it
            # used to promise a piece "still there in two", which his own number contradicted.
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
    # Same two counterparty facts the buy list checks. `my_offers` is already passed in for the
    # `you_could_offer` join, so the biggest single chip is a field read, not a new argument.
    best_chip = max(my_offers or [], key=lambda e: e["value"], default=None)
    others_have_traded = any(n for oid, n in trade_counts.items() if oid != me["owner_id"])
    plausible = []
    for other in _others(states, me, NOT_SELLER):  # sellers are the normal buy path's job
        team_why = _seller_case(other, prior.get(other["owner_id"]))
        for player in other["sellable"]:
            pos, need = player["position"], my_needs.get(player["position"])
            if need is None or not player.get("redraft_value"):
                continue
            if not team_state.clears_relevance_floor(player, thresholds):
                continue
            # A rising Middling team's aging pieces are the buy path's job now (`_sells_him`),
            # and this tier is for players whose owner has to be talked into it. Without this
            # they appear twice, once as a target and once as a harder ask.
            if _sells_him(other, player):
                continue
            # **The floor applies at every need level, not just `weak`.** Asking an owner to
            # change direction for a player who would not crack your lineup is not a trade, and
            # this tier's own note promises "aging production the market discounts age on". A
            # critical need used to skip the check on the theory that any body helps - which is
            # what `depth_adds` is for, at a nominal price and no persuasion. Live: Tyrone Tracy
            # at 255 of production and 0.18x production per unit of cost, listed under that note
            # against a weakest RB starter of 638, alongside Jordan Mason (390) and Blake Corum
            # (581). At 0.18x the market is pricing him far ABOVE what he produces - the exact
            # opposite of the discount the block exists to harvest.
            if player["redraft_value"] <= need["weakest_starter"]:
                continue
            ratio = player["redraft_value"] / player["value"]
            # **Runway defines this tier, not the price ratio.** It is "aging production held by
            # a non-seller", and the project's canonical test for aging is the clock, not a
            # market ratio - the same `MIN_MEANINGFUL_RUNWAY` correction already made in
            # `classify` and `_pivot_path`. Gating on the ratio instead let age in by accident
            # in both directions: Travis Etienne (27.6, runway -0.6, a +1,578 upgrade on the
            # asking team's RB2) was unreachable at 0.85 against a 1.05 bar, while dropping the
            # bar alone admitted Bijan Robinson and Ashton Jeanty with 2.5 and 4.3 years left.
            # The bar keeps `_cliff_case`'s "priced as though his remaining years are gone"
            # sentence honest, which is a different job and where it now lives.
            if (player.get("years_to_decline") or 0) >= MIN_MEANINGFUL_RUNWAY:
                continue
            # The team's reason if it has one, otherwise a mismatch between the owner's
            # window and this player's. A team-level trajectory is an average, and an
            # average hides the individual: the case that forced this was a Contend/steady
            # team reading 16% declining - diluted by a genuinely young core - while
            # starting a 32-year-old RB the market prices at 1.54x. Without a per-player
            # fallback that name is unreachable, because the team gate rejects the whole
            # roster before any player on it is examined.
            # **The now-premium bar belongs to the cliff argument, not to the tier.** It gated
            # every candidate before `team_why` was even consulted, and `_seller_case` makes no
            # claim about pricing - "their roster is falling" and "this core hasn't won with
            # them" are facts about the TEAM. Only `_cliff_case` asserts a player is "priced as
            # though his remaining years are gone", and that is the sentence the bar keeps
            # honest. Live cost of the over-gate: Travis Etienne, a 27.6-year-old declining
            # starter on a Middling team that missed the playoffs at 4-10 with 94% of the same
            # roster, produces 2,221 - a +1,578 upgrade on the asking team's RB2 and more than
            # double its best listed target - was unreachable at ratio 0.85 against a 1.05 bar,
            # while his own teammate Achane qualified at exactly 1.05 off the same team case.
            why = _cliff_case(player, other, ratio, team_case=team_why,
                              discounted=ratio >= premium_bars.get(pos,
                                                                   float("inf"))) or team_why
            if why is None:
                continue
            fit = _counterparty_fit(other, (needs_by_owner_id or {}).get(other["owner_id"], {}),
                                    my_offers or [])
            # This tier is "hard" by construction and carried no `friction` at all, so it was
            # invisible to anything that groups or orders by difficulty - 108 entries across
            # three leagues reading as hard-but-unlabelled. It is not one difficulty either:
            # `cost_note` below already splits "he has a hole you fill" from a real pivot ask,
            # and the friction reuses that split rather than restating it.
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
                my_starters: set[str], already: set[str],
                trade_counts: dict[str, int] | None = None) -> list[dict]:
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
    states_by_id = {s["owner_id"]: s for s in states}
    trade_counts = trade_counts or {}
    others_have_traded = any(n for oid, n in trade_counts.items()
                             if oid != me_roster["owner_id"])
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
                         # Friction travels with the (player, counterparty) pair, not with the
                         # list he happens to be in. Stevenson was cheap depth here AND a long
                         # shot above, and only one of the two mentioned that his owner has
                         # never traded - so one report said "no persuasion needed" and "the
                         # call may not be returned" about the same man.
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

    - **On a clock and starting.** A player his owner has already benched is just a bad asset,
      not a conversation - there's nothing to talk him out of.
    - **`discounted` decides one clause, not whether the case exists.** It reports whether he
      clears `team_values.now_premium_bar` for his own position, which is a statement about
      *shape* - the market pricing him for now rather than later - and not about quality;
      `clears_relevance_floor` answers that above.

    **Runway, not bucket** - the third place this same correction was needed, after
    `team_state.classify` and `_pivot_path`. `bucket == "declining"` missed DeVonta Smith at
    27.8 with **1.2 years** to his cutoff, reading `prime`, on a roster 47% ascending against
    0% declining. That is the archetype this function exists for and the bucket hid it. Runway
    turns out to be a strict superset: across three leagues, 24 starters on ascending-tilt
    teams qualify under both tests, 30 under runway alone, and **zero** under bucket alone.

    Gating the whole case on `discounted` was also wrong, and by its own logic - the bar exists
    to keep the "priced as though his remaining years are gone" sentence honest, which is a job
    for the sentence. Smith's ratio was 0.8790 against a WR bar of 0.8790, so whether an owner
    had any reason at all to move him came down to a coin flip in the fourth decimal place. The
    mismatch argument stands without a discount; it just says so.

    Two things this deliberately does NOT do. It doesn't check whether the owner has a
    replacement behind him - "should they do this?" is answered from their own side of the
    table, where this tier only answers "is this worth asking?", and `cost_note` already
    says an ask is all it is. And it no longer special-cases a reigning champion: that veto
    existed to stop exactly the aging-contender case the tilt now rejects on its merits,
    and a title says less about whether an owner should sell than the shape of their roster
    does. A champion tilting ascending is a team that can afford to sell, trophy or not."""
    if not (player["is_starter"]
            and (player.get("years_to_decline") or 0) < MIN_MEANINGFUL_RUNWAY):
        return None
    if other["ascending_pct"] <= other["declining_pct"]:
        return None
    discount = (f" He produces {ratio:.2f}x his own trade value - still starting, priced as "
                f"though his remaining years are gone." if discounted else
                f" He is not discounted for it ({ratio:.2f}x his own trade value), so this is "
                f"about whose window he fits rather than a price to harvest.")
    # The two arguments used to be `team_why or cliff`, so a team-level reason silenced the
    # player-level one entirely - and the player-level one is the more actionable of the two,
    # being about the man you are ringing about rather than a roster average. Travis Etienne's
    # entry read "this core hasn't won with them" while the far better argument went unsaid:
    # bigbuttboi is 55% ascending against 8% declining and Etienne is 27 with a negative runway.
    # The opener has to move with it, because "nothing about their team says seller" is false
    # once `_seller_case` has just said something about their team.
    opener = (f"{team_case} Separately, their window and {player['name']}'s don't line up"
              if team_case else
              f"Nothing about {other['owner']}'s team says seller, but their window and "
              f"{player['name']}'s don't line up")
    return (f"{opener}: {other['ascending_pct']}% of their "
            f"production is ascending against {other['declining_pct']}% declining, so "
            f"they're built for seasons he won't be part of.{discount} Keeping him may well "
            f"tip this season for them, which is exactly why it's a real decision for them "
            f"rather than a giveaway.")


def _buy_path(me: dict, states: list[dict], needs_by_owner_id: dict, thresholds: dict[str, float],
              trade_counts: dict[str, int], max_per_position: int,
              pick_values: dict[str, int] | None = None,
              my_picks: list[dict] | None = None,
              prior: dict[str, dict] | None = None,
              premium_bars: dict[str, float] | None = None,
              covered: dict[str, float] | None = None,
              backfills: dict[str, dict] | None = None) -> dict:
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
    my_pool = _my_offer_pool(me, thresholds, my_needs, pick_values, covered, backfills)
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
        for other in _others(states, me, MIGHT_SELL):
            for player in other["sellable"]:
                if player["position"] != pos or not team_state.clears_relevance_floor(player, thresholds):
                    continue
                if not _sells_him(other, player):
                    continue
                if upgrade_bar is not None and (player.get("redraft_value") or 0) <= upgrade_bar:
                    continue
                # `production_per_cost` is the same efficiency measure the persuasion tier
                # reports - without it an elite player and a cheap one look equivalent.
                ratio = ((player.get("redraft_value") or 0) / player["value"]
                         if player.get("value") else None)
                # What he beats YOUR weakest starter at the position by, which is the bar the
                # asking manager actually has: *"my replacement is Gainwell's redraft value, not
                # whatever that other number is."* League replacement level (the 24th-best RB in
                # the pool) answers "would he start somewhere in this league" - a different
                # question, and the wrong one for a team whose own RB2 produces 643. It was
                # already computed and sitting in the need entry, shown only on depth lines.
                over_weakest = ((player.get("redraft_value") or 0) - need["weakest_starter"]
                                if need.get("weakest_starter") is not None else None)
                pos_targets.append({"position": pos, "need_level": need["level"],
                                     "need_note": need["note"],
                                     "over_weakest_starter": over_weakest,
                                     # Why this owner is a seller of HIM, since the two answers
                                     # are different asks - see `_sells_him`.
                                     "sells_because": ("rebuilding" if other["window"] == "Rebuild"
                                                       else "rising, so selling age not youth"),
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
    # **Cornerstones belong here, tagged.** They used to be filtered out, on the argument that
    # `situational` says "not your long-term core" and a cornerstone is the core, so the label
    # would contradict itself. The label was right and the remedy was wrong: the exclusion's
    # own justification was that "`my_offers` and `value_upgrades` are the surfaces that say
    # so", and a REBUILD result has neither key - so on a rebuilding roster a cornerstone
    # appeared in no sell surface at all. That is the worst roster to hide it on, because a
    # rebuilder's only real question is which good player converts, and a cornerstone whose
    # runway ends before the rebuild lands is exactly the one to question.
    #
    # Caught by `case_sells_on_runway_not_age`: the agent was asked which of five QBs to trade
    # and could not weigh Jalen Hurts (4.0 years of runway) against Justin Herbert (5.6),
    # because the tool never listed him. It led with Jared Goff at 6.2 instead.
    real_sellable = [e for e in me["sellable"]
                     if team_state.clears_relevance_floor(e, thresholds)]

    def on_a_clock(e):
        return (e["years_to_decline"] or 0) < MIN_MEANINGFUL_RUNWAY

    def tagged(entries: list[dict]) -> list[dict]:
        """One `friction` vocabulary on the sell side too - the same flavor and shape the buy
        path uses, so "hard to move" reads identically whichever direction it is written in."""
        return [e if not e.get("is_cornerstone") else
                {**e, "friction": [_friction("cornerstone", CORNERSTONE_SELL[committed])]}
                for e in entries]

    sell_candidates = tagged([e for e in real_sellable if on_a_clock(e)])
    situational = tagged([e for e in real_sellable if not on_a_clock(e)])
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
    # Who replaces each of them, which is WHY the number above is small when it is small.
    backfills = {ctx.players[pid]["name"]: roster_needs.backfill_for(
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
    weakest_id = roster_needs.weakest_starter(ctx.players, my_starters)
    weakest = ctx.players[weakest_id] if weakest_id else None
    stranded = []
    for player_id in stranded_ids:
        info = ctx.players[player_id]
        entry = by_name.get(info["name"], {"name": info["name"], "position": info["position"],
                                           "value": info["value"],
                                           "redraft_value": info.get("redraft_value")})
        wanted = wanted_by(entry, my_roster, states, needs_by_owner_id, ctx)
        floor = weakest["redraft_value"] or 0
        stranded.append({**entry, "blocked_by": info["position"],
                         "wanted_by": wanted,
                         "times_weakest": (round((info.get("redraft_value") or 0) / floor, 1)
                                           if floor else None),
                         "note": (f"Produces {info.get('redraft_value') or 0:,} this season against the "
                                  f"{weakest['redraft_value'] or 0:,} of {weakest['name']} "
                                  f"({weakest['position']}), who starts - and every "
                                  f"{info['position']}-capable slot is held by someone better, so "
                                  f"none of it reaches the lineup."
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
        # `long_shots` was split out of `targets` and this set was not updated with it, so
        # David Montgomery and Rhamondre Stevenson came back as long shots ("the call may not
        # be returned at all") AND as cheap depth ("worth a late pick") in one report. Any
        # list the buy path prints counts as surfaced, whichever of them it landed in.
        def buy_names(block: dict) -> set[str]:
            return {t["name"] for key in ("targets", "long_shots")
                    for t in block.get(key) or []}

        # A Middling result nests its whole buy side under `push` while `value_upgrades` stays at
        # the top, so anything read from one and used by the other has to look in both places.
        # Reading only the top level handed the upgrade block an EMPTY offer pool for every
        # Middling team, which made `_counterparty_fit` find no fit and tag `needs_a_pivot` on
        # asks the persuasion block was calling straight fits.
        buy_side = result.get("push") or result
        surfaced = buy_names(result) | buy_names(buy_side)
        # Not for a rebuilder: "strictly better to hold if you're winning now" is advice for
        # someone who is. Every other window can buy production, including Middling, whose
        # whole push half is about whether to.
        if me["window"] != "Rebuild":
            upgrades = find_value_upgrades(my_roster, ctx, states, my_starters, trade_counts,
                                           needs_by_owner_id, me["window"], prior, premium_bars,
                                           surfaced, buy_side.get("my_offers"))
            if upgrades:
                result["value_upgrades"] = upgrades
                result["value_upgrade_note"] = VALUE_UPGRADE_NOTE
                # Same contradiction, third pair of lists: depth calls a player "below
                # replacement, never worth a real asset" while the upgrade block calls him a
                # better thing to own than a current starter. Both can be arithmetically true
                # when the starter is dreadful, and reading them together tells you nothing.
                # Eight live cases across three leagues, Kenny Gainwell on two rosters at once.
                surfaced |= {u["name"] for m in upgrades for u in m["returns"]
                             if not u.get("already_mine")}
        depth = _depth_adds(my_roster, ctx, states, me["window"] != "Rebuild",
                            my_starters, surfaced, trade_counts)
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
                                  premium_bars, covered, backfills),
                "pivot": _pivot_path(me, states, thresholds, trade_counts, picks_by_owner,
                                     stranded, committed=False)})

    result = {"me": me, "mode": "buy",
              **_buy_path(me, states, needs_by_owner_id, thresholds, trade_counts,
                          max_per_position, pick_values, my_picks, prior,
                          premium_bars, covered, backfills)}

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
    def names(entries: list[dict]) -> str:
        # A cornerstone's friction is the whole reason he is listed, so it prints inline with
        # the name rather than being data nobody renders.
        return ", ".join(e["name"] + (" [CORNERSTONE]" if e.get("friction") else "")
                         for e in entries) or "none"

    print(f"sell candidates (under {MIN_MEANINGFUL_RUNWAY:g} years before decline): "
          f"{names(pivot['sell_candidates'])}")
    print(f"  {pivot['sell_clock_note']}")
    # This list used to be labelled "just not your long-term core", which was true only because
    # cornerstones were filtered out of it - and filtering them out hid the one decision a
    # rebuilding or middling team most needs to see.
    print(f"pieces with years still on them, most now-weighted first - your cornerstones "
          f"included and tagged: {names(pivot['situational'])}")
    for e in pivot["sell_candidates"] + pivot["situational"]:
        for f in e.get("friction") or []:
            print(f"  - {e['name']}: {f['why']}")
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
    _print_stranded(extras)
    if push["my_offers"]:
        print("you could offer (cleanest first - anything with friction is listed last, with why):")
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
    # Cheapest and most gettable first, escalating to the long shots. The old order led with
    # "harder asks" - teams that aren't selling - and buried the nominal-price depth below two
    # targets stamped NEVER TRADES. Read top-down, it recommended the least likely moves first.
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
            # A body at a count-shaped need still fills an empty slot, so these are not dropped -
            # but calling someone who produces LESS than the man he'd replace a fix is the kind
            # of claim this project keeps having to walk back. Label, don't hide.
            kind = " [DEPTH - does not beat who you start there]" if (ow is not None and ow <= 0) else ""
            print(f"  {t['name']} ({t['position']}, value={t['value']}, {price_note}{beats}) from "
                  f"{t['from_owner']} [{t['sells_because']}] - need: {t['need_level']} - "
                  f"{trade_note}{kind}")
    else:
        # "...a long shot below" pointed at a block that isn't always there. On a team with no
        # needs both lists are empty and the sentence referred the reader to nothing.
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
        # The heading used to say every entry was "asking a team to change direction". Nine of
        # ten under it then said the opposite in their own cost line, because `needs_a_pivot`
        # was computed per entry and the heading ignored it. Ordering by it puts the ones that
        # are nearly fits first, which is also the order to make the calls in.
        print("harder asks (aging production on teams that are NOT shopping it - most are still "
              "a fit for both sides; the ones marked PIVOT need them to change direction):")
        for t in sorted(push["persuasion_targets"], key=lambda t: t["needs_a_pivot"]):
            print(f"  {t['name']} ({t['position']}, {t['production_per_cost']}x production "
                  f"per unit of cost - dyn {t['value']:,} / redraft {t['redraft_value']:,}) "
                  f"from {t['from_owner']}" + (" [PIVOT]" if t["needs_a_pivot"] else ""))
            print(f"      why they might listen: {t['why_they_might_listen']}")
            # `you_could_offer`/`why_it_fits` and `cost_note` were all computed and none of
            # them printed - so the CLI showed the argument for asking and never what the ask
            # costs, which is the half that decides whether to make the call.
            if t.get("you_could_offer"):
                print(f"      what they want from you: {', '.join(t['you_could_offer'])}")
            if t.get("cost_note"):
                print(f"      what it costs: {t['cost_note']}")
            for f in t.get("friction") or []:
                if f["flavor"] != "needs_a_pivot":   # already said by cost_note above
                    print(f"      - [{f['flavor']}] {f['why']}")
        print(f"  {push['persuasion_note']}")


def _print_report(result: dict) -> None:
    me = result["me"]

    if result["mode"] == "rebuild":
        tank_note = "" if me["owns_next_first"] else " (doesn't own next 1st, so tanking for a pick wouldn't help)"
        print(f"{me['owner']}: Rebuilding{tank_note} - playing for future value, not starting-lineup needs")
        # Stranded applies in every window - its own note says a rebuilder converts one into
        # futures - and only the pushing branch was printing it.
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
        # Rank on each scale, not the raw ratio. `now_share < 1.0` labelled 111 entries
        # "upside-priced" against 32 the other way, including Smith-Njigba at 0.88 and Jeanty
        # at 0.89 - elite on both boards - and Kenny Gainwell, a 26-year-old backup with no
        # upside to sell, at 0.39. See `team_values.priced_for`.
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
            # **Relative measure, so the claim has to be relative.** `priced_for` compares a
            # player's two RANKS inside his own position pool, which answers "is the market
            # pricing him more for later than it prices other RBs" - not "does his price contain
            # future value". Read as the latter it contradicted the offer pool two blocks above,
            # on the same player in the same report: TreVeyon Henderson, 23.8 with 3.2 years of
            # runway, was listed as "high - real future value you won't get back" and here as
            # "no future premium to harvest". Both measures were right; this sentence was not.
            pricing = (f"priced like the rest of his position ({ordinal(pf['dynasty_rank'])} of "
                       f"{pf['of']} at {m['position']} in dynasty against "
                       f"{ordinal(pf['redraft_rank'])} in redraft) - he may still be young, this "
                       f"says only that the market is not paying him a premium other "
                       f"{m['position']}s don't get")
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
            price = (f"{u['value_freed']:,} dynasty freed" if u["value_freed"] > 0
                     else "same dynasty price")
            print(f"      <- {u['name']} ({u['redraft_value']:,} this season, "
                  f"{u['production_gained']:+,} production, {price})"
                  f"{UPGRADE_KIND_TAG[u['kind']]} from {u['from_owner']} - {trades}")
            # Half the trade was missing: who wants MY guy was printed, why THEIRS would be
            # available was computed nowhere. A tight end held by a contender read exactly
            # like one held by a seller.
            if u.get("their_reason"):
                print(f"           why they'd move him: {u['their_reason']}")
            # Friction was computed on every return and rendered on none of them, so a Kelce
            # needing his owner to change direction read like a straight swap. `never_trades` is
            # skipped only because the header line above already shouts it.
            for f in u.get("friction") or []:
                if f["flavor"] != "never_trades":
                    print(f"           - [{f['flavor']}] {f['why']}")
    print(f"  {result['value_upgrade_note']}")


def _print_conversion_candidates(result: dict) -> None:
    """`_cliff_case` aimed at your own roster - and computed since it was written without a
    printer, so the CLI told eleven other managers which of your starters to call you about and
    never told you. Its own docstring is the argument for printing it: if they hear it, you hear
    it, in the same terms."""
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
    """Computed for every window since it was written, attached to the result, and printed by
    nothing - the same defect `depth_adds` had. Its own note says to LEAD with these, so it
    prints above the offer list rather than below it."""
    if not result.get("stranded"):
        return
    print("STRANDED - the most valuable thing you own that you cannot use:")
    for e in result["stranded"]:
        wants = ", ".join(f"{w['owner']}[{w['window']}]" for w in (e.get("wanted_by") or [])[:3])
        margin = (f"{e['times_weakest']}x what your weakest starter produces"
                  if e.get("times_weakest") else "production your weakest starter has none of")
        print(f"  {e['name']} ({e['position']}, {e['redraft_value'] or 0:,} this season, "
              f"{e['value']:,} dynasty) - {margin}, and every {e['blocked_by']}-capable slot "
              f"is held by someone better"
              + (f"; wanted by {wants}" if wants else ""))
    print(f"  {result['stranded_note']}")


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
        flavors = (" [" + ", ".join(f["flavor"] for f in a["friction"]) + "]"
                   if a.get("friction") else "")
        print(f"  {a['name']} ({a['position']}, value={a['value']}, "
              f"{a['redraft_value'] or 0:,} this season{edge_note}) from "
              f"{a['from_owner']}{flavors}")
        for f in a.get("friction") or []:
            print(f"      - {f['why']}")
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
