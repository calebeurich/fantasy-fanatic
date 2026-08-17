"""One concrete proposed trade, judged for both sides.

Deliberately NOT a trade calculator: values ride on each piece and are never summed
into a package price, and no margin declares a winner - value is not additive across
players, and "wins by 214" is the exact claim this project refuses to make. The
judgment instead composes what analysis/ already knows:

- who gets the best SINGLE piece in the deal, players and picks alike (the
  consolidation principle - the side giving it up needs the rest of the deal to buy
  something specific)
- which holes each side opens or closes, recomputed against the same league-relative
  startable bar as roster_needs - the whole league re-assessed with the two rosters
  swapped, so only the trade moves the answer
- what each starting lineup actually gains or loses (fill_lineup - the same cascade
  logic behind optimal_lineup, because a vacated FLEX does not refill the way a
  manager assumes)
- timeline fit: an accumulating roster taking on a final-year piece is buying seasons
  that won't be there - the same INSIDE_FINAL_YEAR clock `_sells_him` uses

Smoke test (picks resolve against the sender's owned picks, same fuzzy contract):
    python -m analysis.trade_eval <league_id> "<owner_a>: fannin, 2027 1st (early)" "<owner_b>: tee higgins"
"""

from . import roster_needs
from .team_values import (age_bucket, years_to_decline, INSIDE_FINAL_YEAR,
                          MIN_MEANINGFUL_RUNWAY)
from .trade_targets.board import build_board

EVAL_NOTE = (
    "JUDGMENT, NOT A PRICE. Each side is judged by the lens ITS OWN PATH sets: a buying "
    "path (contend, press) wants a better STARTING LINEUP - production after the lineup "
    "re-settles, holes closed and, just as important, holes newly opened; a selling path "
    "(sell, build) wants more DYNASTY VALUE overall, judged with package concerns (how "
    "many bodies for one, and the measured consolidation premium) rather than roster "
    "concerns; wait/decide sees both lenses. The BALLPARK line is what pieces of the best "
    "piece's tier have actually fetched in 461 real trades - shape (pieces back, centerpiece "
    "share, picks) - not a verdict: quote it as the tool's benchmark, never extend the "
    "arithmetic. Nothing here says which side 'wins by' an amount; a trade can be right "
    "for both seats.")

# What a piece of a given tier has ACTUALLY fetched, measured on crawled trades where the
# best piece stood alone on his side (research/stud_returns.py), keyed by that piece's
# value percentile at the time. Basis (2026-08-16, after the owner's plain-text review
# read the first table's "inside the band" as "close but not enough" on JT / Jefferson /
# Cook): NON-best-ball leagues only (the VEGAS best-ball ecosystem trades lighter - no
# lineup pressure), and 1sts priced at 4,500 / 2nds at 1,800 - the same scale as the
# FantasyCalc slotted pick values the framer prices OUR picks with, where a flat 3,200
# understated every early 1st and pulled the centerpiece band low. Each row: pieces
# back (median), centerpiece as a share of the stud (q1, median, q3), summed multiple
# (median), share of returns containing a 1st, share with no picks. Samples run 36-70
# per tier - bands are wide and honest about it. The consolidation premium measured on
# fc_trades (2-for-1 at 1.36x etc.) does NOT describe stud deals; the ballpark speaks
# SHAPE, tiered.
RETURN_SHAPES = [
    # (upper pct bound, label, pieces, (cp_q1, cp_med, cp_q3), summed, has_1st, no_picks)
    (0.02, "top-2%",   3, (0.50, 0.59, 0.71), 1.11, 0.56, 0.31),
    (0.05, "top-5%",   2, (0.64, 0.71, 0.84), 1.06, 0.55, 0.25),
    (0.10, "top-10%",  2, (0.44, 0.63, 0.90), 0.96, 0.25, 0.31),
    (0.20, "top-20%",  2, (0.36, 0.68, 0.82), 0.81, 0.00, 0.55),
    (0.35, "top-35%",  1, (0.21, 0.47, 0.78), 0.56, 0.00, 0.91),
    (1.01, "mid-tier", 1, (0.22, 0.39, 0.92), 0.39, 0.00, 1.00),
]


def _shape_for(pct: float):
    for bound, *rest in RETURN_SHAPES:
        if pct < bound:
            return rest
    return RETURN_SHAPES[-1][1:]


