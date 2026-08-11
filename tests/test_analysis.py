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
from sources import fantasycalc, sleeper
from analysis.team_values import AGE_CURVE, age_bucket, years_to_decline


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


def test_the_qb_curve_has_three_archetypes_not_two():
    """Four cutoffs by role: 32 mobility-only, 34 default and dual-threat, 38 elite pocket.

    An earlier version made running and passing mutually exclusive with rushing winning,
    which put the league's best run-and-throw quarterbacks on the most pessimistic curve
    available - and the market plainly disagreed, paying 10,415 for one of them at 29.5. A
    QB who does both has something to fall back on when the legs go; one whose game is only
    mobility does not."""
    assert age_bucket("QB", 33) == "prime"
    assert age_bucket("QB", 33, role="rushing_qb") == "declining", "mobility-only, marked down"
    assert age_bucket("QB", 33, role="dual_threat_qb") == "prime", (
        "runs AND throws - no rushing discount, because the arm outlasts the legs")
    assert age_bucket("QB", 35, role="dual_threat_qb") == "declining", (
        "but not priced like a pure pocket passer either; his value still leans on mobility")
    assert age_bucket("QB", 35, role="pocket_passer") == "prime"


def test_pass_catching_rb_declines_later_than_standard_rb():
    """Mirror case: receiving work ages better than between-the-tackles work (27 -> 29)."""
    assert age_bucket("RB", 28) == "declining"
    assert age_bucket("RB", 28, role="pass_catching_rb") == "prime"


def test_elite_pocket_passers_get_a_much_later_decline():
    """The QB curve is 31 / 34 / 38 by role. A domain expert argued the pocket end was too
    pessimistic - a genuine pocket passer holds value to nearly 40 while a rusher slows near
    30 - and the passing-EPA data agreed, so the spread widened from three years to seven."""
    assert age_bucket("QB", 35) == "declining", "an ordinary QB is past it at 35"
    assert age_bucket("QB", 35, role="pocket_passer") == "prime", "a stud pocket passer is not"
    assert age_bucket("QB", 35, role="rushing_qb") == "declining"
    assert age_bucket("QB", 38, role="pocket_passer") == "declining", "the curve still turns"


def test_runway_distinguishes_two_players_in_the_same_bucket():
    """`age_bucket` throws away the distance to the boundary, which is what a dynasty seller
    wants. The live case: a 28.0 rushing QB and a 31.8 pocket passer both read `prime` while
    being three years apart in runway - and the older man has more of it."""
    assert age_bucket("QB", 28.0, "rushing_qb") == age_bucket("QB", 31.8, "pocket_passer")
    assert years_to_decline("QB", 28.0, "rushing_qb") == 4.0
    assert years_to_decline("QB", 31.8, "pocket_passer") == 6.2
    # The third archetype sits between them: a QB who runs *and* throws well carries no
    # rushing discount, because when the legs go he is still a good passer.
    assert years_to_decline("QB", 28.0, "dual_threat_qb") == 6.0
    assert years_to_decline("QB", 38.0) < 0, "negative once past the cutoff"
    assert years_to_decline("QB", None) is None


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
    """spec: list of (position, value) -> the {player_id: info} shape sources produce.

    redraft_value defaults to the dynasty value so fixtures stay terse. Tests that care
    about the distinction (lineup ranking, efficiency swaps) set it explicitly - needs
    and surplus are measured on current production, so it has to be present."""
    return {str(i): {"name": f"P{i}", "position": pos, "value": val,
                     "redraft_value": val, "age": 26}
            for i, (pos, val) in enumerate(spec)}


def _league(specs: dict[str, list[tuple[str, int]]]) -> tuple[list[dict], dict]:
    """A whole league from {owner_id: [(position, value), ...]}. Quality is measured
    against the rest of the league, so these tests need one - a single roster can't
    express the distinction being tested."""
    players, rosters = {}, []
    for owner_id, spec in specs.items():
        ids = []
        for pos, val in spec:
            pid = str(len(players))
            players[pid] = {"name": f"{owner_id}-{pid}", "position": pos, "value": val,
                            "redraft_value": val, "age": 26}
            ids.append(pid)
        rosters.append({"owner_id": owner_id, "roster_id": len(rosters) + 1,
                        "players": ids, "starters": []})
    return rosters, players


def test_needs_name_the_shape_of_the_problem_not_just_its_severity():
    """The four levels, in one league, at one position.

    This replaced a pure count rule (fewer usable than slots = critical, exactly = thin)
    that measured close to *inverted* on a real 12-team league: the 2nd-best WR room in
    the league read `critical` because its WR3 sat below the bar, while the 10th-best read
    as no need at all because four players cleared a low bar by a little. `top-heavy` and
    `weak` are the two halves that count alone conflated - one wants bodies, one wants an
    upgrade.
    """
    slots = {"QB": 0, "RB": 0, "WR": 3, "TE": 0}
    thresholds = {"QB": 100, "RB": 100, "WR": 100, "TE": 100}
    rosters, players = _league({
        "strong":   [("WR", 1000), ("WR", 900), ("WR", 800)],   # 2,700 - best room
        "quality":  [("WR", 900), ("WR", 800), ("WR", 50)],     # 1,750 but only 2 startable
        "mid":      [("WR", 700), ("WR", 600), ("WR", 500)],    # 1,800 - unremarkable
        "mid2":     [("WR", 650), ("WR", 550), ("WR", 450)],    # 1,650 - unremarkable
        "quantity": [("WR", 200), ("WR", 180), ("WR", 160), ("WR", 150)],  # 4 bodies, 540
        "empty":    [("WR", 80), ("WR", 60)],                   # nothing startable
    })
    assessed = roster_needs.assess_positions(rosters, players, slots, thresholds)
    level = {owner: entry["WR"]["level"] for owner, entry in assessed.items()}

    assert level["empty"] == "critical", "no startable bodies and a bottom-of-league room"
    assert level["quality"] == "top-heavy", (
        "2 of 3 slots filled but the 3rd-best room in the league - wants a body, not an "
        "upgrade. The old count rule called this critical")
    assert level["quantity"] == "weak", (
        "4 bodies over the bar for 3 slots, and the worst room in the league bar one - "
        "wants an upgrade, not depth. The old count rule called this no need at all")
    assert level["strong"] == "ok" and level["mid"] == "ok" and level["mid2"] == "ok", (
        "mid-table with the slots filled is not a need - shopping for problems you don't "
        "have is what 'thin' used to cause")

    # And the priority ordering follows the shape: can't-field beats can-field-badly.
    assert (roster_needs.NEED_PRIORITY["critical"]
            < roster_needs.NEED_PRIORITY["top-heavy"]
            < roster_needs.NEED_PRIORITY["weak"])


def test_a_mid_ranked_group_can_still_be_weak_in_absolute_terms():
    """Rank alone misses skewed positions. On a real league the 8th-of-12 TE room (648)
    was 39% of the league median and 10% of the best - "average" by rank, plainly a hole
    in points. `WEAK_VS_MEDIAN` catches that without touching the ordinary cases."""
    slots = {"QB": 0, "RB": 0, "WR": 0, "TE": 1}
    thresholds = {"QB": 100, "RB": 100, "WR": 100, "TE": 100}
    rosters, players = _league({
        "a": [("TE", 6000)], "b": [("TE", 5500)], "c": [("TE", 5000)],
        "skewed": [("TE", 700)], "e": [("TE", 600)], "f": [("TE", 500)],
    })
    entry = roster_needs.assess_positions(rosters, players, slots, thresholds)["skewed"]["TE"]

    assert entry["rank"] == 4 and entry["of"] == 6, "4th of 6 is not the bottom tertile"
    assert entry["startable"] == entry["slots"], "the slot is filled, so this isn't a count problem"
    assert entry["level"] == "weak", "700 against a league median of 2,850 is a real shortfall"


def test_quality_is_not_asserted_when_the_league_is_too_small_to_measure_it():
    """A 1-team league's rank is simultaneously first and last, which the naive tertile
    test read as bottom-tertile - i.e. every position weak, from no evidence. Below
    MIN_TEAMS_FOR_QUALITY the count test stands alone."""
    slots = {"QB": 0, "RB": 0, "WR": 2, "TE": 0}
    thresholds = {"QB": 100, "RB": 100, "WR": 100, "TE": 100}
    rosters, players = _league({"solo": [("WR", 500), ("WR", 400)]})
    assert roster_needs.assess_positions(rosters, players, slots, thresholds)["solo"]["WR"]["level"] == "ok"

    rosters, players = _league({"solo": [("WR", 500)]})
    assert roster_needs.assess_positions(rosters, players, slots, thresholds)["solo"]["WR"]["level"] == "critical"


