"""Trade ideas: concrete starting points between two teams, never price verdicts.

Built from what trade_targets already computes for each side (who a buyer targets and
what that owner would take; what a rebuild collects and what it sells), balanced into a
dynasty-value band by the light side's sweeteners, filtered to what BOTH teams would
actually do, and ranked per lens. Deterministic - no model. Every rule and the owner
quote behind it: LOGIC.md "Trade ideas". The composer's tags (`fits`): LOGIC.md
"Composer tags".

Smoke test: python -m analysis.trade_ideas <league_id> <owner>
"""

import sys
from dataclasses import dataclass

from . import roster_needs, team_state, trade_eval, trade_targets
from .league import context
from .team_values import age_bucket, eppg, years_to_decline
from .trade_targets.board import build_board

BAND_LO, BAND_HI = 0.9, 1.2   # the buyer sends 0.9x-1.2x of what he gets, in dynasty value
BAND_TOLERANCE = 0.04         # a hair outside the band isn't worth another piece
BUYER_DIP_OK = 1.0            # ppg a buyer's lineup may lose and the idea still be a "buy"
UPGRADE_EDGE = 1.05           # an upgrade swap's target out-projects the starter he replaces
UPGRADE_CHIP_GRACE = 0.9      # ...who may sit this far under the trade bar and still go
CENTERPIECE_TOP = 0.35        # value percentile under which sums lie - the centerpiece must hold
MAX_PER_SIDE = 3              # candidate pieces considered per target / per sell list
CONSOLIDATION_POOL = 4        # names considered for two-piece consolidations


@dataclass
class _Pair:
    """Everything one pair of teams' ideas are judged against, computed once. Keyed by
    side ("a" / "b") where it is per team."""
    league_id: str
    owners: dict            # side -> owner name
    stances: dict           # side -> declared stance or None
    results: dict           # side -> trade_targets.find_targets result
    starters: dict          # side -> names in the projected lineup
    movable: dict           # side -> names it would move (offerable, minus ascending situational)
    wish: dict              # side -> its rebuild wish list
    picks: dict             # side -> [(pick name, value)] it would spend, by round
    wants_picks: dict       # side -> takes picks at all (a contender wants production)
    value: dict             # name -> dynasty value
    eppg: dict              # name -> projected points a game
    position: dict          # name -> position (picks are absent)
    runway: dict            # name -> years to decline
    bars: dict              # position -> trade-relevance bar
    players: dict           # the league's player pool, for value percentiles
    qb_stock: dict          # rebuild owner -> young QB chips held
    qb_slots: int
    board: object = None    # trade_eval's board, for the buyer's lineup delta


def _branches(result: dict) -> list[dict]:
    return [result] + [x for x in (result.get("push"), result.get("pivot")) if isinstance(x, dict)]


def _still_gaining(result: dict) -> set[str]:
    """A rebuild's ascending situational pieces - what it is collecting, never idea material."""
    sit = result.get("situational") or (result.get("pivot") or {}).get("situational") or []
    return {e["name"] for e in sit if e.get("bucket") == "ascending"}


def _spendable_picks(result: dict) -> list[tuple[str, float]]:
    picks = result.get("picks_to_trade_away") or (result.get("push") or {}).get("picks_to_trade_away") or []
    return [(pk["pick"].split(" (")[0], pk["value"]) for pk in sorted(picks, key=lambda x: x["round"])]


def _young_qb_chips(ctx, owner: str, qb_bar: float) -> int:
    return sum(1 for pid in ctx.roster_for(owner)["players"] or [] if pid in ctx.players
               and (pl := ctx.players[pid])["position"] == "QB" and pl["value"] >= qb_bar
               and age_bucket("QB", pl["age"], pl.get("usage_role")) == "ascending")