def _value_percentile(value: int, players: dict) -> float:
    """Where a piece sits in this league's valued player pool, 0.0 = the top."""
    vals = sorted((i.get("value") or 0 for i in players.values() if i.get("value")), reverse=True)
    if not vals:
        return 1.0
    return sum(1 for v in vals if v > value) / len(vals)


# How far past his breakpoint a piece has to be before a buyer's note calls him a
# rental rather than declining (owner: "JT isn't a rental, he's in the decline window,
# not past the cliff" at -0.6; Barkley at -2.5 and Henry at -5.6 are).
RENTAL_DEPTH_YEARS = 2.0

BUY_PATHS = ("contend", "press")
SELL_PATHS = ("sell", "build")


def _lens(path: str) -> str:
    word = (path or "").split(" - ")[0]
    if word in BUY_PATHS:
        return "lineup"
    if word in SELL_PATHS:
        return "value"
    return "both"


def evaluate_trade(league_id: str, owner_a: str, sends_a: list[str],
                   owner_b: str, sends_b: list[str],
                   stance_a: str | None = None, stance_b: str | None = None) -> dict:
    out = evaluate_from_board(build_board(league_id), owner_a, sends_a, owner_b, sends_b,
                              stance_a, stance_b)
    out.pop("_after", None)
    return out


# A manager who declares a branch ("kieran wants to pivot", "I'm pressing this year")
# outranks the chip's lean for the lens - the same rule as get_trade_targets' stance.
STANCE_LENS = {"press": "lineup", "contend": "lineup", "buy": "lineup", "push": "lineup",
               "sell": "value", "build": "value", "pivot": "value", "rebuild": "value",
               "wait": "both", "decide": "both"}


def _resolve(ctx, roster, owned_picks, owner_name, queries):
    """Each name matched within what the sender actually holds - roster players first,
    then owned picks ('2027 1st', '2026 Pick 1.03'). 25 of the 28 real trades across
    the validation leagues included a pick, so picks are the main case, not an extra."""
    ids, picks, problems = [], [], []
    for q in queries:
        matches = [pid for pid in (roster["players"] or [])
                   if q.lower() in (ctx.players.get(pid, {}).get("name") or "").lower()]
        if len(matches) == 1:
            ids.append(matches[0])
            continue
        if len(matches) > 1:
            problems.append(f"'{q}' matches several players on {owner_name}'s roster")
            continue
        pick_matches = [p for p in owned_picks if q.lower() in p["pick"].lower()]
        # Two picks can be literally identical (a team holding two "2028 1st"s) - the
        # user can't disambiguate those by name and doesn't need to; take the first.
        if len({(p["pick"], p["value"]) for p in pick_matches}) == 1:
            pick_matches = pick_matches[:1]
        if len(pick_matches) == 1:
            picks.append(pick_matches[0])
        elif pick_matches:
            names = ", ".join(p["pick"] for p in pick_matches)
            problems.append(f"'{q}' matches several of {owner_name}'s picks ({names})")
        else:
            problems.append(f"'{q}' is not on {owner_name}'s roster (players or picks)")
    return ids, picks, problems


def _piece(ctx, pid):
    info = ctx.players[pid]
    return {"name": info["name"], "position": info["position"], "value": info["value"],
            "redraft_value": info.get("redraft_value"), "age": info["age"],
            "bucket": age_bucket(info["position"], info["age"], info.get("usage_role")),
            "years_to_decline": years_to_decline(info["position"], info["age"],
                                                 info.get("usage_role"))}


def _league_needs_with(ctx, replaced: dict[str, list]) -> dict:
    """assess_positions over the whole league with some rosters swapped out. Declared
    starters are stale in a hypothetical, so they are omitted (before AND after, so the
    diff can only come from the trade) - that only costs the injury-exposure notes."""
    rosters = [{**r, "players": replaced.get(r["owner_id"], r["players"])}
               for r in ctx.rosters]
    return roster_needs.assess_positions(rosters, ctx.players, ctx.needs_slots,
                                         ctx.start_thresholds, None,
                                         (ctx.lineup_dedicated, ctx.lineup_flex))


def _lineup_production(ctx, player_ids) -> int:
    filled = roster_needs.fill_lineup({"players": player_ids}, ctx.players,
                                      ctx.lineup_dedicated, ctx.lineup_flex)
    return round(sum(ctx.players[pid].get("redraft_value") or 0 for _, pid in filled))


