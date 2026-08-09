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

from fastapi import FastAPI
from pydantic import BaseModel

from . import budget
from .agent import run_query

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
