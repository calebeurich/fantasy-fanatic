"""Daily spend ceiling for the public HTTP surface.

`agent.py`'s MAX_BUDGET_USD caps a *single* call. That's the wrong unit for a public
endpoint: ~40 uncapped calls would drain the whole project API budget, and a bot
scanning for open endpoints does that in under a minute. This caps the *day*.

**In-process counter, no database - deliberate.** The original plan called for
DynamoDB/Firestore to hold this, but that's only necessary if several instances each
hold their own partial count. Pinning Cloud Run to `max-instances=1` (which this
service wants anyway - every request spawns both a `claude` CLI process and an MCP
server subprocess, so it is memory-heavy and not a horizontal-scaling workload) makes
a plain in-process counter exactly accurate, with zero extra infrastructure. The
tradeoff is no horizontal scaling, which a demo does not need.

Set `max-instances=1` AND `concurrency=1` on the service: concurrency 1 also removes
any check-then-record race, so the ceiling can't be overshot by parallel requests.

Known, bounded imprecision: a call's real cost isn't known until after Claude has
already been called, so the check is "has the ceiling already been passed?" The
ceiling can therefore be exceeded by at most one call's worth (MAX_BUDGET_USD, $0.50)
before the next request is refused. Accepted rather than engineered around - the
alternative is pre-estimating token cost, which would be a guess.
"""

import os
from datetime import date

# Deliberately low. This is a portfolio demo on a small prepaid API budget, not a
# product - the goal is that a bad day costs cents, not the whole budget. Override
# via env var (e.g. a Cloud Run setting) without a code change.
DAILY_BUDGET_USD = float(os.environ.get("DAILY_BUDGET_USD", "1.00"))

# Backstop for the case where a call returns no cost at all (a failed run can have
# cost_usd=None): without this, unpriced calls would never move the dollar counter
# and the dollar ceiling alone would never trip.
DAILY_MAX_REQUESTS = int(os.environ.get("DAILY_MAX_REQUESTS", "50"))

OVER_BUDGET_MESSAGE = (
    "This demo has hit its daily budget and is paused until tomorrow (UTC). "
    "It runs on a small prepaid API budget, so the cap is deliberately low. "
    "The code and full reasoning are at https://github.com/calebeurich/fantasy-fanatic"
)

_day: date | None = None
_spend_usd: float = 0.0
_requests: int = 0


def _roll_day() -> None:
    """Reset the counters when the UTC day changes. Called on every read/write so
    there's no scheduler or background task to get out of sync with."""
    global _day, _spend_usd, _requests
    today = date.today()
    if _day != today:
        _day, _spend_usd, _requests = today, 0.0, 0


def is_exhausted() -> bool:
    _roll_day()
    return _spend_usd >= DAILY_BUDGET_USD or _requests >= DAILY_MAX_REQUESTS


def record(cost_usd: float | None) -> None:
    """Record a completed call. Always counts the request, even when the call
    reported no cost - a failed or unpriced call still consumed real capacity."""
    global _spend_usd, _requests
    _roll_day()
    _requests += 1
    _spend_usd += cost_usd or 0.0


def status() -> dict:
    _roll_day()
    return {
        "date_utc": str(_day),
        "spend_usd": round(_spend_usd, 4),
        "daily_budget_usd": DAILY_BUDGET_USD,
        "requests": _requests,
        "daily_max_requests": DAILY_MAX_REQUESTS,
        "exhausted": is_exhausted(),
    }
