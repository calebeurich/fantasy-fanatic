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
import sys

from fastapi import FastAPI
from pydantic import BaseModel

from . import budget
from .agent import run_query, MCP_SERVER_PATH

app = FastAPI(title="fantasy-fanatic agent")

MAX_QUESTION_CHARS = 1000  # a real question is far shorter; this just bounds abuse


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    text: str
    cost_usd: float | None = None
    num_turns: int | None = None
    grounding_retries: int | None = None
    budget_exhausted: bool = False


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/diagnostics")
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

    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(command=sys.executable, args=[str(MCP_SERVER_PATH)])

        async def _list_tools():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return [t.name for t in (await session.list_tools()).tools]

        # Bounded: a hung subprocess should report as a timeout, not hang the request.
        info["mcp_tools"] = await asyncio.wait_for(_list_tools(), timeout=120)
        info["mcp_ok"] = True
    except Exception as e:
        info["mcp_ok"] = False
        info["mcp_error"] = f"{type(e).__name__}: {e}"
        info["mcp_traceback"] = traceback.format_exc()[-3000:]

    return info


@app.get("/budget")
def budget_status() -> dict:
    """Exposed so the daily cap is externally verifiable, rather than something you
    have to trust is working - used to confirm the ceiling actually trips before this
    endpoint is ever made public."""
    return budget.status()


@app.post("/ask", response_model=AskResponse)
async def ask(request: AskRequest) -> AskResponse:
    # Checked before calling Claude, so an exhausted budget costs nothing to serve.
    if budget.is_exhausted():
        return AskResponse(text=budget.OVER_BUDGET_MESSAGE, budget_exhausted=True)

    question = request.question.strip()
    if not question:
        return AskResponse(text="Ask a question about a Sleeper dynasty league.")
    question = question[:MAX_QUESTION_CHARS]

    try:
        result = await run_query(question, verbose=False)
    except Exception:
        # A failed call still consumed real capacity (it may have burned tokens
        # before failing), so it counts against the day rather than being free to
        # retry in a loop. The exception detail is deliberately not returned to the
        # caller - it's already in the observability log via run_query's own
        # try/finally, and internals shouldn't leak out of a public endpoint.
        budget.record(None)
        return AskResponse(text="Something went wrong answering that. Try again, or try a different league.")

    budget.record(result["cost_usd"])
    return AskResponse(
        text=result["text"],
        cost_usd=result["cost_usd"],
        num_turns=result["num_turns"],
        grounding_retries=result["grounding_retries"],
    )