def test_surplus_is_measured_against_your_own_lineup_not_a_leaguewide_bar():
    """Spare means "my lineup doesn't miss him, and someone would want him", and neither half
    may depend on how the rest of the league is stocked.

    The old rule was zero-sum twice over. It counted players above `replacement_thresholds`
    and beyond `slots[pos]` - but replacement level is *defined* as the Nth-best leaguewide
    where N is every starting slot at that position, so supply above the bar equals demand by
    construction. Measured on two real leagues it was exact: QB 24 slots / 24 above the bar,
    RB 24/24, WR 36/36, TE 12/12. Only 3 of 12 teams had any surplus and mutual swaps returned
    nothing across 36 consecutive team-reads.

    Swapping the redraft bar for the dynasty one does NOT fix that - both are Nth-best
    leaguewide. On a real roster only 2 of 18 receivers cleared the raw dynasty bar. The bar
    has to scale with what kind of value the player carries, which is what
    `clears_relevance_floor` does and why the offer pool always used it: **replacement level
    is a win-now idea**, and a young player below it isn't replaceable to a team that will be
    good in two years - he's a starter who hasn't arrived yet."""
    thresholds = {"QB": 1000, "RB": 1000, "WR": 1000, "TE": 1000}
    players = {
        "s1": {"name": "Starter1", "position": "WR", "value": 900, "redraft_value": 900, "age": 26},
        "s2": {"name": "Starter2", "position": "WR", "value": 800, "redraft_value": 800, "age": 26},
        "prime_ok": {"name": "PrimeOk", "position": "WR", "value": 600, "redraft_value": 600, "age": 26},
        "prime_no": {"name": "PrimeNo", "position": "WR", "value": 400, "redraft_value": 400, "age": 26},
        "rising": {"name": "Rising", "position": "WR", "value": 300, "redraft_value": 20, "age": 23},
    }
    roster = {"players": list(players), "starters": []}
    starters = {"s1", "s2"}

    names = [e["name"] for e in roster_needs.find_surplus(
        roster, players, thresholds, starters)["WR"]]
    assert "Starter1" not in names and "Starter2" not in names, "in the lineup is not spare"
    assert "PrimeOk" in names, "prime clears at 50% of replacement"
    assert "PrimeNo" not in names, "400 is below that 500 bar"
    assert "Rising" in names, (
        "ascending clears at 25%, so a 300-value 23-year-old with almost no current "
        "production is still a real asset - this is the case a flat bar threw away")


def test_needs_are_measured_on_current_production_not_dynasty_value():
    """"Can I field a lineup" is a current-production question, so the bar is the Nth-best
    *producer*, not the Nth-most-*valuable* player - a pool stuffed with young prospects
    priced on upside. On a real league the dynasty-based bar was 2.5x too strict at WR
    (2,126 vs 855) and 3.2x at TE (2,013 vs 630), marking a team with three startable WRs
    and two startable TEs as critical at both."""
    pool = {
        "1": {"name": "Vet", "position": "WR", "value": 1200, "redraft_value": 2000},
        "2": {"name": "Prospect", "position": "WR", "value": 3000, "redraft_value": 100},
    }
    slots = {"QB": 0, "RB": 0, "WR": 1, "TE": 0}
    thresholds = roster_needs.replacement_thresholds(pool, slots, num_teams=1, metric="redraft_value")
    rosters = [{"owner_id": "vet", "players": ["1"], "starters": []},
               {"owner_id": "prospect", "players": ["2"], "starters": []}]
    assessed = roster_needs.assess_positions(rosters, pool, slots, thresholds)

    # The veteran produces now, so he fills the slot, despite the lower dynasty value.
    assert assessed["vet"]["WR"]["startable"] == 1
    # The prospect is the more valuable asset and still can't start.
    assert assessed["prospect"]["WR"]["startable"] == 0
    assert assessed["prospect"]["WR"]["level"] == "critical"


def test_a_position_cannot_be_both_a_need_and_a_surplus():
    """A count shortage and spare depth are mutually exclusive by construction. A `weak`
    position can legitimately have surplus, though - that's the consolidation case, where
    the spare bodies are exactly what you'd package for one better starter."""
    slots = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
    thresholds = {p: 100 for p in slots}
    rosters, players = _league({
        "a": [("QB", 500), ("RB", 500), ("RB", 400), ("WR", 500), ("WR", 450), ("TE", 500)],
        "b": [("QB", 520), ("RB", 510), ("RB", 410), ("WR", 510), ("WR", 460), ("TE", 510)],
        "c": [("QB", 480), ("RB", 490), ("RB", 390), ("WR", 490), ("WR", 440), ("TE", 490)],
        "d": [("QB", 470), ("RB", 480), ("RB", 380), ("WR", 480), ("WR", 430), ("TE", 480)],
    })
    needs = roster_needs.needs_only(
        roster_needs.assess_positions(rosters, players, slots, thresholds)["a"])
    surplus = roster_needs.find_surplus(rosters[0], players, thresholds)
    count_needs = {pos for pos, e in needs.items() if e["level"] in ("critical", "top-heavy")}
    assert not (count_needs & set(surplus))


# ------------------------------------------------------------- team window classification

def _roster(starter_specs):
    """Roster plus the set of starter ids. Nothing reads Sleeper's `starters` snapshot
    any more - `classify` takes the value-derived lineup explicitly, so these fixtures
    hand it over rather than embedding it in the roster dict."""
    players = _players(starter_specs)
    ids = list(players)
    return {"owner_id": "me", "players": ids}, players, set(ids)


def test_trajectory_measures_current_production_not_dynasty_value():
    """"Will my lineup get better or worse on its own" has to be measured in the currency
    that scores points. Weighting by dynasty value double-counts the very thing being
    measured: ascending players are *priced* on the growth in question, so a young roster's
    ascending share is inflated by the market's opinion rather than by roster facts.

    Here the young player is worth far more in dynasty terms but produces less right now,
    so production-weighted trajectory reads mildly rising where value-weighted would read
    overwhelmingly so."""
    roster, players, starters = _roster([("WR", 9000), ("WR", 1000)])
    players["0"] |= {"age": 22, "redraft_value": 1000}   # ascending prospect, big price
    players["1"] |= {"age": 30, "redraft_value": 1000}   # declining vet, same production

    result = team_state.classify(roster, players, 10_000, starters)
    assert result["ascending_pct"] == 50 and result["declining_pct"] == 50
    assert result["trajectory_score"] == 0, "equal production means a flat trajectory"
    assert result["starting_production"] == 2000


def test_window_needs_both_axes_not_either_one():
    """The whole point of the two-axis model: neither axis alone determines the answer.
    A falling roster is Push if it can compete and Rebuild if it can't; a fringe roster is
    Ascend if rising and Rebuild if not."""
    assert team_state.window_for("contender", "falling") == "Push"
    assert team_state.window_for("contender", "rising") == "Contend"
    assert team_state.window_for("contender", "steady") == "Contend"
    assert team_state.window_for("fringe", "rising") == "Ascend"
    assert team_state.window_for("fringe", "falling") == "Rebuild"
    assert team_state.window_for("also-ran", "rising") == "Rebuild", (
        "young and genuinely bad is still a rebuild - being young doesn't make you close")


def test_offer_pool_protects_starters_who_are_actually_producing():
    """A valuable non-cornerstone *starter* is the team, not spare parts. Regression guard
    for a real bug where a team was told to offer its own starting QB2. Tested by comparing
    two otherwise-identical players so the assertion can't pass vacuously - same position,
    same value, same bucket, differing only in is_starter."""
    thresholds = {"QB": 100}
    starter = {"name": "QB2", "position": "QB", "value": 900, "is_starter": True, "bucket": "prime"}
    bench = {"name": "QB3", "position": "QB", "value": 900, "is_starter": False, "bucket": "prime"}

    offered = trade_targets._my_offer_pool({"sellable": [starter, bench], "tradeable_surplus": [],
                                            "window": "Push"},
                                           thresholds, needs={})
    names = [e["name"] for e in offered]
    assert "QB3" in names, "bench depth at this value should be offerable"
    assert "QB2" not in names, "a prime starter is current production, not surplus"


def test_offer_pool_lets_a_push_team_offer_an_ascending_starter():
    """"Is he a starter" was a proxy for "does moving him cost me", and on a real roster it
    hid the owner's single biggest trade chip: an ascending TE at 3,660 dynasty against
    1,035 redraft, i.e. mostly future value sitting in a win-now lineup. A closing window
    exists to spend exactly that.

    Three players, identical except for the one attribute each is testing:
      - ascending starter -> offerable while pushing, protected while contending
      - prime starter at the same value -> protected in both (he IS the production)
      - a starter the bench replaces for free -> offerable regardless of window
    """
    thresholds = {"TE": 100}
    rising = {"name": "Rising", "position": "TE", "value": 900, "is_starter": True, "bucket": "ascending"}
    producing = {"name": "Producing", "position": "TE", "value": 900, "is_starter": True, "bucket": "prime"}
    free = {"name": "Free", "position": "TE", "value": 900, "is_starter": True, "bucket": "prime"}
    covered = {"Rising": 420.0, "Producing": 380.0, "Free": 0.0}
    roster = {"sellable": [rising, producing, free], "tradeable_surplus": []}

    pushing = trade_targets._my_offer_pool({**roster, "window": "Push"}, thresholds,
                                           needs={}, covered=covered)
    contending = trade_targets._my_offer_pool({**roster, "window": "Contend"}, thresholds,
                                              needs={}, covered=covered)

    assert sorted(e["name"] for e in pushing) == ["Free", "Rising"]
    assert [e["name"] for e in contending] == ["Free"], "no clock, no reason to sell the future"
    # The cost is reported, not used as a veto - what it's worth paying depends on the
    # return, which this module deliberately doesn't price.
    assert next(e for e in pushing if e["name"] == "Rising")["lineup_cost"] == 420


