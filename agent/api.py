"""HTTP wrapper around run_query - the entrypoint any hosting platform needs, since
none of them can invoke a one-shot CLI directly. Deliberately platform-agnostic: a
plain FastAPI app with no provider-specific code.

Run locally: python -m uvicorn agent.api:app --reload
Then: curl -X POST http://127.0.0.1:8000/ask -H "Content-Type: application/json" \
  -d '{"question": "For Sleeper league <id>, what team window is <owner> in?"}'

Two cost ceilings apply here, at different units:
- agent.py's MAX_BUDGET_USD caps any single call.
- budget.py caps the whole day, and is what makes a *public* endpoint safe to expose
  at all. It fails closed to a static message that costs nothing to serve.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

# NOTE (Windows dev only): do not run this app under `uvicorn --reload`. Reload puts the
# worker on asyncio's SelectorEventLoop, which cannot spawn subprocesses at all - it
# raises a bare NotImplementedError from _make_subprocess_transport - and this agent
# spawns two (the `claude` CLI and the MCP server). Every request then fails with an
# opaque "Failed to start Claude Code". Plain `uvicorn` lands on the proactor loop and
# works fine. Setting the event loop policy here does *not* help: uvicorn creates the
# loop before importing this module, so the policy applies too late (tried it).
# Unaffected on Linux, which is why the container never hit this.

from analysis import format_support, roster_needs, team_state, trade_activity, warm

from . import budget, observability
from .agent import run_query, MCP_SERVER_PATH, ToolsUnavailable, _options
from .sessions import SessionBusy, SessionManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    warm.start_from_env()
    yield
    # Sessions hold live subprocesses; without this they'd be orphaned on shutdown.
    await sessions.close_all()


app = FastAPI(title="fantasy-fanatic agent", lifespan=lifespan)
sessions = SessionManager(_options)

MAX_QUESTION_CHARS = 1000  # a real question is far shorter; this just bounds abuse

# Two tiers, one deploy. FRIENDS: the shared key in the link (not real auth - it keeps
# bots off the friends' budget and a leaked key rotates by changing one GitHub secret).
# DEMO: the bare public URL with no key - table, rosters and composer free, model
# questions per-visitor-capped from a separate small budget (budget.py). The demo tier
# exists only when DEMO_BUDGET_USD > 0; otherwise a missing key is a 401 as before.
# Unset LINK_KEY (local dev) means everyone is a friend.
LINK_KEY = os.environ.get("FF_LINK_KEY")
DEMO_LEAGUE = os.environ.get("DEMO_LEAGUE")   # what a key-less visitor lands on


def tier(request: Request) -> str:
    """'friend' or 'demo'; 401 when there is no demo tier and the key is missing."""
    if not LINK_KEY:
        return "friend"
    supplied = request.headers.get("x-ff-key") or request.query_params.get("key")
    if supplied == LINK_KEY:
        return "friend"
    if budget.demo.usd > 0:
        return "demo"
    raise HTTPException(status_code=401, detail=(
        "This link needs its key. Open the exact link you were sent (it carries the "
        "key) - or ask Caleb for the current one."))


def require_key(request: Request) -> None:
    """Friends only - the endpoints that expose internals or spend real money freely."""
    if tier(request) != "friend":
        raise HTTPException(status_code=401, detail="Friends link only.")


def _visitor(request: Request) -> str:
    """Cloud Run puts the caller first in X-Forwarded-For."""
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?"))


class AskRequest(BaseModel):
    question: str
    # Optional: omit for a one-shot question, supply a stable id to continue a
    # conversation. Generated client-side (see static/index.html) rather than issued
    # by the server, which keeps this stateless to look at and means a lost session
    # just starts a new one instead of erroring.
    session_id: str | None = None
    # The other side of the table: an owner name makes this a one-shot answer AS that
    # manager reacting to the proposal in `question` (COUNTERPARTY_PROMPT). Runs on its
    # own client, never on the asker's conversation; progress/partial text are keyed by
    # session_id like any other ask so the page can stream it beside the advisor's read.
    counterparty: str | None = None


class AskResponse(BaseModel):
    text: str
    cost_usd: float | None = None
    num_turns: int | None = None
    grounding_retries: int | None = None
    budget_exhausted: bool = False
    # True when the supplied session_id had no live conversation on the server, so this
    # answer came from a model with no memory of anything asked before. On a first-ever
    # question that's trivially true and the UI ignores it; on a follow-up it means the
    # conversation was silently reset (idle TTL, LRU eviction, or a deploy) and the page
    # tells the user so - the alternative is the model confidently answering "as we
    # discussed" questions it never saw, which is exactly the jwall failure mode.
    conversation_reset: bool = False


STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """Single static page, no build step and no npm - it ships inside the same
    container as the API, so there's nothing separate to deploy or host."""
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/defaults")
def defaults(request: Request) -> dict:
    """What the page opens on when nothing is selected. Friends: FF_DEFAULT_* (set on
    staging so the owner lands on his own team), else the same league the demo lands
    on. Demo visitors: DEMO_LEAGUE, a real league to look at without owning one, plus
    the tier so the page can say what the demo allows."""
    t = tier(request)
    if t == "demo":
        return {"tier": "demo", "league_id": DEMO_LEAGUE, "owner": None,
                "asks_per_visitor": budget.DEMO_ASKS_PER_VISITOR}
    return {"tier": "friend", "league_id": os.environ.get("FF_DEFAULT_LEAGUE") or DEMO_LEAGUE,
            "owner": os.environ.get("FF_DEFAULT_OWNER")}


