"""The trade board - every league-wide fact the trade paths read, built once per
report - plus the shared vocabulary those paths speak: who counts as a seller, and
`friction`, the one {flavor, why} shape for "how hard is this ask" on both sides of
the table. History and measurements behind each rule: LOGIC.md, "Trade target matching".
"""

from dataclasses import dataclass, field

from sources import fantasycalc

from .. import team_state, roster_needs, trade_activity, prior_season
from ..league import context
from ..team_values import (owned_picks, now_premium_bar, INSIDE_FINAL_YEAR,
                           MIN_MEANINGFUL_RUNWAY, NOISE_BAND, NOISE_RETAINED)

# A parameter, not a hard limit - "give me more" means call again with a higher number.
DEFAULT_MAX_PER_POSITION = 5

# How now-weighted a price has to be to count as extreme FOR THE PLAYER'S OWN POSITION.
# Dynasty and redraft are unnormalized scales (an absolute bar once sat above the entire
# TE pool), so this is a percentile of redraft/dynasty within each position - it picks
# which *sentence* describes a price, never whether an entry exists.
NOW_PREMIUM_PERCENTILE = 0.9

@dataclass
class Board:
    """Every league-wide fact the trade paths read, derived once per `find_targets` call.
    The league rides here; facts about the asking team stay arguments."""
    ctx: object = None                                    # LeagueContext, when available
    states: list = field(default_factory=list)            # team_state.classify_league rows
    needs_by_owner_id: dict = field(default_factory=dict)
    thresholds: dict = field(default_factory=dict)        # dynasty trade-relevance floor
    premium_bars: dict = field(default_factory=dict)      # team_values.now_premium_bar
    trade_counts: dict = field(default_factory=dict)
    prior: dict = field(default_factory=dict)             # prior_season.results
    pick_values: dict = field(default_factory=dict)
    picks_by_owner: dict = field(default_factory=dict)

    def others_have_traded(self, me_owner_id: str) -> bool:
        """Whether a counterparty's zero trade count means anything. `never_trades` is only
        ever a fact about a counterparty, so the asking team's own count is excluded: if I
        am the only trader in the league, everyone else's zero describes the league."""
        return any(n for oid, n in self.trade_counts.items() if oid != me_owner_id)


def build_board(league_id: str) -> Board:
    states = team_state.classify_league(league_id)
    ctx = context(league_id)
    pick_values = fantasycalc.get_pick_values(ctx.fmt["num_qbs"], ctx.num_teams,
                                              ctx.fmt["ppr"], ctx.fmt["is_dynasty"])
    # Picks keyed on the window, which is where a pick's slot actually comes from: how good
    # the originating team finishes. Contending teams pick late, rebuilders early.
    strategy_by_roster = {r["roster_id"]: r["window"] for r in states}
    return Board(
        ctx=ctx,
        states=states,
        needs_by_owner_id=roster_needs.league_needs(league_id),
        thresholds=ctx.trade_thresholds,
        premium_bars=now_premium_bar(ctx.players, NOW_PREMIUM_PERCENTILE),
        trade_counts=trade_activity.get_trade_counts(league_id),
        prior=prior_season.results(league_id),
        pick_values=pick_values,
        picks_by_owner=owned_picks(league_id, int(ctx.league["season"]),
                                   ctx.league["settings"]["draft_rounds"],
                                   [r["roster_id"] for r in ctx.rosters], pick_values,
                                   strategy_by_roster),
    )


def _best_chip(entries: list[dict]) -> dict | None:
    """The biggest SINGLE thing a team could put on the table. One player against one
    player is the only comparison this project can make, so 'out of reach' means above
    this - never above a sum, which would imply value is additive across players."""
    return max(entries or [], key=lambda e: e["value"], default=None)


def _others(states: list[dict], me: dict, window_test) -> list[dict]:
    """Every team but this one whose window passes `window_test`. Named so the direction
    of each search is explicit at the call site."""
    return [o for o in states if o["owner_id"] != me["owner_id"] and window_test(o["window"])]


def NOT_SELLER(window: str) -> bool:
    """Everyone but the rebuilders, who have to be talked into selling."""
    return window != "Rebuild"


