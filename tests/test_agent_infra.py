"""Unit tests for the agent-side infrastructure: TTL cache, daily budget ceiling,
the trade-grounding check, and per-session cost accounting.

All free and offline - none of this touches the API. These were previously verified
only by ad-hoc shell commands during development, which left nothing behind to catch
a regression.

Run: python -m pytest tests/ -q
"""

import time

from agent import budget
from agent.agent import _trade_violations
from agent.sessions import Session
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


def test_cache_expires_after_its_ttl():
    calls = []

    @ttl_cache(0.05)
    def fetch():
        calls.append(1)
        return len(calls)

    fetch()
    fetch()
    assert len(calls) == 1
    time.sleep(0.06)
    fetch()
    assert len(calls) == 2, "entry should have expired and refetched"


# --------------------------------------------------------------------- daily budget

def _reset_budget(spend=0.0, requests=0):
    budget._roll_day()
    budget._spend_usd = spend
    budget._requests = requests


def test_budget_trips_on_the_dollar_ceiling():
    _reset_budget()
    assert not budget.is_exhausted()
    budget.record(budget.DAILY_BUDGET_USD - 0.001)
    assert not budget.is_exhausted()
    budget.record(0.002)
    assert budget.is_exhausted()
    _reset_budget()


def test_unpriced_calls_still_count_against_the_request_backstop():
    """A failed call can report cost_usd=None. Without the request counter, unpriced
    calls would never move the dollar total and the ceiling would never trip - the
    exact hole the backstop exists to close."""
    _reset_budget()
    for _ in range(budget.DAILY_MAX_REQUESTS):
        budget.record(None)
    assert budget.status()["spend_usd"] == 0.0
    assert budget.is_exhausted(), "request backstop should trip even with zero recorded spend"
    _reset_budget()


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