@app.get("/api/league/{league_id}", dependencies=[Depends(tier)])
def league_overview(league_id: str) -> dict:
    """Deterministic league snapshot, rendered by the UI as a table rather than
    described by the model.

    This is the frontend expression of the project's core split: the analysis layer
    already computes team windows, ranks, needs and cornerstones exactly, so paying
    Claude tokens to *recite* them is both wasteful and the one place confabulation
    can creep in (the model inventing a rank or a player). The agent is for reasoning
    - "should I trade for him", "why is this team in Push mode" - not for reading a table
    aloud.

    Free and fast in practice: `sources/cache.py` means a second view of the same
    league, or a question about it right after, reuses the same fetches.
    """
    tier = format_support.assess_format(league_id)
    if tier["tier"] == "unsupported":
        return {"league_id": league_id, "supported": False, "reason": tier["reason"]}

    teams = team_state.classify_league(league_id)
    needs = roster_needs.league_needs(league_id)
    # Realized trades per manager - the first manager attribute the table shows. A zero
    # only means "never trades" when someone else in the league has (LOGIC.md "Trade
    # activity"); the page gets both the count and the flag.
    trade_counts = trade_activity.get_trade_counts(league_id)
    anyone_traded = any(trade_counts.values())

    return {
        "league_id": league_id,
        "supported": True,
        "tier": tier["tier"],
        "reason": tier["reason"],
        "no_trade_history": bool(teams and teams[0].get("no_trade_history")),
        "teams": [
            {
                "owner": t["owner"],
                "team_name": t["team_name"],
                "rank": t["contention_rank"],
                "starting_production": t["starting_production"],
                "pct_of_best": t["pct_of_best"],
                "starter_value": t["starter_value"],
                "record": t["record"],
                "ppg": t["ppg"],
                "alignment": t["alignment"],
                "path": t["path"],
                "path_reason": t["path_reason"],
                "contention": t["contention"],
                "trajectory": t["trajectory"],
                "ascending_pct": t["ascending_pct"],
                "declining_pct": t["declining_pct"],
                "owns_next_first": t["owns_next_first"],
                "pick_share": t["pick_share"],
                "firsts": t["firsts"],
                "trades": trade_counts.get(t["owner_id"], 0),
                "never_trades": anyone_traded and not trade_counts.get(t["owner_id"]),
                # Cornerstones alone made the column lie by omission: a roster showed
                # "Lamar" while holding CeeDee Lamb, because Lamb misses the tag on the
                # CLOCK, not on value (he lives in win_now_core). The reader of this
                # table wants the roster's headline pieces; which of them are young
                # enough to build around is a flag on the name, not a filter.
                "core": [{"name": e["name"], "cornerstone": bool(e.get("is_cornerstone"))}
                         for e in sorted(t["cornerstones"] + t["win_now_core"],
                                         key=lambda e: -e["value"])],
                "needs": needs.get(t["owner_id"], {}),
            }
            for t in teams
        ],
    }


@app.get("/api/league/{league_id}/team/{owner}", dependencies=[Depends(tier)])
def team_detail(league_id: str, owner: str) -> dict:
    """One team expanded: full roster by position with values/ages/buckets, the picks
    with their market prices, and the team-level reads (window, clock mismatch).
    Deterministic and rendered directly by the UI, same reasoning as the league table -
    the analysis already knows all of it, so no tokens are spent reciting a roster."""
    from analysis.league import context
    ctx = context(league_id)
    return _team_detail(ctx, ctx.roster_for(owner))


@app.get("/api/league/{league_id}/team/{owner}/movable", dependencies=[Depends(tier)])
def movable(league_id: str, owner: str, stance: str | None = None) -> dict:
    """What this team would plausibly move, given its path (or a declared stance: press /
    sell) - the composer greys out everything else. Straight from trade_targets'
    offerable set and picks-to-pay-with; the single source of truth the agent's grounding
    check already uses. Facts, no verdicts."""
    from analysis import trade_targets
    result = trade_targets.find_targets(league_id, owner, stance=stance or None)
    picks = result.get("picks_to_trade_away") or (result.get("push") or {}).get("picks_to_trade_away") or []
    # A rebuild's "situational" pieces (young cornerstones with years on them) are in
    # the offerable set by doctrine - the hardest ask is a price, never a veto - but the
    # page must not paint Loveland as if Ben were shopping him (owner: "I don't want him
    # and can't afford him"). They come back separately as "priced".
    situational = result.get("situational") or (result.get("pivot") or {}).get("situational") or []
    priced = {e["name"] for e in situational}
    return {"owner": owner, "mode": result["mode"], "stance": stance or None,
            "players": sorted(trade_targets.offerable_names(result) - priced),
            "priced": sorted(priced),
            "picks": [{"season": p["season"], "round": p["round"]} for p in picks],
            "stance_note": result.get("stance_note")}