def _need_changes(before: dict, after: dict) -> list[dict]:
    changes = []
    for pos in sorted(set(before) | set(after)):
        lb = before.get(pos, {}).get("level", "ok")
        la = after.get(pos, {}).get("level", "ok")
        if lb != la:
            worse = (roster_needs.NEED_PRIORITY.get(la, 3)
                     < roster_needs.NEED_PRIORITY.get(lb, 3))
            changes.append({"position": pos, "before": lb, "after": la,
                            "direction": "opens" if worse else "closes"})
    return changes


def _throw_in(piece: dict, trade_bars: dict, start_bars: dict) -> bool:
    """A body below both the trade floor and the startable bar - the same test the offer
    floor uses - is a throw-in, not a piece (owner: Quinn Ewers at 254 "is worth nothing
    and shouldn't be mentioned as notable"). Picks are never throw-ins."""
    if piece["position"] == "PICK":
        return False
    return (piece["value"] <= trade_bars.get(piece["position"], 0)
            and (piece.get("redraft_value") or 0) < start_bars.get(piece["position"], 0))


def _package_read(receives: list[dict], best: dict, receives_best: bool, pct: float,
                  trade_bars: dict = None, start_bars: dict = None) -> str | None:
    """The shape ballpark for the side sending the deal's best piece: what pieces of his
    tier have actually fetched, held against what this return looks like. Sums appear
    here and nowhere else - the benchmark was measured that way. Silent when this side
    holds the best piece (nothing to benchmark) or the piece is mid-tier and the return
    is a single player (a plain swap - the shape table has nothing to add)."""
    if receives_best or best["position"] == "PICK":
        return None  # the shape table describes players; a pick-for-pick swap has no comp here
    label, pieces, (q1, med, q3), summed, has_first, no_picks = _shape_for(pct)
    throw_ins = [p for p in receives
                 if trade_bars and start_bars and _throw_in(p, trade_bars, start_bars)]
    real = [p for p in receives if p not in throw_ins]
    if len(real) < 2 and label == "mid-tier":
        return None
    n = len(real)
    picks = [p for p in real if p["position"] == "PICK"]
    firsts = [p for p in picks if "1st" in p["name"]]
    cp = max((p["value"] for p in real), default=0) / best["value"] if best["value"] else 0
    ratio = sum(p["value"] for p in real) / best["value"] if best["value"] else 0
    band = ("below the usual band" if cp < q1 else "above the usual band" if cp > q3
            else "in the low half of the usual band" if cp < med
            else "in the high half of the usual band")
    extra = (f" plus {len(throw_ins)} throw-in{'s' if len(throw_ins) != 1 else ''} "
             f"({', '.join(p['name'] for p in throw_ins)} - below both the trade floor and "
             f"the startable bar, not counted)" if throw_ins else "")
    this = (f"THIS RETURN: {n} piece{'s' if n != 1 else ''} ({len(picks)} pick{'s' if len(picks) != 1 else ''}"
            f"{', ' + str(len(firsts)) + ' of them 1sts' if firsts else ''}){extra}, centerpiece "
            f"{cp:.2f}x of {best['name']} - {band} - summing to {ratio:.2f}x.")
    wide = (" The band is wide at this tier - a thin sample, so where a return sits in it "
            "means less than at the top." if q3 - q1 >= 0.4 else "")
    usual = (f"BALLPARK for a {label} piece, from real trades: {pieces} piece{'s' if pieces != 1 else ''} back, "
             f"centerpiece {q1:.2f}-{q3:.2f}x of him (median {med:.2f}), summing to ~{summed:.2f}x; "
             f"{round(has_first * 100)}% of returns include a 1st, {round(no_picks * 100)}% include no pick at all.{wide}")
    tail = ""
    if n >= 4:
        tail = (" Four-plus-piece returns are rare (12-24% only for top-5% pieces, and nearly always "
                "pick-inclusive; of 18 measured four-piece stud returns exactly one was all players) - "
                "bodies cost roster spots.")
    if label in ("top-2%", "top-5%", "top-10%") and not picks:
        tail += " Studs of this tier usually bring back a 1st; this return has none."
    return f"{usual} {this}{tail} A benchmark for the ask, not a fairness verdict."


