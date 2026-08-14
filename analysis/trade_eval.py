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
    "JUDGMENT, NOT A PRICE. Values appear per piece and are never summed - a package "
    "has no arithmetic total, and nothing here says which side 'wins by' an amount. "
    "Each side's `read` is the case for and against FROM THAT SIDE'S OWN SEAT; a trade "
    "can be right for both. Need changes are recomputed against the real league bar, "
    "not guessed from position labels.")


def evaluate_trade(league_id: str, owner_a: str, sends_a: list[str],
                   owner_b: str, sends_b: list[str]) -> dict:
    return evaluate_from_board(build_board(league_id), owner_a, sends_a, owner_b, sends_b)


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


def _side_read(state, receives, changes, lineup_delta, best, receives_best) -> list[str]:
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
    if state["window"] == "Rebuild":
        bar, horizon = MIN_MEANINGFUL_RUNWAY, ("a rebuild's next competitive season is "
                                               "past that runway, so he won't be part of it")
    elif state.get("ascending_pct", 0) > state.get("declining_pct", 0):
        bar, horizon = INSIDE_FINAL_YEAR, ("a roster tilting young is accumulating the "
                                           "seasons he won't be there for - the piece it "
                                           "should be selling, not buying")
    else:
        bar = None
    # `is not None` matters: a pick has no runway at all - it is the longest-dated
    # asset there is, the opposite of a piece at his edge - and `or 0` would flag it.
    for p in receives:
        if bar and p.get("years_to_decline") is not None and p["years_to_decline"] < bar:
            read.append(f"takes on {p['name']} with {p['years_to_decline']} yrs of "
                        f"runway - {horizon}")
    if state["window"] in ("Push", "Contend"):
        futures = [p["name"] for p in receives if p["position"] == "PICK"]
        if futures:
            read.append(f"takes back futures ({', '.join(futures)}) while built to win "
                        f"now - value that pays after the window, so the rest of the "
                        f"return has to carry this season")
    return read


def evaluate_from_board(board, owner_a: str, sends_a: list[str],
                        owner_b: str, sends_b: list[str]) -> dict:
    ctx = board.ctx
    a = ctx.pick_owner(owner_a, board.states)
    b = ctx.pick_owner(owner_b, board.states)
    if a["owner_id"] == b["owner_id"]:
        return {"ok": False, "problem": "both sides resolve to the same team"}
    rosters = {r["owner_id"]: r for r in ctx.rosters}

    # picks_by_owner is keyed by roster_id, not owner_id, despite the name.
    picks_of = lambda state: board.picks_by_owner.get(
        rosters[state["owner_id"]].get("roster_id"), [])
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
    needs_before = _league_needs_with(ctx, {})
    needs_after = _league_needs_with(ctx, after)

    best = max(pieces_a + pieces_b, key=lambda p: p["value"])
    best_to = b["owner"] if best in pieces_a else a["owner"]

    sides = []
    for state, sends, receives, changes_key in (
            (a, pieces_a, pieces_b, a["owner_id"]), (b, pieces_b, pieces_a, b["owner_id"])):
        changes = _need_changes(needs_before[changes_key], needs_after[changes_key])
        delta = (_lineup_production(ctx, after[changes_key])
                 - _lineup_production(ctx, rosters[changes_key]["players"]))
        sides.append({
            "owner": state["owner"], "window": state["window"],
            "trajectory": state.get("trajectory"),
            "sends": sends, "receives": receives,
            "need_changes": changes, "lineup_production_delta": delta,
            "read": _side_read(state, receives, changes, delta, best,
                               receives_best=(state["owner"] == best_to)),
        })
    return {"ok": True, "note": EVAL_NOTE,
            "best_piece": {**best, "to": best_to}, "sides": sides}


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