def _pair(league_id: str, a: str, b: str, stance_a: str | None, stance_b: str | None) -> _Pair:
    ctx = context(league_id)
    owners = {"a": a, "b": b}
    results = {"a": trade_targets.find_targets(league_id, a, stance=stance_a or None),
               "b": trade_targets.find_targets(league_id, b, stance=stance_b or None)}
    bars = ctx.trade_thresholds
    return _Pair(
        league_id=league_id, owners=owners, stances={"a": stance_a, "b": stance_b},
        results=results,
        starters={s: [ctx.players[pid]["name"] for pid in ctx.starters_for(ctx.roster_for(o))
                      if pid in ctx.players] for s, o in owners.items()},
        movable={s: trade_targets.offerable_names(r) - _still_gaining(r) for s, r in results.items()},
        wish={s: {t["name"] for t in r.get("acquire_targets") or []} for s, r in results.items()},
        picks={s: _spendable_picks(r) for s, r in results.items()},
        wants_picks={s: r["mode"] != "buy" for s, r in results.items()},
        value={p["name"]: p["value"] for p in ctx.players.values()},
        eppg={p["name"]: p.get("projected_ppg") or 0 for p in ctx.players.values()},
        position={p["name"]: p["position"] for p in ctx.players.values()},
        runway={p["name"]: years_to_decline(p["position"], p["age"], p.get("usage_role"))
                for p in ctx.players.values()},
        bars=bars, players=ctx.players,
        qb_stock={owners[s]: _young_qb_chips(ctx, owners[s], bars.get("QB", 0))
                  for s, r in results.items() if r.get("mode") == "rebuild"},
        qb_slots=ctx.needs_slots.get("QB", 1),
        board=build_board(league_id))


def consolidations(names: list[str], starts, is_depth) -> list[tuple[str, str]]:
    """Two pieces for one deal: at least one STARTS for the receiver, the other starts or
    is depth for him - never two depth pieces."""
    names = [n for n in names if starts(n) or is_depth(n)][:CONSOLIDATION_POOL]
    return [(p1, p2) for i, p1 in enumerate(names) for p2 in names[i + 1:] if starts(p1) or starts(p2)]


def _starts_tag(tag: str) -> bool:
    return tag.startswith(("starts", "level", "beats"))


