"""Surface obvious trade fits: a team needing a position looks at what other teams can
part with, shaped by which window each side is in. This is a discovery tool, not a
fairness calculator - it finds *who* to call, not whether a package is fair, because
value is not additive across players and nothing here prices a bundle.

The package, by surface:
  board.py        - the Board (league-wide facts, built once) and the shared vocabulary
  counterparty.py - why the other side would move a player; the persuasion tier
  buy.py          - the offer pool, the buy path, cheap depth
  pivot.py        - the sell path: sell lists, acquire targets, picks
  upgrades.py     - better holdings than current starters; your own conversion candidates
  report.py       - the CLI rendering (everything computed must print - audited)

Why each rule is what it is: LOGIC.md. Smoke test:
python -m analysis.trade_targets <league_id> [owner_name] [max_per_position]
"""

from .. import roster_needs, team_state
from .board import (Board, DEFAULT_MAX_PER_POSITION, NOISE_BAND, NOISE_RETAINED,
                    NOW_PREMIUM_PERCENTILE, acquires_by_default, build_board, _sells_him)
from .buy import _buy_path, _depth_adds, _my_offer_pool, DEPTH_NOTE, DEPTH_NOTE_REBUILD
from .counterparty import (_cliff_case, _counterparty_fit, _persuasion_targets,
                           _seller_case, _why_they_would_move_him, wanted_by, wanted_line)
from .outlook import OUTLOOK_NOTE, outlook_from_board, player_outlook
from .pivot import STRANDED_NOTE, _pivot_path
from .report import _print_report
from .upgrades import (CONTEND_CHOICE_NOTE, RETURNS_PER_MOVE, VALUE_UPGRADE_NOTE,
                       PRESS_CHOICE_NOTE, _conversion_candidates, _holding_kind,
                       find_value_upgrades)

# The choice a Middling team is actually facing, stated rather than left implicit. Two
# versions, because patience is only free for a rising roster - handing the rising text
# to a falling team contradicted its own window note.
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


