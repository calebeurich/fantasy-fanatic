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

from analysis import format_support, roster_needs, team_state, warm

from . import budget, observability
from .agent import run_query, MCP_SERVER_PATH, _options
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
    staging so the owner lands on his own team; unset on prod -> the picker). Demo
    visitors: DEMO_LEAGUE, a real league to look at without owning one, plus the tier
    so the page can say what the demo allows."""
    t = tier(request)
    if t == "demo":
        return {"tier": "demo", "league_id": DEMO_LEAGUE, "owner": None,
                "asks_per_visitor": budget.DEMO_ASKS_PER_VISITOR}
    return {"tier": "friend", "league_id": os.environ.get("FF_DEFAULT_LEAGUE"),
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
    return {"owner": owner, "mode": result["mode"], "stance": stance or None,
            "players": sorted(trade_targets.offerable_names(result)),
            "picks": [{"season": p["season"], "round": p["round"]} for p in result.get("picks_to_trade_away", [])],
            "stance_note": result.get("stance_note")}


@app.get("/api/league/{league_id}/team/{owner}/fits", dependencies=[Depends(tier)])
def fits(league_id: str, owner: str, stance: str | None = None) -> list[dict]:
    """Pieces on OTHER rosters that fit this team, flattened from trade_targets: buy-path
    targets, efficiency swaps (value_upgrades: more production for less dynasty cost than
    a current starter), production adds (would start today), depth adds. The composer
    tags the counterparty's rows with these so "why would shivvv want Evans" is visible
    without reading a list. Facts from the same tool the agent uses; no verdicts."""
    from analysis import trade_targets
    result = trade_targets.find_targets(league_id, owner, stance=stance or None)
    branches = [result] + [b for b in (result.get("push"), result.get("pivot")) if isinstance(b, dict)]
    out, seen = [], set()

    def add(name, from_owner, tag, why):
        if name and from_owner and (name, tag) not in seen:
            seen.add((name, tag))
            out.append({"name": name, "owner": from_owner, "tag": tag, "why": (why or "")[:200]})

    # Tags are written from the OTHER roster's point of view ("fills their RB hole") -
    # the row they sit on belongs to the counterparty, and "swap for Odunze" on your own
    # Higgins read as you converting Higgins into Odunze (owner, 2026-08-17).
    # Owner's rule: unless a piece is a CLEAR upgrade - it would start over what they
    # already start (positive margin over their weakest starter) or beats a starter on
    # both axes - it is depth, whatever list it came from. A "target" that fills a hole
    # without out-producing the incumbent is depth in that format.
    def production_tag(p):
        return "starts for them today" if (p.get("over_weakest_starter") or 0) > 0 else "depth for them"

    for r in branches:
        for t in r.get("targets") or []:
            add(t.get("name"), t.get("owner") or t.get("from_owner"), production_tag(t),
                t.get("why_it_fits") or t.get("why") or t.get("note"))
        for t in r.get("acquire_targets") or []:
            add(t.get("name"), t.get("owner") or t.get("from_owner"), "on their rebuild wish list",
                t.get("why_it_fits") or t.get("why") or t.get("note"))
        for u in r.get("value_upgrades") or []:
            for ret in u.get("returns") or []:
                if not ret.get("already_mine"):
                    add(ret.get("name"), ret.get("owner") or ret.get("from_owner"),
                        f"beats their {u['move_off']}", ret.get("note"))
        for p in r.get("production_adds") or []:
            add(p.get("name"), p.get("from_owner"), production_tag(p),
                p.get("starter_caveat") or "production-priced; would start for them today")
        for p in r.get("depth_adds") or []:
            add(p.get("name"), p.get("from_owner"), "depth for them", p.get("note"))
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
        if request.session_id:
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