@app.get("/api/league/{league_id}/team/{owner}/fits", dependencies=[Depends(tier)])
def fits(league_id: str, owner: str, seller: str, stance: str | None = None,
         seller_stance: str | None = None) -> list[dict]:
    """Why `owner` would want pieces on `seller`'s roster - the composer's tags. One rule,
    stance-aware on both sides: for every piece the seller would MOVE (its offerable set
    under its read or declared stance), fill `owner`'s lineup with him added - if he
    starts, "starts for them today" (naming who he displaces); if he'd start once one
    starter were out, "depth for them"; else nothing. Plus efficiency swaps from
    `owner`'s value_upgrades whose return sits on the seller (the page shows those only
    when the seller is selling). Facts from the same functions the tools use."""
    from analysis import roster_needs, trade_targets
    from analysis.league import context
    from analysis.team_values import eppg
    ctx = context(league_id)
    buyer = ctx.roster_for(owner)
    seller_roster = ctx.roster_for(seller)
    movable = trade_targets.offerable_names(trade_targets.find_targets(league_id, seller, stance=seller_stance or None))
    starters = ctx.starters_for(buyer)
    # The weakest starter at each slot a position can fill (its own slot, FLEX, SUPER_FLEX):
    # "depth" = within reach of that starter, not only "starts if exactly one is out" (DJ
    # Moore at 10.5 behind Metcalf 10.8 for dez's flex is one flex spot from play; owner).
    filled = roster_needs.fill_lineup(buyer, ctx.players, ctx.lineup_dedicated, ctx.lineup_flex)
    def reach_bar(position):
        eligible = [ctx.players[q] for slot, q in filled if q in ctx.players and (
            ctx.players[q]["position"] == position
            or (slot == "FLEX" and position in ("RB", "WR", "TE"))
            or slot == "SUPER_FLEX")]
        return min((eppg(q) for q in eligible), default=0)
    result = trade_targets.find_targets(league_id, owner, stance=stance or None)
    # A seller/rebuild is not buying production - its interest is young value (the wish
    # list below), so lineup tags are for buying and middle paths only (owner: a sell
    # team "not making a starting lineup" shouldn't tag my starters as upgrades).
    buying = result["mode"] != "rebuild"
    out = []
    for pid in (seller_roster["players"] or []) if buying else []:
        info = ctx.players.get(pid)
        if not info or info["name"] not in movable:
            continue
        with_him = {**buyer, "players": list(buyer["players"] or []) + [pid]}
        new_starters = roster_needs.projected_starters(with_him, ctx.players, ctx.lineup_dedicated, ctx.lineup_flex)
        gain = (sum(eppg(ctx.players[q]) for q in new_starters if q in ctx.players)
                - sum(eppg(ctx.players[q]) for q in starters if q in ctx.players))
        # If he'd start, say so and let the gain speak ("Burrow ain't depth" - he starts
        # for dez by +0.3 over Dak); depth is for pieces that would NOT crack the lineup.
        if pid in new_starters and gain >= 0.1:   # a wash (+0.0) is depth, not a start
            dropped = [ctx.players[q] for q in starters - new_starters if q in ctx.players]
            # The market's opinion rides along: projections flatten some gaps (Burrow 17.4
            # vs Dak 17.1 a game, but 8,226 vs 4,403 in season price) and the reader
            # should see both.
            market = ""
            if dropped:
                d0 = max(dropped, key=lambda d: d.get("redraft_value") or 0)
                mine, theirs = info.get("redraft_value") or 0, d0.get("redraft_value") or 0
                market = f"; season price {mine:,} vs {d0['name']}'s {theirs:,}"
                loud = theirs and mine >= 1.5 * theirs and gain < 1.0
            else:
                loud = False
            out.append({"name": info["name"], "owner": seller,
                        "tag": f"starts for them (+{gain:.1f}{' · market says much more' if loud else ''})",
                        "why": f"projects {eppg(info):.1f} a game; {owner}'s lineup gains {gain:.1f} a game"
                               + (f", displacing {', '.join(d['name'] for d in dropped)}" if dropped else "") + market})
        elif pid in new_starters or eppg(info) >= 0.85 * reach_bar(info["position"]) or roster_needs.would_start_if_one_out(
                buyer, ctx.players, pid, starters, ctx.lineup_dedicated, ctx.lineup_flex):
            # Within noise of the starter he'd sit behind is not "depth" (Purdy at 17.1 vs
            # Dak's 17.1 is a wash, not a backup) - say level with whom.
            same = [ctx.players[q] for q in starters if q in ctx.players and ctx.players[q]["position"] == info["position"]]
            weakest = min(same, key=lambda q: eppg(q)) if same else None
            if weakest and eppg(info) >= 0.95 * eppg(weakest):
                mine, theirs = info.get("redraft_value") or 0, weakest.get("redraft_value") or 0
                gap = " · market says much more" if theirs and mine >= 1.5 * theirs else (" · market says much less" if mine and theirs >= 1.5 * mine else "")
                out.append({"name": info["name"], "owner": seller, "tag": f"level with their {weakest['name']}{gap}",
                            "why": f"{eppg(info):.1f} vs {eppg(weakest):.1f} a game; season price "
                                   f"{info.get('redraft_value') or 0:,} vs {weakest.get('redraft_value') or 0:,}"})
            else:
                out.append({"name": info["name"], "owner": seller, "tag": "depth for them",
                            "why": f"would start for {owner} if one {info['position']} were out"})
    for r in [result] + [b for b in (result.get("push"), result.get("pivot")) if isinstance(b, dict)]:
        # A rebuild's wish list is young VALUE, not production - tagged only above the
        # position's trade bar (a 748-dynasty QB3 is a roster clogger, not a wish; owner).
        for t in r.get("acquire_targets") or []:
            if (t.get("from_owner") or t.get("owner")) == seller and t.get("value", 0) >= ctx.trade_thresholds.get(t.get("position"), 0):
                out.append({"name": t["name"], "owner": seller, "tag": "on their rebuild wish list",
                            "why": (t.get("why_it_fits") or "young value a rebuild is collecting")[:200]})
        for u in r.get("value_upgrades") or []:
            for ret in u.get("returns") or []:
                if (ret.get("owner") or ret.get("from_owner")) == seller and not ret.get("already_mine"):
                    out.append({"name": ret["name"], "owner": seller, "tag": f"beats their {u['move_off']}",
                                "why": (ret.get("note") or "")[:200]})
    return out