def _goal_line(lens: str, lineup_delta: int, changes: list[dict], value_in: int,
               value_out: int, n_in: int, n_out: int) -> str:
    """One sentence per side saying whether the trade serves what that side's path is
    FOR - a better starting lineup for buyers, more dynasty value for sellers."""
    opened = [c["position"] for c in changes if c["direction"] == "opens"]
    closed = [c["position"] for c in changes if c["direction"] == "closes"]
    sign = "+" if lineup_delta >= 0 else ""
    lineup = (f"starting lineup {sign}{lineup_delta:,} after it re-settles"
              + (f", closes {'/'.join(closed)}" if closed else "")
              + (f", but OPENS A NEW HOLE at {'/'.join(opened)}" if opened else ""))
    value = (f"dynasty value {value_in:,} in for {value_out:,} out, across "
             f"{n_in} piece{'s' if n_in != 1 else ''} received / {n_out} sent")
    if lens == "lineup":
        return f"GOAL for a buying path is a better starting lineup: {lineup}."
    if lens == "value":
        return f"GOAL for a selling path is more dynasty value overall: {value}."
    return f"Both doors open here - lineup lens: {lineup}; value lens: {value}."


def _side_read(state, receives, changes, lineup_delta, best, receives_best,
               best_pct: float = 1.0, lens: str = "both", sends: list[dict] = (),
               trade_bars: dict = None, start_bars: dict = None) -> list[str]:
    read = []
    if receives_best:
        read.append(f"gets the best single piece in the deal ({best['name']}, "
                    f"{best['value']:,}) - consolidation favors the side holding him")
    else:
        read.append(f"sends the best single piece ({best['name']}, {best['value']:,}) - "
                    f"the return has to buy something specific to be worth that")
    for c in changes:
        if c["direction"] == "closes":
            read.append(f"closes the {c['position']} need ({c['before']} -> {c['after']})")
        else:
            read.append(f"opens a hole at {c['position']} ({c['before']} -> {c['after']})")
    if lineup_delta:
        read.append(f"starting production {'+' if lineup_delta > 0 else ''}{lineup_delta:,} "
                    f"this season, after the lineup re-settles")
    # Timeline check on what this side takes back, with the bar matched to how far out
    # its window is. A true Rebuild's next competitive season is beyond the buyer's
    # two-season horizon, so anything under MIN_MEANINGFUL_RUNWAY won't be part of it;
    # a roster that is merely tilting young is closer in, and only a piece at his own
    # edge (INSIDE_FINAL_YEAR, the _sells_him clock) is the trade backwards.
    path = state.get("path", "")
    if lens == "value":
        bar, horizon = MIN_MEANINGFUL_RUNWAY, ("a rebuild's next competitive season is "
                                               "past that runway, so he won't be part of it")
    elif lens == "both":
        # Both doors open: aging production fits the push door and not the wait door -
        # said as which door it belongs to, never as a scolding.
        bar, horizon = INSIDE_FINAL_YEAR, ("a push-door piece: this season's production, "
                                           "not next season's - buying him is choosing the "
                                           "push door")
    elif lens == "lineup":
        # A buyer taking aging production is the direction gate working, not a mistake -
        # said as a fact about the piece, never as a scolding (owner: a contender being
        # told a rental is "the piece it should be selling" was wrong). Two depths, per
        # the owner: just past the breakpoint is DECLINING (still producing, resale
        # shrinking - JT), deep past it is a RENTAL (this season is the whole purchase -
        # Barkley, Henry).
        bar, horizon = INSIDE_FINAL_YEAR, None
    else:
        bar = None
    # `is not None` matters: a pick has no runway at all - it is the longest-dated
    # asset there is, the opposite of a piece at his edge - and `or 0` would flag it.
    for p in receives:
        y = p.get("years_to_decline")
        if bar and y is not None and y < bar:
            if horizon is None:
                depth = ("a rental - this season's production is the whole purchase"
                         if y <= -RENTAL_DEPTH_YEARS else
                         "declining, not done - priced on production, resale value "
                         "shrinking from here")
                read.append(f"takes on {p['name']} with {y} yrs of runway - {depth}")
            else:
                read.append(f"takes on {p['name']} with {y} yrs of runway - {horizon}")
    # Only an ALIGNED contender is scolded for taking back futures: for a press team,
    # converting the aging half into future value IS its path (redline from the first
    # spot check - it read the window and told a press team off for its own move).
    if path.split(" - ")[0] == "contend" and lens == "lineup":
        futures = [p["name"] for p in receives if p["position"] == "PICK"]
        # Swapping picks for picks is not "taking back futures" - only a net intake is.
        if futures and len(futures) > sum(1 for p in sends if p["position"] == "PICK"):
            read.append(f"takes back futures ({', '.join(futures)}) while built to win "
                        f"now - value that pays after the window, so the rest of the "
                        f"return has to carry this season")
    # A seller judges the return by whether its centerpiece is on the timeline he is
    # accumulating for - not whether it starts (owner: "Egbuka is about jq's timeline,
    # not his starting roster").
    if lens == "value" and receives:
        cp = max(receives, key=lambda p: p["value"])
        y = cp.get("years_to_decline")
        if cp["position"] == "PICK":
            read.append(f"the centerpiece of the return is a pick ({cp['name']}) - the "
                        f"longest-dated asset there is, squarely on a rebuild's timeline")
        elif y is not None:
            fits = y >= MIN_MEANINGFUL_RUNWAY
            read.append(f"the centerpiece of the return is {cp['name']} ({y} yrs of runway) - "
                        + ("on the timeline this roster is accumulating for" if fits else
                           "NOT on this roster's timeline; he will be past his edge before "
                           "the next competitive season"))
    package = _package_read(receives, best, receives_best, best_pct, trade_bars, start_bars)
    if package:
        read.append(package)
    return read


