"""The sell side: cash in value that doesn't fit the timeline, and what to want back -
picks first, then young value surplus to its owner's plan. History: LOGIC.md, "The
rebuild path finally got the buy path's treatment".
"""

from .. import team_state
from ..team_values import MIN_MEANINGFUL_RUNWAY
from .board import (Board, NOT_SELLER, _best_chip, _buy_friction, _friction, _others,
                    _with_trade_note)
from .counterparty import wanted_by, wanted_line

# The same runway means different things depending on whether the team has picked this
# direction: a committed seller should move the piece, a Middling team's clock bounds how
# long waiting stays free. One string each so the CLI and the agent's JSON can't drift.
SELL_CLOCK_COMMITTED = "value only goes down from here, real urgency to move it"

SELL_CLOCK_OPTIONAL = (
    "value only goes down from here - so these are what waiting costs, and the deadline on "
    "deciding. This team has NOT committed to selling: the clock is a reason to pick a "
    "direction before the price decays, not an instruction to sell now"
)

# Selling your own cornerstone is one action with two meanings, split by `committed`: on a
# team that has picked its direction it is the hardest defined move; on a Middling team it
# IS the choice of direction, and is stated as a decision rather than a sell instruction.
CORNERSTONE_SELL = {
    True: ("cornerstone - the runway this roster is built around, so expect to pay over market "
           "or be told no if you are on the other side of it. Moveable, and the hardest ask "
           "here: sell him when the return actually shortens the rebuild, never for a fair "
           "price on paper"),
    False: ("cornerstone - and for a team that has NOT picked a direction this is not one move "
            "among others, it IS the choice. Converting him is what committing to the future "
            "consists of; keeping him is what committing to now consists of. Decide the "
            "direction first, then this answers itself - do not trade him to find out"),
}

# The one sell block that shipped without a note - and the eval that measures whether the
# runway rule gets applied failed 6/6 runs while the rule lived only in a tool docstring.
# Attached to the entries it governs, it survives to the sentence that needs it.
SITUATIONAL_NOTE = (
    "PIECES WITH YEARS STILL ON THEM, most now-weighted first - cornerstones included and "
    "tagged, because the hardest ask is a price, never a veto. No clock forces any of these, "
    "so the question each answers is fit: whose remaining years will this roster actually be "
    "there for. Between two at the same position, years_to_decline picks the sale and age "
    "never does - keep the years, sell the piece whose decline lands first, even when he is "
    "the younger man (the curves cross: a running quarterback runs out of years before an "
    "older pocket passer). An easier sale elsewhere in this report does not answer that "
    "comparison - and where this roster's own numbers invert it, the entry carries "
    "RUNWAY INVERSION naming the pair: price that version first."
)

PICKS_NOTE = (
    "ASK FOR PICKS, NOT JUST PLAYERS - and this is the block to work from first. A pick is the "
    "cleanest thing a rebuild can hold: it has no age, so it is not on any clock and cannot "
    "decline while you wait; it occupies no roster spot; and it is worth strictly less to a "
    "contender than to you, which is the only kind of asset both sides can rationally want to "
    "move in the same direction. That last point is why these are cheaper to ask for than the "
    "players below - a contender giving up a future 1st is giving up something it has already "
    "decided not to use, where giving up a young player costs it a piece it may still want. "
    "Prefer picks when the price of a player would mean taking on someone else's timeline."
)