def _suggest(league_id: str, a: str, b: str, stance_a: str | None = None, stance_b: str | None = None,
             limit: int = 3) -> list[dict]:
    """Up to `limit` concrete starting points between two teams, from what trade_targets
    already computes for each: a buyer's targets on the other roster paired with the
    piece the tool says that owner would take (`offer_any_one_of`) or the starter the
    target would replace, and a rebuild's wish-list pieces on the other roster paired
    with what it is selling; mirrored both ways. Each is balanced into the band by the
    light side's sweeteners (see `sweeteners`). A starting point, never a price verdict
    - the framer's impact and the assistant's read follow. The rules and their owner
    quotes: LOGIC.md "Trade ideas"."""
    from analysis import trade_targets
    from analysis.league import context

    def branches(result):
        return [result] + [x for x in (result.get("push"), result.get("pivot")) if isinstance(x, dict)]

    def proposals(me, them, stance):
        result = trade_targets.find_targets(league_id, me, stance=stance or None)
        offerable = trade_targets.offerable_names(result)
        my_starters = [ctx.players[pid]["name"] for pid in ctx.starters_for(ctx.roster_for(me)) if pid in ctx.players]
        out = []
        for r in branches(result):
            for t in (r.get("targets") or []) + (r.get("long_shots") or []):
                if t.get("from_owner") != them:
                    continue
                gives = [n for n in (t.get("offer_any_one_of") or []) if n in offerable][:3]
                for give in gives:
                    out.append({"a_sends": [give], "b_sends": [t["name"]], "lens": "buy",
                                "why": f"{me} fills a {t.get('for_slot') or t.get('position')} hole with {t['name']}; {them} would take {give}"})
                # The upgrade swap: the man he replaces goes back the other way ("Bryce Young
                # + a pick for Goff" - owner). Not in the offerable set - he starts - but the
                # target starting instead of him is the whole point, so the target has to
                # out-produce him (Parker Washington for McLaurin at the same ePPG is a swap,
                # not an upgrade).
                for mine in my_starters:
                    same_slot = position.get(mine) == position.get(t["name"])
                    if same_slot and value.get(mine, 0) < value.get(t["name"], 0) and eppg.get(t["name"], 0) > 1.05 * eppg.get(mine, 0):
                        out.append({"a_sends": [mine], "b_sends": [t["name"]], "lens": "buy", "upgrade": mine,
                                    "why": f"{me} upgrades {mine} to {t['name']} at {position.get(mine)}"})
                if not gives:   # no named piece they'd take: pay in picks (balance() finds them)
                    out.append({"a_sends": [], "b_sends": [t["name"]], "lens": "buy",
                                "why": f"{me} fills a {t.get('for_slot') or t.get('position')} hole with {t['name']}"})
            # Production a seller is moving, paid for in picks - "a 2nd for Evans" (owner):
            # the buyer's a_sends starts empty and balance() finds the pick(s).
            for t in r.get("production_adds") or []:
                if t.get("from_owner") == them:
                    out.append({"a_sends": [], "b_sends": [t["name"]], "lens": "buy",
                                "why": f"{t['name']} would start for {me} today; {them} is selling production for picks"})
            sells = [e["name"] for e in (r.get("sell_candidates") or []) if e["name"] in offerable][:3]
            # A rebuild's biggest piece is a conversation once he has stopped GAINING value
            # (Chase at 26.5 - owner: "have to give jq something with Chase"), never while he
            # is still ascending (Drake Maye is what a rebuild is collecting, not selling).
            headline = None
            if r.get("mode") == "rebuild":
                bucket = {e["name"]: e.get("bucket") for e in (r.get("sell_candidates") or []) + (r.get("situational") or [])}
                headline = next(iter(sorted((n for n in offerable if n in value and n not in sells and bucket.get(n) != "ascending"),
                                            key=lambda n: -value[n])), None)
            for t in r.get("acquire_targets") or []:
                if t.get("from_owner") == them:
                    for sell in sells:
                        out.append({"a_sends": [sell], "b_sends": [t["name"]], "lens": "sell",
                                    "why": f"{me} converts {sell} into {t['name']} - young value for aging production"})
                    if headline:
                        out.append({"a_sends": [headline], "b_sends": [t["name"]], "lens": "sell",
                                    "why": f"{headline} is {me}'s biggest chip and no longer gaining value; a rebuild turns him into youth and picks"})
        return out

    ctx = context(league_id)
    value = {p["name"]: p["value"] for p in ctx.players.values()}
    eppg = {p["name"]: p.get("projected_ppg") or 0 for p in ctx.players.values()}
    position = {p["name"]: p["position"] for p in ctx.players.values()}
    bars = ctx.trade_thresholds

    def real_chip(name):
        """Above the position's trade-relevance bar - nobody cares about a Mike Washington
        for Charbonnet idea (owner)."""
        return position.get(name) is None or value.get(name, 0) >= bars.get(position[name], 0)

    result_a = trade_targets.find_targets(league_id, a, stance=stance_a or None)
    result_b = trade_targets.find_targets(league_id, b, stance=stance_b or None)

    def spendable_picks(r):
        picks = r.get("picks_to_trade_away") or (r.get("push") or {}).get("picks_to_trade_away") or []
        return [(pk["pick"].split(" (")[0], pk["value"]) for pk in sorted(picks, key=lambda x: x["round"])]
    picks_a, picks_b = spendable_picks(result_a), spendable_picks(result_b)
    # A pick only tops up a side when the RECEIVER wants picks - a rebuild or a middle
    # team; a contender wants production, not your '28 1st (owner: kb).
    wants_picks = {"a": result_a["mode"] != "buy", "b": result_b["mode"] != "buy"}

    from analysis.trade_eval import _shape_for, _value_percentile

    def centerpiece_ok(prop):
        """For a top-tier piece, sums lie: the framer's measured shape says what the
        centerpiece coming back has to be (top-5%: 0.64-0.84 of him). Sadiq + a 1st for
        Jefferson summed to 0.85x and still read "below the usual band" (owner: "feels
        like Jefferson might be worth more"). Picks don't count as a centerpiece."""
        best_a = max((value.get(n, 0) for n in prop["a_sends"] if n in position), default=0)
        best_b = max((value.get(n, 0) for n in prop["b_sends"] if n in position), default=0)
        best, other = (best_a, best_b) if best_a >= best_b else (best_b, best_a)
        if not best:
            return True
        pct = _value_percentile(best, ctx.players)
        if pct >= 0.35:
            return True
        _label, _pieces, (cp_q1, _cp_med, _cp_q3), *_ = _shape_for(pct)
        return other / best >= cp_q1

    # The band is DIRECTIONAL, in the buyer's view (lens 'buy' -> a pays; 'sell' -> b
    # pays): he sends at least 0.9x of what he gets (Shough for a young Chase Brown at
    # 0.75x needs the 2nd on top; Cam Ward alone for Jonathan Taylor at 0.69x is a slap
    # in the face - owner) and may overpay to 1.2x - contenders do; past that the SELLER
    # evens it up. The aging discount is already in a production piece's dynasty price,
    # so it gets no second discount here.
    BAND_LO, BAND_HI = 0.9, 1.2

    def in_band(prop, va, vb, tolerance=0.04):
        sent, got = (va, vb) if prop.get("lens") == "buy" else (vb, va)   # buyer's view
        return got and BAND_LO - tolerance <= sent / got <= BAND_HI + tolerance   # a hair either way isn't worth another piece

    def balance(prop):
        chip = lambda n: real_chip(n) or (n == prop.get("upgrade") and value.get(n, 0) >= 0.9 * bars.get(position.get(n), 0))
        if not any(chip(n) for n in prop["b_sends"]) or (prop["a_sends"] and not any(chip(n) for n in prop["a_sends"])):
            return None
        va = sum(value.get(n, 0) for n in prop["a_sends"]); vb = sum(value.get(n, 0) for n in prop["b_sends"])
        if not vb:
            return None
        if va and in_band(prop, va, vb):
            return prop if centerpiece_ok(prop) else None
        buyer_is_a = prop.get("lens") == "buy"
        # Which side is light? The buyer when he sends too little, the seller when the
        # buyer overpays past the band. The light side sweetens from what it would move.
        sent, got = (va, vb) if buyer_is_a else (vb, va)
        light = ("a" if buyer_is_a else "b") if (not va or sent < BAND_LO * got) else ("b" if buyer_is_a else "a")
        key = light + "_sends"
        light_is_buyer = (light == "a") == buyer_is_a
        for combo in sweeteners(light, prop[key], light_is_buyer):
            add_v = sum(pv for _, pv in combo)
            new = dict(prop); new[key] = prop[key] + [n for n, _ in combo]
            va2, vb2 = va + (add_v if light == "a" else 0), vb + (add_v if light == "b" else 0)
            if in_band(prop, va2, vb2):
                new["why"] = prop["why"] + (f"; {' + '.join(n for n, _ in combo)} evens it up" if prop[key] else f" - {' + '.join(n for n, _ in combo)}")
                two_firsts = len(combo) == 2 and all(" 1st" in n for n, _ in combo)
                if two_firsts or not prop[key] or centerpiece_ok(new):
                    return new
        return None

    def sweeteners(light, already, light_is_buyer):
        """What the light side can add, cheapest that lands first, singles then pairs.
        A buyer adds picks - its 1sts and 2nds, and only if the other side wants picks (a
        contender wants production; 3rds and 4ths aren't currency for a real chip - "at
        least a 2nd", owner). A seller adds picks the same way if it has any to spend, or
        a smaller piece it is moving anyway - Evans on top of McLaurin for Shough, not a
        pick it is keeping (owner) - never a piece bigger than the one it started with:
        that would be a different trade. Same-round picks cost the same here so the
        nearest year goes first ('27 1st, not '29 - owner); two 1sts is the stud shape
        (RETURN_SHAPES) and skips the centerpiece test."""
        picks = (picks_a if light == "a" else picks_b) if wants_picks["b" if light == "a" else "a"] else []
        nearest = {}
        for n, v in picks:
            nearest.setdefault(n.split(" ", 1)[1], v)   # picks come sorted by round, then season
        opts = [(n, v, nearest[n.split(" ", 1)[1]], int(n[:4])) for n, v in picks if " 1st" in n or " 2nd" in n]
        if not light_is_buyer:
            movable = movable_a if light == "a" else movable_b
            cap = min((value[n] for n in already if n in value), default=0)
            opts += [(n, value[n], value[n], 0) for n in movable
                     if n in value and real_chip(n) and n not in already and value[n] < cap]
        opts.sort(key=lambda o: (o[2], o[3]))
        singles = [[(n, v)] for n, v, *_ in opts]
        pairs = [[(x[0], x[1]), (y[0], y[1])] for x, y in
                 sorted(((x, y) for i, x in enumerate(opts) for y in opts[i + 1:]), key=lambda c: (c[0][2] + c[1][2], c[0][3] + c[1][3]))]
        return singles + pairs

    cands = list(proposals(a, b, stance_a))
    cands += [{**p, "a_sends": p["b_sends"], "b_sends": p["a_sends"],
               "lens": {"buy": "sell", "sell": "buy"}[p["lens"]]} for p in proposals(b, a, stance_b)]
    # Every named player must be something ITS OWN team would move - a rebuild wanting
    # kb's Brian Thomas doesn't make Thomas available (kb is WR-weak; he's a starter). The
    # one exception is the starter an upgrade swap replaces: he goes because a better one
    # arrives.
    # ...and a rebuild's ASCENDING situational pieces (Drake Maye) are not idea material
    # at all - "at a cornerstone's price" is the composer's business; here they are what
    # the rebuild is collecting, not selling (owner: "win now but also win later").
    def still_gaining(result):
        sit = result.get("situational") or (result.get("pivot") or {}).get("situational") or []
        return {e["name"] for e in sit if e.get("bucket") == "ascending"}
    movable_a = trade_targets.offerable_names(result_a) - still_gaining(result_a)
    movable_b = trade_targets.offerable_names(result_b) - still_gaining(result_b)
    wish_a = {t["name"] for t in result_a.get("acquire_targets") or []}
    wish_b = {t["name"] for t in result_b.get("acquire_targets") or []}
    def available(prop):
        # The replaced starter is fine to send - unless the receiver is a rebuild that
        # didn't ask for him (spugz doesn't want DJ Moore for Jefferson; owner).
        swap_ok = lambda n, receiver_wish, receiver: n == prop.get("upgrade") and (receiver["mode"] != "rebuild" or n in receiver_wish)
        ok_a = lambda n: n in movable_a or swap_ok(n, wish_b, result_b) or n not in position
        ok_b = lambda n: n in movable_b or swap_ok(n, wish_a, result_a) or n not in position
        return all(ok_a(n) for n in prop["a_sends"]) and all(ok_b(n) for n in prop["b_sends"])
    seen, used_a, used_b, out = set(), set(), set(), []
    for prop in filter(None, (balance(c) for c in cands if available(c))):
        key = (tuple(prop["a_sends"]), tuple(prop["b_sends"]))
        if key in seen or (set(prop["a_sends"]) & used_a) or (set(prop["b_sends"]) & used_b):
            continue
        seen.add(key); used_a |= set(prop["a_sends"]); used_b |= set(prop["b_sends"])
        out.append({**prop, "partner": b})
        if len(out) == limit:
            break
    return out


