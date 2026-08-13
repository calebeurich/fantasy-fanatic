"""One concrete proposed trade, judged for both sides.

Deliberately NOT a trade calculator: values ride on each piece and are never summed
into a package price, and no margin declares a winner - value is not additive across
players, and "wins by 214" is the exact claim this project refuses to make. The
judgment instead composes what analysis/ already knows:

- who gets the best SINGLE player in the deal (the consolidation principle - the side
  giving him up needs the rest of the deal to buy something specific)
- which holes each side opens or closes, recomputed against the same league-relative
  startable bar as roster_needs - the whole league re-assessed with the two rosters
  swapped, so only the trade moves the answer
- what each starting lineup actually gains or loses (fill_lineup - the same cascade
  logic behind optimal_lineup, because a vacated FLEX does not refill the way a
  manager assumes)
- timeline fit: an accumulating roster taking on a final-year piece is buying seasons
  that won't be there - the same INSIDE_FINAL_YEAR clock `_sells_him` uses

Smoke test:
    python -m analysis.trade_eval <league_id> "<owner_a>: name, name" "<owner_b>: name"
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


def _resolve(ctx, roster, owner_name, queries):
    """Player ids for each name, matched within the sending roster only - a trade can
    only send what the sender actually holds, so that is the only place to look."""
    ids, problems = [], []
    for q in queries:
        matches = [pid for pid in (roster["players"] or [])
                   if q.lower() in (ctx.players.get(pid, {}).get("name") or "").lower()]
        if len(matches) == 1:
            ids.append(matches[0])
        else:
            what = "matches several players" if matches else "is not"
            problems.append(f"'{q}' {what} on {owner_name}'s roster")
    return ids, problems


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
        read.append(f"gets the best single player in the deal ({best['name']}, "
                    f"{best['value']:,}) - consolidation favors the side holding him")
    else:
        read.append(f"sends the best single player ({best['name']}, {best['value']:,}) - "
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
    for p in receives:
        if bar and (p.get("years_to_decline") or 0) < bar:
            read.append(f"takes on {p['name']} with {p['years_to_decline']} yrs of "
                        f"runway - {horizon}")
    return read


def evaluate_from_board(board, owner_a: str, sends_a: list[str],
                        owner_b: str, sends_b: list[str]) -> dict:
    ctx = board.ctx
    a = ctx.pick_owner(owner_a, board.states)
    b = ctx.pick_owner(owner_b, board.states)
    if a["owner_id"] == b["owner_id"]:
        return {"ok": False, "problem": "both sides resolve to the same team"}
    rosters = {r["owner_id"]: r for r in ctx.rosters}

    ids_a, problems_a = _resolve(ctx, rosters[a["owner_id"]], a["owner"], sends_a)
    ids_b, problems_b = _resolve(ctx, rosters[b["owner_id"]], b["owner"], sends_b)
    if problems_a or problems_b:
        return {"ok": False, "problem": "; ".join(problems_a + problems_b)}

    pieces_a = [_piece(ctx, pid) for pid in ids_a]   # what A sends (B receives)
    pieces_b = [_piece(ctx, pid) for pid in ids_b]

    after = {a["owner_id"]: [p for p in rosters[a["owner_id"]]["players"]
                             if p not in ids_a] + ids_b,
             b["owner_id"]: [p for p in rosters[b["owner_id"]]["players"]
                             if p not in ids_b] + ids_a}
    needs_before = _league_needs_with(ctx, {})
    needs_after = _league_needs_with(ctx, after)

    best = max(pieces_a + pieces_b, key=lambda p: p["value"])
    best_to = b["owner"] if best in pieces_a else a["owner"]

    sides = []
    for state, sent_ids, receives, changes_key in (
            (a, ids_a, pieces_b, a["owner_id"]), (b, ids_b, pieces_a, b["owner_id"])):
        changes = _need_changes(needs_before[changes_key], needs_after[changes_key])
        delta = (_lineup_production(ctx, after[changes_key])
                 - _lineup_production(ctx, rosters[changes_key]["players"]))
        sides.append({
            "owner": state["owner"], "window": state["window"],
            "trajectory": state.get("trajectory"),
            "sends": [_piece(ctx, pid) for pid in sent_ids], "receives": receives,
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