def _proposals(p: _Pair, me: str, them: str) -> list[dict]:
    """Every raw idea `me` would open with toward `them`, unbalanced (lens from `me`'s side)."""
    side = "a" if p.owners["a"] == me else "b"
    result = p.results[side]
    offerable = trade_targets.offerable_names(result)
    out = []
    for r in _branches(result):
        for t in (r.get("targets") or []) + (r.get("long_shots") or []):
            if t.get("from_owner") != them:
                continue
            slot = t.get("for_slot") or t.get("position")
            gives = [n for n in (t.get("offer_any_one_of") or []) if n in offerable][:MAX_PER_SIDE]
            for give in gives:
                out.append({"a_sends": [give], "b_sends": [t["name"]], "lens": "buy",
                            "why": f"{me} fills a {slot} hole with {t['name']}; {them} would take {give}"})
            # The upgrade swap: the starter he replaces goes back the other way.
            for mine in p.starters[side]:
                if (p.position.get(mine) == p.position.get(t["name"])
                        and p.value.get(mine, 0) < p.value.get(t["name"], 0)
                        and p.eppg.get(t["name"], 0) > UPGRADE_EDGE * p.eppg.get(mine, 0)):
                    out.append({"a_sends": [mine], "b_sends": [t["name"]], "lens": "buy", "upgrade": mine,
                                "why": f"{me} upgrades {mine} to {t['name']} at {p.position.get(mine)}"})
            if not gives:   # nothing named they'd take: pay in picks (_balance finds them)
                out.append({"a_sends": [], "b_sends": [t["name"]], "lens": "buy",
                            "why": f"{me} fills a {slot} hole with {t['name']}"})
        # Production a seller is moving, paid for in picks - alone, or two at once.
        adds = [t["name"] for t in r.get("production_adds") or [] if t.get("from_owner") == them]
        for n in adds:
            out.append({"a_sends": [], "b_sends": [n], "lens": "buy",
                        "why": f"{n} would start for {me} today; {them} is selling production for picks"})
        if adds:
            tag = {f["name"]: f["tag"] for f in fits(p.league_id, me, them)}
            for p1, p2 in consolidations(list(tag), lambda n: _starts_tag(tag[n]),
                                         lambda n: tag[n].startswith("depth")):
                out.append({"a_sends": [], "b_sends": [p1, p2], "lens": "buy",
                            "why": f"{me} takes {p1} and {p2} from {them} in one deal - starter and cover for a run"})
        sells = [e["name"] for e in (r.get("sell_candidates") or []) if e["name"] in offerable][:MAX_PER_SIDE]
        # A rebuild's biggest piece is a conversation once he has stopped gaining value.
        headline, pairs = None, []
        if r.get("mode") == "rebuild":
            bucket = {e["name"]: e.get("bucket") for e in (r.get("sell_candidates") or []) + (r.get("situational") or [])}
            headline = next(iter(sorted((n for n in offerable if n in p.value and n not in sells
                                         and bucket.get(n) != "ascending"), key=lambda n: -p.value[n])), None)
        if sells:
            tag = {f["name"]: f["tag"] for f in fits(p.league_id, them, me)}
            pairs = consolidations(sells, lambda n: _starts_tag(tag.get(n, "")),
                                   lambda n: tag.get(n, "").startswith("depth"))
        for t in r.get("acquire_targets") or []:
            if t.get("from_owner") != them:
                continue
            for sell in sells:
                out.append({"a_sends": [sell], "b_sends": [t["name"]], "lens": "sell",
                            "why": f"{me} converts {sell} into {t['name']} - young value for aging production"})
            if headline:
                out.append({"a_sends": [headline], "b_sends": [t["name"]], "lens": "sell",
                            "why": f"{headline} is {me}'s biggest chip and no longer gaining value; a rebuild turns him into youth and picks"})
            for p1, p2 in pairs:   # two aging pieces that both start for the buyer
                out.append({"a_sends": [p1, p2], "b_sends": [t["name"]], "lens": "sell",
                            "why": f"{me} moves {p1} and {p2} in one deal - {them} would play both - for {t['name']}"})
    return out


def _real_chip(p: _Pair, name: str) -> bool:
    """Above the position's trade-relevance bar (picks always count)."""
    return p.position.get(name) is None or p.value.get(name, 0) >= p.bars.get(p.position[name], 0)


def in_band(lens: str, va: float, vb: float) -> bool:
    """The band, in the BUYER's view: lens 'buy' means side a pays."""
    sent, got = (va, vb) if lens == "buy" else (vb, va)
    return bool(got) and BAND_LO - BAND_TOLERANCE <= sent / got <= BAND_HI + BAND_TOLERANCE


def _centerpiece_ok(p: _Pair, prop: dict) -> bool:
    """For a top-tier piece the return's best single piece must clear the measured shape
    (trade_eval.RETURN_SHAPES q1) - a sum of small pieces is not a centerpiece."""
    best_a = max((p.value.get(n, 0) for n in prop["a_sends"] if n in p.position), default=0)
    best_b = max((p.value.get(n, 0) for n in prop["b_sends"] if n in p.position), default=0)
    best, other = (best_a, best_b) if best_a >= best_b else (best_b, best_a)
    if not best:
        return True
    pct = trade_eval.value_percentile(best, p.players)
    if pct >= CENTERPIECE_TOP:
        return True
    _label, _pieces, (cp_q1, _cp_med, _cp_q3), *_ = trade_eval.shape_for(pct)
    return other / best >= cp_q1