@app.get("/api/league/{league_id}/suggest", dependencies=[Depends(tier)])
def suggest(league_id: str, a: str, b: str, stance_a: str | None = None, stance_b: str | None = None) -> list[dict]:
    return _suggest(league_id, a, b, stance_a, stance_b)


@app.get("/api/league/{league_id}/team/{owner}/ideas", dependencies=[Depends(tier)])
def trade_ideas(league_id: str, owner: str, limit: int = 3) -> list[dict]:
    """Up to three starting points for one team across the whole league - one per
    partner - shown in the team's expanded row, click-to-load into the composer.
    Deterministic and cheap once the board is warm; cached client-side per team."""
    from analysis.league import context
    ctx = context(league_id)
    value = {p["name"]: p["value"] for p in ctx.players.values()}
    cands = []
    for other in ctx.owner_names.values():
        if other != owner:
            cands += _suggest(league_id, owner, other, limit=3)
    # Bigger deals first (what comes back, in dynasty value), one per partner, and no
    # repeating the outgoing piece - three ideas should be three different conversations.
    # A waiting team is patient by definition: nothing to convert, so sell-lens ideas are
    # noise; its buy-lens ideas are only live IF it chose to push (owner). Decide teams
    # get both, labelled; contenders buy; rebuilds sell.
    path = next((t["path"] for t in team_state.classify_league(league_id) if t["owner"] == owner), "")
    if path.startswith("wait"):
        cands = [{**c, "framing": "if you decided to push"} for c in cands if c.get("lens") == "buy"]
    cands.sort(key=lambda c: -sum(value.get(n, 0) for n in c["b_sends"]))
    # Up to three PER LENS - a decide team's "as buyer" and "as seller" are two columns.
    out = []
    for lens in ("buy", "sell"):
        partners, sent, n = set(), set(), 0
        shapes = set()
        for c in (x for x in cands if x.get("lens") == lens):
            mine = {n for n in c["a_sends"] if n in value}
            # The pick pattern is the "shape" - "starter + two 1sts for a stud" is real but
            # once is enough (owner); plain player-for-player ideas differ by the players.
            shape = tuple(sorted(n.split(" ", 1)[1] for n in c["a_sends"] + c["b_sends"] if n not in value))
            if c["partner"] in partners or (mine & sent) or (shape and shape in shapes):
                continue
            partners.add(c["partner"]); sent |= mine; shapes.add(shape); out.append(c); n += 1
            if n == limit:   # per lens; the page shows three and reveals the rest on a tap
                break
    return out


