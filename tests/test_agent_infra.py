"""Unit tests for the agent-side infrastructure: TTL cache, daily budget ceiling,
the trade-grounding check, and per-session cost accounting.

All free and offline - none of this touches the API. These were previously verified
only by ad-hoc shell commands during development, which left nothing behind to catch
a regression.

Run: python -m pytest tests/ -q
"""



import asyncio

from agent import budget
from agent.agent import _trade_violations
from agent.sessions import Session, SessionManager
from sources.cache import ttl_cache


# ------------------------------------------------------------------------ ttl cache

def test_cache_serves_repeat_calls_without_reinvoking():
    calls = []

    @ttl_cache(60)
    def fetch(league_id):
        calls.append(league_id)
        return {"league": league_id}

    assert fetch("A") == {"league": "A"}
    assert fetch("A") == {"league": "A"}
    assert calls == ["A"], "second call should have been served from cache"


def test_cache_keys_on_arguments():
    """A shared cache across leagues would be a correctness disaster, not just a
    performance detail - one league's rosters served for another."""
    @ttl_cache(60)
    def fetch(league_id):
        return league_id

    assert fetch("A") == "A"
    assert fetch("B") == "B"


def test_cache_expires_after_its_ttl(monkeypatch):
    """Clock is controlled rather than slept through. The first version of this test
    slept 0.06s against a 0.05s TTL - a 10ms margin that a loaded CI runner would miss
    intermittently, and an intermittently-red suite is worse than no suite because it
    teaches you to ignore failures."""
    import sources.cache as cache_module

    now = {"t": 1000.0}
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: now["t"])

    calls = []

    @ttl_cache(60)
    def fetch():
        calls.append(1)
        return len(calls)

    fetch()
    fetch()
    assert len(calls) == 1, "within TTL, second call should be cached"

    now["t"] += 61  # step past the TTL
    fetch()
    assert len(calls) == 2, "entry should have expired and refetched"


# --------------------------------------------------------------------- daily budget

def test_budget_trips_on_the_dollar_ceiling():
    b = budget.DailyBudget("t", usd=1.0, max_requests=50, over_message="over")
    assert not b.is_exhausted()
    b.record(0.999)
    assert not b.is_exhausted()
    b.record(0.002)
    assert b.is_exhausted()


def test_unpriced_calls_still_count_against_the_request_backstop():
    """A failed call can report cost_usd=None. Without the request counter, unpriced
    calls would never move the dollar total and the ceiling would never trip - the
    exact hole the backstop exists to close."""
    b = budget.DailyBudget("t", usd=1.0, max_requests=5, over_message="over")
    for _ in range(5):
        b.record(None)
    assert b.status()["spend_usd"] == 0.0
    assert b.is_exhausted(), "request backstop should trip even with zero recorded spend"


def test_demo_visitor_cap_counts_the_moment_it_allows():
    """One stranger cannot spend the whole demo day: after the per-visitor cap the next
    ask is refused, and a retry does not double-dip. Other visitors are unaffected."""
    budget._visitor_day, budget._visitor_asks = None, {}
    for _ in range(budget.DEMO_ASKS_PER_VISITOR):
        assert budget.visitor_allowed("1.2.3.4")
    assert not budget.visitor_allowed("1.2.3.4")
    assert budget.visitor_allowed("5.6.7.8")



# ------------------------------------------------------------- trade grounding check

def test_grounding_flags_a_name_only_in_trade_context():
    """The check was deliberately narrowed to trade-action lines: describing a roster
    necessarily names non-offerable players, and flagging those fired a needless retry
    on nearly every real question."""
    banned = {"Jonathan Taylor"}
    descriptive = "Your cornerstones: Jonathan Taylor and others anchor the roster."
    suggestion = "You could offer Jonathan Taylor for WR help."
    assert _trade_violations(descriptive, banned) == []
    assert _trade_violations(suggestion, banned) == ["Jonathan Taylor"]


def test_grounding_reports_every_violation_not_just_the_first():
    """Regression guard: an earlier version reported one name via next(), so an answer
    naming two non-offerable players got only half-corrected by the single retry."""
    banned = {"Jonathan Taylor", "Christian McCaffrey"}
    text = "You could trade Jonathan Taylor or Christian McCaffrey."
    assert _trade_violations(text, banned) == ["Christian McCaffrey", "Jonathan Taylor"]


def test_grounding_is_line_scoped():
    """A trade verb on one line shouldn't implicate a name mentioned on another."""
    banned = {"Lamar Jackson"}
    text = "Your cornerstone is Lamar Jackson.\nYou could offer your bench depth."
    assert _trade_violations(text, banned) == []


# ------------------------------------------------------------ per-session cost delta

def test_session_bills_the_delta_not_the_cumulative_total():
    """total_cost_usd is cumulative for the client's lifetime. On a persistent session
    the raw value would over-report each question and charge the budget the running
    total every turn - draining the ceiling far faster than real spend."""
    session = Session.__new__(Session)
    session.cost_baseline = 0.0
    assert session.cost_delta(0.015) == 0.015
    assert round(session.cost_delta(0.031), 3) == 0.016
    assert round(session.cost_delta(0.044), 3) == 0.013


def test_session_cost_delta_handles_missing_cost():
    session = Session.__new__(Session)
    session.cost_baseline = 0.0
    assert session.cost_delta(None) is None


# ------------------------------------------------------- silent reset made visible

class _FakeClient:
    async def connect(self):
        pass

    async def disconnect(self):
        pass


def test_acquire_reports_when_a_known_id_gets_a_fresh_conversation(monkeypatch):
    """A tester whose session was evicted (idle TTL, LRU, a deploy) used to get a model
    with amnesia and no warning - the fabricated-needs incident grew from exactly that.
    The `created` flag is what lets the page say 'this answer starts fresh'."""
    import agent.sessions as sessions_mod
    monkeypatch.setattr(sessions_mod, "ClaudeSDKClient", lambda options: _FakeClient())
    manager = SessionManager(options_factory=lambda: None)

    async def scenario():
        _, created_first = await manager.acquire("jwall-tab")
        _, created_again = await manager.acquire("jwall-tab")
        await manager.close_all()  # the eviction, from the client's point of view
        _, created_after_reset = await manager.acquire("jwall-tab")
        return created_first, created_again, created_after_reset

    first, again, after_reset = asyncio.run(scenario())
    assert first is True
    assert again is False
    assert after_reset is True


# ----------------------------------------------------------- sleeper season chains

def test_season_chain_treats_string_zero_as_the_end(monkeypatch):
    """Some Sleeper leagues terminate their chain with previous_league_id "0" rather
    than null; chasing it 404s. Found crawling leagues beyond our own - any tool
    pointed at such a league would crash on its first context build."""
    from sources import sleeper
    leagues = {"a": {"previous_league_id": "b"}, "b": {"previous_league_id": "0"}}
    monkeypatch.setattr(sleeper, "get_league", lambda lid: leagues[lid])
    assert sleeper.get_season_chain("a") == ["a", "b"]