def test_depth_is_measured_by_refilling_the_lineup_not_by_counting():
    """Needs are binary, so a team starting five receivers and one starting three look
    identical at WR once both are filled - even though only one is a single absence from an
    empty slot. `would_start_if_one_out` tells them apart by actually removing the weakest
    starter at the position and refilling.

    Both rosters below hold the same candidate. The deep one has better bodies behind the
    starter, so he never sees the field and is not depth for them; the thin one has nothing,
    so he is. This is the real reason a live candidate was correctly refused: he was fifth
    at his position, not second."""
    slots, flex = {"RB": 1, "WR": 1}, [("RB", "WR")]
    players = {
        "rb1": {"name": "RB1", "position": "RB", "redraft_value": 900, "value": 900},
        "rb2": {"name": "RB2", "position": "RB", "redraft_value": 600, "value": 600},
        "wr1": {"name": "WR1", "position": "WR", "redraft_value": 800, "value": 800},
        "wr2": {"name": "WR2", "position": "WR", "redraft_value": 500, "value": 500},
        "cand": {"name": "Cand", "position": "RB", "redraft_value": 200, "value": 200},
    }
    # More bodies than slots on both, so the flex is genuinely contested - otherwise
    # everyone starts and the test passes for the wrong reason.
    deep = {"players": ["rb1", "rb2", "wr1", "wr2"]}
    thin = {"players": ["rb1", "wr1", "wr2"]}

    deep_starters = roster_needs.projected_starters(deep, players, slots, flex)
    thin_starters = roster_needs.projected_starters(thin, players, slots, flex)
    assert not roster_needs.would_start_if_one_out(
        deep, players, "cand", deep_starters, slots, flex), "RB2 covers it - not depth here"
    assert roster_needs.would_start_if_one_out(
        thin, players, "cand", thin_starters, slots, flex), "nothing behind the starter"


def test_leverage_separates_what_a_team_is_from_what_it_could_become():
    """The state the window model could not express, found by reading a stranger's league.
    A roster ranked 9th of 12 in starting production and 2nd in total tradeable value was
    labelled `also-ran` - which reads as "bad", when the true statement is "bad right now,
    holding the second-largest war chest in the league". Its owner's own summary: he doesn't
    expect to win, but if the season opens well he has the assets to convert.

    The mirror is equally real and comes from the same comparison - a team 1st in production
    and 8th in assets is winning now with nothing left to reload from. Teams whose two ranks
    agree get nothing, which is most of them."""
    assert team_state.leverage(contention_rank=9, asset_rank=2, num_teams=12) == "convertible"
    assert team_state.leverage(contention_rank=1, asset_rank=8, num_teams=12) == "mortgaged"
    assert team_state.leverage(contention_rank=2, asset_rank=3, num_teams=12) is None, \
        "top third on both axes is just a good team"
    assert team_state.leverage(contention_rank=9, asset_rank=10, num_teams=12) is None, \
        "bottom on both is just a bad team"
    assert team_state.leverage(contention_rank=4, asset_rank=1, num_teams=4) is None, \
        "in a tiny league 'top third' is one team and the comparison means nothing"


def test_stranded_production_is_capacity_not_quality():
    """The miss that a live rebuilding roster exposed: four startable QBs in superflex, two
    QB-capable slots, and the QB3 producing 4,880 sat on the bench while a receiver producing
    420 started. Every number was already computed; nothing put them side by side.

    `stranded` is a *capacity* statement - these players out-produce the weakest starter and
    cannot be fielded - so it must not sweep in ordinary bench depth that simply isn't good
    enough. Both are on the roster below."""
    slots, flex = {"QB": 1, "WR": 1}, []
    players = {
        "qb1": {"name": "QB1", "position": "QB", "redraft_value": 900, "value": 900},
        "qb2": {"name": "QB2", "position": "QB", "redraft_value": 800, "value": 800},
        "wr1": {"name": "WR1", "position": "WR", "redraft_value": 100, "value": 100},
        "wr2": {"name": "WR2", "position": "WR", "redraft_value": 50, "value": 50},
    }
    roster = {"players": ["qb1", "qb2", "wr1", "wr2"]}
    starters = roster_needs.projected_starters(roster, players, slots, flex)
    stranded = roster_needs.stranded_starters(roster, players, starters)
    assert [players[p]["name"] for p in stranded] == ["QB2"], (
        "QB2 beats the weakest starter and cannot be fielded; WR2 is merely worse")


def test_exposure_flags_an_unpriced_replacement():
    """Redraft coverage runs out around the 30th player at a position while dynasty rosters
    keep going, so a real backup can carry `redraft_value = None` - which the arithmetic
    reads as zero. A live roster was told losing its TE cost 100% of its TE production with
    a rostered NFL tight end sitting behind him. The number is unanswerable, not wrong, and
    the caller has to be told which."""
    slots, flex = {"TE": 1}, []
    priced = {"te1": {"name": "TE1", "position": "TE", "redraft_value": 900, "value": 900},
              "te2": {"name": "TE2", "position": "TE", "redraft_value": 300, "value": 300}}
    unpriced = {"te1": priced["te1"],
                "te2": {"name": "TE2", "position": "TE", "redraft_value": None, "value": 300}}
    roster = {"players": ["te1", "te2"]}
    for players, expected in ((priced, False), (unpriced, True)):
        starters = roster_needs.projected_starters(roster, players, slots, flex)
        assert roster_needs.replacement_is_unpriced(
            roster, players, "TE", starters, slots, flex) is expected


def test_depth_needs_someone_to_be_behind():
    """An empty position is a *need*, handled elsewhere and with a different fix. Returning
    True here would double-report it as cheap insurance and invite filling a hole with a
    body nobody wants."""
    slots, flex = {"RB": 1}, []
    players = {"cand": {"name": "Cand", "position": "RB", "redraft_value": 200, "value": 200}}
    assert not roster_needs.would_start_if_one_out({"players": []}, players, "cand",
                                                    set(), slots, flex)


# --------------------------------------------------------------------- offerable names

def test_offerable_names_covers_every_find_targets_mode():
    """agent.py's grounding check depends on this being complete for all three modes -
    a mode returning an empty set would silently ban every player on the roster."""
    buy = {"mode": "buy", "my_offers": [{"name": "A"}]}
    rebuild = {"mode": "rebuild", "sell_candidates": [{"name": "B"}], "situational": [{"name": "C"}]}
    ascend = {"mode": "ascend",
                "push": {"my_offers": [{"name": "D"}]},
                "pivot": {"sell_candidates": [{"name": "E"}], "situational": [{"name": "F"}]}}
    assert trade_targets.offerable_names(buy) == {"A"}
    assert trade_targets.offerable_names(rebuild) == {"B", "C"}
    assert trade_targets.offerable_names(ascend) == {"D", "E", "F"}


def test_missing_next_first_reads_differently_by_window():
    """A bare boolean got read as universally bad. A live run told a Win-Now team that not
    owning its next 1st was "concerning for a contender" and to "reclaim a first-round
    pick" - in the same answer that correctly told it to spend picks aggressively. Having
    spent that pick is the window working as intended; it only hurts a rebuilder, who
    loses the one payoff for a bad season."""
    contender = team_state.next_first_note(False, "Push")
    rebuilder = team_state.next_first_note(False, "Rebuild")

    assert "not a concern" in contender.lower()
    assert "not a reason to trade back" in contender.lower()
    assert "a real constraint" in rebuilder.lower()
    assert contender != rebuilder, "the same fact must not read the same way in both windows"
    assert "own" in team_state.next_first_note(True, "Rebuild").lower()
    # It lowers the RETURN on pivoting rather than removing the option - a contender with
    # real current production can still sell, it just gets back less than it gives up.
    assert "return on pivoting" in contender.lower()