ACQUIRE_NOTE = (
    "YOUNG VALUE SURPLUS TO ITS OWNER'S PLAN, which is the one thing every name here has in "
    "common - so it is said once rather than repeated under each. A contender's ascending pieces "
    "score nothing in the seasons it is actually playing for, and neither do a middling team's if "
    "that team is going nowhere. Two things follow. A team RISING through the middle is excluded "
    "outright: accumulating this kind of value IS its plan, so its youth is what it builds with "
    "and asking for it argues for a trade nobody would make. And none of these owners is SHOPPING "
    "the player - surplus to a plan is not the same as on the market - so expect to pay for the "
    "asking, and read the friction on each line for what else is in the way. Capped per position "
    "and cleanest first, because an uncapped list sorted by price alone printed thirty names down "
    "to a 726-value quarterback and buried the reachable ones."
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


def _runway_inversion(lists: list[list[dict]]) -> None:
    """Tag the starter a seller should actually be pricing: at each position, when the
    bench piece with the MOST years out-runways the starter with the FEWEST, the starter's
    entry says so, with both numbers. The block note states the keep-the-years rule; six of
    six live runs applied it only within the bench until the roster's own counterexample
    rode on the entry - the model repeats an attached fact far more reliably than it
    extends an instruction to a player the question didn't mention.

    Replaces the tagged entry with a copy - some entries here are still the caller's own
    dicts (untagged pieces pass through `tagged`/`with_buyers` uncopied), and a tag written
    into those would survive into the next report built from the same rows."""
    entries = [e for lst in lists for e in lst]
    by_pos = {}
    for e in entries:
        if e.get("years_to_decline") is not None:
            by_pos.setdefault(e["position"], []).append(e)
    for pos, group in by_pos.items():
        starters = [e for e in group if e.get("is_starter")]
        bench = [e for e in group if not e.get("is_starter")]
        if not starters or not bench:
            continue
        s = min(starters, key=lambda e: e["years_to_decline"])
        b = max(bench, key=lambda e: e["years_to_decline"])
        if b["years_to_decline"] <= s["years_to_decline"]:
            continue
        note = (f"RUNWAY INVERSION at {pos}: {s['name']} starts with {s['years_to_decline']} "
                f"years before decline while {b['name']} holds {b['years_to_decline']} behind "
                f"him - a seller keeping years sells {s['name']} and keeps {b['name']}, even "
                f"though moving the bench piece is the easier call. Weigh that version of the "
                f"trade before settling for the easy one.")
        for lst in lists:
            for i, e in enumerate(lst):
                if e is s:
                    lst[i] = {**e, "runway_inversion": note}


def _pivot_path(me: dict, board: Board,
                stranded: list[dict] | None = None, committed: bool = True,
                my_roster: dict | None = None,
                max_per_position: int = 5) -> dict:
    """The sell case. Sell lists split by runway (`MIN_MEANINGFUL_RUNWAY`) into urgent and
    situational, cornerstones included and tagged rather than hidden - a rebuilder's only
    real question is which good player converts. Acquire targets get the buy path's full
    treatment: capped per position, friction, cleanest first, rising-middling owners
    excluded because accumulating young value IS their plan."""
    states, thresholds, trade_counts = board.states, board.thresholds, board.trade_counts
    real_sellable = [e for e in me["sellable"]
                     if team_state.clears_relevance_floor(e, thresholds)]

    def on_a_clock(e):
        return (e["years_to_decline"] or 0) < MIN_MEANINGFUL_RUNWAY

    def tagged(entries: list[dict]) -> list[dict]:
        return [e if not e.get("is_cornerstone") else
                {**e, "friction": [_friction("cornerstone", CORNERSTONE_SELL[committed])]}
                for e in entries]

    def with_buyers(entries: list[dict]) -> list[dict]:
        """Who would take him - the fact that turns a sell list into a phone call."""
        if my_roster is None:
            return entries
        return [{**e, "wanted_by": wanted_line(wanted_by(e, my_roster, board))}
                for e in entries]

    sell_candidates = with_buyers(tagged([e for e in real_sellable if on_a_clock(e)]))
    situational = with_buyers(tagged([e for e in real_sellable if not on_a_clock(e)]))
    # Most now-weighted first, not most valuable first: a seller is converting present into
    # future, so the order is how much of a price is present. No redraft price sorts last -
    # unknown, not zero.
    situational.sort(key=lambda e: -((e.get("redraft_value") or 0) / e["value"]) if e["value"] else 0)
    # Across BOTH lists: the short-runway starter may be on the clock while the long
    # bench piece is situational, and the inversion is about the pair.
    _runway_inversion([sell_candidates, situational])

    others_have_traded = board.others_have_traded(me["owner_id"])
    best_chip = _best_chip(real_sellable)
    by_position = {}
    for other in _others(states, me, NOT_SELLER):
        # The mirror of `_sells_him`: a rising middling team's young value is what it is
        # building with, and asking for it argues for a trade nobody would make.
        if other["window"] == "Middling" and other.get("trajectory") == "rising":
            continue
        for player in other["tradeable_surplus"]:
            if not team_state.clears_relevance_floor(player, thresholds):
                continue
            entry = {**_with_trade_note(player, other, trade_counts),
                     **_buy_friction(player, other, best_chip,
                                     trade_counts.get(other["owner_id"], 0), others_have_traded),
                     # Why the owner is a seller of youth is the same sentence for all of
                     # them, so it lives in ACQUIRE_NOTE; only what varies rides here.
                     "seller_path": other.get("path", "")}
            by_position.setdefault(player["position"], []).append(entry)
    acquire_targets = []
    for pos in sorted(by_position):
        ranked = sorted(by_position[pos],
                        key=lambda t: (bool(t["friction"]), -t["value"], -t["from_owner_trades"]))
        acquire_targets += ranked[:max_per_position]
    acquire_targets.sort(key=lambda t: (bool(t["friction"]), -t["value"]))
    result = {"sell_candidates": sell_candidates, "situational": situational,
              "situational_note": SITUATIONAL_NOTE,
              "acquire_targets": acquire_targets, "acquire_note": ACQUIRE_NOTE,
              "sell_clock_note": SELL_CLOCK_COMMITTED if committed else SELL_CLOCK_OPTIONAL}
    if stranded:
        result["stranded"] = stranded
        result["stranded_note"] = STRANDED_NOTE

    # The mirror of picks_to_trade_away: a contender's future 1st is worth more to the
    # rebuilder asking than to its owner.
    if board.picks_by_owner:
        pick_targets = []
        for other in _others(states, me, NOT_SELLER):
            for pick in board.picks_by_owner.get(other["roster_id"], []):
                pick_targets.append({
                    **pick, "from_owner": other["owner"],
                    "from_owner_trades": trade_counts.get(other["owner_id"], 0),
                    "note": (f"{other['owner']} is not rebuilding (path: {other.get('path', '')}) - "
                             f"future picks are worth less to them than to you"),
                })
        pick_targets.sort(key=lambda t: (-t["value"], -t["from_owner_trades"]))
        if pick_targets:
            result["picks_to_acquire"] = pick_targets[:8]
            result["picks_note"] = PICKS_NOTE
    return result
