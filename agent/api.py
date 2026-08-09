"""HTTP wrapper around run_query - the entrypoint any hosting platform (Cloud Run,
Lambda + Function URL, Render) actually needs, since none of them can invoke a
one-shot CLI directly. Deliberately platform-agnostic: a plain FastAPI app, no
provider-specific code, so the eventual hosting choice doesn't change this file.

Run locally: python -m uvicorn agent.api:app --reload
Then: curl -X POST http://127.0.0.1:8000/ask -H "Content-Type: application/json" \
  -d '{"question": "For Sleeper league <id>, what team window is <owner> in?"}'

Cost caps beyond agent.py's existing per-call MAX_BUDGET_USD (a persistent daily
request counter, checked before calling Claude) are deliberately NOT built here yet -
that needs real persistent storage (DynamoDB/Firestore/etc.), which depends on
whichever hosting platform gets picked. Not guessing at that ahead of the decision.
"""

from fastapi import FastAPI
from pydantic import BaseModel

from .agent import run_query

app = FastAPI(title="fantasy-fanatic agent")


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    text: str
    cost_usd: float | None
    num_turns: int | None
    grounding_retries: int


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/ask", response_model=AskResponse)
async def ask(request: AskRequest) -> AskResponse:
    result = await run_query(request.question, verbose=False)
    return AskResponse(
        text=result["text"],
        cost_usd=result["cost_usd"],
        num_turns=result["num_turns"],
        grounding_retries=result["grounding_retries"],
    )