def test_window_ships_with_an_explanation_not_a_bare_number():
    """Regression guard for a real confabulation: the field was once emitted as a bare
    {"diff": -11}, and the model reliably invented meanings for it - "below their
    expected win total", "underperforming by 25 points" - none of which exist, least of
    all in a preseason with no games played. An unlabeled number in a tool result is an
    invitation to make one up, so the label must ship with the value."""
    roster, players, starters = _roster([("RB", 1000), ("RB", 1000)])
    for info in players.values():
        info["age"] = 30
    result = team_state.classify(roster, players, 10_000, starters)
    assert "diff" not in result, "bare unlabelled 'diff' must not come back"

    note = team_state.window_note("Push", contention_rank=4, num_teams=12,
                                  pct_of_best=80, asc_pct=3, dec_pct=23).lower()
    assert "4 of 12" in note and "80%" in note, "the measurements that produced it"
    assert "no wins or points scored" in note, "the note must rule out the wrong reading"


def test_projected_starters_ignores_the_live_snapshot():
    """Sleeper's `starters` field is the current week's lineup, which is meaningless
    before Week 1. In a real superflex league (2 QB slots) it listed only one QB, so the
    team's obvious QB2 was classed as bench and offered away as spare parts."""
    slots = {"QB": 2, "RB": 2, "WR": 3, "TE": 1}
    players = _players([("QB", 3528), ("QB", 3288), ("QB", 2735), ("QB", 1325)])
    for i, redraft in enumerate([4722, 2744, 2704, 1299]):
        players[str(i)]["redraft_value"] = redraft
    # Snapshot claims only the first QB starts - the exact preseason bug.
    roster = {"players": list(players), "starters": ["0"]}

    projected = roster_needs.projected_starters(roster, players, slots)
    assert {"0", "1"} <= projected, "top 2 QBs are the lineup"
    assert not ({"2", "3"} & projected), "QB3/QB4 are genuinely spare"


def test_projected_starters_rank_by_current_production_not_dynasty_value():
    """A lineup is "who scores most this week", which is redraft value; dynasty value
    governs who you keep, not who you start. Modelled on the real RB room that exposed
    it - Bijan (10,255 dyn / 10,004 redraft), a rookie (7,008 / 6,290), and McCaffrey
    (4,367 / 6,518). By dynasty McCaffrey is RB3 and was offered away; by current
    production he is the second-best back on the roster and belongs in the lineup."""
    slots = {"QB": 1, "RB": 2, "WR": 3, "TE": 1}
    players = _players([("RB", 10255), ("RB", 7008), ("RB", 4367)])
    for i, redraft in enumerate([10004, 6290, 6518]):
        players[str(i)]["redraft_value"] = redraft
    roster = {"players": list(players), "starters": []}

    projected = roster_needs.projected_starters(roster, players, slots)
    assert projected == {"0", "2"}, "redraft ranking starts McCaffrey over the rookie"


def test_flex_slots_let_three_good_backs_all_start():
    """Real league shape: QB 1 / RB 2 / WR 3 / TE 1 / FLEX 2 / SUPER_FLEX 1. A team with
    three excellent RBs starts all three - two at RB, one at FLEX - and modelling only
    dedicated slots claimed 8 starters where there are 10, treating the third back as
    spare parts. It also folded SUPER_FLEX into a second dedicated QB, asserting a QB
    must fill a slot any position can."""
    positions = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "FLEX", "SUPER_FLEX"] + ["BN"] * 14
    dedicated, flex = roster_needs.lineup_slots(positions)
    assert dedicated == {"QB": 1, "RB": 2, "WR": 3, "TE": 1}
    assert len(flex) == 3, "two FLEX plus one SUPER_FLEX"

    players = _players([("RB", 10255), ("RB", 7008), ("RB", 4367), ("QB", 3528), ("QB", 3288)])
    for i, redraft in enumerate([10004, 6290, 6518, 4707, 2683]):
        players[str(i)]["redraft_value"] = redraft
    roster = {"players": list(players), "starters": []}

    starters = roster_needs.projected_starters(roster, players, dedicated, flex)
    assert {"0", "1", "2"} <= starters, "all three backs start - two at RB, one at FLEX"
    assert "4" in starters, "the second QB fills SUPER_FLEX"


def test_flex_fills_most_restrictive_slot_first():
    """A SUPER_FLEX takes any position, so filling it before a narrower FLEX can strand
    the narrower slot. Here the only RB/WR/TE-eligible player must go to FLEX, leaving
    the QB for SUPER_FLEX - filling greedily in the other order would waste both."""
    dedicated = {"QB": 0, "RB": 0, "WR": 0, "TE": 0}
    flex = [("QB", "RB", "WR", "TE"), ("RB", "WR", "TE")]
    players = _players([("QB", 9000), ("WR", 100)])
    players["0"]["redraft_value"] = 9000
    players["1"]["redraft_value"] = 100
    roster = {"players": list(players), "starters": []}

    assert roster_needs.projected_starters(roster, players, dedicated, flex) == {"0", "1"}


def test_projected_starters_sorts_missing_redraft_prices_last():
    """Redraft covers the top ~200 players, so deep dynasty-only assets have no price.
    Safe to treat as non-starters: across a real 12-team league the highest-dynasty
    rostered player missing one was 1,350, far below every replacement level."""
    slots = {"QB": 1, "RB": 1, "WR": 1, "TE": 1}
    players = _players([("WR", 1350), ("WR", 900)])
    players["0"]["redraft_value"] = None   # dynasty-only prospect
    players["1"]["redraft_value"] = 2500   # real current producer
    roster = {"players": list(players), "starters": []}

    assert roster_needs.projected_starters(roster, players, slots) == {"1"}


def test_win_now_buyer_sees_production_priced_targets_first():
    """A Win-Now team buys current production. This project's own pricing model calls
    declining players 'production-priced' and prime ones 'upside-priced, may cost more
    than the fit justifies' - so ordering purely by (trade activity, value) contradicted
    it. A real Win-Now team was handed six buy targets, every one prime."""
    # _buy_path also builds the offer pool, so `me` needs those lists even though this
    # test only asserts on target ordering.
    me = {"owner_id": "me", "window": "Push", "sellable": [], "tradeable_surplus": []}
    thresholds = {"WR": 100}
    seller = {
        "owner_id": "them", "owner": "them", "window": "Rebuild",
        "sellable": [
            {"name": "PrimeGuy", "position": "WR", "value": 4000, "bucket": "prime", "is_starter": False},
            {"name": "AgingGuy", "position": "WR", "value": 2000, "bucket": "declining", "is_starter": False},
        ],
    }
    need = {"level": "critical", "weakest_starter": 0, "note": "", "rank": 12, "of": 12}
    out = trade_targets._buy_path(me, [seller], {"me": {"WR": need}}, thresholds,
                                  trade_counts={}, max_per_position=5)
    assert [t["name"] for t in out["targets"]][0] == "AgingGuy", \
        "declining (production-priced) should outrank higher-value prime for a Win-Now buyer"


def test_offers_are_tiered_by_value_over_replacement_not_raw_value():
    """Trade value isn't linear in raw value. Value above replacement is scarce; value
    below it is replaceable off waivers, so the raw number overstates what it fetches.
    Depth is real (injuries, byes, handcuffs) but discounted - the tier labels say
    exactly that rather than calling it worthless. Real case: Ollie Gordon, 947 raw but
    1,637 *below* replacement, listed alongside McCaffrey as if comparable."""
    # Mirrors how the real pair actually reaches the pool: McCaffrey is a declining
    # sellable, while Ollie Gordon only clears the relevance floor at all because he's
    # ascending (25% of replacement, vs 50% for production/mixed) and so arrives via
    # tradeable_surplus. A declining player at 947 would be filtered out entirely.
    thresholds = {"RB": 2584}
    me = {"sellable": [
              {"name": "Real", "position": "RB", "value": 4367, "bucket": "declining", "is_starter": False},
          ],
          "tradeable_surplus": [
              {"name": "Filler", "position": "RB", "value": 947, "bucket": "ascending", "is_starter": False},
          ]}
    offers = trade_targets._my_offer_pool(me, thresholds, needs={})
    by_name = {e["name"]: e for e in offers}
    assert by_name["Real"]["tier"].startswith("core piece")
    assert by_name["Filler"]["tier"].startswith("depth")
    assert by_name["Real"]["value_over_replacement"] > 0 > by_name["Filler"]["value_over_replacement"]
    assert offers[0]["name"] == "Real", "core pieces must lead the list"


def test_efficiency_swap_finds_cheaper_equivalent_production():
    """Win-now arbitrage: a bench player producing nearly as much *this season* for
    meaningfully less dynasty value. Modelled on the real case - a superflex QB2
    (3,288 dynasty / 2,744 redraft) against QB3 (2,735 / 2,704): 99% of the production
    for 553 less in trade value."""
    entries = [
        {"name": "QB2", "position": "QB", "value": 3288, "redraft_value": 2744, "is_starter": True},
        {"name": "QB3", "position": "QB", "value": 2735, "redraft_value": 2704, "is_starter": False},
    ]
    swaps = trade_targets.find_efficiency_swaps(entries)
    assert len(swaps) == 1
    assert swaps[0]["sell"] == "QB2" and swaps[0]["start_instead"] == "QB3"
    assert swaps[0]["production_retained_pct"] == 99
    assert swaps[0]["dynasty_value_freed"] == 553