def evaluate_from_board(board, owner_a: str, sends_a: list[str],
                        owner_b: str, sends_b: list[str],
                        stance_a: str | None = None, stance_b: str | None = None,
                        players_override: dict | None = None,
                        picks_override: dict | None = None) -> dict:
    """`players_override` / `picks_override` (owner_id -> list) are the rosters an
    earlier leg of a sequence produced; absent, the live rosters."""
    ctx = board.ctx
    a = ctx.pick_owner(owner_a, board.states)
    b = ctx.pick_owner(owner_b, board.states)
    if a["owner_id"] == b["owner_id"]:
        return {"ok": False, "problem": "both sides resolve to the same team"}
    rosters = {r["owner_id"]: (dict(r, players=players_override[r["owner_id"]])
                               if players_override and r["owner_id"] in players_override else r)
               for r in ctx.rosters}

    # picks_by_owner is keyed by roster_id, not owner_id, despite the name.
    def picks_of(state):
        if picks_override and state["owner_id"] in picks_override:
            return picks_override[state["owner_id"]]
        return board.picks_by_owner.get(rosters[state["owner_id"]].get("roster_id"), [])
    ids_a, picks_a, problems_a = _resolve(ctx, rosters[a["owner_id"]], picks_of(a),
                                          a["owner"], sends_a)
    ids_b, picks_b, problems_b = _resolve(ctx, rosters[b["owner_id"]], picks_of(b),
                                          b["owner"], sends_b)
    if problems_a or problems_b:
        return {"ok": False, "problem": "; ".join(problems_a + problems_b)}

    # A pick rides as a piece with a value and its slot basis; it never enters the
    # needs or lineup math because it fills no slot this season.
    def _pick_piece(p):
        return {"name": p["pick"], "position": "PICK", "value": p["value"],
                "slot_basis": p["slot_basis"]}

    pieces_a = [_piece(ctx, pid) for pid in ids_a] + [_pick_piece(p) for p in picks_a]
    pieces_b = [_piece(ctx, pid) for pid in ids_b] + [_pick_piece(p) for p in picks_b]

    after = {a["owner_id"]: [p for p in rosters[a["owner_id"]]["players"]
                             if p not in ids_a] + ids_b,
             b["owner_id"]: [p for p in rosters[b["owner_id"]]["players"]
                             if p not in ids_b] + ids_a}
    # Picks move too, so a later leg can send what this one brought in and cannot
    # send what it gave away.
    picks_after = {a["owner_id"]: [p for p in picks_of(a) if p not in picks_a] + picks_b,
                   b["owner_id"]: [p for p in picks_of(b) if p not in picks_b] + picks_a}
    base = players_override or {}
    needs_before = _league_needs_with(ctx, base)
    needs_after = _league_needs_with(ctx, {**base, **after})

    best = max(pieces_a + pieces_b, key=lambda p: p["value"])
    best_to = b["owner"] if best in pieces_a else a["owner"]
    best_pct = _value_percentile(best["value"], ctx.players) if best["position"] != "PICK" else 1.0

    sides = []
    for state, sends, receives, changes_key, stance in (
            (a, pieces_a, pieces_b, a["owner_id"], stance_a),
            (b, pieces_b, pieces_a, b["owner_id"], stance_b)):
        changes = _need_changes(needs_before[changes_key], needs_after[changes_key])
        delta = (_lineup_production(ctx, after[changes_key])
                 - _lineup_production(ctx, rosters[changes_key]["players"]))
        lens = _lens(state.get("path", ""))
        declared = STANCE_LENS.get((stance or "").lower())
        stance_note = None
        if declared and declared != lens:
            stance_note = (f"MANAGER-DECLARED BRANCH: judged on the {declared} lens because "
                           f"the manager chose '{stance}'; the measured read (path: "
                           f"{state.get('path')}) would use the {lens} lens - present the "
                           f"declared branch as their choice and say where the measured "
                           f"read would push back.")
            lens = declared
        value_in = sum(p["value"] for p in receives)
        value_out = sum(p["value"] for p in sends)
        sides.append({
            "owner": state["owner"], "path": state.get("path"),
            "alignment": state.get("alignment"), "lens": lens,
            "sends": sends, "receives": receives,
            "need_changes": changes, "lineup_production_delta": delta,
            "goal": _goal_line(lens, delta, changes, value_in, value_out,
                               len(receives), len(sends)),
            **({"stance_note": stance_note} if stance_note else {}),
            "read": _side_read(state, receives, changes, delta, best,
                               receives_best=(state["owner"] == best_to),
                               best_pct=best_pct, lens=lens, sends=sends,
                               trade_bars=board.thresholds,
                               start_bars=getattr(ctx, "start_thresholds", None) or {}),
        })
    return {"ok": True, "note": EVAL_NOTE,
            "best_piece": {**best, "to": best_to}, "sides": sides,
            "_after": {"players": {**base, **after},
                       "picks": {**(picks_override or {}), **picks_after}}}


