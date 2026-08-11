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
    surplus = roster_needs.find_surplus(rosters[0], players, slots, thresholds)
    count_needs = {pos for pos, e in needs.items() if e["level"] in ("critical", "top-heavy")}
    assert not (count_needs & set(surplus))


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


def test_missing_next_first_reads_differently_by_window():
    """A bare boolean got read as universally bad. A live run told a Win-Now team that not
    owning its next 1st was "concerning for a contender" and to "reclaim a first-round
    pick" - in the same answer that correctly told it to spend picks aggressively. Having
    spent that pick is the window working as intended; it only hurts a rebuilder, who
    loses the one payoff for a bad season."""
    contender = team_state.next_first_note(False, "Win-Now")
    rebuilder = team_state.next_first_note(False, "Rebuilding")

    assert "not a concern" in contender.lower()
    assert "not a reason to trade back" in contender.lower()
    assert "a real problem" in rebuilder.lower()
    assert contender != rebuilder, "the same fact must not read the same way in both windows"
    assert "own" in team_state.next_first_note(True, "Rebuilding").lower()


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
    assert "P0" in projected and "P1" in projected, "top 2 QBs are the lineup"
    assert "P2" not in projected and "P3" not in projected, "QB3/QB4 are genuinely spare"


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
    assert projected == {"P0", "P2"}, "redraft ranking starts McCaffrey over the rookie"


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
    assert {"P0", "P1", "P2"} <= starters, "all three backs start - two at RB, one at FLEX"
    assert "P4" in starters, "the second QB fills SUPER_FLEX"


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

    assert roster_needs.projected_starters(roster, players, dedicated, flex) == {"P0", "P1"}


def test_projected_starters_sorts_missing_redraft_prices_last():
    """Redraft covers the top ~200 players, so deep dynasty-only assets have no price.
    Safe to treat as non-starters: across a real 12-team league the highest-dynasty
    rostered player missing one was 1,350, far below every replacement level."""
    slots = {"QB": 1, "RB": 1, "WR": 1, "TE": 1}
    players = _players([("WR", 1350), ("WR", 900)])
    players["0"]["redraft_value"] = None   # dynasty-only prospect
    players["1"]["redraft_value"] = 2500   # real current producer
    roster = {"players": list(players), "starters": []}

    assert roster_needs.projected_starters(roster, players, slots) == {"P1"}


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
    need = {"level": "critical", "weakest_starter": 0, "note": "", "rank": 12, "of": 12}
    out = trade_targets._buy_path(me, [seller], {"me": {"WR": need}}, thresholds,
                                  trade_counts={}, max_per_position=5, projected=set())
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
    offers = trade_targets._my_offer_pool(me, thresholds, needs={}, projected=set())
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
        {"name": "QB2", "position": "QB", "value": 3288, "redraft_value": 2744},
        {"name": "QB3", "position": "QB", "value": 2735, "redraft_value": 2704},
    ]
    swaps = trade_targets.find_efficiency_swaps(entries, projected={"QB2"})
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
        "owner_id": "me", "effective_strategy": "Win-Now",
        "sellable": [
            {"name": "QB2", "position": "QB", "value": 2728, "redraft_value": 2744,
             "bucket": "prime", "is_starter": True},
            {"name": "QB3", "position": "QB", "value": 2189, "redraft_value": 2704,
             "bucket": "prime", "is_starter": False},
        ],
        "tradeable_surplus": [],
    }
    out = trade_targets._buy_path(me, [], {"me": {}}, thresholds, trade_counts={},
                                  max_per_position=3, projected={"QB2"})
    names = [e["name"] for e in out["my_offers"]]
    assert "QB2" in names, "the swap's sell target must be offerable"
    entry = next(e for e in out["my_offers"] if e["name"] == "QB2")
    assert entry.get("swap_note"), "and must carry why it's safe to move"


def test_efficiency_swap_ignores_a_real_production_downgrade():
    """The point is arbitrage, not selling the team. A replacement that loses real
    current production shouldn't be suggested however much value it frees."""
    entries = [
        {"name": "Stud", "position": "RB", "value": 5000, "redraft_value": 5000},
        {"name": "Scrub", "position": "RB", "value": 800, "redraft_value": 1500},  # 30% of production
    ]
    assert trade_targets.find_efficiency_swaps(entries, projected={"Stud"}) == []


def test_efficiency_swap_skips_players_with_no_redraft_price():
    """Deep dynasty-only assets (rookies, prospects) have no redraft market - FantasyCalc
    carries ~200 redraft players against ~400 dynasty. Missing must mean skipped, not
    treated as zero production."""
    entries = [
        {"name": "Starter", "position": "WR", "value": 4000, "redraft_value": 3000},
        {"name": "Prospect", "position": "WR", "value": 900, "redraft_value": None},
    ]
    assert trade_targets.find_efficiency_swaps(entries, projected={"Starter"}) == []


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
        strategy_by_roster={1: "Win-Now", 2: "Rebuilding"},
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
        me | {"owner": "me", "effective_strategy": "Rebuilding"},
        {"owner_id": "win", "roster_id": 2, "owner": "Contender",
         "effective_strategy": "Win-Now", "tradeable_surplus": []},
        {"owner_id": "reb", "roster_id": 3, "owner": "OtherRebuild",
         "effective_strategy": "Rebuilding", "tradeable_surplus": []},
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
          "effective_strategy": "Rebuilding", "sellable": [], "tradeable_surplus": []}
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