def test_efficiency_swap_target_reaches_the_offer_pool():
    """These contradicted each other: the swap named a player to sell while the offer
    list excluded him for being a starter, so the most efficient chip on the roster
    never appeared among the things to offer. A swap target is offerable by definition -
    that's the finding - so it must join the pool, carrying its reasoning."""
    thresholds = {"QB": 2131}
    me = {
        "owner_id": "me", "window": "Push",
        "sellable": [
            {"name": "QB2", "position": "QB", "value": 2728, "redraft_value": 2744,
             "bucket": "prime", "is_starter": True},
            {"name": "QB3", "position": "QB", "value": 2189, "redraft_value": 2704,
             "bucket": "prime", "is_starter": False},
        ],
        "tradeable_surplus": [],
    }
    out = trade_targets._buy_path(me, [], {"me": {}}, thresholds, trade_counts={},
                                  max_per_position=3)
    names = [e["name"] for e in out["my_offers"]]
    assert "QB2" in names, "the swap's sell target must be offerable"
    entry = next(e for e in out["my_offers"] if e["name"] == "QB2")
    assert entry.get("swap_note"), "and must carry why it's safe to move"


def test_efficiency_swap_ignores_a_real_production_downgrade():
    """The point is arbitrage, not selling the team. A replacement that loses real
    current production shouldn't be suggested however much value it frees."""
    entries = [
        {"name": "Stud", "position": "RB", "value": 5000, "redraft_value": 5000, "is_starter": True},
        {"name": "Scrub", "position": "RB", "value": 800, "redraft_value": 1500, "is_starter": False},
    ]
    assert trade_targets.find_efficiency_swaps(entries) == []


def test_efficiency_swap_skips_players_with_no_redraft_price():
    """Deep dynasty-only assets (rookies, prospects) have no redraft market - FantasyCalc
    carries ~200 redraft players against ~400 dynasty. Missing must mean skipped, not
    treated as zero production."""
    entries = [
        {"name": "Starter", "position": "WR", "value": 4000, "redraft_value": 3000, "is_starter": True},
        {"name": "Prospect", "position": "WR", "value": 900, "redraft_value": None, "is_starter": False},
    ]
    assert trade_targets.find_efficiency_swaps(entries) == []


def test_pick_slot_follows_the_original_team_not_the_holder(monkeypatch):
    """A "2027 1st" isn't one thing - a rebuilder's is early, a contender's is late, and
    the market prices that at nearly 2x (Early 4,487 / Mid 2,955 / Late 2,263). What
    decides the slot is how good the team the pick *originally* belongs to turns out to
    be, so a contender holding a rebuilder's first has an early pick. Real case this
    mirrors: a Middling team held a 2027 3rd originating from a Win-Now team, and it
    correctly priced as (Late) rather than the holder's own (Mid)."""
    from analysis import team_values
    monkeypatch.setattr(team_values.sleeper, "get_traded_picks",
                        lambda _lid: [{"season": "2027", "round": 1, "roster_id": 2, "owner_id": 1}])

    pick_values = {"2027 1st": 2853, "2027 1st (Early)": 4487,
                   "2027 1st (Mid)": 2955, "2027 1st (Late)": 2263}
    owned = team_values.owned_picks(
        "L", season=2026, draft_rounds=1, roster_ids=[1, 2], pick_values=pick_values,
        strategy_by_roster={1: "Push", 2: "Rebuild"},
    )
    # Roster 1 (a contender) acquired roster 2's (a rebuilder's) first.
    acquired = next(p for p in owned[1] if p["originally"] == 2)
    assert acquired["value"] == 4487, "priced early - it's the rebuilder's pick"
    assert "Early" in acquired["pick"]

    own_pick = next(p for p in owned[1] if p["originally"] == 1)
    assert own_pick["value"] == 2263, "the contender's own first is a late pick"


def test_pick_falls_back_to_flat_value_when_no_tier_is_published(monkeypatch):
    """Only the next class has Early/Mid/Late prices - the honest limit, since a window
    predicts next season's finish poorly two years out. Later picks must keep the flat
    round value and say so, not silently inherit a tier."""
    from analysis import team_values
    monkeypatch.setattr(team_values.sleeper, "get_traded_picks", lambda _lid: [])
    owned = team_values.owned_picks(
        "L", season=2026, draft_rounds=1, roster_ids=[1],
        pick_values={"2028 1st": 2028},  # no tiered variants published
        strategy_by_roster={1: "Rebuilding"},
    )
    pick = next(p for p in owned[1] if p["season"] == 2028)
    assert pick["value"] == 2028
    assert "unknowable" in pick["slot_basis"]


def test_rebuilder_is_pointed_at_picks_held_by_contenders():
    """Direction matters: a future pick is worth more to a rebuilder than to the
    contender holding it, so those are the ones to ask about. A pick held by another
    rebuilding team isn't going anywhere and shouldn't be suggested."""
    me = {"owner_id": "me", "roster_id": 1, "sellable": [], "tradeable_surplus": []}
    states = [
        me | {"owner": "me", "window": "Rebuild"},
        {"owner_id": "win", "roster_id": 2, "owner": "Contender",
         "window": "Push", "tradeable_surplus": []},
        {"owner_id": "reb", "roster_id": 3, "owner": "OtherRebuild",
         "window": "Rebuild", "tradeable_surplus": []},
    ]
    picks = {
        2: [{"pick": "2027 1st", "value": 2853, "round": 1, "season": 2027, "originally": 2}],
        3: [{"pick": "2027 1st", "value": 2853, "round": 1, "season": 2027, "originally": 3}],
    }
    out = trade_targets._pivot_path(states[0], states, thresholds={}, trade_counts={"win": 5},
                                    picks_by_owner=picks)
    owners = [p["from_owner"] for p in out["picks_to_acquire"]]
    assert owners == ["Contender"], "only contenders' picks are realistic targets"


def test_rebuilder_pick_targets_are_empty_without_pick_data():
    """No pick data must mean no suggestions, not an empty-looking key that reads as
    'this team has no picks available'."""
    me = {"owner_id": "me", "roster_id": 1, "owner": "me",
          "window": "Rebuild", "sellable": [], "tradeable_surplus": []}
    out = trade_targets._pivot_path(me, [me], thresholds={}, trade_counts={}, picks_by_owner=None)
    assert "picks_to_acquire" not in out


def test_offer_pool_never_includes_a_position_the_team_needs():
    """Trading a WR while WR is your own need just moves the shortage. Real bug this
    guards: a Win-Now team with a critical WR need was told to offer its WRs."""
    me = {"sellable": [{"name": "W", "position": "WR", "value": 900, "is_starter": False, "bucket": "prime"}],
          "tradeable_surplus": []}
    thresholds = {"WR": 100}
    assert trade_targets._my_offer_pool(me, thresholds, needs={}) != []
    assert trade_targets._my_offer_pool(me, thresholds, needs={"WR": "critical"}) == []


# ------------------------------------------------------------------- mutual swaps

def _swap_league(monkeypatch, needs, surplus):
    """A two-team Win-Now league with the given needs/surplus, so find_mutual_swaps can be
    exercised without network. Both teams are swap-eligible; the strategy gate and the
    owner lookup are covered elsewhere."""
    states = [{"owner_id": "me", "owner": "Me", "roster_id": 1, "window": "Push"},
              {"owner_id": "you", "owner": "You", "roster_id": 2, "window": "Push"}]
    monkeypatch.setattr(team_state, "classify_league", lambda _: states)
    monkeypatch.setattr(roster_needs, "league_needs", lambda _: needs)
    monkeypatch.setattr(roster_needs, "league_surplus", lambda _: surplus)
    monkeypatch.setattr(trade_targets, "context",
                        lambda _: type("Ctx", (), {"pick_owner": lambda s, q, rows: rows[0]})())
    return states


def _need(level, weakest=0):
    return {"level": level, "weakest_starter": weakest, "note": "", "rank": 6, "of": 12}


def _spare(name, pos, value, redraft):
    return {"name": name, "position": pos, "value": value, "redraft_value": redraft,
            "is_starter": False}


def test_mutual_swap_matches_each_side_spare_depth_to_the_other_side_need(monkeypatch):
    """The shape this exists for: I'm short at TE with spare QB depth, you're the mirror.
    Neither of us touches a starter and both lineups improve."""
    _swap_league(monkeypatch,
                 needs={"me": {"TE": _need("critical")}, "you": {"QB": _need("critical")}},
                 surplus={"me": {"QB": [_spare("MyQB3", "QB", 2000, 1900)]},
                          "you": {"TE": [_spare("TheirTE2", "TE", 1900, 1800)]}})
    swaps = trade_targets.find_mutual_swaps("L", "Me")["swaps"]

    assert len(swaps) == 1
    assert [e["name"] for e in swaps[0]["you_receive"]] == ["TheirTE2"]
    assert [e["name"] for e in swaps[0]["you_send"]] == ["MyQB3"]
    assert swaps[0]["balance"]["you_receive_value"] == 1900


