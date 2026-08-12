"""Better things to own than what you start: same-position holdings that beat a current
starter for less dynasty value, and the same cliff argument pointed at your own roster.
History: LOGIC.md, "Better holdings and the mirror".
"""

from .. import team_state
from ..team_values import age_bucket, priced_for
from .board import Board, NOISE_BAND, NOISE_RETAINED, _with_trade_note
from .counterparty import _cliff_case, _counterparty_fit, _why_they_would_move_him, wanted_by

# Below NOISE_RETAINED of the production, the loss is REAL and the trade is a conversion -
# a defensible dynasty play with no clock, the wrong answer while pushing. Below this floor
# the loss stops being a conversion and is simply a worse team.
MIN_PRODUCTION_RETAINED = 0.90

# How much dynasty value has to come back for the trade to be worth mentioning at all -
# below this it's churn, not arbitrage.
MIN_VALUE_FREED = 300

UPGRADE_KIND_TAG = {
    "upgrade": "",
    "value_decision": " [value decision - lineup unchanged]",
    "conversion": " [CONVERSION - gives up real production for value]",
}

RETURNS_PER_MOVE = 4  # a shortlist of who to ask about, not the league

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

# A contender whose production is tilting ascending has two live plays, and the window
# label alone hides that - it contends either way, so `window` stays "Contend" and this
# is additive: the choice is about HOW it contends, not whether.
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


def _holding_kind(produced: float, costs: float, mine: dict) -> str | None:
    """Is this player a better thing to own than one of my starters, and in which of the
    three senses? Not costing MORE in dynasty value is required by all three - that is what
    keeps this from collapsing into "go buy someone better", the buy path's question.
    `upgrade` gets `NOISE_BAND` on both axes: two prices inside the band are the same
    price, and a gain inside it is the same lineup."""
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
        # `freed` can be very slightly negative inside the noise band, and "costs -3 less"
        # is not a sentence.
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


def find_value_upgrades(me_roster: dict, board: Board, my_starters: set[str],
                        window: str = "Contend",
                        already_surfaced: set[str] | None = None,
                        my_offers: list[dict] | None = None) -> list[dict]:
    """Which single holding beats one of my starters at his own position, for less dynasty
    value? Candidates come from every roster INCLUDING my own bench (a man already mine
    needs no trade). Comparisons stay one player against one at the same position - the
    only pairing where the two unnormalized value scales cancel - matched against the
    weakest starter he beats. Organised as MOVES around the starter being replaced, with
    who wants him and why each return's owner would part with theirs; returns are ranked
    within a move rather than capped globally, and Push never sees a conversion."""
    ctx, states, trade_counts = board.ctx, board.states, board.trade_counts
    needs_by_owner_id, prior, premium_bars = (board.needs_by_owner_id, board.prior,
                                              board.premium_bars)
    # Rank within position on each scale - see `team_values.priced_for` for why the raw
    # ratio cannot say whether a starter is upside-priced.
    pricing = priced_for(ctx.players)
    mine_by_pos = {}
    for pid in my_starters:
        info = ctx.players.get(pid)
        if info:
            mine_by_pos.setdefault(info["position"], []).append(info)

    by_owner_id = {s["owner_id"]: s for s in states}
    others_have_traded = board.others_have_traded(me_roster["owner_id"])
    upgrades = []
    for roster in ctx.rosters:
        other = by_owner_id.get(roster["owner_id"])
        if other is None:
            continue
        mine_side = roster["owner_id"] == me_roster["owner_id"]
        their_starters = ctx.starters_for(roster)
        # Fit-or-pivot is a fact about the OWNER, resolved once per roster off the same
        # `_counterparty_fit` the persuasion tier uses.
        hole = bool((_counterparty_fit(other, (needs_by_owner_id or {}).get(roster["owner_id"], {}),
                                      my_offers or []) or {}).get("fills_a_hole"))
        for pid in roster["players"] or []:
            # My own starters are the comparison set; my own bench is a live candidate.
            if mine_side and pid in their_starters:
                continue
            info = ctx.players.get(pid)
            if not info:
                continue
            # The buy path already named him at a position it knows this roster needs, and
            # says the more useful thing - re-deriving him here is the same call in weaker
            # words.
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
            # The weakest starter beaten is both the slot he'd take and the largest gain.
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
            # A counterparty's trade history is meaningless for a player already mine.
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
        # A man already on my bench leads and is never truncated away: he ranks last on
        # production gained by construction, and he is the only option costing no trade.
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
            "wanted_by": wanted_by(profile, me_roster, board),
            "returns": returns,
            "best_gain": returns[0]["production_gained"],
        })
    return sorted(moves, key=lambda m: -m["best_gain"])


def _conversion_candidates(me: dict, board: Board) -> list[dict]:
    """`_cliff_case` turned around and pointed at your own roster: the aging starters
    whose remaining seasons don't reach the ones the roster is built for. Deliberately the
    same rule the rest of the league is handed about you, end to end - it drifted into two
    rules once, and a starter was pitched to eleven managers while absent from his own
    manager's report. The floor decides who is worth calling about, the cliff case decides
    whether the argument exists, the bar picks only the price sentence."""
    out = []
    for player in me["sellable"]:
        if not (player.get("redraft_value") and player.get("value")):
            continue
        if not team_state.clears_relevance_floor(player, board.thresholds):
            continue
        ratio = player["redraft_value"] / player["value"]
        discounted = ratio >= board.premium_bars.get(player["position"], float("inf"))
        if not _cliff_case(player, me, ratio, discounted=discounted):
            continue
        note = (f"Still starting for you and still producing, but priced at {ratio:.2f}x his "
                f"own trade value - the market is paying for this season and writing off the "
                f"rest, which is the season your roster is least short of."
                if discounted else
                f"Still starting for you and still producing, and not discounted for it "
                f"({ratio:.2f}x his own trade value) - so the case is about whose window he "
                f"fits: his remaining seasons aren't the ones the rest of this roster is "
                f"built for.")
        out.append({**player, "production_per_cost": round(ratio, 2), "note": note})
    return out
