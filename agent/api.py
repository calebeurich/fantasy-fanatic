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

from analysis import format_support, roster_needs, team_state

from . import budget, observability
from .agent import run_query, MCP_SERVER_PATH, _options
from .sessions import SessionManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Sessions hold live subprocesses; without this they'd be orphaned on shutdown.
    await sessions.close_all()


app = FastAPI(title="fantasy-fanatic agent", lifespan=lifespan)
sessions = SessionManager(_options)

MAX_QUESTION_CHARS = 1000  # a real question is far shorter; this just bounds abuse

# The friends gate: one shared key in the link, checked on everything that costs money
# or exposes internals. Not real auth - it keeps bots and drive-by traffic off the daily
# budget once the Cloud Run service goes public, and a leaked key rotates by changing
# one GitHub secret. Unset (local dev) means no gate at all.
import os
LINK_KEY = os.environ.get("FF_LINK_KEY")


def require_key(request: Request) -> None:
    if not LINK_KEY:
        return
    supplied = request.headers.get("x-ff-key") or request.query_params.get("key")
    if supplied != LINK_KEY:
        raise HTTPException(status_code=401, detail=(
            "This link needs its key. Open the exact link you were sent (it carries the "
            "key) - or ask Caleb for the current one."))


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


@app.get("/api/league/{league_id}", dependencies=[Depends(require_key)])
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
                "rank": t["contention_rank"],
                "starting_production": t["starting_production"],
                "pct_of_best": t["pct_of_best"],
                "starter_value": t["starter_value"],
                "window": t["window"],
                "contention": t["contention"],
                "trajectory": t["trajectory"],
                # The flavor word ("stalled", "convertible") often says more than the
                # trajectory ("steady") - a steady rebuild IS a stalled one, and the
                # table hiding that word read as false comfort to the league.
                "flavor": t["flavor"],
                "ascending_pct": t["ascending_pct"],
                "declining_pct": t["declining_pct"],
                "owns_next_first": t["owns_next_first"],
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


@app.get("/sessions", dependencies=[Depends(require_key)])
def session_status() -> dict:
    """Visible so session count/idle time can be checked against the memory ceiling
    rather than guessed at - each live session holds two subprocesses."""
    return sessions.status()


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


@app.post("/feedback", dependencies=[Depends(require_key)])
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
    return budget.status()


# What each in-flight question is doing right now, keyed by the client's session id.
# Polling rather than streaming on purpose: /ask already returns one whole answer, and
# turning it into an SSE stream would rewrite the response contract, the retry logic and
# the eval harness to show a progress line. A dict the client reads while it waits costs
# ~15 lines and nothing else changes.
_progress: dict[str, str] = {}


@app.get("/progress/{session_id}", dependencies=[Depends(require_key)])
def progress(session_id: str) -> dict:
    return {"step": _progress.get(session_id)}


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


@app.get("/answer/{session_id}", dependencies=[Depends(require_key)])
def last_answer(session_id: str) -> dict:
    return _answers.get(session_id) or {}


@app.post("/ask", response_model=AskResponse, dependencies=[Depends(require_key)])
async def ask(request: AskRequest) -> AskResponse:
    # Checked before calling Claude, so an exhausted budget costs nothing to serve.
    if budget.is_exhausted():
        return AskResponse(text=budget.OVER_BUDGET_MESSAGE, budget_exhausted=True)

    question = request.question.strip()
    if not question:
        return AskResponse(text="Ask a question about a Sleeper dynasty league.")
    question = question[:MAX_QUESTION_CHARS]

    track = (lambda step: _progress.__setitem__(request.session_id, step)) \
        if request.session_id else None
    created = False
    try:
        if request.session_id:
            session, created = await sessions.acquire(request.session_id)
            # Held for the whole turn: two concurrent requests on one session would
            # interleave on the same client and corrupt the conversation.
            async with session.lock:
                result = await run_query(question, verbose=False, client=session.client,
                                         on_progress=track)
                # Convert the client's cumulative total into this question's own cost -
                # see Session.cost_delta for why the raw value would both over-report
                # and over-charge the daily budget on a persistent session.
                result["cost_usd"] = session.cost_delta(result["cost_usd"])
        else:
            result = await run_query(question, verbose=False, on_progress=track)
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
        budget.record(None)
        _progress.pop(request.session_id, None)
        return AskResponse(text="Something went wrong answering that. Try again, or try a different league.")

    _progress.pop(request.session_id, None)
    budget.record(result["cost_usd"])
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