class EvaluateRequest(BaseModel):
    owner_a: str
    sends_a: list[str]
    owner_b: str
    sends_b: list[str]
    stance_a: str | None = None
    stance_b: str | None = None


@app.post("/api/league/{league_id}/evaluate", dependencies=[Depends(tier)])
def evaluate(league_id: str, req: EvaluateRequest) -> dict:
    """The framer's deterministic facts for a proposed trade, so the composer can show
    impact on every tap: per side the goal line (lineup delta / value in-out, holes
    opened or closed), need changes, best single piece. Free per call, no model. The
    'should' stays the agent's."""
    from analysis import trade_eval
    from analysis.trade_targets.board import build_board
    out = trade_eval.evaluate_from_board(build_board(league_id), req.owner_a, req.sends_a,
                                         req.owner_b, req.sends_b, req.stance_a or None, req.stance_b or None)
    if not out["ok"]:
        return {"ok": False, "problem": out["problem"]}
    return {"ok": True, "best_piece": out["best_piece"],
            "sides": [{k: s.get(k) for k in ("owner", "path", "alignment", "lens", "goal",
                                             "lineup_production_delta", "need_changes", "sends", "receives")}
                      for s in out["sides"]]}


@app.get("/api/league/{league_id}/teams", dependencies=[Depends(tier)])
def all_team_details(league_id: str) -> dict:
    """Every team's detail in one response, keyed by owner. The UI prefetches all of
    them right after the table renders so clicks are instant; as twelve parallel
    requests that read as request spam to a tester watching the network tab."""
    from analysis.league import context
    ctx = context(league_id)
    return {ctx.owner_names[r["owner_id"]]: _team_detail(ctx, r) for r in ctx.rosters}


def _team_detail(ctx, roster: dict) -> dict:
    from analysis import team_values
    from analysis.team_values import age_bucket, years_to_decline
    from sources import fantasycalc

    # Built straight from the cached league context, NOT get_roster_rows - that path
    # downloads nflverse contracts and injury rates, neither of which this view shows,
    # and the first click paid several seconds for data it never rendered.
    league_id = ctx.league_id
    owner_name = ctx.owner_names[roster["owner_id"]]
    starters = ctx.starters_for(roster)
    t = next(t for t in team_state.classify_league(league_id)
             if t["owner"] == owner_name)
    corner = {e["name"] for e in t["cornerstones"]}

    by_pos: dict[str, list] = {}
    for pid in roster["players"] or []:
        info = ctx.players.get(pid)
        if info is None:
            continue
        by_pos.setdefault(info["position"], []).append({
            "name": info["name"], "value": info["value"],
            "redraft_value": info.get("redraft_value"), "age": info["age"],
            "projected_ppg": team_values.eppg(info),
            "bucket": age_bucket(info["position"], info["age"], info.get("usage_role")),
            "starter": pid in starters,
            "cornerstone": info["name"] in corner,
            # The continuous variable behind the bucket - the UI colors on it, because
            # a 0.1-year piece and a 3.9-year piece are different facts the same
            # bucket color was hiding (the James Cook effect, visually).
            "years_to_decline": years_to_decline(info["position"], info["age"],
                                                 info.get("usage_role")),
            "prime_span": team_values.prime_span(info["position"], info.get("usage_role")),
        })
    # Sorted by THIS-SEASON value: starters are computed from redraft, so they
    # cluster at the top, the marker draws the cut line, and "who is close to
    # starting" is literally the first name below it - dynasty-sorting hid that.
    for rows in by_pos.values():
        rows.sort(key=lambda x: (-(x["redraft_value"] or 0), -(x["value"] or 0)))

    pick_values = fantasycalc.get_pick_values(ctx.fmt["num_qbs"], ctx.fmt["num_teams"],
                                              ctx.fmt["ppr"], ctx.fmt["is_dynasty"])
    picks = team_values.owned_picks(league_id, int(ctx.league["season"]),
                                    ctx.league["settings"]["draft_rounds"],
                                    [r["roster_id"] for r in ctx.rosters], pick_values)
    return {
        "owner": owner_name,
        "alignment": t["alignment"], "path": t["path"], "path_reason": t["path_reason"],
        "clock_mismatch_note": t.get("clock_mismatch_note"),
        "players": by_pos,
        "start_bars": {pos: round(v) for pos, v in (getattr(ctx, "start_thresholds", None) or {}).items()},
        "picks": [{"pick": p["pick"], "value": p["value"], "season": p["season"], "round": p["round"]}
                  for p in picks.get(t["roster_id"], [])],
    }


@app.get("/sessions", dependencies=[Depends(require_key)])
def session_status() -> dict:
    """Visible so session count/idle time can be checked against the memory ceiling
    rather than guessed at - each live session holds two subprocesses."""
    return sessions.status()


@app.post("/sessions/{session_id}/warm")
async def warm_session(session_id: str, request: Request) -> dict:
    """The page calls this on load so the first question skips the ~8s subprocess
    start-up. Only takes a free slot - see SessionManager.prewarm."""
    return {"opened": await sessions.prewarm(session_id, tier(request))}