def sweeteners(p: _Pair, light: str, already: list[str], light_is_buyer: bool) -> list[list[tuple]]:
    """What the light side can add, cheapest first, singles then pairs. Picks only if
    the other side takes picks - 1sts and 2nds, nearest year first; a seller may instead
    add a smaller piece it is moving anyway, never one bigger than what it started with."""
    other = "b" if light == "a" else "a"
    picks = p.picks[light] if p.wants_picks[other] else []
    nearest = {}
    for n, v in picks:
        nearest.setdefault(n.split(" ", 1)[1], v)   # sorted by round, then season
    opts = [(n, v, nearest[n.split(" ", 1)[1]], int(n[:4])) for n, v in picks if " 1st" in n or " 2nd" in n]
    if not light_is_buyer:
        cap = min((p.value[n] for n in already if n in p.value), default=0)
        opts += [(n, p.value[n], p.value[n], 0) for n in p.movable[light]
                 if n in p.value and _real_chip(p, n) and n not in already and p.value[n] < cap]
    opts.sort(key=lambda o: (o[2], o[3]))
    singles = [[(n, v)] for n, v, *_ in opts]
    pairs = [[(x[0], x[1]), (y[0], y[1])] for x, y in
             sorted(((x, y) for i, x in enumerate(opts) for y in opts[i + 1:]),
                    key=lambda c: (c[0][2] + c[1][2], c[0][3] + c[1][3]))]
    return singles + pairs


def _balance(p: _Pair, prop: dict) -> dict | None:
    """The idea brought into the band, or None if it can't be."""
    def chip(n):
        return _real_chip(p, n) or (n == prop.get("upgrade")
                                    and p.value.get(n, 0) >= UPGRADE_CHIP_GRACE * p.bars.get(p.position.get(n), 0))

    def side_ok(side):
        """One real chip - or a pair that clears the bar together (Evans + Warren)."""
        players = [n for n in side if n in p.position]
        return any(chip(n) for n in side) or (
            len(players) >= 2 and sum(p.value[n] for n in players) >= max(p.bars.get(p.position[n], 0) for n in players))

    if not side_ok(prop["b_sends"]) or (prop["a_sends"] and not side_ok(prop["a_sends"])):
        return None
    va = sum(p.value.get(n, 0) for n in prop["a_sends"])
    vb = sum(p.value.get(n, 0) for n in prop["b_sends"])
    if not vb:
        return None
    if va and in_band(prop["lens"], va, vb):
        return prop if _centerpiece_ok(p, prop) else None
    buyer_is_a = prop["lens"] == "buy"
    sent, got = (va, vb) if buyer_is_a else (vb, va)
    # The light side: the buyer if he sends too little, the seller if he overpays.
    light = ("a" if buyer_is_a else "b") if (not va or sent < BAND_LO * got) else ("b" if buyer_is_a else "a")
    key = light + "_sends"
    for combo in sweeteners(p, light, prop[key], (light == "a") == buyer_is_a):
        add_v = sum(pv for _, pv in combo)
        if in_band(prop["lens"], va + (add_v if light == "a" else 0), vb + (add_v if light == "b" else 0)):
            names = " + ".join(n for n, _ in combo)
            new = {**prop, key: prop[key] + [n for n, _ in combo],
                   "why": prop["why"] + (f"; {names} evens it up" if prop[key] else f" - {names}")}
            two_firsts = len(combo) == 2 and all(" 1st" in n for n, _ in combo)   # the stud shape
            if two_firsts or not prop[key] or _centerpiece_ok(p, new):
                return new
    return None


