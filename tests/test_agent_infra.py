"""Unit tests for the agent-side infrastructure: TTL cache, daily budget ceiling,
the trade-grounding check, and conversation storage.

All free and offline - none of this touches the API. These were previously verified
only by ad-hoc shell commands during development, which left nothing behind to catch
a regression.

Run: python -m pytest tests/ -q
"""



import asyncio

from agent import budget
from agent.agent import _trade_violations
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


def test_concurrent_misses_compute_once():
    """A cold page load fires ~10 requests at once; each used to rebuild the same league
    context, so all ten took 10x as long and the requests queued behind them timed out."""
    import threading, time as _time
    calls = []

    @ttl_cache(60)
    def build(league_id):
        calls.append(league_id)
        _time.sleep(0.2)
        return league_id

    threads = [threading.Thread(target=build, args=("A",)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == ["A"]
    assert build("B") == "B" and calls == ["A", "B"]   # other keys are not blocked by A's lock


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


# ------------------------------------------------------- silent reset made visible

def test_a_chat_reads_as_new_until_saved_and_again_after_it_is_dropped():
    """A tester whose conversation was lost used to get a model with amnesia and no warning -
    the fabricated-needs incident grew from exactly that. `load` returning None is what lets
    the page say 'this answer starts fresh'."""
    from agent import conversations

    async def scenario():
        first = await conversations.load("jwall-tab")
        await conversations.save("jwall-tab", [{"role": "user", "content": "hi"}])
        again = await conversations.load("jwall-tab")
        await conversations.drop("jwall-tab")
        return first, again, await conversations.load("jwall-tab")

    first, again, after_drop = asyncio.run(scenario())
    assert first is None and again == [{"role": "user", "content": "hi"}] and after_drop is None


# ----------------------------------------------------------- sleeper season chains

def test_season_chain_treats_string_zero_as_the_end(monkeypatch):
    """Some Sleeper leagues terminate their chain with previous_league_id "0" rather
    than null; chasing it 404s. Found crawling leagues beyond our own - any tool
    pointed at such a league would crash on its first context build."""
    from sources import sleeper
    leagues = {"a": {"previous_league_id": "b"}, "b": {"previous_league_id": "0"}}
    monkeypatch.setattr(sleeper, "get_league", lambda lid: leagues[lid])
    assert sleeper.get_season_chain("a") == ["a", "b"]


def test_fake_tool_call_xml_in_an_answer_is_detected():
    """A session with no tools makes the model write tool-call XML as text and invent a
    verdict (staging, 2026-08-17: 1 turn, no tool calls, an evaluate_trade invoke with
    parameters that don't exist). Deterministic to spot: real tool calls never appear
    in answer text."""
    from agent.agent import FAKE_TOOL_CALL
    fake = ("I'll evaluate this trade. <function_calls> <invoke name=\"check_league_format\">"
            "<parameter name=\"league_id\">1</parameter></invoke></function_calls> Mild win for you.")
    assert FAKE_TOOL_CALL.search(fake)
    assert not FAKE_TOOL_CALL.search("For dezdroppedit27 (contend - on a clock): starting lineup +1.3 points a game.")


def test_ballpark_guard_needs_an_evaluated_trade():
    """The guard fires only when evaluate_trade ran AND the answer benchmarks without a
    BALLPARK line - the word "benchmark" in a format answer must not trigger a retry
    (it did, live: the retry replaced a correct answer with "I haven't evaluated a trade")."""
    from agent.agent import _ballpark_violation
    text = "Every trade benchmark the tools return is priced for this format."
    no_eval = [{"name": "mcp__fantasy_fanatic__check_league_format", "input": {}}]
    assert _ballpark_violation(text, no_eval, []) is False
    with_eval = [{"name": "mcp__fantasy_fanatic__evaluate_trade", "input": {}}]
    assert _ballpark_violation("Ballpark: a QB-for-pick deal typically lands around a 2nd.", with_eval, []) is True
    has_line = [{"content": "... BALLPARK for a top-10% piece ..."}]
    assert _ballpark_violation("Ballpark: a QB-for-pick deal typically lands around a 2nd.", with_eval, has_line) is False


def test_ppg_guard_flags_prices_wearing_pergame_units():
    """A 3+ digit number with a per-game unit is a season market price narrated as
    points (live, 2026-08-19: "Shough scores 2,984 PPG this season"); real per-game
    numbers are two digits and pass untouched."""
    from agent.agent import _unit_violations
    assert _unit_violations("Shough scores 2,984 PPG this season")
    assert _unit_violations("adds 590 ppg over Dobbins")
    assert _unit_violations("costs 1,611 points a game")
    assert not _unit_violations("gains 2.4 points a game; Lamar projects 18.1 ppg")
    assert not _unit_violations("priced at 2,984 for the season")
    # A whole lineup's total IS three digits a game - live on Haiku 5.5, 2026-10-11, the
    # guard forced a retry on a correct "projects to score 149.3 PPG".
    assert not _unit_violations("the lineup projects to score 149.3 PPG, 172 points a game at best")


def _api_error(text):
    import anthropic
    import httpx2
    return anthropic.APIError(text, request=httpx2.Request("POST", "https://api.anthropic.com"), body=None)


def test_a_refused_call_is_never_shown_as_the_answer(monkeypatch):
    """No credit, a bad key or an outage is a service problem to say plainly, not an answer."""
    import pytest
    from agent import tool_loop
    from agent.agent import AIServiceDown, new_conversation

    class Client:
        class messages:
            @staticmethod
            def stream(**kwargs):
                class Stream:
                    async def __aenter__(self):
                        raise _api_error("Your credit balance is too low to access the Anthropic API.")

                    async def __aexit__(self, *a):
                        pass
                return Stream()

    monkeypatch.setattr(tool_loop, "_api", lambda: Client())
    with pytest.raises(AIServiceDown) as e:
        asyncio.run(new_conversation().turn("q"))
    assert "out of AI credit" in e.value.message and "still work" in e.value.message


def test_low_credit_reads_as_billing_whatever_the_status():
    from agent.tool_loop import _service_error
    assert _service_error(_api_error("Your credit balance is too low")) == "billing_error"
    assert _service_error(_api_error("overloaded")) == "server_error"


def test_ask_says_the_service_is_down_and_charges_nothing(monkeypatch):
    import asyncio
    from agent import api
    from agent.agent import AIServiceDown

    async def refuse(*a, **k):
        raise AIServiceDown("billing_error")
    charged = []
    monkeypatch.setattr(api, "run_query", refuse)
    monkeypatch.setattr(api, "tier", lambda request: "friend")
    monkeypatch.setattr(api.budget.friends, "is_exhausted", lambda: False)
    monkeypatch.setattr(api.budget.friends, "record", charged.append)
    out = asyncio.run(api.ask(api.AskRequest(question="q"), None))
    assert out.service_down and "out of AI credit" in out.text
    assert charged == []


def test_an_unknown_owner_is_a_404_the_page_will_not_retry():
    """A trade partner carried over from another league caused a burst of retried 500s."""
    import json
    from agent import api
    from analysis.league import UnknownOwner
    assert issubclass(UnknownOwner, ValueError)            # tools that catch ValueError still do
    response = api.unknown_owner(None, UnknownOwner("no owner matching 'shivvv'"))
    assert response.status_code == 404
    assert "shivvv" in json.loads(response.body)["detail"]


def test_a_bad_league_id_is_the_visitors_to_fix_not_a_server_error(monkeypatch):
    import pytest
    import requests
    from fastapi import HTTPException
    from agent import api
    with pytest.raises(HTTPException) as e:
        api._league_or_404("notanumber")
    assert e.value.status_code == 404

    def gone(league_id):
        raise requests.HTTPError("404")
    monkeypatch.setattr(api.sleeper, "get_league", gone)
    with pytest.raises(HTTPException) as e:
        api._league_or_404("123")
    assert e.value.status_code == 404
    monkeypatch.setattr(api.sleeper, "get_league", lambda league_id: None)   # how Sleeper says "no such league"
    with pytest.raises(HTTPException):
        api._league_or_404("123")
    monkeypatch.setattr(api.sleeper, "get_league", lambda league_id: {"name": "XFL 2"})
    assert api._league_or_404("123")["name"] == "XFL 2"


def test_browser_errors_reach_the_logs_but_cannot_flood_them(monkeypatch):
    from types import SimpleNamespace
    from agent import api
    logged = []
    monkeypatch.setattr(api.observability, "log_run", logged.append)
    monkeypatch.setattr(api, "_client_error_counts", {})
    request = SimpleNamespace(headers={"x-forwarded-for": "1.2.3.4", "user-agent": "phone"})
    for _ in range(api.MAX_CLIENT_ERRORS_PER_VISITOR + 5):
        api.client_error(api.ClientError(message="x" * 900, where="1:2"), request)
    assert len(logged) == api.MAX_CLIENT_ERRORS_PER_VISITOR
    assert len(logged[0]["error"]) == 500
    assert logged[0]["outcome"] == "client_error"


def test_the_page_is_never_cached_against_a_newer_api():
    from agent import api
    assert api.index().headers["cache-control"] == "no-cache"
