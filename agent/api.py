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
import traceback
from contextlib import asynccontextmanager
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
                "ascending_pct": t["ascending_pct"],
                "declining_pct": t["declining_pct"],
                "owns_next_first": t["owns_next_first"],
                "cornerstones": [e["name"] for e in t["cornerstones"]],
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


@app.get("/budget")
def budget_status() -> dict:
    """Exposed so the daily cap is externally verifiable, rather than something you
    have to trust is working - used to confirm the ceiling actually trips before this
    endpoint is ever made public."""
    return budget.status()


@app.post("/ask", response_model=AskResponse, dependencies=[Depends(require_key)])
async def ask(request: AskRequest) -> AskResponse:
    # Checked before calling Claude, so an exhausted budget costs nothing to serve.
    if budget.is_exhausted():
        return AskResponse(text=budget.OVER_BUDGET_MESSAGE, budget_exhausted=True)

    question = request.question.strip()
    if not question:
        return AskResponse(text="Ask a question about a Sleeper dynasty league.")
    question = question[:MAX_QUESTION_CHARS]

    try:
        if request.session_id:
            session = await sessions.acquire(request.session_id)
            # Held for the whole turn: two concurrent requests on one session would
            # interleave on the same client and corrupt the conversation.
            async with session.lock:
                result = await run_query(question, verbose=False, client=session.client)
                # Convert the client's cumulative total into this question's own cost -
                # see Session.cost_delta for why the raw value would both over-report
                # and over-charge the daily budget on a persistent session.
                result["cost_usd"] = session.cost_delta(result["cost_usd"])
        else:
            result = await run_query(question, verbose=False)
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
        return AskResponse(text="Something went wrong answering that. Try again, or try a different league.")

    budget.record(result["cost_usd"])
    return AskResponse(
        text=result["text"],
        cost_usd=result["cost_usd"],
        num_turns=result["num_turns"],
        grounding_retries=result["grounding_retries"],
    )