@app.get("/diagnostics", dependencies=[Depends(require_key)])
async def diagnostics() -> dict:
    """Spawns the MCP server directly and reports what actually happens.

    Exists because the real failure was invisible: when the MCP subprocess fails to
    start, the SDK swallows it, the model is handed an empty toolset, and it then
    *confabulates* - first claiming it had unrelated tools, later emitting
    <function_calls> blocks as plain text and inventing a fabricated answer. The most
    informative error in the system was being hidden behind a plausible-sounding
    excuse, which made this debuggable only by inference. This endpoint replaces that
    inference with a stack trace.

    Reports environment facts too, since the container differs from local in exactly
    the ways that matter here (interpreter path, working directory, uid). Never
    returns the API key itself - only whether one is present.
    """
    import os
    import traceback

    info = {
        "python_executable": sys.executable,
        "mcp_server_path": str(MCP_SERVER_PATH),
        "mcp_server_exists": MCP_SERVER_PATH.is_file(),
        "cwd": os.getcwd(),
        "uid": os.getuid() if hasattr(os, "getuid") else None,
        "home": os.environ.get("HOME"),
        "anthropic_key_present": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "path": os.environ.get("PATH"),
    }

    # Run the server directly with captured output. The MCP handshake failing with
    # "Connection closed" means the subprocess died before it could respond, and its
    # stderr went nowhere - so an import-time crash is invisible through the MCP client
    # alone. Spawning it plainly is the only way to actually read the traceback.
    # Note: *timing out here is the healthy result* - it means the server started and
    # is waiting on stdin rather than crashing.
    try:
        proc = subprocess.run(
            [sys.executable, str(MCP_SERVER_PATH)],
            capture_output=True, text=True, timeout=90, input="",
        )
        info["import_check"] = {
            "returncode": proc.returncode,
            "stdout": proc.stdout[-1500:],
            "stderr": proc.stderr[-4000:],
        }
    except subprocess.TimeoutExpired as e:
        info["import_check"] = {
            "timed_out_which_means_it_started_ok": True,
            "stderr": (e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr or ""))[-4000:],
        }
    except Exception as e:
        info["import_check"] = {"spawn_error": f"{type(e).__name__}: {e}"}

    # errlog must be a real file object, not io.StringIO - it's handed to the subprocess
    # as a file descriptor, and StringIO has no fileno(), which failed with
    # "UnsupportedOperation: fileno" on the first run of this endpoint.
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as errlog:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(command=sys.executable, args=[str(MCP_SERVER_PATH)])

            async def _list_tools():
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return [t.name for t in (await session.list_tools()).tools]

            # Bounded: a hung subprocess should report as a timeout, not hang the request.
            info["mcp_tools"] = await asyncio.wait_for(_list_tools(), timeout=120)
            info["mcp_ok"] = True
        except Exception as e:
            info["mcp_ok"] = False
            info["mcp_error"] = f"{type(e).__name__}: {e}"
            info["mcp_traceback"] = traceback.format_exc()[-2000:]
        try:
            errlog.seek(0)
            info["mcp_subprocess_stderr"] = errlog.read()[-4000:]
        except Exception:
            info["mcp_subprocess_stderr"] = "(unavailable)"

    return info


class FeedbackRequest(BaseModel):
    verdict: str          # "up" | "down"
    comment: str | None = None
    session_id: str | None = None
    question: str | None = None


@app.post("/feedback", dependencies=[Depends(tier)])
def feedback(fb: FeedbackRequest) -> dict:
    """One thumbs-click per answer, into the same JSONL the runs land in - the friends
    test exists to learn, and 'that doesn't make sense' has been the single richest
    source of real bugs in this project. This is that sentence, with a button."""
    observability.log_run({
        "kind": "feedback",
        "verdict": "up" if fb.verdict == "up" else "down",
        "comment": (fb.comment or "")[:500],
        "session_id": fb.session_id,
        "question": (fb.question or "")[:300],
    })
    return {"ok": True}


ACTIVITY_CSS = """
body{background:#0d1017;color:#e7eaf0;font:14px/1.5 ui-sans-serif,system-ui,sans-serif;
     margin:0;padding:14px}
h1{font-size:15px;margin:0 0 4px} .sub{color:#6b7789;font-size:12px;margin-bottom:14px}
.r{border:1px solid #262d3a;border-radius:10px;padding:10px 12px;margin-bottom:9px;
   background:#151922}
.q{font-weight:600;margin-bottom:5px} .m{color:#6b7789;font-size:12px}
.down{border-color:#f87171} .up{border-color:#4ade80}
.err{border-color:#fbbf24} .c{color:#fbbf24;margin-top:5px}
"""


@app.get("/activity", response_class=HTMLResponse, dependencies=[Depends(require_key)])
def activity() -> str:
    """What people actually asked, and what they thought of the answer - readable on a
    phone, which is where it gets read.

    Every run and every thumbs-click already went to stdout, which Cloud Run pipes into
    Cloud Logging. That is durable and queryable and completely out of reach while the
    author is away from a desktop watching friends test - so the same records, in memory,
    rendered. Downvotes and errors sort to the top of the eye, not the top of the list:
    order stays chronological because a bad answer usually needs the question before it.
    """
    rows = []
    for r in observability.recent():
        when = datetime.fromtimestamp(r["timestamp"]).strftime("%H:%M:%S")
        if r.get("kind") == "feedback":
            cls = "down" if r["verdict"] == "down" else "up"
            mark = "👎 downvote" if r["verdict"] == "down" else "👍 upvote"
            comment = f'<div class="c">"{r["comment"]}"</div>' if r.get("comment") else ""
            rows.append(f'<div class="r {cls}"><div class="q">{mark}</div>'
                        f'<div class="m">{when} · on: {r.get("question") or "(no question)"}</div>'
                        f'{comment}</div>')
            continue
        cls = "err" if r.get("outcome") != "ok" else ""
        bits = [when]
        for key, fmt in (("cost_usd", "${:.4f}"), ("latency_seconds", "{:.0f}s"),
                         ("num_turns", "{} turns"), ("grounding_retries", "{} retry")):
            if r.get(key):
                bits.append(fmt.format(r[key]))
        if r.get("error"):
            bits.append(f'ERROR {r["error"][:120]}')
        rows.append(f'<div class="r {cls}"><div class="q">'
                    f'{(r.get("question") or "(no question logged)")}</div>'
                    f'<div class="m">{" · ".join(bits)}</div></div>')
    body = "".join(rows) or '<div class="r">Nothing yet.</div>'
    return (f"<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<style>{ACTIVITY_CSS}</style><h1>fantasy-fanatic · recent activity</h1>"
            f"<div class=sub>Newest first. In-memory, so a deploy clears it - Cloud Logging "
            f"keeps the durable copy.</div>{body}")