def find_targets(league_id: str, owner_query: str,
                 max_per_position: int = DEFAULT_MAX_PER_POSITION,
                 stance: str | None = None) -> dict:
    board = build_board(league_id)
    ctx = board.ctx

    me = ctx.pick_owner(owner_query, board.states)

    # The direction gate is a DEFAULT and the manager outranks it: the chip says what
    # the position calls for, never what its manager is doing - so a manager who
    # declares a direction ("the tool says I'm on the Middling cutoff, but I want to
    # press this season") gets that side's full report. The measured read still rides
    # in `me` and the note says both, so the answer can honestly disagree.
    stance_note = None
    if stance:
        declared = {"press": "Push", "contend": "Push", "buy": "Push",
                    "decide": "Middling", "wait": "Middling",
                    "sell": "Rebuild", "build": "Rebuild", "rebuild": "Rebuild"}.get(stance.lower())
        if declared and declared != me["window"]:
            stance_note = (
                f"MANAGER-DECLARED DIRECTION: this report runs the {declared}-side "
                f"paths because the manager chose '{stance}', overriding the measured "
                f"label ({me['window']} - {me.get('path', '')}). The measurements have "
                f"not changed - state the declared direction as the manager's choice "
                f"and note where the measured read would push back.")
            me = {**me, "window": declared}

    # What each of my starters actually costs to lose after the lineup refills itself
    # (zero = the bench covers him for free), and who backfills - see buy._my_offer_pool.
    my_roster = ctx.roster_for(owner_query)
    my_starters = ctx.starters_for(my_roster)
    covered = {ctx.players[pid]["name"]: roster_needs.production_lost_without(
                   my_roster, ctx.players, pid, my_starters,
                   ctx.lineup_dedicated, ctx.lineup_flex)
               for pid in my_starters if pid in ctx.players}
    backfills = {ctx.players[pid]["name"]: roster_needs.backfill_for(
                     my_roster, ctx.players, pid, my_starters,
                     ctx.lineup_dedicated, ctx.lineup_flex)
                 for pid in my_starters if pid in ctx.players}

    my_picks = board.picks_by_owner.get(me["roster_id"], [])

    # Bench production the lineup can never collect - applies in every window.
    stranded_ids = roster_needs.stranded_starters(my_roster, ctx.players, my_starters)
    by_name = {e["name"]: e for e in me["sellable"] + me["tradeable_surplus"]}
    weakest_id = roster_needs.weakest_starter(ctx.players, my_starters)
    weakest = ctx.players[weakest_id] if weakest_id else None
    # WHO holds this player's cheapest reachable slot, and by how much. "Every slot is
    # held by someone better" was true but absolute-sounding: a TE 61 points behind the
    # last FLEX read back to a tester as "you have 5 WR slots so he can't start" - the
    # model invented slot mechanics because the payload stated a verdict without its
    # margin. The margin self-defends in both directions: 61 is a competition, 4,000
    # is a wall, and the reader can tell which without being told.
    filled = roster_needs.fill_lineup(my_roster, ctx.players, ctx.lineup_dedicated,
                                      ctx.lineup_flex)

    def _nearest_door(pos):
        doors = [(slot, ctx.players[pid]) for slot, pid in filled if pid in ctx.players
                 and (slot == pos or pos in roster_needs.FLEX_ELIGIBILITY.get(slot, ()))]
        if not doors:
            return None
        slot, occ = min(doors, key=lambda d: d[1].get("redraft_value") or 0)
        return {"slot": slot, "held_by": occ["name"],
                "redraft_value": occ.get("redraft_value") or 0}

    # The same fact for every non-starter the payload offers as sellable: his distance
    # from the lineup is a number with a name on it. Skipped when the margin comes out
    # negative - declared starters and the optimal lineup can disagree, and a player
    # who would beat the door isn't locked out at all.
    for e in me["sellable"] + me["tradeable_surplus"]:
        if not e.get("is_starter") and e.get("redraft_value") is not None:
            d = _nearest_door(e["position"])
            if d:
                m = d["redraft_value"] - (e["redraft_value"] or 0)
                if m >= 0:
                    e["nearest_door"] = {**d, "margin": m}

    stranded = []
    for player_id in stranded_ids:
        info = ctx.players[player_id]
        entry = by_name.get(info["name"], {"name": info["name"], "position": info["position"],
                                           "value": info["value"],
                                           "redraft_value": info.get("redraft_value")})
        wanted = wanted_by(entry, my_roster, board)
        floor = weakest["redraft_value"] or 0
        door = _nearest_door(info["position"])
        margin = (door["redraft_value"] - (info.get("redraft_value") or 0)) if door else None
        door_sentence = (
            f" His cheapest reachable slot is {door['slot']}, where {door['held_by']} "
            f"starts at {door['redraft_value']:,} - {info['name']} is {margin:,} points "
            f"of current production behind that door." if door else "")
        stranded.append({**entry, "blocked_by": info["position"],
                         **({"nearest_door": {**door, "margin": margin}} if door else {}),
                         "wanted_by": wanted_line(wanted),
                         "times_weakest": (round((info.get("redraft_value") or 0) / floor, 1)
                                           if floor else None),
                         "note": (f"Produces {info.get('redraft_value') or 0:,} this season against the "
                                  f"{weakest['redraft_value'] or 0:,} of {weakest['name']} "
                                  f"({weakest['position']}), who starts - and every "
                                  f"{info['position']}-capable slot is held by someone better, so "
                                  f"none of it reaches the lineup today."
                                  + door_sentence
                                  + (f" {len(wanted)} team(s) are short at {info['position']}: "
                                     + ", ".join(f"{w['owner']} ({w['need_level']})" for w in wanted[:3])
                                     + " - start there."
                                     if wanted else
                                     f" No team in this league currently needs a "
                                     f"{info['position']}, so he will be hard to move at "
                                     f"anything like his value."))})

    def with_extras(result: dict) -> dict:
        """Value upgrades and cheap depth, computed after the buy path so its output can
        be excluded - the lists partition the space, and every list the buy path prints
        counts as surfaced whichever of them it landed in."""
        def buy_names(block: dict) -> set[str]:
            return {t["name"] for key in ("targets", "long_shots")
                    for t in block.get(key) or []}

        # A Middling result nests its whole buy side under `push` while `value_upgrades`
        # stays at the top, so anything shared between them has to look in both places.
        buy_side = result.get("push") or result
        surfaced = buy_names(result) | buy_names(buy_side)
        # Not for a rebuilder: "better to hold if you're winning now" is advice for
        # someone who is.
        if me["window"] != "Rebuild":
            upgrades = find_value_upgrades(my_roster, board, my_starters, me["window"],
                                           surfaced, buy_side.get("my_offers"),
                                           max_moves=max_per_position)
            if upgrades:
                result["value_upgrades"] = upgrades
                result["value_upgrade_note"] = VALUE_UPGRADE_NOTE
                surfaced |= {u["name"] for m in upgrades for u in m["returns"]
                             if not u.get("already_mine")}
        depth = _depth_adds(my_roster, board, me["window"] != "Rebuild",
                            my_starters, surfaced)
        if depth:
            result["depth_adds"] = depth
            result["depth_note"] = (DEPTH_NOTE_REBUILD if me["window"] == "Rebuild"
                                    else DEPTH_NOTE)
        return result

    stamped = (lambda r: {**r, "stance_note": stance_note} if stance_note else r)

    if me["window"] == "Rebuild":
        return stamped(with_extras({"me": _me_summary(me), "mode": "rebuild",
                            **_pivot_path(me, board, stranded, my_roster=my_roster,
                                          max_per_position=max_per_position)}))

    if me["window"] == "Middling":
        # Both directions are open, so show both - whichever makes sense usually depends
        # on how the season starts, which nothing here has yet.
        timing = (MIDDLING_TIMING_NOTE_RISING if me["trajectory"] == "rising"
                  else MIDDLING_TIMING_NOTE)
        return stamped(with_extras({"me": _me_summary(me), "mode": "middling", "timing_note": timing,
                "push": _buy_path(me, board, max_per_position, my_picks, covered, backfills),
                "pivot": _pivot_path(me, board, stranded, committed=False,
                                     my_roster=my_roster,
                                     max_per_position=max_per_position)}))

    result = {"me": _me_summary(me), "mode": "buy",
              **_buy_path(me, board, max_per_position, my_picks, covered, backfills)}
    if stance_note:
        result["stance_note"] = stance_note

    result = with_extras(result)
    if stranded:
        result["stranded"] = stranded
        result["stranded_note"] = STRANDED_NOTE

    # Additive on purpose - `window` is untouched: a contender tilting ascending contends
    # whichever path it takes, so this is a choice about HOW, not whether.
    conversions = _conversion_candidates(me, board)
    if conversions:
        result["choice_note"] = (PRESS_CHOICE_NOTE if me.get("alignment") == "unaligned"
                                 else CONTEND_CHOICE_NOTE)
        result["conversion_candidates"] = conversions
    return result