SEQUENCE_NOTE = (
    "A SEQUENCE: each leg is judged on the rosters the legs before it produced, so a "
    "later leg can close a hole an earlier one opened (a consolidation move plus its "
    "backfill), and `cumulative` is each team's net position against TODAY - lineup "
    "after everything re-settles, needs from before the first leg to after the last. "
    "Judge the plan by the cumulative line and each leg by its own reads.")


# Two legs: a move and its backfill. Longer chains are where reasoning goes to die -
# every leg compounds the guesses about who says yes (owner: "we don't want it
# reasoning infinite or even long and difficult chains").
MAX_SEQUENCE_LEGS = 2


def evaluate_sequence(league_id: str, legs: list[dict]) -> dict:
    """Legs in order, each {owner_a, sends_a, owner_b, sends_b, stance_a?, stance_b?}."""
    if len(legs) > MAX_SEQUENCE_LEGS:
        return {"ok": False, "problem": f"a sequence is at most {MAX_SEQUENCE_LEGS} legs - a move "
                                        f"and its backfill; judge longer plans two legs at a time"}
    if len(legs) < 2:
        return {"ok": False, "problem": "a sequence needs two legs; use evaluate_trade for one"}
    board = build_board(league_id)
    ctx = board.ctx
    players, picks, out_legs = None, None, []
    for i, leg in enumerate(legs, 1):
        res = evaluate_from_board(board, leg["owner_a"], leg["sends_a"], leg["owner_b"],
                                  leg["sends_b"], leg.get("stance_a"), leg.get("stance_b"),
                                  players_override=players, picks_override=picks)
        if not res["ok"]:
            return {"ok": False, "problem": f"leg {i}: {res['problem']}"}
        after = res.pop("_after")
        players, picks = after["players"], after["picks"]
        out_legs.append({"leg": i, **res})
    original = {r["owner_id"]: r["players"] for r in ctx.rosters}
    needs_start = _league_needs_with(ctx, {})
    needs_end = _league_needs_with(ctx, players)
    cumulative = {
        ctx.owner_names[oid]: {
            "lineup_production_delta": (_lineup_production(ctx, players[oid])
                                        - _lineup_production(ctx, original[oid])),
            "need_changes": _need_changes(needs_start[oid], needs_end[oid]),
        }
        for oid in players}
    return {"ok": True, "note": SEQUENCE_NOTE, "legs": out_legs, "cumulative": cumulative}


def main():
    import json
    import sys

    league_id = sys.argv[1]
    (owner_a, names_a), (owner_b, names_b) = (
        (part.split(":")[0].strip(), [n.strip() for n in part.split(":")[1].split(",")])
        for part in sys.argv[2:4])
    print(json.dumps(evaluate_trade(league_id, owner_a, names_a, owner_b, names_b),
                     indent=2))


if __name__ == "__main__":
    main()