def _available(p: _Pair, prop: dict) -> bool:
    """Both sides would do it: every piece is something its own team moves (or the
    starter an upgrade replaces), and a rebuild only receives what it is collecting -
    its wish list, or a year-plus more runway than the best piece it sends."""
    def sender_ok(n, side):
        return n in p.movable[side] or n == prop.get("upgrade") or n not in p.position

    def taker_ok(n, side, it_sends):
        if p.results[side]["mode"] != "rebuild" or n not in p.position or n in p.wish[side]:
            return True
        leaving = max((p.runway.get(m) or 0 for m in it_sends if m in p.position), default=0)
        return (p.runway.get(n) or 0) >= leaving + 1

    return (all(sender_ok(n, "a") and taker_ok(n, "b", prop["b_sends"]) for n in prop["a_sends"])
            and all(sender_ok(n, "b") and taker_ok(n, "a", prop["a_sends"]) for n in prop["b_sends"]))


def _buyer_delta(p: _Pair, prop: dict) -> float:
    """The framer's lineup delta for the buyer (0.0 when the framer can't resolve it)."""
    buyer = p.owners["a"] if prop["lens"] == "buy" else p.owners["b"]
    try:
        ev = trade_eval.evaluate_from_board(p.board, p.owners["a"], prop["a_sends"], p.owners["b"],
                                            prop["b_sends"], p.stances["a"] or None, p.stances["b"] or None)
    except Exception:
        return 0.0
    if not ev.get("ok"):
        return 0.0
    side = next((sd for sd in ev["sides"] if sd["owner"] == buyer), None)
    return float(side.get("lineup_production_delta") or 0) if side else 0.0


def qb_stockpile(p: _Pair, prop: dict) -> str | None:
    """Friction, not a veto: a rebuild already holding a young QB chip per QB slot."""
    for receiver, incoming in ((p.owners["b"], prop["a_sends"]), (p.owners["a"], prop["b_sends"])):
        held = p.qb_stock.get(receiver)
        if held is not None and held >= p.qb_slots and any(
                p.position.get(n) == "QB" and (p.runway.get(n) or 0) >= 1 for n in incoming):
            return (f"{receiver} already holds {held} young QB chip{'s' if held > 1 else ''}"
                    " - one more can't start and is slow to resell")
    return None


def suggest(league_id: str, a: str, b: str, stance_a: str | None = None,
            stance_b: str | None = None, limit: int = 3) -> list[dict]:
    """Up to `limit` ideas between two teams, lens from `a`'s side."""
    p = _pair(league_id, a, b, stance_a, stance_b)
    cands = _proposals(p, a, b)
    cands += [{**c, "a_sends": c["b_sends"], "b_sends": c["a_sends"],
               "lens": {"buy": "sell", "sell": "buy"}[c["lens"]]} for c in _proposals(p, b, a)]
    seen, used_a, used_b, out = set(), set(), set(), []
    for prop in filter(None, (_balance(p, c) for c in cands if _available(p, c))):
        key = (tuple(prop["a_sends"]), tuple(prop["b_sends"]))
        if key in seen or (set(prop["a_sends"]) & used_a) or (set(prop["b_sends"]) & used_b):
            continue
        delta = _buyer_delta(p, prop)
        if delta < -BUYER_DIP_OK:
            continue
        seen.add(key); used_a |= set(prop["a_sends"]); used_b |= set(prop["b_sends"])
        pile = qb_stockpile(p, prop)
        if pile:
            prop = {**prop, "why": f"{prop.get('why', '')}; {pile}".strip("; "), "qb_stockpile": True}
        out.append({**prop, "partner": b, "buyer_delta": delta})
        if len(out) == limit:
            break
    return out


def _order(c: dict, value: dict) -> tuple:
    """A buyer ranks by lineup gain (dips last, value in breaks ties); a seller by the
    aging value it moves out. QB-stockpile friction sinks within either."""
    vin = sum(value.get(n, 0) for n in (c["a_sends"] if c.get("lens") == "sell" else c["b_sends"]))
    if c.get("lens") == "sell":
        return (False, bool(c.get("qb_stockpile")), -vin, 0)
    return (c.get("buyer_delta", 0) <= 0, bool(c.get("qb_stockpile")), -c.get("buyer_delta", 0), -vin)


