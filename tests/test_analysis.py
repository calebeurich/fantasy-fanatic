"""Unit tests for the analysis heuristics.

**Free and offline by design.** Almost every rule in `analysis/` is already a pure
function taking plain data, so these need no fixtures, no network, and no API spend -
which is the whole reason they're worth having alongside `agent/evals.py`. The evals
cost real money and catch *agent misbehavior*; they would happily pass while a
threshold was silently wrong. These catch the opposite failure.

What's deliberately tested here is the **boundaries** - the exact ages, ranks, and
fractions the heuristics turn on. Those constants came from inspecting real league
distributions (see LOGIC.md), so an accidental edit is exactly the kind of change that
would otherwise go unnoticed until a recommendation looked odd.

Run: python -m pytest tests/ -q
"""

import pytest

from analysis import roster_needs, team_state, trade_targets
from analysis.team_values import AGE_CURVE, age_bucket


# --------------------------------------------------------------------------- age curve

@pytest.mark.parametrize("position,cutoffs", list(AGE_CURVE.items()))
def test_age_bucket_boundaries_are_inclusive_on_decline(position, cutoffs):
    young, old = cutoffs
    """Exactly at the young cutoff is prime, not ascending; exactly at the old cutoff
    is declining. Off-by-one here would silently reclassify a whole age cohort."""
    assert age_bucket(position, young - 0.1) == "ascending"
    assert age_bucket(position, young) == "prime"
    assert age_bucket(position, old - 0.1) == "prime"
    assert age_bucket(position, old) == "declining"


def test_rushing_qb_declines_earlier_than_pocket_qb():
    """The usage-role override is the point of player_roles.py: a mobile QB's decline
    pulls forward (34 -> 31). At 32 the same player is prime as a pocket passer and
    declining as a rusher."""
    assert age_bucket("QB", 32) == "prime"
    assert age_bucket("QB", 32, role="rushing_qb") == "declining"


def test_pass_catching_rb_declines_later_than_standard_rb():
    """Mirror case: receiving work ages better than between-the-tackles work (27 -> 29)."""
    assert age_bucket("RB", 28) == "declining"
    assert age_bucket("RB", 28, role="pass_catching_rb") == "prime"


def test_unknown_age_or_position_is_not_guessed():
    assert age_bucket("RB", None) == "unknown"
    assert age_bucket("K", 27) == "unknown"


# ------------------------------------------------------------------ relevance floor

def test_upside_players_clear_a_lower_bar_than_production():
    """Ascending value is priced on future growth, so it clears at 25% of replacement
    level while production/mixed needs 50%. Same value, different verdict - this
    asymmetry was calibrated against real trade chips, not picked arbitrarily."""
    thresholds = {"WR": 1000}
    ascending = {"bucket": "ascending", "position": "WR", "value": 300}
    declining = {"bucket": "declining", "position": "WR", "value": 300}
    assert team_state.clears_relevance_floor(ascending, thresholds) is True
    assert team_state.clears_relevance_floor(declining, thresholds) is False


def test_relevance_floor_is_exactly_at_the_fraction():
    thresholds = {"RB": 1000}
    assert team_state.clears_relevance_floor({"bucket": "prime", "position": "RB", "value": 500}, thresholds)
    assert not team_state.clears_relevance_floor({"bucket": "prime", "position": "RB", "value": 499}, thresholds)


# --------------------------------------------------------------- needs and surplus

def _players(spec):
    """spec: list of (position, value) -> the {player_id: info} shape sources produce."""
    return {str(i): {"name": f"P{i}", "position": pos, "value": val, "age": 26}
            for i, (pos, val) in enumerate(spec)}


def test_needs_critical_thin_and_satisfied():
    """Fewer usable than required is critical; exactly enough is thin (no cushion);
    more is neither."""
    slots = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
    thresholds = {"QB": 100, "RB": 100, "WR": 100, "TE": 100}
    players = _players([("QB", 500),                      # 1 usable QB  -> thin
                        ("RB", 500),                      # 1 usable RB  -> critical (needs 2)
                        ("WR", 500), ("WR", 500), ("WR", 500),  # 3 usable WR -> fine
                        ("TE", 50)])                      # below threshold -> critical
    roster = {"players": list(players), "starters": []}
    needs = roster_needs.find_needs(roster, players, slots, thresholds)
    assert needs["QB"] == "thin"
    assert needs["RB"] == "critical"
    assert needs["TE"] == "critical"
    assert "WR" not in needs