@app.get("/budget")
def budget_status() -> dict:
    """Exposed so the daily cap is externally verifiable, rather than something you
    have to trust is working - used to confirm the ceiling actually trips before this
    endpoint is ever made public."""
    return {"friends": budget.friends.status(), "demo": budget.demo.status()}


# What each in-flight question is doing right now, keyed by the client's session id:
# the tool step it is on, and the answer text so far. Polling rather than streaming on
# purpose: /ask already returns one whole answer, and turning it into an SSE stream
# would rewrite the response contract, the retry logic and the eval harness. Two dicts
# the client reads while it waits cost ~15 lines and nothing else changes.
_progress: dict[str, str] = {}
_partial: dict[str, str] = {}


@app.get("/progress/{session_id}", dependencies=[Depends(tier)])
def progress(session_id: str) -> dict:
    return {"step": _progress.get(session_id), "text": _partial.get(session_id)}


# The last finished answer per session, so leaving does not destroy it.
#
# Measured, not assumed: a request abandoned by its client after 6 seconds still ran to
# completion on the deployed service (28s, 3 turns, logged and charged) - Cloud Run counts
# the request in-flight while the handler runs, so CPU stays allocated, and uvicorn does
# not cancel a handler when the client disconnects. The work was never the fragile part;
# the answer simply had nowhere to go once the connection died. Stashing it here means a
# tab that closed, slept, or lost signal can come back and claim the result - with no
# background tasks, no always-on CPU billing, and no database. What it does NOT survive is
# the instance itself going away (a deploy, or scale-to-zero after idle); that is the line
# where a durable store would actually be needed.
_answers: dict[str, dict] = {}


@app.get("/answer/{session_id}", dependencies=[Depends(tier)])
def last_answer(session_id: str) -> dict:
    return _answers.get(session_id) or {}


@app.post("/ask", response_model=AskResponse)
async def ask(request: AskRequest, http: Request) -> AskResponse:
    t = tier(http)
    ledger = budget.friends if t == "friend" else budget.demo
    # Checked before calling Claude, so an exhausted budget costs nothing to serve.
    if ledger.is_exhausted():
        return AskResponse(text=ledger.over_message, budget_exhausted=True)
    if t == "demo" and not budget.visitor_allowed(_visitor(http)):
        return AskResponse(text=budget.DEMO_VISITOR_MESSAGE, budget_exhausted=True)

    question = request.question.strip()
    if not question:
        return AskResponse(text="Ask a question about a Sleeper dynasty league.")
    question = question[:MAX_QUESTION_CHARS]

    track = (lambda step: _progress.__setitem__(request.session_id, step)) \
        if request.session_id else None
    stream = (lambda text: _partial.__setitem__(request.session_id, text)) \
        if request.session_id else None
    created = False
    try:
        if request.counterparty:
            result = await run_query(question, verbose=False, on_progress=track, on_text=stream,
                                     persona=request.counterparty)
        elif request.session_id:
            session, created = await sessions.acquire(request.session_id, t)
            # Held for the whole turn: two concurrent requests on one session would
            # interleave on the same client and corrupt the conversation.
            async with session.lock:
                result = await run_query(question, verbose=False, client=session.client,
                                         on_progress=track, on_text=stream)
                # Convert the client's cumulative total into this question's own cost -
                # see Session.cost_delta for why the raw value would both over-report
                # and over-charge the daily budget on a persistent session.
                result["cost_usd"] = session.cost_delta(result["cost_usd"])
        else:
            result = await run_query(question, verbose=False, on_progress=track, on_text=stream)
    except ToolsUnavailable:
        # The session's MCP server never registered. Throw the session away (its client
        # is the broken part) and answer once more on a fresh one; if that also has no
        # tools, say so plainly rather than show a confabulated verdict.
        if request.session_id:
            await sessions.drop(request.session_id)
        try:
            result = await run_query(question, verbose=False, on_progress=track, on_text=stream,
                                     persona=request.counterparty)
            created = True
        except Exception as e2:  # a second no-tools run, or anything else on the retry
            observability.log_run({"question": question[:300], "outcome": "error",
                                   "error": f"retry after no_tools: {type(e2).__name__}: {e2}"})
            ledger.record(None)
            _progress.pop(request.session_id, None); _partial.pop(request.session_id, None)
            return AskResponse(text=("The analysis tools didn't load for that one - nothing was answered, "
                                     "and no verdict was invented. Ask again in a moment."))
    except SessionBusy:
        return AskResponse(text=("The public demo is busy right now - every conversation slot is in "
                                 "use. The table, rosters and composer above still work; try the "
                                 "question again in a few minutes."))
    except Exception as e:
        # Log before swallowing. The detail is still withheld from the caller (internals
        # shouldn't leak from a public endpoint), but it must go *somewhere*: failures in
        # sessions.acquire() happen outside run_query, so its try/finally never sees
        # them, and this handler previously discarded the only copy. That produced
        # exactly the debugging dead-end this project already hit once with the MCP
        # subprocess - a generic message standing in for a specific, fixable error.
        observability.log_run({
            "question": question[:300],
            "outcome": "error",
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc()[-2000:],
            "session_id": bool(request.session_id),
        })
        # A failed call still consumed real capacity (it may have burned tokens before
        # failing), so it counts against the day rather than being free to retry in a loop.
        ledger.record(None)
        _progress.pop(request.session_id, None)
        _partial.pop(request.session_id, None)
        return AskResponse(text="Something went wrong answering that. Try again, or try a different league.")

    _progress.pop(request.session_id, None)
    _partial.pop(request.session_id, None)
    ledger.record(result["cost_usd"])
    if request.session_id:
        # Written whether or not anyone is still listening - that is the entire point.
        _answers[request.session_id] = {
            "question": question, "text": result["text"], "cost_usd": result["cost_usd"],
            "num_turns": result["num_turns"],
            "grounding_retries": result["grounding_retries"],
            "conversation_reset": created,
            "finished_at": time.time(),
        }
    return AskResponse(
        text=result["text"],
        cost_usd=result["cost_usd"],
        num_turns=result["num_turns"],
        grounding_retries=result["grounding_retries"],
        conversation_reset=created,
    )