def MIGHT_SELL(window: str) -> bool:
    """Teams the buy path is allowed to look at - see `_sells_him` for which of their players."""
    return window in ("Rebuild", "Middling")


# THE DIRECTION GATE (owner's rule, 2026-08-15): trade suggestions are only generated
# along each side's default direction - contenders acquire production and part with
# future; rebuilds acquire future and part with production; the middle can do either.
# Violations are CUT, never surfaced with a friction label: "shivvv might sell Henry
# (holds_to_win)" and "offer Stafford to a rebuilder (they're short at QB)" are both
# nonsense by construction, and a labelled nonsense suggestion is still noise.
def _rental(piece: dict) -> bool:
    """Value that is mostly this-season: a player inside the buyer's two-season bar.
    Picks are never rentals - they are the purest future-weighted asset - and neither
    is a piece with UNKNOWN runway (`is not None`, the same guard the pick-runway bug
    needed: `None or 0` reads absence as a 0-year clock)."""
    y = piece.get("years_to_decline")
    return (piece.get("position") != "PICK"
            and y is not None and y < MIN_MEANINGFUL_RUNWAY)


def acquires_by_default(window: str, piece: dict) -> bool:
    """The asker-side gate: would this team's default direction want this piece at all?"""
    if window == "Rebuild":
        return not _rental(piece)
    return True  # contenders' pools are production-only by construction; middle takes either


def _sells_him(other: dict, player: dict) -> bool:
    """Is this owner a seller **of this player**? Seller-ness is a property of the pair:
    a Rebuild team sells everything; a rising Middling team sells only its aging pieces,
    because it is accumulating the seasons those players won't be there for. The clock is
    `INSIDE_FINAL_YEAR`, not the buyer's two-season horizon - on the RB curve two years of
    runway means "any RB over 25", which would offer up a rising team's own core."""
    if other["window"] == "Rebuild":
        return True
    return (other["window"] == "Middling" and other.get("trajectory") == "rising"
            and (player.get("years_to_decline") or 0) < INSIDE_FINAL_YEAR)


# ONE vocabulary for "how hard is this, and why", used on both sides of the table. An entry
# with no friction is easy. Flavors rather than a difficulty score because they call for
# different responses, and because they group. None of these is a price.
BUY_FRICTION = ("cornerstone", "beyond_your_best_chip", "never_trades", "needs_a_pivot",
                "holds_to_win")
SELL_FRICTION = ("cornerstone", "costs_you_production")


def _friction(flavor: str, why: str) -> dict:
    return {"flavor": flavor, "why": why}


# Difficulty, not availability: a cornerstone's owner answers the call and wants more
# than market.
CORNERSTONE_ASK = ("cornerstone - the runway this roster is built around, so expect to pay "
                   "over market or be told no. Moveable, just the hardest ask here")


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
    # Only when somebody in this league HAS traded - in a league with no trade history a
    # zero describes the league, not this owner (see Board.others_have_traded).
    if trades_are_informative and not trades:
        friction.append(_friction("never_trades",
                                  f"{other['owner']} has never made a trade, so the call may "
                                  f"not be returned at all"))
    return {"friction": friction}


def _with_trade_note(entry: dict, other: dict, trade_counts: dict[str, int]) -> dict:
    """Stamp an entry with who holds him and the two counterparty facts a bare name hides:
    a rebuilder's "starter" is a value claim rather than intent, and a zero trade count is
    a fact worth words rather than an unlabelled number."""
    starter_note = ({"starter_caveat": (
        f"Listed as a starter for {other['owner']}, but they are rebuilding - that reflects "
        f"his value on that roster, not that the owner is trying to win with him.")}
        if entry.get("is_starter") and other["window"] == "Rebuild" else {})
    trades = trade_counts.get(other["owner_id"], 0)
    never = ({"never_trades": (
        f"{other['owner']} has made no trades in this league's history. Not a reason to skip "
        f"him - it may just mean nobody has asked - but expect a harder conversation than "
        f"with an active trader, and weigh that against how much this player helps.")}
        if trades == 0 else {})
    return {**entry, "from_owner": other["owner"], "from_owner_trades": trades,
            **never, **starter_note}