def test_mutual_swap_rejects_a_lopsided_package(monkeypatch):
    """Both sides being spare depth doesn't make the trade proposable. Before this, the
    cartesian match happily offered a genuine RB3 for a fringe backup QB - nobody accepts
    that, so surfacing it is noise."""
    _swap_league(monkeypatch,
                 needs={"me": {"TE": _need("critical")}, "you": {"QB": _need("critical")}},
                 surplus={"me": {"QB": [_spare("MyStud", "QB", 5000, 4800)]},
                          "you": {"TE": [_spare("TheirScrub", "TE", 700, 650)]}})
    assert trade_targets.find_mutual_swaps("L", "Me")["swaps"] == []


def test_mutual_swap_will_not_fix_a_weak_position_with_a_worse_player(monkeypatch):
    """A `weak` position has its slots covered and wants an upgrade, so incoming depth has
    to actually beat the current worst starter. Live case: rjl22 (weak at TE, worst
    starter 660 redraft) was offered Isaiah Likely at 634 - strictly a downgrade."""
    weak_te = _need("weak", weakest=660)
    _swap_league(monkeypatch,
                 needs={"me": {"TE": weak_te}, "you": {"QB": _need("critical")}},
                 surplus={"me": {"QB": [_spare("MyQB3", "QB", 2000, 1900)]},
                          "you": {"TE": [_spare("Likely", "TE", 2076, 634)]}})
    assert trade_targets.find_mutual_swaps("L", "Me")["swaps"] == []

    # ...but the same swap is on if the incoming TE actually is an upgrade.
    _swap_league(monkeypatch,
                 needs={"me": {"TE": weak_te}, "you": {"QB": _need("critical")}},
                 surplus={"me": {"QB": [_spare("MyQB3", "QB", 2000, 1900)]},
                          "you": {"TE": [_spare("RealUpgrade", "TE", 2076, 1800)]}})
    assert len(trade_targets.find_mutual_swaps("L", "Me")["swaps"]) == 1


# ------------------------------------------------------------------- league format

@pytest.mark.parametrize("bonus,te_slots,expected", [
    (0,    1, "none"),    # no TE premium
    (0.25, 1, "none"),    # FantasyCalc's Off band is "no/minimal (<=0.25)"
    (0.5,  1, "tep"),     # both real leagues in this project
    (1.0,  1, "tep"),     # top of the "+0.5 to 1.0" band
    (1.5,  1, "teppp"),   # ">1.0 TEP"
    (0,    2, "teppp"),   # "Start 2 TE" reaches the top band on slots alone
])
def test_tep_tier_follows_fantasycalc_bands(bonus, te_slots, expected):
    """The bands are FantasyCalc's, taken from their own control labels, because the
    multipliers we apply are theirs. Boundaries are the point: 0.25 is Off and 1.0 is
    still TEP+, so an off-by-one here silently mis-scales every TE in the league."""
    assert sleeper.tep_tier(bonus, te_slots) == expected


def test_superflex_and_2qb_both_price_as_two_qbs():
    """FantasyCalc serves one market for numQbs>=2. Superflex is one dedicated QB slot
    plus a flex, so counting dedicated slots alone would fetch 1QB values - and the QB
    market between the two differs by a flat 1.88x, the largest mispricing available
    here."""
    sf = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "FLEX", "SUPER_FLEX"]
    assert sleeper.starting_qbs(sf) == 2
    assert sleeper.starting_qbs(["QB", "QB", "RB", "WR"]) == 2, "true 2QB"
    assert sleeper.starting_qbs(["QB", "RB", "WR", "FLEX"]) == 1, "1QB - FLEX takes no QB"
    assert sleeper.starting_qbs(["QB", "QB", "SUPER_FLEX"]) == 2, "clamped: one market above 2"


def test_te_premium_scales_only_tight_ends(monkeypatch):
    """TEP is applied locally because FantasyCalc applies it in the browser - their API
    404s on every tep value but 'none'. It must touch TEs and nothing else."""
    raw = [{"player": {"sleeperId": "1", "position": "TE", "name": "TE1", "maybeAge": 25}, "value": 1000},
           {"player": {"sleeperId": "2", "position": "WR", "name": "WR1", "maybeAge": 25}, "value": 1000}]

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return raw

    monkeypatch.setattr(fantasycalc.requests, "get", lambda *a, **k: FakeResp())
    fantasycalc.get_players.cache_clear()
    out = fantasycalc.get_players(2, 12, 1.0, True, "tep")
    assert out["1"]["value"] == round(1000 * fantasycalc.TEP_MULTIPLIER["tep"])
    assert out["2"]["value"] == 1000, "non-TE values must not move"
    fantasycalc.get_players.cache_clear()


# ------------------------------------------------------------- persuasion targets

def _holder(owner, window, trajectory, players, asc=20, dec=30):
    return {"owner_id": owner, "owner": owner, "roster_id": 1, "window": window,
            "trajectory": trajectory, "ascending_pct": asc, "declining_pct": dec,
            "sellable": players, "tradeable_surplus": []}


def _aging(name, value, redraft, pos="RB"):
    return {"name": name, "position": pos, "value": value, "redraft_value": redraft,
            "bucket": "declining", "is_starter": True}


def _prior(finish, champion=False, made_playoffs=True, continuity=1.0):
    return {"season": "2025", "finish": finish, "wins": 10, "losses": 4,
            "points_for": 2000.0, "champion": champion, "made_playoffs": made_playoffs,
            "continuity": continuity, "describes_this_team": continuity >= 0.6,
            "note": f"2025: finished {finish}."}


# The real p90 of redraft/dynasty per position, measured on both live leagues. The
# spread is the point: an absolute bar that looks strict for RB is unreachable for TE.
BARS = {"QB": 1.31, "RB": 1.05, "WR": 0.89, "TE": 0.81}

ME = {"owner_id": "me", "owner": "Me", "window": "Push"}
NEED_RB = {"RB": {"level": "critical", "weakest_starter": 0, "note": "", "rank": 10, "of": 12}}


def test_persuasion_excludes_an_aging_contender_whose_own_window_is_now():
    """A contender that is itself aging into its window should keep its aging producer -
    that player is aligned with the seasons the roster is built for. Real case: a contender
    at 21% ascending against 23% declining, holding McCaffrey at the second-best
    production-per-cost ratio in the league, who would not move him at any sane price.

    This is the case the reigning-champion veto used to catch. The veto is gone: the tilt
    rejects the same team on its merits, and a title says less than roster shape does."""
    holder = _holder("aging", "Contend", "steady", [_aging("Stud", 4000, 6000)],
                     asc=21, dec=23)
    out = trade_targets._persuasion_targets(
        ME, [holder], NEED_RB, {"RB": 100}, {}, {"aging": _prior(1, champion=True)}, BARS)
    assert out == []


def test_persuasion_surfaces_a_cliff_player_when_the_owners_window_outlasts_him():
    """The mirror, and the case the whole tier was rebuilt for. Same window, same asset,
    but this owner's production is tilting *ascending* - it contends now and later, so the
    aging starter is surplus to a future arriving without him.

    A team-level read cannot reach this: the trajectory is `steady` because a young core
    dilutes one old starter, and the old gate rejected the roster before any player on it
    was examined. Real case - the league's best team starting a 32.6-year-old RB at 1.54x."""
    holder = _holder("young", "Contend", "steady", [_aging("Stud", 4000, 6000)],
                     asc=26, dec=16)
    out = trade_targets._persuasion_targets(
        ME, [holder], NEED_RB, {"RB": 100}, {}, {"young": _prior(3)}, BARS)
    assert [t["name"] for t in out] == ["Stud"]
    why = out[0]["why_they_might_listen"]
    assert "don't line up" in why and "26%" in why, "must name the mismatch, not the age"
    assert "not currently a seller" in out[0]["cost_note"], "the ask must carry its price"


def test_persuasion_cliff_ignores_a_declining_player_on_the_bench():
    """A declining player his owner has already benched is a bad asset, not a conversation -
    there is nothing to talk him out of. Same player, same tilt, only the lineup role
    differs."""
    benched = _aging("Benched", 4000, 6000) | {"is_starter": False}
    holder = _holder("young", "Contend", "steady", [benched], asc=26, dec=16)
    out = trade_targets._persuasion_targets(
        ME, [holder], NEED_RB, {"RB": 100}, {}, {"young": _prior(3)}, BARS)
    assert out == []