def test_surplus_is_the_mirror_of_needs_and_excludes_the_starting_group():
    """Only players *beyond* the required starter count are surplus - the top slots[pos]
    are the actual lineup and must never be offered as spare depth."""
    slots = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
    thresholds = {"QB": 100, "RB": 100, "WR": 100, "TE": 100}
    players = _players([("WR", 900), ("WR", 800), ("WR", 700), ("WR", 600)])
    roster = {"players": list(players), "starters": []}
    surplus = roster_needs.find_surplus(roster, players, slots, thresholds)
    names = [e["name"] for e in surplus["WR"]]
    assert len(names) == 2, "4 usable WR with 2 slots should yield exactly 2 surplus"
    assert [e["value"] for e in surplus["WR"]] == [700, 600], "surplus is the lowest-valued, not the best"
    assert "QB" not in surplus, "a position with no usable players is a need, never a surplus"


def test_a_position_cannot_be_both_a_need_and_a_surplus():
    slots = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
    thresholds = {p: 100 for p in slots}
    players = _players([("QB", 500), ("RB", 500), ("RB", 400), ("WR", 500), ("TE", 500)])
    roster = {"players": list(players), "starters": []}
    needs = roster_needs.find_needs(roster, players, slots, thresholds)
    surplus = roster_needs.find_surplus(roster, players, slots, thresholds)
    assert not (set(needs) & set(surplus))


# ------------------------------------------------------------- team window classification

def _roster(starter_specs):
    players = _players(starter_specs)
    ids = list(players)
    return {"players": ids, "starters": ids}, players


def test_window_classification_follows_the_age_mix():
    """Win-Now when value skews declining, Rebuilding when it skews ascending.
    Thresholds (-10 / +30) are organic breakpoints from a real league's distribution."""
    old_roster, old_players = _roster([("RB", 1000), ("RB", 1000)])
    for info in old_players.values():
        info["age"] = 30  # RB declines at 27
    assert team_state.classify(old_roster, old_players, threshold=10_000)["state"] == "Win-Now"

    young_roster, young_players = _roster([("WR", 1000), ("WR", 1000)])
    for info in young_players.values():
        info["age"] = 22  # WR ascends below 25
    assert team_state.classify(young_roster, young_players, threshold=10_000)["state"] == "Rebuilding"


def test_offer_pool_excludes_starters_but_keeps_equivalent_bench_players():
    """A valuable non-cornerstone *starter* is the team, not spare parts. Regression
    guard for a real bug where a team was told to offer its own starting QB2. Tested by
    comparing two otherwise-identical players so the assertion can't pass vacuously -
    same position, same value, same bucket, differing only in is_starter."""
    thresholds = {"QB": 100}
    starter = {"name": "QB2", "position": "QB", "value": 900, "is_starter": True, "bucket": "prime"}
    bench = {"name": "QB3", "position": "QB", "value": 900, "is_starter": False, "bucket": "prime"}

    offered = trade_targets._my_offer_pool({"sellable": [starter, bench], "tradeable_surplus": []},
                                           thresholds, needs={})
    names = [e["name"] for e in offered]
    assert "QB3" in names, "bench depth at this value should be offerable"
    assert "QB2" not in names, "a starter must never be offered as surplus"


# --------------------------------------------------------------------- offerable names

def test_offerable_names_covers_every_find_targets_mode():
    """agent.py's grounding check depends on this being complete for all three modes -
    a mode returning an empty set would silently ban every player on the roster."""
    buy = {"mode": "buy", "my_offers": [{"name": "A"}]}
    rebuild = {"mode": "rebuild", "sell_candidates": [{"name": "B"}], "situational": [{"name": "C"}]}
    middling = {"mode": "middling",
                "push": {"my_offers": [{"name": "D"}]},
                "pivot": {"sell_candidates": [{"name": "E"}], "situational": [{"name": "F"}]}}
    assert trade_targets.offerable_names(buy) == {"A"}
    assert trade_targets.offerable_names(rebuild) == {"B", "C"}
    assert trade_targets.offerable_names(middling) == {"D", "E", "F"}


def test_offer_pool_never_includes_a_position_the_team_needs():
    """Trading a WR while WR is your own need just moves the shortage. Real bug this
    guards: a Win-Now team with a critical WR need was told to offer its WRs."""
    me = {"sellable": [{"name": "W", "position": "WR", "value": 900, "is_starter": False, "bucket": "prime"}],
          "tradeable_surplus": []}
    thresholds = {"WR": 100}
    assert trade_targets._my_offer_pool(me, thresholds, needs={}) != []
    assert trade_targets._my_offer_pool(me, thresholds, needs={"WR": "critical"}) == []
