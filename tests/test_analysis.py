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


def test_age_mix_ships_with_an_explanation_not_a_bare_number():
    """Regression guard for a real confabulation: the field was once emitted as a bare
    {"diff": -11}, and the model reliably invented meanings for it - "below their
    expected win total", "underperforming by 25 points" - none of which exist, least of
    all in a preseason with no games played. An unlabeled number in a tool result is an
    invitation to make one up, so the label must ship with the value."""
    roster, players = _roster([("RB", 1000), ("RB", 1000)])
    for info in players.values():
        info["age"] = 30
    result = team_state.classify(roster, players, threshold=10_000)

    assert "diff" not in result, "bare unlabelled 'diff' must not come back"
    assert result["age_mix_score"] < 0
    note = result["age_mix_note"].lower()
    assert "age-composition" in note
    assert "says nothing about wins" in note, "the note must rule out the wrong reading"


def test_projected_starters_uses_value_not_the_live_snapshot():
    """Sleeper's `starters` field is the current week's lineup, which is meaningless
    before Week 1. In a real superflex league (2 QB slots) it listed only one QB, so the
    team's obvious QB2 was classed as bench and offered away as spare parts. Projected
    starters are derived from value and the league's own slot counts instead."""
    slots = {"QB": 2, "RB": 2, "WR": 3, "TE": 1}
    players = _players([("QB", 3528), ("QB", 3288), ("QB", 2735), ("QB", 1325)])
    # Snapshot claims only the first QB starts - the exact preseason bug.
    roster = {"players": list(players), "starters": ["0"]}

    projected = roster_needs.projected_starters(roster, players, slots)
    assert "P0" in projected and "P1" in projected, "top 2 QBs by value are the lineup"
    assert "P2" not in projected and "P3" not in projected, "QB3/QB4 are genuinely spare"


def test_win_now_buyer_sees_production_priced_targets_first():
    """A Win-Now team buys current production. This project's own pricing model calls
    declining players 'production-priced' and prime ones 'upside-priced, may cost more
    than the fit justifies' - so ordering purely by (trade activity, value) contradicted
    it. A real Win-Now team was handed six buy targets, every one prime."""
    # _buy_path also builds the offer pool, so `me` needs those lists even though this
    # test only asserts on target ordering.
    me = {"owner_id": "me", "effective_strategy": "Win-Now", "sellable": [], "tradeable_surplus": []}
    thresholds = {"WR": 100}
    seller = {
        "owner_id": "them", "owner": "them", "effective_strategy": "Rebuilding",
        "sellable": [
            {"name": "PrimeGuy", "position": "WR", "value": 4000, "bucket": "prime", "is_starter": False},
            {"name": "AgingGuy", "position": "WR", "value": 2000, "bucket": "declining", "is_starter": False},
        ],
    }
    out = trade_targets._buy_path(me, [seller], {"me": {"WR": "critical"}}, thresholds,
                                  trade_counts={}, max_per_position=5, projected=set())
    assert [t["name"] for t in out["targets"]][0] == "AgingGuy", \
        "declining (production-priced) should outrank higher-value prime for a Win-Now buyer"


def test_offer_pool_never_includes_a_position_the_team_needs():
    """Trading a WR while WR is your own need just moves the shortage. Real bug this
    guards: a Win-Now team with a critical WR need was told to offer its WRs."""
    me = {"sellable": [{"name": "W", "position": "WR", "value": 900, "is_starter": False, "bucket": "prime"}],
          "tradeable_surplus": []}
    thresholds = {"WR": 100}
    assert trade_targets._my_offer_pool(me, thresholds, needs={}) != []
    assert trade_targets._my_offer_pool(me, thresholds, needs={"WR": "critical"}) == []