def pick_ideas(cands: list[dict], value: dict, limit: int) -> list[dict]:
    """Up to `limit` per lens. Strict pass: a new partner and no repeated outgoing piece.
    Refill pass (a thin roster runs out of pieces): repeats allowed, unseen partners
    first. A pick shape ("starter + two 1sts") shows once. Each partner keeps ONE idea;
    its other deals ride on it as `also`."""
    cands = sorted(cands, key=lambda c: _order(c, value))
    out = []
    for lens in ("buy", "sell"):
        pool = [c for c in cands if c.get("lens") == lens]
        picked, partners, sent, shapes = [], set(), set(), set()
        for strict in (True, False):
            todo = pool if strict else sorted(pool, key=lambda c: c["partner"] in partners)
            for c in todo:
                if len(picked) == limit:
                    break
                mine = frozenset(n for n in c["a_sends"] if n in value)
                shape = tuple(sorted(n.split(" ", 1)[1] for n in c["a_sends"] + c["b_sends"] if n not in value))
                if c in picked or (shape and shape in shapes):
                    continue
                if strict and (c["partner"] in partners or (mine & sent)):
                    continue
                partners.add(c["partner"]); sent |= mine; shapes.add(shape); picked.append(c)
        firsts = {}
        for c in picked:
            firsts.setdefault(c["partner"], c)
        for c in pool:
            first = firsts.get(c["partner"])
            if first is not None and c is not first:
                first.setdefault("also", []).append(" + ".join(c["b_sends"] if lens == "buy" else c["a_sends"]))
        out += [c for c in picked if firsts[c["partner"]] is c]
    return out


def ideas(league_id: str, owner: str, limit: int = 3, stance: str | None = None) -> list[dict]:
    """One team's ideas across the whole league. A waiting team (no declared stance) sees
    only buys, framed "if you decided to push" - patience needs no sell."""
    ctx = context(league_id)
    value = {p["name"]: p["value"] for p in ctx.players.values()}
    cands = []
    for other in ctx.owner_names.values():
        if other != owner:
            cands += suggest(league_id, owner, other, stance_a=stance or None, limit=3)
    path = next((t["path"] for t in team_state.classify_league(league_id) if t["owner"] == owner), "")
    if path.startswith("wait") and not stance:
        cands = [{**c, "framing": "if you decided to push"} for c in cands if c.get("lens") == "buy"]
    return pick_ideas(cands, value, limit)