def test_persuasion_bar_is_relative_to_the_players_own_position():
    """The bug this replaced, stated as a test. Dynasty and redraft are unnormalized scales
    whose relationship differs sharply by position: measured p90s are RB 1.05 but TE 0.81,
    and the *entire* TE pool tops out at 1.01. The old absolute 1.0 bar therefore excluded
    every tight end in every league while looking like an ordinary strictness setting.

    Real pair, identical ratio, opposite verdicts: a 36.9-year-old TE at 0.83 is top-decile
    now-weighted for a TE, while an RB at the same 0.83 is unremarkable."""
    te = _aging("Kelce", 1810, 1504, pos="TE")   # 0.83 - clears the 0.81 TE bar
    rb = _aging("Jacobs", 2770, 2300)            # 0.83 - misses the 1.05 RB bar
    needs = {"RB": NEED_RB["RB"], "TE": NEED_RB["RB"]}
    holder = _holder("kk", "Push", "falling", [te, rb])
    out = trade_targets._persuasion_targets(
        ME, [holder], needs, {"RB": 100, "TE": 100}, {}, {"kk": _prior(9, made_playoffs=False)}, BARS)
    assert [t["name"] for t in out] == ["Kelce"]


def test_persuasion_includes_a_falling_contender_that_has_not_won():
    """The mirror: same window, same kind of asset, but the roster is aging out and the
    core has not delivered. That team has a real reason to listen."""
    falling = _holder("kk", "Push", "falling", [_aging("Aging", 4000, 6000)])
    out = trade_targets._persuasion_targets(
        ME, [falling], NEED_RB, {"RB": 100}, {}, {"kk": _prior(9, made_playoffs=False)}, BARS)
    assert [t["name"] for t in out] == ["Aging"]
    why = out[0]["why_they_might_listen"]
    assert "falling" in why and "hasn't delivered" in why
    assert "not currently a seller" in out[0]["cost_note"], "the ask must carry its price"


def test_a_pushing_team_ranks_buy_targets_on_production_not_on_age():
    """"Declining" used to be the hard first sort key for a pushing team, on the reasoning
    that declining players are production-priced while prime ones carry an upside premium.
    That reasoning is about price *per unit of production*, and as an absolute ordering it
    meant any declining player outranked every prime one however little he produced - a real
    Push team was shown a 70-redraft receiver above a 3,439-redraft one, and the default cap
    of three then hid the better player entirely.

    Age still breaks ties, because at equal production the shorter asset is cheaper."""
    order = lambda entries: sorted(entries, key=lambda t: (
        -(t.get("redraft_value") or 0), 0 if t["bucket"] == "declining" else 1))
    fringe_old = {"name": "FringeOld", "bucket": "declining", "redraft_value": 70}
    good_prime = {"name": "GoodPrime", "bucket": "prime", "redraft_value": 3439}
    same_prime = {"name": "SamePrime", "bucket": "prime", "redraft_value": 70}
    assert [e["name"] for e in order([fringe_old, good_prime])] == ["GoodPrime", "FringeOld"]
    assert [e["name"] for e in order([same_prime, fringe_old])] == ["FringeOld", "SamePrime"],         "at equal production the declining player is the cheaper buy"


def test_persuasion_says_what_the_other_owner_would_actually_want():
    """Ranking on production-per-cost alone put a 1.54x back at the top, held by the one team
    in the league with **no needs at all**, above a 1.37x back whose owner had a critical need
    for the exact quarterback the asking team could not play. Every fact was computed; nothing
    joined them.

    Two ways an owner is interested, and the second is the one that was missing: a team with
    no positional hole that is contending now *and* tilting ascending doesn't need a position,
    it needs value that scores this season and is still there later. Saying "he needs nothing,
    so there's no deal" misses the trade that is the whole reason his aging starter is on the
    list."""
    offers = [
        {"name": "SpareQB", "position": "QB", "redraft_value": 3349, "bucket": "prime",
         "years_to_decline": 7.1, "value_over_replacement": 700},
        # Prime by bucket, but 0.3 years from his own cutoff - the real case that broke this.
        {"name": "AboutToTurn", "position": "WR", "redraft_value": 781, "bucket": "prime",
         "years_to_decline": 0.3, "value_over_replacement": 400},
        # Long runway, but depth rather than a core piece - not worth restructuring around.
        {"name": "Filler", "position": "TE", "redraft_value": 33, "bucket": "ascending",
         "years_to_decline": 6.0, "value_over_replacement": -900},
        {"name": "OldTE", "position": "TE", "redraft_value": 600, "bucket": "declining",
         "years_to_decline": -1.0, "value_over_replacement": 100},
    ]

    short_at_qb = _holder("needy", "Push", "falling", [], asc=10, dec=30)
    fit = trade_targets._counterparty_fit(
        short_at_qb, {"QB": {"level": "critical"}}, offers)
    assert fit["you_could_offer"] == ["SpareQB"] and "critical need at QB" in fit["why_it_fits"]

    # No hole, but rising while starting aging players - wants now-and-later value.
    rising = _holder("rising", "Contend", "steady", [], asc=26, dec=16)
    fit = trade_targets._counterparty_fit(rising, {}, offers)
    assert fit["you_could_offer"] == ["SpareQB"], (
        "'still there later' means runway, not bucket - a player 0.3 years from his own "
        "decline cutoff still reads `prime`, and offering him as a lasting asset was the bug")
    assert "no positional hole" in fit["why_it_fits"]

    # Aging into its own window with nothing to fill: no obvious fit, and say so.
    assert trade_targets._counterparty_fit(
        _holder("aging", "Contend", "steady", [], asc=21, dec=23), {}, offers) is None


def test_persuasion_ranks_by_production_per_cost_not_by_value():
    """The trap this exists to avoid. Ranking by dynasty value puts the *more valuable*
    player first, which is backwards for a win-now buyer: the cheaper name delivers more
    current production per unit paid, because the market discounts him for seasons a
    pushing team isn't buying. Modelled on the real pair - Barkley 3,746/5,081 (1.36x)
    against Taylor 5,240/6,649 (1.27x)."""
    holder = _holder("kk", "Push", "falling",
                     [_aging("Taylor", 5240, 6649), _aging("Barkley", 3746, 5081)])
    out = trade_targets._persuasion_targets(
        ME, [holder], NEED_RB, {"RB": 100}, {}, {"kk": _prior(9, made_playoffs=False)}, BARS)
    assert [t["name"] for t in out] == ["Barkley", "Taylor"], "cheaper but better ratio leads"
    assert out[0]["production_per_cost"] > out[1]["production_per_cost"]


def test_persuasion_skips_players_who_are_not_age_discounted():
    """Asking a non-seller only makes sense for production the market prices *down* for
    seasons you aren't buying. Below 1.0x you'd pay a future premium to a team that
    doesn't want to sell - the worst of both."""
    holder = _holder("kk", "Push", "falling",
                     [_aging("Premium", 4000, 2800), _aging("Discounted", 3000, 4000)])
    out = trade_targets._persuasion_targets(
        ME, [holder], NEED_RB, {"RB": 100}, {}, {"kk": _prior(9, made_playoffs=False)}, BARS)
    assert [t["name"] for t in out] == ["Discounted"]


def test_persuasion_ignores_last_season_when_the_roster_turned_over():
    """Last season's result is only allowed to speak about the roster that produced it.
    "This core hasn't won" is a real second reason to listen when the team still has that
    core, and meaningless once it has been torn down - so continuity gates the reason
    without changing whether the player surfaces at all."""
    holder = _holder("kk", "Push", "falling", [_aging("Stud", 3000, 4500)])
    intact = trade_targets._persuasion_targets(
        ME, [holder], NEED_RB, {"RB": 100}, {}, {"kk": _prior(9, made_playoffs=False, continuity=1.0)}, BARS)
    turned_over = trade_targets._persuasion_targets(
        ME, [holder], NEED_RB, {"RB": 100}, {}, {"kk": _prior(9, made_playoffs=False, continuity=0.2)}, BARS)
    assert [t["name"] for t in intact] == ["Stud"] == [t["name"] for t in turned_over]
    assert "hasn't delivered" in intact[0]["why_they_might_listen"]
    assert "hasn't delivered" not in turned_over[0]["why_they_might_listen"], \
        "a result cannot describe a roster that no longer exists"


def test_a_contender_still_rising_is_shown_its_own_conversion_candidates():
    """The same rule read from the other side. If the rest of the league is told your aging
    starter is the one piece worth calling you about, you should be told so too - in the
    same terms, from the same function, so the two can never disagree.

    Only for a contender whose production is still tilting ascending: it contends either
    way, so this is a choice about *how*, which is why `window` stays untouched. A team
    aging into its own window has no such choice and gets nothing here."""
    aging_starter = _aging("Old Star", 4000, 6000)
    rising = _holder("rising", "Contend", "steady", [aging_starter], asc=26, dec=16)
    aging = _holder("aging", "Contend", "steady", [aging_starter], asc=21, dec=23)
    assert [c["name"] for c in trade_targets._conversion_candidates(rising, BARS)] == ["Old Star"]
    assert trade_targets._conversion_candidates(aging, BARS) == []