# What the report needs to know about the asking team - NOT its rosters. The full
# `team_state` row shipped here re-sent every cornerstone, sell candidate and surplus
# piece that `get_team_state` had just returned, 19% of the biggest payload, and the
# duplication was what pushed that payload past the 50KB wire limit where the SDK
# replaces the whole result with a 2KB preview (LOGIC.md, "The tool result the model
# never saw"). Roster lists have exactly one home.
# state/flavor/flavor_note are the pre-tier dialect - internal fields, never shipped
# to the model (two dialects in one payload is how answers contradict the chips).
_ME_FIELDS = ("owner", "owner_id", "roster_id", "window",
              "alignment", "path", "path_reason",
              "window_note", "window_edge", "next_first_note",
              "leverage", "leverage_note", "owns_next_first", "trajectory",
              "contention_rank", "of_teams", "pct_of_best", "starting_production",
              "ascending_pct", "declining_pct", "pick_capital", "no_trade_history")


def _me_summary(me: dict) -> dict:
    return {k: me[k] for k in _ME_FIELDS if k in me}


def offerable_names(result: dict) -> set[str]:
    """Every player this team could reasonably be told to trade away, across whichever
    path(s) its mode returned. Single source of truth for "is this a real give-up piece" -
    agent.py's grounding check reads this instead of re-deriving the mode logic."""
    if result["mode"] == "rebuild":
        return {e["name"] for e in result["sell_candidates"] + result["situational"]}
    if result["mode"] == "middling":
        return ({e["name"] for e in result["push"]["my_offers"]}
                | {e["name"] for e in result["pivot"]["sell_candidates"] + result["pivot"]["situational"]})
    return {e["name"] for e in result["my_offers"]}


def main(league_id: str, owner_query: str = None,
         max_per_position: int = DEFAULT_MAX_PER_POSITION) -> None:
    # Smoke test for the player surface: python -m analysis.trade_targets <league_id>
    # "player=rashee rice=jwall567" (second "=asker" part optional).
    if owner_query and owner_query.startswith("player="):
        from .report import _print_outlook
        name, _, asker = owner_query[len("player="):].partition("=")
        _print_outlook(player_outlook(league_id, name, asker or None))
        return
    if owner_query:
        _print_report(find_targets(league_id, owner_query, max_per_position))
        return
    # No owner given - run the whole league in one pass for eyeballing side by side.
    owners = [row["owner"] for row in team_state.classify_league(league_id)]
    for i, owner in enumerate(owners):
        if i > 0:
            print("\n" + "=" * 60 + "\n")
        _print_report(find_targets(league_id, owner, max_per_position))