def fits(league_id: str, owner: str, seller: str, stance: str | None = None,
         seller_stance: str | None = None) -> list[dict]:
    """Why `owner` would want pieces on `seller`'s roster - the composer's tags: for each
    piece the seller would move, "starts for them" / "level with their X" / "depth for
    them" from `owner`'s lineup with him added; plus "on their rebuild wish list" and
    "beats their X" from `owner`'s own trade_targets. LOGIC.md "Composer tags"."""
    ctx = context(league_id)
    buyer = ctx.roster_for(owner)
    seller_roster = ctx.roster_for(seller)
    movable = trade_targets.offerable_names(trade_targets.find_targets(league_id, seller, stance=seller_stance or None))
    starters = ctx.starters_for(buyer)
    filled = roster_needs.fill_lineup(buyer, ctx.players, ctx.lineup_dedicated, ctx.lineup_flex)

    def reach_bar(position):
        """The weakest starter in any slot this position can fill."""
        eligible = [ctx.players[q] for slot, q in filled if q in ctx.players and (
            ctx.players[q]["position"] == position
            or (slot == "FLEX" and position in ("RB", "WR", "TE"))
            or slot == "SUPER_FLEX")]
        return min((eppg(q) for q in eligible), default=0)

    result = trade_targets.find_targets(league_id, owner, stance=stance or None)
    buying = result["mode"] != "rebuild"   # a rebuild's interest is its wish list, not its lineup
    out = []
    for pid in (seller_roster["players"] or []) if buying else []:
        info = ctx.players.get(pid)
        if not info or info["name"] not in movable:
            continue
        with_him = {**buyer, "players": list(buyer["players"] or []) + [pid]}
        new_starters = roster_needs.projected_starters(with_him, ctx.players, ctx.lineup_dedicated, ctx.lineup_flex)
        gain = (sum(eppg(ctx.players[q]) for q in new_starters if q in ctx.players)
                - sum(eppg(ctx.players[q]) for q in starters if q in ctx.players))
        if pid in new_starters and gain >= 0.1:   # a wash is depth, not a start
            dropped = [ctx.players[q] for q in starters - new_starters if q in ctx.players]
            market, loud = "", False
            if dropped:
                d0 = max(dropped, key=lambda d: d.get("redraft_value") or 0)
                mine, theirs = info.get("redraft_value") or 0, d0.get("redraft_value") or 0
                market = f"; season price {mine:,} vs {d0['name']}'s {theirs:,}"
                loud = bool(theirs and mine >= 1.5 * theirs and gain < 1.0)
            out.append({"name": info["name"], "owner": seller,
                        "tag": f"starts for them (+{gain:.1f}{' · market says much more' if loud else ''})",
                        "why": f"projects {eppg(info):.1f} a game; {owner}'s lineup gains {gain:.1f} a game"
                               + (f", displacing {', '.join(d['name'] for d in dropped)}" if dropped else "") + market})
        elif pid in new_starters or eppg(info) >= 0.85 * reach_bar(info["position"]) or roster_needs.would_start_if_one_out(
                buyer, ctx.players, pid, starters, ctx.lineup_dedicated, ctx.lineup_flex):
            same = [ctx.players[q] for q in starters if q in ctx.players and ctx.players[q]["position"] == info["position"]]
            weakest = min(same, key=lambda q: eppg(q)) if same else None
            if weakest and eppg(info) >= 0.95 * eppg(weakest):
                mine, theirs = info.get("redraft_value") or 0, weakest.get("redraft_value") or 0
                gap = (" · market says much more" if theirs and mine >= 1.5 * theirs
                       else (" · market says much less" if mine and theirs >= 1.5 * mine else ""))
                out.append({"name": info["name"], "owner": seller, "tag": f"level with their {weakest['name']}{gap}",
                            "why": f"{eppg(info):.1f} vs {eppg(weakest):.1f} a game; season price "
                                   f"{info.get('redraft_value') or 0:,} vs {weakest.get('redraft_value') or 0:,}"})
            else:
                out.append({"name": info["name"], "owner": seller, "tag": "depth for them",
                            "why": f"would start for {owner} if one {info['position']} were out"})
    for r in _branches(result):
        for t in r.get("acquire_targets") or []:   # tagged only above the trade bar
            if (t.get("from_owner") or t.get("owner")) == seller and t.get("value", 0) >= ctx.trade_thresholds.get(t.get("position"), 0):
                out.append({"name": t["name"], "owner": seller, "tag": "on their rebuild wish list",
                            "why": (t.get("why_it_fits") or "young value a rebuild is collecting")[:200]})
        for u in r.get("value_upgrades") or []:
            for ret in u.get("returns") or []:
                if (ret.get("owner") or ret.get("from_owner")) == seller and not ret.get("already_mine"):
                    out.append({"name": ret["name"], "owner": seller, "tag": f"beats their {u['move_off']}",
                                "why": (ret.get("note") or "")[:200]})
    return out


def main(league_id: str, owner: str) -> None:
    for c in ideas(league_id, owner, limit=8):
        also = f"  (also: {', '.join(c['also'])})" if c.get("also") else ""
        print(f"  [{c['lens']}] {' + '.join(c['a_sends']) or '-'} -> {c['partner']} for "
              f"{' + '.join(c['b_sends'])}   {c['buyer_delta']:+.1f}{also}\n      {c['why']}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