def test_persuasion_never_searches_teams_that_are_already_sellers():
    """Rebuild teams are what the normal buy path covers. Including them here would
    double-list the same player under a framing that says it's a hard ask."""
    seller = _holder("reb", "Rebuild", "falling", [_aging("Cheap", 3000, 4500)])
    out = trade_targets._persuasion_targets(
        ME, [seller], NEED_RB, {"RB": 100}, {}, {"reb": _prior(12, made_playoffs=False)}, BARS)
    assert out == []


# --------------------------------------------------------------- injury exposure

def test_injury_exposure_is_measured_but_is_not_a_need():
    """Depth and lineup quality are separate questions. A team whose starting lineup is
    entirely fine can still be one injury from disaster, and saying so must not turn every
    position into a "need" - a healthy team would then read as having four problems."""
    slots = {"QB": 0, "RB": 0, "WR": 2, "TE": 0}
    thresholds = {p: 100 for p in slots}
    rosters, players = _league({
        "brittle": [("WR", 900), ("WR", 800), ("WR", 20)],    # huge cliff behind the starters
        "deep":    [("WR", 900), ("WR", 800), ("WR", 780)],   # barely any drop
        "c":       [("WR", 880), ("WR", 790), ("WR", 400)],
        "d":       [("WR", 870), ("WR", 780), ("WR", 300)],
    })
    starters = {r["owner_id"]: set(r["players"][:2]) for r in rosters}
    out = roster_needs.assess_positions(rosters, players, slots, thresholds, starters,
                                        ({"QB": 0, "RB": 0, "WR": 2, "TE": 0}, []))

    assert out["brittle"]["WR"]["level"] == "ok", "the starting lineup is not the problem"
    assert out["brittle"]["WR"]["drop_if_injured"] == 780, "800 starter -> 20 replacement"
    assert out["deep"]["WR"]["drop_if_injured"] == 20
    assert out["brittle"]["WR"]["exposure"] == "high"
    assert out["deep"]["WR"]["exposure"] == "low"
    assert roster_needs.needs_only(out["brittle"]) == {}, "exposure must never become a need"


def test_injury_drop_prices_the_marginal_lineup_spot():
    """If a team starts four RBs and its best one is hurt, everyone shuffles up and what
    actually enters the lineup is the best bench RB. So the loss is (worst starter - best
    bench), not (best starter - best bench). Real case: a roster starting McCaffrey,
    Taylor, Henderson and Swift is exposed by 1,043, not by 4,298."""
    players = {"a": {"name": "Best", "position": "RB", "value": 6653, "redraft_value": 6653},
               "b": {"name": "Mid", "position": "RB", "value": 2330, "redraft_value": 2330},
               "c": {"name": "Last", "position": "RB", "value": 1527, "redraft_value": 1527},
               "d": {"name": "Bench", "position": "RB", "value": 484, "redraft_value": 484}}
    roster = {"owner_id": "me", "players": list(players)}
    drop = roster_needs._injury_drop(roster, players, "RB", {"a", "b", "c"},
                                     {"QB": 0, "RB": 3, "WR": 0, "TE": 0}, [])
    assert drop == 1527 - 484


def test_injury_drop_accounts_for_flex_slots_backfilling_from_any_position():
    """A QB lost from a SUPER_FLEX is replaced by the best remaining player of ANY
    position, not by the team's QB3. A same-position reading overstates how exposed a
    superflex team with two good QBs and a cheap third is - which is how the format is
    meant to be built, not a weakness."""
    players = {
        "qb1": {"name": "QB1", "position": "QB", "value": 7000, "redraft_value": 7000},
        "qb2": {"name": "QB2", "position": "QB", "value": 6000, "redraft_value": 6000},
        "qb3": {"name": "Cheap", "position": "QB", "value": 300, "redraft_value": 300},
        "wr1": {"name": "WR1", "position": "WR", "value": 5000, "redraft_value": 5000},
        "wr2": {"name": "GoodBench", "position": "WR", "value": 4000, "redraft_value": 4000},
    }
    roster = {"owner_id": "me", "players": list(players)}
    dedicated, flex = {"QB": 1, "RB": 0, "WR": 1, "TE": 0}, [("QB", "RB", "WR", "TE")]
    starters = roster_needs.projected_starters(roster, players, dedicated, flex)
    assert starters == {"qb1", "qb2", "wr1"}, "QB2 fills the superflex"

    drop = roster_needs._injury_drop(roster, players, "QB", starters, dedicated, flex)
    assert drop == 6000 - 4000, "the WR backfills the superflex, not the 300 QB3"


def test_injury_exposure_is_absent_rather_than_guessed_without_a_lineup():
    """No lineup supplied means no exposure read - reported as None, never as 0, which
    would read as 'perfectly deep'. Same rule as a missing redraft price."""
    slots = {"QB": 0, "RB": 0, "WR": 2, "TE": 0}
    thresholds = {p: 100 for p in slots}
    rosters, players = _league({f"t{i}": [("WR", 900), ("WR", 800)] for i in range(4)})
    entry = roster_needs.assess_positions(rosters, players, slots, thresholds)["t0"]["WR"]
    assert entry["drop_if_injured"] is None and entry["exposure"] is None


def test_efficiency_swaps_are_allowed_at_a_weak_need_but_not_a_count_need():
    """A `weak` position has its slots covered and wants a better starter - which is what
    the freed value buys, at flat production since the swap retains >=90% by construction.
    A count-shaped need has an empty slot, where promoting the backup spends the last body
    you had. A blanket rule silenced the only two swaps in a real league."""
    me = {"owner_id": "me", "owner": "Me", "window": "Contend",
          "sellable": [
              {"name": "Pricey", "position": "QB", "value": 3288, "redraft_value": 2744,
               "bucket": "prime", "is_starter": True},
              {"name": "Cheap", "position": "QB", "value": 2735, "redraft_value": 2704,
               "bucket": "prime", "is_starter": False}],
          "tradeable_surplus": []}
    thresholds = {"QB": 100}

    def swaps_for(level):
        need = {"QB": {"level": level, "weakest_starter": 0, "note": "", "rank": 9, "of": 12}}
        return trade_targets._buy_path(me, [], {"me": need}, thresholds, {}, 3).get("efficiency_swaps", [])

    assert len(swaps_for("weak")) == 1, "capital toward the upgrade, production unchanged"
    assert swaps_for("critical") == [], "an empty slot must not be filled by spending depth"


def test_lineup_fill_reports_which_slot_each_player_occupies():
    """Slot assignments are the point of fill_lineup over projected_starters: the visible
    effect of an injury is players *moving* between slots, and a set of names can't show
    that."""
    positions = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "FLEX", "SUPER_FLEX"]
    dedicated, flex = roster_needs.lineup_slots(positions)
    players = _players([("QB", 7000), ("QB", 6000), ("RB", 5000), ("RB", 4000),
                        ("RB", 3000), ("WR", 2000), ("WR", 1900), ("WR", 1800), ("TE", 1000)])
    roster = {"owner_id": "me", "players": list(players)}

    filled = dict((pid, slot) for slot, pid in
                  roster_needs.fill_lineup(roster, players, dedicated, flex))
    assert filled["0"] == "QB" and filled["1"] == "SUPER_FLEX", "QB2 takes the superflex"
    assert filled["2"] == "RB" and filled["3"] == "RB"
    assert filled["4"] == "FLEX", "the third RB starts, at FLEX"
    assert set(filled) == set(roster_needs.projected_starters(roster, players, dedicated, flex))


def test_losing_a_starter_cascades_through_flex_from_any_eligible_position():
    """The case a manager gets wrong by hand. Lose the RB2 and the FLEX slides back into
    RB2, leaving a FLEX to fill - and it fills from RB/WR/TE, so a bench TE outproducing
    a bench WR takes it. On a real roster this promoted a tight end (627) where the
    manager expected the backup WR (259)."""
    positions = ["RB", "RB", "FLEX"]
    dedicated, flex = roster_needs.lineup_slots(positions)
    players = _players([("RB", 5000), ("RB", 4000), ("RB", 3000), ("TE", 627), ("WR", 259)])
    roster = {"owner_id": "me", "players": list(players)}

    before = roster_needs.projected_starters(roster, players, dedicated, flex)
    assert before == {"0", "1", "2"}, "three RBs: two at RB, one at FLEX"

    thinned = {**roster, "players": [p for p in roster["players"] if p != "1"]}
    after = dict((pid, slot) for slot, pid in
                 roster_needs.fill_lineup(thinned, players, dedicated, flex))
    assert after["2"] == "RB", "the FLEX back slides up into the vacated RB slot"
    assert "3" in after and after["3"] == "FLEX", "the bench TE fills FLEX, not the WR"
    assert "4" not in after
