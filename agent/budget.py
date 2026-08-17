"""Daily spend ceilings for the public HTTP surface - one per tier.

`agent.py`'s MAX_BUDGET_USD caps a *single* call. That's the wrong unit for a public
endpoint: ~40 uncapped calls would drain the whole project API budget, and a bot
scanning for open endpoints does that in under a minute. This caps the *day*, twice:
the FRIENDS tier (the shared link key) and the DEMO tier (the bare public URL, no key),
each with its own ceiling, plus a per-visitor cap on the demo so one stranger cannot
spend the whole demo day.

**In-process counters, no database - deliberate.** The original plan called for
DynamoDB/Firestore to hold this, but that's only necessary if several instances each
hold their own partial count. Pinning Cloud Run to `max-instances=1` (which this
service wants anyway - every request spawns both a `claude` CLI process and an MCP
server subprocess, so it is memory-heavy and not a horizontal-scaling workload) makes
a plain in-process counter exactly accurate, with zero extra infrastructure. The
tradeoff is no horizontal scaling, which a demo does not need.

`max-instances=1` is what makes the counters exact - every request lands in this
process. It is load-bearing and lives in deploy.yml.

Known, bounded imprecision, in three parts, all accepted rather than engineered around:

1. A call's real cost isn't known until after Claude has been called, so the check is
   "has the ceiling already been passed?" - the alternative is pre-estimating token
   cost, which would be a guess.
2. `concurrency=2` (raised from 1 so two friends don't queue behind each other's
   60-90s answer) reopens a check-then-record race the single-lane version had closed:
   both in-flight calls can pass the check before either records. Overshoot is
   therefore bounded by TWO calls' worth (2 x MAX_BUDGET_USD) rather than one.
3. **A deploy resets the counters.** They live in process memory, so a new revision
   starts the day at zero - which means the real ceiling is per instance lifetime, not
   per calendar day, on any day with deploys. Harmless for a demo (a deploy is a
   deliberate act by the author, not something a caller can trigger), and the cost of
   fixing it is the database this design deliberately avoids.
"""

import os
from datetime import date

REPO = "https://github.com/calebeurich/fantasy-fanatic"


class DailyBudget:
    """A dollar ceiling and a request backstop that both roll at the UTC day."""

    def __init__(self, name: str, usd: float, max_requests: int, over_message: str):
        self.name, self.usd, self.max_requests, self.over_message = name, usd, max_requests, over_message
        self._day: date | None = None
        self._spend = 0.0
        self._requests = 0

    def _roll_day(self) -> None:
        """Reset when the UTC day changes. Called on every read/write so there's no
        scheduler or background task to get out of sync with."""
        today = date.today()
        if self._day != today:
            self._day, self._spend, self._requests = today, 0.0, 0

    def is_exhausted(self) -> bool:
        self._roll_day()
        return self._spend >= self.usd or self._requests >= self.max_requests

    def record(self, cost_usd: float | None) -> None:
        """Always counts the request, even when the call reported no cost - a failed or
        unpriced call still consumed real capacity (and unpriced calls would otherwise
        never trip the dollar ceiling)."""
        self._roll_day()
        self._requests += 1
        self._spend += cost_usd or 0.0

    def status(self) -> dict:
        self._roll_day()
        return {"tier": self.name, "date_utc": str(self._day), "spend_usd": round(self._spend, 4),
                "daily_budget_usd": self.usd, "requests": self._requests,
                "daily_max_requests": self.max_requests, "exhausted": self.is_exhausted()}


# Deliberately low. This is a portfolio demo on a small prepaid API budget, not a
# product - the goal is that a bad day costs cents, not the whole budget. Override
# via env vars (Cloud Run settings) without a code change.
friends = DailyBudget(
    "friends", float(os.environ.get("DAILY_BUDGET_USD", "1.00")),
    int(os.environ.get("DAILY_MAX_REQUESTS", "50")),
    "This has hit its daily budget and is paused until tomorrow (UTC). It runs on a "
    f"small prepaid API budget, so the cap is deliberately low. The code and full "
    f"reasoning are at {REPO}")

demo = DailyBudget(
    "demo", float(os.environ.get("DEMO_BUDGET_USD", "0")),   # 0 = the public demo is off
    int(os.environ.get("DEMO_MAX_REQUESTS", "40")),
    "The public demo has used its budget for today (UTC) - the league table and rosters "
    f"above still work, and it resets tomorrow. Code, evals and the reasoning behind "
    f"every read: {REPO}")

# How many questions one visitor gets from the demo tier per day. The table, rosters
# and composer are free; only the model costs money.
DEMO_ASKS_PER_VISITOR = int(os.environ.get("DEMO_ASKS_PER_VISITOR", "3"))
DEMO_VISITOR_MESSAGE = (
    f"That's the {DEMO_ASKS_PER_VISITOR} demo questions for today from this connection - "
    "the table, rosters and trade composer above keep working, and questions reset "
    f"tomorrow (UTC). Want more? The code is at {REPO}, or ask Caleb for a friends link.")

_visitor_day: date | None = None
_visitor_asks: dict[str, int] = {}


def visitor_allowed(ip: str) -> bool:
    """Per-visitor cap on the demo tier - counts a question the moment it is allowed,
    so a retry cannot double-dip."""
    global _visitor_day, _visitor_asks
    today = date.today()
    if _visitor_day != today:
        _visitor_day, _visitor_asks = today, {}
    if _visitor_asks.get(ip, 0) >= DEMO_ASKS_PER_VISITOR:
        return False
    _visitor_asks[ip] = _visitor_asks.get(ip, 0) + 1
    return True
