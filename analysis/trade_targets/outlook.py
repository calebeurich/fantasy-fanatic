"""One player, both sides of the phone call.

Every other surface answers a TEAM question ("who should I target"), so a friend asking
about a SPECIFIC player ("how do I trade for Rashee Rice?") got told he "isn't a trade
target" - absence from a ranked, capped, need-filtered list read back as a verdict.
Absence has five meanings (not a need position, capped out, below the floor, non-seller,
trimmed for wire size) and none of them is "unavailable". This composes machinery that
already exists - seller-ness, the why-they-would-listen cases, the counterparty fit -
around one named player instead of around a team. Discovery, not a calculator: it never
prices a deal.
"""

from ..team_values import age_bucket, years_to_decline
from .. import team_state
from .board import Board, build_board, _best_chip, _buy_friction, _sells_him
from .buy import _my_offer_pool
from .counterparty import _counterparty_fit, _why_they_would_move_him

OUTLOOK_NOTE = (
    "ONE PLAYER, BOTH SIDES OF THE CALL. `availability` is why his owner would or would "
    "not move him; `your_fit` is what of yours that owner would want back - any ONE of "
    "them, never a bundle, and nothing here says the two sides are worth the same. This "
    "tool does not price trades: it tells you whether the call is worth making and how to "
    "open it. A player can be absent from get_trade_targets' ranked lists and still be "
    "entirely gettable - those lists answer 'who fits best', not 'who is available'."
)


def _normalize(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum())


def _find_player(ctx, player_query: str):
    """(player_id, info) for the best name match, or a list of candidate names when the
    query is ambiguous. Same normalization as owner matching - people type from memory."""
    q = _normalize(player_query)
    matches = [(pid, info) for pid, info in ctx.players.items()
               if q in _normalize(info.get("name") or "")]
    exact = [(pid, info) for pid, info in matches if _normalize(info["name"]) == q]
    if exact:
        return exact[0]
    names = sorted({info["name"] for _, info in matches})
    if len(names) == 1:
        return matches[0]
    return names  # empty = not found, several = ambiguous


def player_outlook(league_id: str, player_query: str, asker_query: str | None = None) -> dict:
    return outlook_from_board(build_board(league_id), player_query, asker_query)


def outlook_from_board(board: Board, player_query: str,
                       asker_query: str | None = None) -> dict:
    ctx = board.ctx

    found = _find_player(ctx, player_query)
    if isinstance(found, list):
        if not found:
            return {"found": False,
                    "note": f"No player matching '{player_query}' in this league's data."}
        return {"found": False, "candidates": found[:8],
                "note": f"'{player_query}' matches several players - which one?"}
    player_id, info = found

    owner_roster = next((r for r in ctx.rosters
                         if player_id in (r["players"] or [])), None)
    bucket = age_bucket(info["position"], info["age"], info.get("usage_role"))
    player = {"name": info["name"], "position": info["position"], "value": info["value"],
              "redraft_value": info.get("redraft_value"), "age": info["age"],
              "bucket": bucket,
              "years_to_decline": years_to_decline(info["position"], info["age"],
                                                   info.get("usage_role"))}

    if owner_roster is None:
        return {"found": True, "player": player, "rostered": False,
                "note": ("Unrostered - nobody to call. If he clears the waiver bar he "
                         "belongs to get_waiver_upgrades, not a trade.")}

    owner = next(r for r in board.states if r["owner_id"] == owner_roster["owner_id"])
    # The owner's own list entry carries the fields the notes hang off (price_note,
    # is_cornerstone, is_starter); a small player below every list is still askable.
    entry = next((e for e in owner["sellable"] + owner["tradeable_surplus"]
                  if e["name"] == info["name"]), player)
    player = {**player, **entry}

    result = {"found": True, "player": player,
              "owner": owner["owner"], "owner_window": owner["window"],
              "owner_flavor": owner["flavor"], "owner_window_note": owner["window_note"],
              "owner_is_short_at": {pos: n["level"] for pos, n in
                                    board.needs_by_owner_id.get(owner["owner_id"], {}).items()},
              "note": OUTLOOK_NOTE}

    asker = ctx.pick_owner(asker_query, board.states) if asker_query else None
    if asker and asker["owner_id"] == owner["owner_id"]:
        return {**result, "already_yours": True,
                "availability": ("Already on your roster - the question is whether to "
                                 "keep or sell him, which is get_trade_targets' job.")}

    if _sells_him(owner, entry):
        result["availability"] = (
            f"{owner['owner']} is a seller of exactly this kind of piece "
            f"({owner['state']}, {owner['flavor']}) - no persuasion needed, this is a "
            f"price conversation from the first call.")
    else:
        ratio = ((entry.get("redraft_value") or 0) / entry["value"]) if entry.get("value") else 0
        counts = board.trade_counts
        moved = _why_they_would_move_him(
            entry, owner, board.prior.get(owner["owner_id"]), board.premium_bars,
            never_trades=(not counts.get(owner["owner_id"])
                          and board.others_have_traded(owner["owner_id"])))
        result["availability"] = moved["their_reason"]
        result["friction"] = moved.get("friction") or []

    if asker:
        my_needs = board.needs_by_owner_id.get(asker["owner_id"], {})
        my_offers = _my_offer_pool(asker, board, my_needs)
        fit = _counterparty_fit(owner, board.needs_by_owner_id.get(owner["owner_id"], {}),
                                my_offers, target=entry)
        if fit:
            result["your_fit"] = fit
        else:
            result["your_fit"] = {
                "why_it_fits": (f"No obvious fit: {owner['owner']} has no positional hole "
                                f"your spare pieces cover, and nothing of yours matches "
                                f"what their window wants. The ask stands on price alone.")}
        best = _best_chip(my_offers)
        result.setdefault("friction", [])
        existing = {f["flavor"] for f in result["friction"]}
        for f in _buy_friction(entry, owner, best,
                               board.trade_counts.get(owner["owner_id"], 0),
                               board.others_have_traded(asker["owner_id"]))["friction"]:
            if f["flavor"] not in existing:
                result["friction"].append(f)

    return result
