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

from analysis import roster_needs, team_state, team_values, trade_targets
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


def test_a_superflex_room_of_mahomes_plus_nobody_is_top_heavy_not_critical():
    """The first friend-tester pushback: the agent pitched selling a QB to teams it
    called "short at QB (critical)... group among the league's worst" - and one of those
    rooms was Patrick Mahomes plus nothing. The manager said "those teams have great QB
    rooms" and the agent apologised for advice that was RIGHT (superflex: they need a
    second BODY). The bug: group quality was ranked on the group TOTAL, and the empty
    second slot dragged a one-stud room to the bottom - the hole contaminated the verdict
    on the players. Count-short groups are now judged per startable body.

    Two count-short rooms, same hole, opposite bodies - only the bodies should differ."""
    slots = {"QB": 2, "RB": 0, "WR": 0, "TE": 0}
    thresholds = {"QB": 2000, "RB": 0, "WR": 0, "TE": 0}
    rosters, players = _league({
        "mahomes":  [("QB", 7000)],                    # one elite body, one empty slot
        "love":     [("QB", 5131), ("QB", 1717), ("QB", 1360)],  # a FULL room, one above the bar
        "scrub":    [("QB", 1100)],                    # one weak body, one empty slot
        "full1":    [("QB", 5000), ("QB", 4000)],
        "full2":    [("QB", 4500), ("QB", 3500)],
        "full3":    [("QB", 4200), ("QB", 3000)],
        "full4":    [("QB", 3800), ("QB", 2800)],
    })
    out = roster_needs.assess_positions(rosters, players, slots, thresholds)

    m = out["mahomes"]["QB"]
    assert m["level"] == "top-heavy", (
        "one elite starter and an empty slot is a BODY problem - ranking the hole-dragged "
        "total called this room among the league's worst")
    assert "dragged by the empty slot, not by the players" in m["note"]
    assert "per body" in m["note"]

    # The owner's second correction ("the Love/Willis guy had a fine QB room"): startable
    # is a BAR, not a headcount. This room holds three QBs - saying "1 startable QB" with
    # no mention of the others reads as a count of players, and anyone who knows the
    # roster catches the tool being "wrong". The note must say the gap is the quality of
    # the next body, not an empty room.
    love = out["love"]["QB"]
    assert love["level"] == "top-heavy" and love["rostered_bodies"] == 3
    assert "3 QBs rostered" in love["note"] and "below the startable bar" in love["note"]
    assert "not an empty room" in love["note"]

    assert out["scrub"]["QB"]["level"] == "critical", (
        "a genuinely weak body plus a hole is still both problems at once")


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

    # The other side of the bar, which this test used to leave unpinned: 700 is weak under
    # any bar from 25% to 90% of the median, so on its own it proves nothing about WHERE
    # the bar sits. 55% of the median at a mid-table rank has to read `ok`.
    rosters, players = _league({
        "a": [("TE", 6000)], "b": [("TE", 5500)], "c": [("TE", 5000)],
        "midling": [("TE", 1900)], "e": [("TE", 600)], "f": [("TE", 500)],
    })
    entry = roster_needs.assess_positions(rosters, players, slots, thresholds)["midling"]["TE"]
    assert entry["level"] == "ok", "1,900 against a median of 3,450 is unremarkable, not weak"


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


def test_missing_the_cornerstone_tag_on_the_clock_does_not_cheapen_the_ask():
    """The owner's eye test caught it: a roster read "cornerstones: none" while holding
    a top-6 receiver at ~1.8 years - above the cornerstone VALUE bar, under the runway
    gate - and the tool sold its premium asset like an ordinary piece. The label may
    cliff at 2.0; the ASK must not: the market is still paying the top-10% price, which
    is precisely the argument for selling now. The note rides only where both facts hold
    (value above the bar AND a known clock) - an unknown age claims nothing."""
    roster, players, starters = _roster([("WR", 12_000), ("WR", 12_000),
                                         ("WR", 1_000), ("WR", 12_000)])
    players["0"] |= {"age": 27.5}   # 1.5 years on the WR curve: tag lost to the clock
    players["1"] |= {"age": 24}     # real cornerstone
    players["2"] |= {"age": 27.5}   # same clock, but never near the value bar
    players["3"] |= {"age": None}   # value clears the bar, clock unknown

    result = team_state.classify(roster, players, 10_000, starters)
    assert [e["name"] for e in result["cornerstones"]] == ["P1"]
    core = {e["name"]: e for e in result["win_now_core"]}
    assert "cornerstone-priced" in core["P0"]["price_note"]
    assert "12,000" in core["P0"]["price_note"] and "1.5 years" in core["P0"]["price_note"]
    assert "price_note" not in core["P3"], "no clock, no claim about the clock"
    cheap = next(e for e in result["sellable"] if e["name"] == "P2")
    assert "price_note" not in cheap, "the note is about the VALUE bar, not the clock alone"


def test_priced_for_reads_rank_on_each_scale_not_the_raw_ratio():
    """The ratio cannot answer "is he priced for now or later" and rank can. Dynasty prices ~400
    players against redraft's ~200, so the ratio decays toward zero down the board - measured
    medians QB 0.36, RB 0.05, WR 0.00, TE 0.00 - and a threshold of 1.0 mislabelled 111 live
    entries against 32.

    Four players at one position, ratios chosen so the ratio verdict is WRONG for three of them:
    the two elite names have near-identical ratios to the aging one and the marginal one, and
    only their ranks separate them."""
    players = {
        # elite on both boards - the ratio calls this "upside-priced" at 0.88
        "elite":    {"name": "Elite", "position": "WR", "value": 8800, "redraft_value": 7700},
        # young: much better in dynasty than redraft
        "young":    {"name": "Young", "position": "WR", "value": 6000, "redraft_value": 1000},
        # aging: better in redraft than dynasty despite a ratio under 1.0
        "aging":    {"name": "Aging", "position": "WR", "value": 1800, "redraft_value": 1500},
        # marginal: tiny ratio, but ranked the same on both - the Pollard/Gainwell case
        "marginal": {"name": "Marginal", "position": "WR", "value": 1500, "redraft_value": 600},
        "nopricing": {"name": "Prospect", "position": "WR", "value": 900, "redraft_value": None},
    }
    out = team_values.priced_for(players)
    verdict = {players[k]["name"]: v["priced_for"] for k, v in out.items()}

    assert verdict["Elite"] == "aligned", "1st on both boards is not upside-priced at 0.88"
    assert verdict["Young"] == "later"
    assert verdict["Aging"] == "now", "better in redraft than dynasty, whatever the ratio says"
    assert verdict["Marginal"] == "aligned", "a small ratio at the bottom is depth, not upside"
    assert "nopricing" not in out, "no redraft price means no verdict, not a zero"
    assert out["aging"]["dynasty_rank"] == 3 and out["aging"]["redraft_rank"] == 2


def test_window_needs_both_axes_not_either_one():
    """Contention decides *which* windows are on the table; trajectory picks between them
    only at the top. A falling roster is Push if it can compete and Rebuild if it can't."""
    assert team_state.window_for("contender", "falling") == "Push"
    assert team_state.window_for("contender", "rising") == "Contend"
    assert team_state.window_for("contender", "steady") == "Contend"
    assert team_state.window_for("also-ran", "rising") == "Rebuild", (
        "young and genuinely bad is still a rebuild - being young doesn't make you close")


def test_there_are_three_states_and_the_window_is_a_flavor_of_one():
    """`window` returns four labels and there are three states - Push and Contend are one
    state with a clock question, exactly as rising/falling are flavors of Middling. Shipping
    four peer labels made both a reader and this file's author count four base states."""
    assert team_state.STATE["Push"] == team_state.STATE["Contend"] == "Contending"
    assert len(set(team_state.STATE.values())) == 3


def test_a_rebuild_flavor_is_absolute_because_the_label_makes_a_claim():
    """The one place a tertile and the honest answer diverge. BartolosHeroes is 40% ascending
    against 3% declining and lands in the MIDDLE trajectory tertile only because its league is
    full of ascending rebuilds - so the tertile said "steady" and the flavor said `stalled`,
    i.e. "nothing arriving and nothing to convert", about the clearest working rebuild on the
    board. "Is the rebuild working" is a question about the roster, not its rank.

    Middling deliberately keeps the tertile: there the question really is comparative, since
    whether waiting is free *relative to this league* is what decides push versus pivot."""
    assert team_state.flavor_for("Rebuild", "steady", None, 40, 3) == "ascending"
    assert team_state.flavor_for("Rebuild", "rising", None, 9, 12) == "stalled", (
        "and it works the other way too - a flattering tertile cannot make a rebuild ascending")
    assert team_state.flavor_for("Rebuild", "steady", None, 9, 9) == "stalled", (
        "nothing arriving is stalled, ties included")
    # The quality floor on the tilt: a positive ratio from players who are all bad still
    # read "ascending" (live: 25/7 while dead last in lineup AND assets - the owner: "his
    # ascending assets are just bad"). Accumulation is the evidence the tilt is real, and
    # every measured working rebuild sat mid-table or better in total assets.
    assert team_state.flavor_for("Rebuild", "steady", None, 40, 3,
                                 assets_bottom=True) == "stalled", (
        "an ascending tilt with a bottom-third war chest is not a rebuild that is working")
    assert team_state.flavor_for("Rebuild", "steady", None, 40, 3,
                                 assets_bottom=False) == "ascending"
    assert team_state._assets_bottom(9, 12) and not team_state._assets_bottom(8, 12), (
        "bottom tertile of 12 starts at rank 9")
    assert not team_state._assets_bottom(5, 5), (
        "below MIN_TEAMS_FOR_LEVERAGE the tertile is one team and asserts nothing")
    assert team_state.flavor_for("Middling", "steady", None, 40, 3) == "steady"
    # `convertible` outranks trajectory: a weak lineup on a top-third war chest is not
    # described by which way it happens to be tilting.
    assert team_state.flavor_for("Rebuild", "steady", "convertible", 40, 3) == "convertible"
    assert team_state.flavor_for("Push", "falling", "convertible", 0, 40) == "Push", (
        "a contender's flavor is the clock, whatever its asset base says")


_SAME_PRODUCTION = lambda a, b: a - b <= a * team_values.NOISE_BAND


def test_a_tertile_line_through_a_tie_hedges_both_sides_and_a_clear_gap_neither():
    """A live trajectory line once ran through a literal 37-37 tie - which side each team
    sat on was decided by dict order, not football. The pair straddling a line is hedged
    when their scores are within refresh noise of each other; across a clear gap the
    labels are solid and nothing is hedged."""
    scores = {f"o{r}": s for r, s in enumerate(
        [50_000, 46_000, 42_000, 38_000, 34_000, 30_000], start=1)}  # every gap ~10%
    assert team_state.tertile_edges(team_values.rank_map(scores), scores, 6,
                                    _SAME_PRODUCTION) == {}

    scores["o3"] = scores["o2"]  # tie exactly on the top line of a 6-team field
    edges = team_state.tertile_edges(team_values.rank_map(scores), scores, 6,
                                     _SAME_PRODUCTION)
    assert edges == {"o2": ("middle", 0, 46_000), "o3": ("top", 0, 46_000)}, (
        "each side is told which tier it is one refresh from, and both quote the SAME "
        "gap against the same reference - one line, one number")


def test_the_hedge_band_splits_the_measured_flip_boundary():
    """Calibration, from jittering every player +/-2% across 300 simulated refreshes on
    three real leagues: the one straddling pair 0.5% apart flipped windows on 20% of
    refreshes, and the pair 2.4% apart never flipped once. The band (NOISE_BAND, 2%)
    must land between those - hedge the first pair, trust the second."""
    scores = {"o1": 50_000,
              "o2": 40_000, "o3": 39_040,   # 2.4% apart across the top line
              "o4": 35_000, "o5": 34_825,   # 0.5% apart across the bottom line
              "o6": 20_000}
    edges = team_state.tertile_edges(team_values.rank_map(scores), scores, 6,
                                     _SAME_PRODUCTION)
    assert edges == {"o4": ("bottom", 175, 35_000), "o5": ("middle", 175, 35_000)}


def _edge_row(**kw):
    row = {"owner_id": "me", "contention": "fringe", "trajectory": "steady",
           "window": "Middling", "flavor": "steady", "leverage": None,
           "ascending_pct": 20, "declining_pct": 20, "starting_production": 30_000,
           "asset_rank": 6, "of_teams": 12}
    return {**row, **kw}


def test_an_edge_is_only_an_edge_when_crossing_the_line_changes_the_message():
    """A Rebuild's flavor is absolute and `convertible` outranks trajectory, so a
    trajectory flip tells neither team anything new - hedging them would be noise about
    noise. The same flip IS news to a contender (the clock: Push vs Contend) and to a
    plain Middling team (whether patience is free)."""
    a_point_from_lower = {"me": ("middle", 1, 20)}
    rebuild = _edge_row(window="Rebuild", contention="also-ran", trajectory="rising",
                        flavor="ascending", ascending_pct=40, declining_pct=3)
    assert team_state.window_edge(rebuild, {}, a_point_from_lower) is None

    convertible = _edge_row(trajectory="rising", flavor="convertible",
                            leverage="convertible")
    assert team_state.window_edge(convertible, {}, a_point_from_lower) is None

    middling = _edge_row(trajectory="rising", flavor="rising")
    note = team_state.window_edge(middling, {}, a_point_from_lower)
    assert "'rising'" in note and "'steady'" in note, (
        "whether patience is free is the thing that can flip")

    contender = _edge_row(window="Contend", contention="contender", flavor="Contend")
    note = team_state.window_edge(contender, {}, {"me": ("bottom", 2, 20)})
    assert "Push" in note, "the clock is the thing that can flip"

    assert team_state.window_edge(middling, {}, {}) is None


def test_a_contention_edge_names_the_window_across_the_line():
    """The alternate window is computed with the team's OWN trajectory - a falling team
    one refresh from the top tier would arrive there as Push, not generic Contending."""
    note = team_state.window_edge(_edge_row(), {"me": ("top", 300, 30_000)}, {})
    assert "Contend" in note and "1.0%" in note, (
        "the gap ships labelled, against the shared reference score")
    note = team_state.window_edge(_edge_row(trajectory="falling"),
                                  {"me": ("top", 300, 30_000)}, {})
    assert "Push" in note


def test_the_trajectory_band_is_in_points_and_matches_its_calibration():
    """2, in POINTS not a share - the score crosses zero, where a relative band means
    nothing. Calibrated: the score moved 1 point at p95 under refresh jitter, so a pair
    2 apart is one update from swapping and a pair 3 apart is not."""
    same = lambda a, b: a - b <= team_state.TRAJECTORY_NOISE_POINTS
    scores = {"o1": 40, "o2": 20, "o3": 17, "o4": 5, "o5": 3, "o6": -30}
    edges = team_state.tertile_edges(team_values.rank_map(scores), scores, 6, same)
    assert edges == {"o4": ("bottom", 2, 5), "o5": ("middle", 2, 5)}, (
        "3 points across the top line holds; 2 across the bottom line is a coin flip")


def test_value_basis_uses_the_final_year_clock_not_the_buyer_horizon():
    """A shipped bug the suite used to let back in: with the 2.0 buyer horizon here
    instead of INSIDE_FINAL_YEAR, the RB curve calls any back over 25 production-priced.
    1.5 years is inside a buyer's two-season horizon but NOT inside his own final year,
    so the bucket answers; only past his own edge does the price become production."""
    assert team_state.value_basis({"years_to_decline": 0.8, "bucket": "prime"}) == "production"
    assert team_state.value_basis({"years_to_decline": 1.5, "bucket": "prime"}) == "mixed"
    assert team_state.value_basis({"years_to_decline": 1.5, "bucket": "ascending"}) == "upside"
    assert team_state.value_basis({"years_to_decline": None, "bucket": "declining"}) == "production"


class _Ctx:
    """Minimal LeagueContext stand-in for find_value_upgrades."""
    def __init__(self, players, rosters, starters):
        self.players, self.rosters, self._starters = players, rosters, starters

    def starters_for(self, roster):
        return self._starters.get(roster["owner_id"], set())


def _board(ctx=None, states=None, **kw):
    """A trade_targets.Board with only the fields a test cares about set."""
    return trade_targets.Board(ctx=ctx, states=states or [], **kw)


def _upgrade_fixture():
    players = {
        "mine": {"name": "Pricey", "position": "TE", "value": 3515, "redraft_value": 1811},
        "better": {"name": "Cheaper", "position": "TE", "value": 2442, "redraft_value": 2044},
        "lateral": {"name": "Lateral", "position": "TE", "value": 4000, "redraft_value": 2500},
        "worse": {"name": "Worse", "position": "TE", "value": 900, "redraft_value": 500},
    }
    rosters = [{"owner_id": "me", "players": ["mine"]},
               {"owner_id": "them", "players": ["better", "lateral", "worse"]}]
    ctx = _Ctx(players, rosters, {"me": {"mine"}, "them": {"better", "lateral"}})
    # The trajectory fields are what `_seller_case`/`_cliff_case` read to say whether the OTHER
    # owner has a reason to part with the player. Real `classify_league` rows always carry them,
    # so the fixture does too - a stub missing them let the counterparty half go untested.
    states = [{"owner_id": "me", "owner": "me", "window": "Push",
               "trajectory": "falling", "ascending_pct": 3, "declining_pct": 24},
              {"owner_id": "them", "owner": "them", "window": "Middling",
               "trajectory": "steady", "ascending_pct": 10, "declining_pct": 10}]
    return ctx, rosters[0], states


def test_value_upgrade_requires_beating_a_starter_on_both_axes():
    """More production AND less dynasty value. A player who is better but pricier is a
    normal buy target, not this - the whole point is that it costs nothing to prefer him."""
    ctx, me, states = _upgrade_fixture()
    moves = trade_targets.find_value_upgrades(me, _board(ctx, states, trade_counts={"them": 3}), {"mine"})
    assert [m["move_off"] for m in moves] == ["Pricey"]
    assert [u["name"] for u in moves[0]["returns"]] == ["Cheaper"], (
        "Lateral is pricier, Worse produces less")
    assert moves[0]["returns"][0]["production_gained"] == 233
    assert moves[0]["returns"][0]["value_freed"] == 1073


def test_the_three_kinds_are_labelled_by_how_much_production_survives():
    """-994 and -47 are not the same decision and must not share a word. Above 98% the lineup
    is effectively unchanged (`value_decision`); down to 90% real production is being sold for
    real value (`conversion`); below that it is just a worse team and nothing is returned. All
    three sit on the same fixture so the only thing separating them is the production."""
    ctx, me, states = _upgrade_fixture()
    # mine: 3515 dynasty / 1811 redraft
    ctx.players["noise"] = {"name": "Noise", "position": "TE", "value": 3000,
                            "redraft_value": 1790}   # 98.8%, frees 515
    ctx.players["convert"] = {"name": "Convert", "position": "TE", "value": 2500,
                              "redraft_value": 1700}  # 93.9%, frees 1015
    # The mirror of `Noise`, and the case that was missing: `NOISE_RETAINED` caught noise on the
    # way DOWN and nothing caught it on the way UP, so a live return worth +3 of 2,572 (+0.12%)
    # was called "strictly the better holding for a team trying to win now". The lineup does not
    # move at +3; the finding is the value freed.
    ctx.players["hair"] = {"name": "Hair", "position": "TE", "value": 3000,
                           "redraft_value": 1813}    # +0.11%, frees 515
    for pid in ("noise", "convert", "hair"):
        ctx.rosters[1]["players"].append(pid)
    moves = trade_targets.find_value_upgrades(me, _board(ctx, states, trade_counts={"them": 3}), {"mine"})
    by_name = {u["name"]: u for u in moves[0]["returns"]}

    assert by_name["Cheaper"]["kind"] == "upgrade"
    assert by_name["Hair"]["kind"] == "value_decision", (
        "producing 0.11% MORE is the same lineup, not a lineup upgrade - the noise band has to "
        "work in both directions or `upgrade` claims a gain nobody would notice")
    assert by_name["Noise"]["kind"] == "value_decision"
    assert "not a lineup upgrade" in by_name["Noise"]["note"]
    assert by_name["Convert"]["kind"] == "conversion"
    assert "GIVES UP" in by_name["Convert"]["note"], "a conversion must state the loss"
    assert "Worse" not in by_name, "28% of the production is not a conversion at any price"


def test_every_return_says_why_its_owner_would_part_with_him():
    """Half the trade was missing. `wanted_by` said who would want the player I'd MOVE, and
    nothing said why the player I'd GET would be available - so a tight end held by a contender
    read exactly like one held by a seller. The owner named it: *"the fannin for kelce stuff -
    shiv is win now and could choose to move off the aging value but doesn't have to."*

    A `Rebuild` owner is already selling; anyone else has to be argued into it, which is the
    same `_seller_case`/`_cliff_case` pair the persuasion tier uses, reused rather than
    restated - and the no-hole friction is named by the holder's window: this holder is a
    CONTENDER, so the ask is `holds_to_win`, which is what the owner's quote above was
    actually describing all along."""
    ctx, me, states = _upgrade_fixture()
    seller, holder = states[1], {"owner_id": "holder", "owner": "holder", "window": "Contend",
                                "trajectory": "rising", "ascending_pct": 26, "declining_pct": 16}
    seller["window"] = "Rebuild"
    ctx.players["theirs"] = {"name": "Aging", "position": "TE", "value": 2000,
                             "redraft_value": 1900, "age": 36}
    ctx.rosters.append({"owner_id": "holder", "players": ["theirs"]})
    ctx._starters["holder"] = {"theirs"}
    states.append(holder)

    moves = trade_targets.find_value_upgrades(
        me, _board(ctx, states, trade_counts={"them": 3, "holder": 3},
                   premium_bars={"TE": 0.5}), {"mine"}, "Push")
    by_name = {u["name"]: u for u in moves[0]["returns"]}

    assert "no persuasion needed" in by_name["Cheaper"]["their_reason"], (
        "a rebuilding owner is already selling this kind of production")
    assert not by_name["Cheaper"].get("friction")

    aging = by_name["Aging"]
    assert "don't line up" in aging["their_reason"], "the cliff argument, not just 'he's old'"
    assert [f["flavor"] for f in aging["friction"]] == ["holds_to_win"]


def test_a_pushing_team_is_never_offered_a_conversion():
    """A closing window needs the points, so trading production away for capital is the one
    thing it must not be told to do. The same roster contending sees it, which is what makes
    this a window rule rather than a threshold."""
    ctx, me, states = _upgrade_fixture()
    ctx.players["convert"] = {"name": "Convert", "position": "TE", "value": 2500,
                              "redraft_value": 1700}
    ctx.rosters[1]["players"].append("convert")

    contending = trade_targets.find_value_upgrades(
        me, _board(ctx, states, trade_counts={"them": 3}), {"mine"}, "Contend")
    assert "Convert" in [u["name"] for u in contending[0]["returns"]]

    pushing = trade_targets.find_value_upgrades(
        me, _board(ctx, states, trade_counts={"them": 3}), {"mine"}, "Push")
    assert "Convert" not in [u["name"] for u in pushing[0]["returns"]]


def test_a_better_holding_already_on_my_own_bench_leads_and_is_never_capped():
    """Real case the old within-roster function was built for and could not reach:
    BradTheInhaler starts a TE producing 353 while T.J. Hockenson produces 331 on his bench for
    1,293 less dynasty value. It needs no trade at all - promote him, sell the starter - yet it
    ranks LAST on production gained by construction, so `RETURNS_PER_MOVE` deleted it behind
    four better external returns. The free move cannot be the one the cap removes."""
    ctx, me, states = _upgrade_fixture()
    ctx.players["mybench"] = {"name": "MyBench", "position": "TE",
                              "value": 2222, "redraft_value": 1700}  # 93.9%, frees 1293
    me["players"].append("mybench")
    for i in range(trade_targets.RETURNS_PER_MOVE + 2):  # crowd the shortlist
        pid = f"ext{i}"
        ctx.players[pid] = {"name": f"Ext{i}", "position": "TE",
                            "value": 2400 + i, "redraft_value": 2200 + i}
        ctx.rosters[1]["players"].append(pid)

    returns = trade_targets.find_value_upgrades(
        me, _board(ctx, states, trade_counts={"them": 3}), {"mine"}, "Contend")[0]["returns"]
    assert returns[0]["name"] == "MyBench", "the no-trade option leads"
    assert returns[0]["already_mine"] and returns[0]["from_owner"] == "your own bench"
    assert len(returns) == trade_targets.RETURNS_PER_MOVE + 1, (
        "it is additive to the shortlist, not competing for a slot in it")


def test_a_near_equal_swap_that_frees_almost_nothing_is_churn():
    """Retaining the production is necessary but not sufficient - if barely any value comes
    back there is no reason to make the trade at all."""
    ctx, me, states = _upgrade_fixture()
    ctx.players["churn"] = {"name": "Churn", "position": "TE",
                            "value": 3400, "redraft_value": 1800}  # 99.4%, frees only 115
    ctx.rosters[1]["players"].append("churn")
    moves = trade_targets.find_value_upgrades(me, _board(ctx, states, trade_counts={"them": 3}), {"mine"})
    assert "Churn" not in [u["name"] for u in moves[0]["returns"]]


def test_value_upgrades_skip_players_with_no_redraft_price():
    """Deep dynasty-only assets (rookies, prospects) have no redraft market - FantasyCalc
    carries ~200 redraft players against ~400 dynasty. Missing must mean skipped, not read as
    zero production, which the near-equal band would otherwise let in from the wrong side."""
    ctx, me, states = _upgrade_fixture()
    ctx.players["prospect"] = {"name": "Prospect", "position": "TE",
                               "value": 900, "redraft_value": None}
    ctx.rosters[1]["players"].append("prospect")
    moves = trade_targets.find_value_upgrades(me, _board(ctx, states, trade_counts={"them": 3}), {"mine"})
    assert "Prospect" not in [u["name"] for u in moves[0]["returns"]]


def test_value_upgrades_are_organised_around_the_player_being_moved():
    """A flat ranked list reads as a one-for-one swap and hides moves whose gain is mostly
    value freed. Every upgradeable starter gets an entry, ordered by its best return."""
    ctx, me, states = _upgrade_fixture()
    ctx.players["mine2"] = {"name": "Pricey2", "position": "RB",
                            "value": 3488, "redraft_value": 2319}
    ctx.players["rb"] = {"name": "OldBack", "position": "RB",
                         "value": 2982, "redraft_value": 4581}
    me["players"].append("mine2")
    ctx.rosters[1]["players"].append("rb")
    moves = trade_targets.find_value_upgrades(
        me, _board(ctx, states, trade_counts={"them": 3}), {"mine", "mine2"})
    assert [m["move_off"] for m in moves] == ["Pricey2", "Pricey"], (
        "both starters represented, ordered by their best available gain")


def test_value_upgrade_names_who_would_want_the_player_being_moved():
    """The join that turns a fact into a phone call. `them` is short at TE but already starts
    two better ones, so he is not a counterparty; `needy` starts a worse one and is."""
    ctx, me, states = _upgrade_fixture()
    ctx.players["their_scrub"] = {"name": "Scrub", "position": "TE",
                                  "value": 400, "redraft_value": 300}
    ctx.rosters.append({"owner_id": "needy", "players": ["their_scrub"]})
    ctx._starters["needy"] = {"their_scrub"}
    # Same trajectory fields `_upgrade_fixture` carries and for the same reason: every real
    # `classify_league` row has them, and `_counterparty_fit` now reads them on this path to
    # decide whether an ask is a fit or a pivot.
    states.append({"owner_id": "needy", "owner": "needy", "window": "Push",
                   "trajectory": "steady", "ascending_pct": 10, "declining_pct": 10})
    needs = {"them": {"TE": {"level": "critical", "rank": 12, "startable": 0, "slots": 1}},
             "needy": {"TE": {"level": "critical", "rank": 11, "startable": 0, "slots": 1}}}

    moves = trade_targets.find_value_upgrades(me, _board(ctx, states, trade_counts={"them": 3}, needs_by_owner_id=needs), {"mine"})
    # Entries carry `wanted_by` as ONE composed string, not a list of dicts - the dicts
    # repeated near-identically on every same-position entry and measured at 21-26% of a
    # sell report's tokens. `them` must not appear: short at TE but starts two better ones.
    assert "needy" in moves[0]["wanted_by"] and "them" not in moves[0]["wanted_by"]
    assert "short at TE (critical: 0 startable for 1 slot)" in moves[0]["wanted_by"], (
        "the why carries the SHAPE of the need, not just its label - a bare '(critical)' "
        "about a top-heavy superflex room read as 'their players are bad' and got the "
        "model apologising for correct advice")
    assert "pays a premium for production" in moves[0]["wanted_by"], (
        "the contender-premium clause ships in the payload now, not only in the CLI")


def test_a_middling_buyer_reads_as_undecided_not_urgent():
    """A Middling team's need is real but he hasn't committed to contending - the buy
    would BE the commitment. Presented bare, the model gave his interest a contender's
    urgency, which overstates both his motivation and his price."""
    line = trade_targets.wanted_line([
        {"owner": "fence-sitter", "window": "Middling", "why": "short at TE (thin)"},
        {"owner": "pusher", "window": "Push", "why": "short at TE (critical)"},
    ])
    assert "buying is what would push them in" in line
    assert line.index("fence-sitter") < line.index("buying is what would push them in") < line.index("pusher"), (
        "the undecided clause belongs to the Middling entry, not the contender")


def test_a_falling_roster_wants_ascending_value_at_any_position():
    """Positional need alone missed the most obvious counterparty on a live board: the owner
    holding both best returns needed no TE and no RB, he needed youth."""
    me_roster = {"owner_id": "me"}
    young = {"name": "Kid", "position": "TE", "bucket": "ascending"}
    old = {"name": "Vet", "position": "TE", "bucket": "declining"}
    states = [{"owner_id": "me", "owner": "me", "window": "Push"},
              {"owner_id": "falling", "owner": "falling", "window": "Middling",
               "ascending_pct": 4, "declining_pct": 40}]

    wants = trade_targets.wanted_by(young, me_roster, _board(None, states))
    assert [w["owner"] for w in wants] == ["falling"], "no positional need, wants youth"
    assert "falling roster" in wants[0]["why"] and wants[0]["need_level"] is None

    assert trade_targets.wanted_by(old, me_roster, _board(None, states)) == [], (
        "a falling roster has no special appetite for another declining player")


def test_need_claims_about_teams_are_grounded_or_fired_on():
    """A live answer told a rebuilder to sell a QB to MSpoto29 - a Contend team holding
    Josh Allen, Jaxson Dart AND Kyler Murray, flagged with no QB need anywhere in the
    data. The trade-away side had a deterministic tripwire; the need-claim side had
    nothing, so a fabricated need sailed through and collapsed to "oops my bad" under
    pushback. Same generate-then-verify pattern, next surface.

    Verified against today's REAL answers so the guard cannot false-fire on correct
    behaviour - both recorded sentences below shipped in live passes."""
    from agent.agent import _need_claim_violations

    flagged = {("FitzmagicsEMUs", "QB"), ("FitzmagicsEMUs", "RB"), ("SeanCenter", "QB")}
    names = {"FitzmagicsEMUs", "SeanCenter", "MSpoto29"}

    # The fabrication fires.
    v = _need_claim_violations(
        "MSpoto29 has a critical QB need and would jump at Goff.", flagged, names)
    assert v and "MSpoto29" in v[0]

    # Real recorded sentences from live passing answers must NOT fire.
    assert not _need_claim_violations(
        "FitzmagicsEMUs has a critical QB need, and Darnold is exactly the kind of "
        "swap a rebuilder wants.", flagged, names)
    assert not _need_claim_violations(
        "Lead with Sam Darnold. FitzmagicsEMUs has a critical QB need.", flagged, names)

    # Negations and need-free descriptions never fire.
    assert not _need_claim_violations(
        "MSpoto29 doesn't need a QB - that room is loaded.", flagged, names)
    assert not _need_claim_violations(
        "MSpoto29 has a great QB room with Josh Allen and Dart.", flagged, names)
    # A need claim with no position in the clause is not checkable - stay silent.
    assert not _need_claim_violations(
        "MSpoto29 needs to decide a direction.", flagged, names)


def test_grounding_check_ignores_advice_not_to_trade_someone():
    """A live run spent a retry telling the model off for advice it had given correctly:
    "Don't trade ... or Tyler Warren" has a trade word and a non-offerable name on one line.
    Telling someone to KEEP a cornerstone is what this rule wants."""
    from agent import agent

    banned = {"Tyler Warren", "Ghost Player"}
    keep = "Don't trade the QBs you can actually start (Herbert, Hurts) or Tyler Warren."
    assert agent._trade_violations(keep, banned) == []

    real = "Package Ghost Player and a 2027 2nd for a starting back."
    assert agent._trade_violations(real, banned) == ["Ghost Player"], (
        "a genuine ungrounded suggestion still fires")


def test_a_positional_need_is_not_the_same_as_wanting_this_player():
    """Listed four counterparties for a QB none of them would have started. He only helps a
    team whose current starter at the position produces less than he does."""
    players = {"weak": {"name": "Weak", "position": "QB", "redraft_value": 780},
               "theirs_good": {"name": "TheirStud", "position": "QB", "redraft_value": 7894},
               "theirs_bad": {"name": "TheirScrub", "position": "QB", "redraft_value": 518}}
    ctx = _Ctx(players,
               [{"owner_id": "stocked", "players": ["theirs_good"]},
                {"owner_id": "desperate", "players": ["theirs_bad"]}],
               {"stocked": {"theirs_good"}, "desperate": {"theirs_bad"}})
    states = [{"owner_id": "stocked", "owner": "stocked", "window": "Push"},
              {"owner_id": "desperate", "owner": "desperate", "window": "Push"}]
    needs = {"stocked": {"QB": {"level": "critical", "startable": 1, "slots": 2}},
             "desperate": {"QB": {"level": "critical", "startable": 1, "slots": 2}}}

    wants = trade_targets.wanted_by(players["weak"], {"owner_id": "me"}, _board(ctx, states, needs_by_owner_id=needs))
    assert [w["owner"] for w in wants] == ["desperate"], (
        "both are short at QB; only one of them he actually improves")


def test_middle_of_the_league_is_a_window_not_a_leftover():
    """Rebuild used to be the else branch, so a fringe team that merely wasn't rising fell
    into it and was told to sell. The live case was 3rd of 12 in total dynasty value with
    the league's best QB room. In dynasty you are winning now or rebuilding - in between,
    you see both directions and may wait on how the season starts."""
    for trajectory in ("rising", "steady", "falling"):
        assert team_state.window_for("fringe", trajectory) == "Middling"


def test_middling_note_promises_free_patience_only_when_actually_rising():
    """Middling shows both paths regardless, but only a rising roster gets next season's
    production for free - claiming that for a falling one is the label lying about the
    data printed in the same sentence."""
    rising = team_state.window_note("Middling", 6, 12, 74, 39, 0, trajectory="rising")
    falling = team_state.window_note("Middling", 7, 12, 71, 8, 14, trajectory="falling")
    assert "for free" in rising
    assert "for free" not in falling
    assert "will not be cheaper" in falling


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
                                           _board(thresholds=thresholds), needs={})
    names = [e["name"] for e in offered]
    assert "QB3" in names, "bench depth at this value should be offerable"
    assert "QB2" not in names, "a prime starter is current production, not surplus"


def test_a_cornerstone_is_askable_and_tagged_rather_than_hidden():
    """Cornerstones used to be left out of `sellable` entirely, which made the best pieces on
    a roster literally unaskable - an owner deciding what he'd move names them first. Being
    the hardest ask is a PRICE, not a veto, so they surface carrying `friction`. The
    comparison player is a prime starter at the same value who stays protected, so this can't
    pass by accident: what lets the cornerstone through is the flag, not the value."""
    thresholds = {"QB": 100}
    rock = {"name": "Rock", "position": "QB", "value": 5916, "is_starter": True,
            "bucket": "prime", "is_cornerstone": True}
    producing = {"name": "Producing", "position": "QB", "value": 5916, "is_starter": True,
                 "bucket": "prime"}

    offered = trade_targets._my_offer_pool(
        {"sellable": [rock, producing], "tradeable_surplus": [], "window": "Contend"},
        _board(thresholds=thresholds), needs={})
    by_name = {e["name"]: e for e in offered}
    assert "Rock" in by_name, "a cornerstone must be askable"
    assert "Producing" not in by_name, "an ordinary producing starter is still protected"
    assert [f["flavor"] for f in by_name["Rock"]["friction"]] == ["cornerstone"]
    assert "hardest ask" in by_name["Rock"]["friction"][0]["why"]


def test_an_offer_says_who_backfills_and_what_the_trade_off_is():
    """A starter shown as costing 122 of production reads as an arbitrary number. "122, because
    DK Metcalf takes the FLEX at 944" is the argument for moving him, and it puts both currencies
    on one line: *"seeing if the prod lost is anywhere close to the value realized from moving an
    asset priced for youth."* Live contrast the line has to make legible - Fannin frees 3,688 for
    122, Lamar Jackson frees 7,255 for 6,043."""
    thresholds = {"TE": 100}
    cheap = {"name": "Cheap", "position": "TE", "value": 3688, "redraft_value": 1066,
             "bucket": "ascending", "is_starter": True}
    me = {"sellable": [cheap], "tradeable_surplus": [], "window": "Push",
          "starting_production": 38467}

    offers = trade_targets._my_offer_pool(
        me, _board(thresholds=thresholds), needs={}, covered={"Cheap": 122.0},
        backfills={"Cheap": {"name": "Backup", "position": "WR", "redraft_value": 944}})
    entry = offers[0]
    assert entry["backfill"]["name"] == "Backup"
    assert "frees 3,688 of dynasty value for 122 of production" in entry["trade_off"]
    assert "because Backup" in entry["trade_off"]
    # 122 of 38,467 leaves 99.7% standing, so it is not friction - but the trade-off line
    # still has to state the number, which is the only place it now appears.
    assert not entry["friction"]

    # The other side of NOISE_RETAINED, previously unpinned: 1,600 of 38,467 (4.2%) is a
    # hit the lineup notices, and it must arrive as friction with the share stated.
    offers = trade_targets._my_offer_pool(
        me, _board(thresholds=thresholds), needs={}, covered={"Cheap": 1600.0},
        backfills={"Cheap": {"name": "Backup", "position": "WR", "redraft_value": 944}})
    friction = offers[0]["friction"]
    assert [f["flavor"] for f in friction] == ["costs_you_production"]
    assert "4% of what it scores now" in friction[0]["why"]


def test_out_of_reach_means_above_one_piece_never_a_sum():
    """`_best_chip` is the single biggest thing a team could put on the table - one
    player against one player is the only comparison this project can make, so it must
    be the max, never a min or a sum."""
    chips = [{"value": 3000}, {"value": 5000}, {"value": 800}]
    assert trade_targets.board._best_chip(chips)["value"] == 5000
    assert trade_targets.board._best_chip([]) is None


def test_the_cliff_case_turns_on_meaningful_runway_and_a_real_tilt():
    """Its own docstring's example, previously unpinned: a 1.2-year starter reads `prime`
    by bucket AND sits past the final-year clock, so any bar tighter than
    MIN_MEANINGFUL_RUNWAY (2.0) quietly loses him. And a flat roster (40/40) is not
    tilting ascending - the tie means their window says nothing about his."""
    them = {"owner": "them", "ascending_pct": 40, "declining_pct": 10}
    receiver = {"name": "WR1", "is_starter": True, "years_to_decline": 1.2}
    assert trade_targets.counterparty._cliff_case(receiver, them, 0.9) is not None
    assert trade_targets.counterparty._cliff_case(
        {**receiver, "years_to_decline": 2.0}, them, 0.9) is None, (
        "at the bar is meaningful runway - no cliff to argue from")
    assert trade_targets.counterparty._cliff_case(
        receiver, {**them, "declining_pct": 40}, 0.9) is None


def test_a_cornerstone_is_in_the_pivot_sell_lists_tagged_by_direction():
    """He used to be filtered OUT of these lists, because `situational` was labelled "years
    still on them, just not your long-term core" and a cornerstone is the core by definition.
    The label was the problem, not his presence: the exclusion's own defence was that
    "`my_offers` and `value_upgrades` are the surfaces that name him instead", and a REBUILD
    result has neither key - so on a rebuilding roster he appeared in no sell surface at all.

    `case_sells_on_runway_not_age` is what caught it. Asked which of five QBs to trade, the
    agent could not weigh Jalen Hurts (4.0 years of runway) against Justin Herbert (5.6),
    because the tool never listed him - so it led with Jared Goff at 6.2.

    The tag differs by direction, which is the distinction `committed` already carries: a
    committed team is making a hard move, a middling team is making THE choice."""
    rock = {"name": "Rock", "position": "WR", "value": 6000, "redraft_value": 4000,
            "bucket": "prime", "years_to_decline": 5.0, "is_starter": True,
            "is_cornerstone": True}
    aging = {"name": "Aging", "position": "WR", "value": 3000, "redraft_value": 2800,
             "bucket": "declining", "years_to_decline": 0.5, "is_starter": True}
    me = {"sellable": [rock, aging], "owner_id": "me", "roster_id": 1}

    out = trade_targets._pivot_path(me, _board(thresholds={"WR": 0}))
    assert [e["name"] for e in out["sell_candidates"]] == ["Aging"]
    assert [e["name"] for e in out["situational"]] == ["Rock"], "the core must be surfaced"
    assert [f["flavor"] for f in out["situational"][0]["friction"]] == ["cornerstone"]
    assert not out["sell_candidates"][0].get("friction"), "an ordinary sell carries none"
    assert "hardest ask" in out["situational"][0]["friction"][0]["why"], (
        "a team that has picked its direction is being told this is a hard move")

    undecided = trade_targets._pivot_path(me, _board(thresholds={"WR": 0}), committed=False)
    assert "IS the choice" in undecided["situational"][0]["friction"][0]["why"], (
        "for a middling team converting the core is not one move among others, it is the "
        "decision - which is the thing most worth surfacing to them")


def test_the_situational_block_ships_its_note_and_the_note_carries_the_runway_rule():
    """`situational` was the ONE block without a note - the CLI printed a header the agent
    never saw, and the runway-picks-the-sale rule lived only in a tool docstring. Measured
    consequence: asked which of five QBs a rebuilder should trade, the agent failed to weigh
    the short-runway cornerstone on 6 of 6 runs, every time leading with the easier stranded
    sale instead. The rule has to ride at the entries it governs."""
    rock = {"name": "Rock", "position": "WR", "value": 6000, "redraft_value": 4000,
            "bucket": "prime", "years_to_decline": 5.0, "is_starter": True,
            "is_cornerstone": True}
    me = {"sellable": [rock], "owner_id": "me", "roster_id": 1}
    out = trade_targets._pivot_path(me, _board(thresholds={"WR": 0}))
    assert "years_to_decline picks the sale" in out["situational_note"]
    assert "age never does" in out["situational_note"]
    assert "does not answer that comparison" in out["situational_note"], (
        "the note must forbid settling the question with an easier sale elsewhere")


def test_a_starter_out_runwayed_by_his_own_bench_carries_the_inversion():
    """Round two of the same eval failure: with the rule in the note, six of six runs
    applied keep-the-years WITHIN the bench and never asked whether the starter was the
    real sale. The roster's own counterexample now rides on the starter's entry - the
    model repeats an attached fact far more reliably than it extends an instruction.
    Fires only on a real inversion: bench piece with the most years vs starter with the
    fewest, both named with their numbers."""
    starter = {"name": "ShortStarter", "position": "QB", "value": 5000,
               "redraft_value": 6000, "bucket": "prime", "years_to_decline": 4.0,
               "is_starter": True}
    bench = {"name": "LongBench", "position": "QB", "value": 3000,
             "redraft_value": 4000, "bucket": "prime", "years_to_decline": 6.1,
             "is_starter": False}
    me = {"sellable": [starter, bench], "owner_id": "me", "roster_id": 1}
    out = trade_targets._pivot_path(me, _board(thresholds={"QB": 0}))
    tagged = {e["name"]: e.get("runway_inversion") for e in out["situational"]}
    assert tagged["LongBench"] is None, "the inversion is a fact about the STARTER"
    note = tagged["ShortStarter"]
    assert "4.0" in note and "6.1" in note and "LongBench" in note
    assert "sells ShortStarter and keeps LongBench" in note

    # No inversion, no tag: bench piece with fewer years than the starter is the
    # ordinary case the sell lists already order correctly.
    ordinary = {**bench, "years_to_decline": 2.5}
    out = trade_targets._pivot_path({"sellable": [starter, ordinary], "owner_id": "me",
                                     "roster_id": 1}, _board(thresholds={"QB": 0}))
    assert not any(e.get("runway_inversion") for e in out["situational"])


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

    pushing = trade_targets._my_offer_pool({**roster, "window": "Push"}, _board(thresholds=thresholds),
                                           needs={}, covered=covered)
    contending = trade_targets._my_offer_pool({**roster, "window": "Contend"}, _board(thresholds=thresholds),
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


def test_stranded_production_needs_a_real_margin_not_just_a_blocked_slot():
    """The miss that a live rebuilding roster exposed: four startable QBs in superflex, two
    QB-capable slots, and the QB3 producing 4,880 sat on the bench while a receiver producing
    420 started. Every number was already computed; nothing put them side by side.

    But `stranded` is a MAGNITUDE statement, not a capacity one - the lineup is picked
    optimally, so every bench player is blocked by something and "he'd start if a slot
    allowed" separates nothing. Reading it as capacity told a live roster its 944 receiver
    was the most valuable thing it couldn't use, on the strength of clearing a 643 RB sitting
    in a dedicated slot the receiver could never occupy.

    The marginal man can only arise that way, since a bench player above the weakest starter
    in a slot he IS eligible for would simply be starting. `RB1` is that dedicated-slot
    starter here and `WR3` is that man: past him, nowhere near double him, ordinary depth,
    and he must not appear beside a QB producing multiples of the lineup floor."""
    slots, flex = {"QB": 1, "RB": 1, "WR": 1}, [("WR", "TE")]
    players = {
        "qb1": {"name": "QB1", "position": "QB", "redraft_value": 900, "value": 900},
        "qb2": {"name": "QB2", "position": "QB", "redraft_value": 800, "value": 800},
        "rb1": {"name": "RB1", "position": "RB", "redraft_value": 60, "value": 60},
        "wr1": {"name": "WR1", "position": "WR", "redraft_value": 300, "value": 300},
        "wr2": {"name": "WR2", "position": "WR", "redraft_value": 200, "value": 200},
        "wr3": {"name": "WR3", "position": "WR", "redraft_value": 100, "value": 100},
    }
    roster = {"players": list(players)}
    starters = roster_needs.projected_starters(roster, players, slots, flex)
    assert players[roster_needs.weakest_starter(players, starters)]["name"] == "RB1"
    stranded = roster_needs.stranded_starters(roster, players, starters)
    assert [players[p]["name"] for p in stranded] == ["QB2"], (
        "QB2 produces 13x the weakest starter with both QB slots held by someone better; "
        "WR3 clears that starter's 60 without doubling it, so he is depth rather than "
        "stranded value - the live Metcalf-behind-Gainwell case")


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
    ascend = {"mode": "middling",
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
    need = {"level": "critical", "weakest_starter": 0, "note": "", "rank": 12, "of": 12, "startable": 0, "slots": 1}
    out = trade_targets._buy_path(me, _board(None, [seller], needs_by_owner_id={"me": {"WR": need}},
                                       thresholds=thresholds), max_per_position=5)
    assert [t["name"] for t in out["targets"]][0] == "AgingGuy", \
        "declining (production-priced) should outrank higher-value prime for a Win-Now buyer"


def test_unreachable_targets_are_split_out_rather_than_ranked_below():
    """One ranked list made attainability compete with quality for the same ordering, and
    quality won by design. Live: a critical RB list read Gibbs (a cornerstone priced above the
    roster's best chip), then three names from an owner who has never traded, and only then
    Jaylen Warren - the one realistic call on the board. "Who do I ring first" is a different
    question from "who is best", so it gets its own list, and the cap applies per half so a
    blocked target can never displace a reachable one."""
    me = {"owner_id": "me", "window": "Push", "tradeable_surplus": [],
          "sellable": [{"name": "MyChip", "position": "WR", "value": 3000,
                        "redraft_value": 2800, "bucket": "prime", "is_starter": False}]}
    thresholds = {"RB": 100, "WR": 100}
    sellers = [
        {"owner_id": "active", "owner": "active", "window": "Rebuild", "sellable": [
            {"name": "Gettable", "position": "RB", "value": 2000, "redraft_value": 2000,
             "bucket": "declining", "is_starter": False},
            {"name": "TheirRock", "position": "RB", "value": 2200, "redraft_value": 4000,
             "bucket": "prime", "is_starter": True, "is_cornerstone": True},
            {"name": "Unaffordable", "position": "RB", "value": 9000, "redraft_value": 5000,
             "bucket": "prime", "is_starter": False},
        ]},
        {"owner_id": "quiet", "owner": "quiet", "window": "Rebuild", "sellable": [
            {"name": "QuietGuy", "position": "RB", "value": 1900, "redraft_value": 3000,
             "bucket": "declining", "is_starter": False},
        ]},
    ]
    need = {"level": "critical", "weakest_starter": 0, "note": "", "rank": 12, "of": 12, "startable": 0, "slots": 1}
    out = trade_targets._buy_path(me, _board(None, sellers, needs_by_owner_id={"me": {"RB": need}},
                                       thresholds=thresholds, trade_counts={"active": 4}),
                            max_per_position=5)

    assert [t["name"] for t in out["targets"]] == ["Gettable"], (
        "only the one with nothing structural in the way belongs on the buy list")
    blocked = {t["name"]: [f["why"] for f in t["friction"]] for t in out["long_shots"]}
    assert set(blocked) == {"TheirRock", "Unaffordable", "QuietGuy"}
    # QuietGuy outproduces Gettable (3,000 vs 2,000), so under one ranked list he would have
    # led - which is the bug. The reason he is blocked has to be stated, not implied by order.
    assert "never made a trade" in blocked["QuietGuy"][0]
    assert "cornerstone" in blocked["TheirRock"][0]
    assert "biggest single chip (MyChip, 3,000)" in blocked["Unaffordable"][0]


def test_no_trade_history_anywhere_does_not_block_every_target():
    """A zero trade count only says something about an owner when SOMEBODY ELSE in the league
    has traded. In a fresh league it describes the league, and treating it as friction would
    empty the buy list for all twelve teams at once.

    `never_trades` is only ever a fact about a counterparty - the asking team's own history
    says nothing about whether it can sell, and the app's user may well have never traded. So
    my own trades don't make another owner's zero meaningful either: the third case here is a
    league where I am the only person who has ever traded."""
    me = {"owner_id": "me", "window": "Push", "sellable": [], "tradeable_surplus": []}
    seller = {"owner_id": "them", "owner": "them", "window": "Rebuild", "sellable": [
        {"name": "Available", "position": "RB", "value": 2000, "redraft_value": 2000,
         "bucket": "declining", "is_starter": False}]}
    need = {"level": "critical", "weakest_starter": 0, "note": "", "rank": 12, "of": 12, "startable": 0, "slots": 1}

    def run(trade_counts):
        return trade_targets._buy_path(me, _board(None, [seller], needs_by_owner_id={"me": {"RB": need}},
                                       thresholds={"RB": 100}, trade_counts=trade_counts),
                            max_per_position=5)

    nobody = run({})
    assert [t["name"] for t in nobody["targets"]] == ["Available"]
    assert not nobody.get("long_shots")

    only_me = run({"me": 6})
    assert [t["name"] for t in only_me["targets"]] == ["Available"], (
        "my own trading does not make their zero informative")

    someone_else = run({"me": 6, "third": 2})
    assert not someone_else.get("targets"), "now the zero means something"
    assert [f["flavor"] for f in someone_else["long_shots"][0]["friction"]] == ["never_trades"]


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
    offers = trade_targets._my_offer_pool(me, _board(thresholds=thresholds), needs={})
    by_name = {e["name"]: e for e in offers}
    assert by_name["Real"]["tier"].startswith("core piece")
    assert by_name["Filler"]["tier"].startswith("depth")
    assert by_name["Real"]["value_over_replacement"] > 0 > by_name["Filler"]["value_over_replacement"]
    assert offers[0]["name"] == "Real", "core pieces must lead the list"


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
    out = trade_targets._pivot_path(states[0], _board(None, states, trade_counts={"win": 5},
                                                picks_by_owner=picks))
    owners = [p["from_owner"] for p in out["picks_to_acquire"]]
    assert owners == ["Contender"], "only contenders' picks are realistic targets"


def test_rebuilder_pick_targets_are_empty_without_pick_data():
    """No pick data must mean no suggestions, not an empty-looking key that reads as
    'this team has no picks available'."""
    me = {"owner_id": "me", "roster_id": 1, "owner": "me",
          "window": "Rebuild", "sellable": [], "tradeable_surplus": []}
    out = trade_targets._pivot_path(me, _board(None, [me]))
    assert "picks_to_acquire" not in out


def test_offer_pool_never_includes_a_position_the_team_needs():
    """Trading a WR while WR is your own need just moves the shortage. Real bug this
    guards: a Win-Now team with a critical WR need was told to offer its WRs."""
    me = {"sellable": [{"name": "W", "position": "WR", "value": 900, "is_starter": False, "bucket": "prime"}],
          "tradeable_surplus": []}
    thresholds = {"WR": 100}
    assert trade_targets._my_offer_pool(me, _board(thresholds=thresholds), needs={}) != []
    assert trade_targets._my_offer_pool(me, _board(thresholds=thresholds), needs={"WR": "critical"}) == []


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

def _holder(owner, window, trajectory, players, asc=20, dec=30, traj_rank=9, of_teams=12):
    """`trajectory_rank`/`of_teams` are on every real `classify_league` row. The seller case
    quotes them because "falling" is a league tertile, not an absolute: a live team with 24%
    declining against 22% ascending was told its roster was falling on the strength of a
    two-point gap."""
    return {"owner_id": owner, "owner": owner, "roster_id": 1, "window": window,
            "trajectory": trajectory, "ascending_pct": asc, "declining_pct": dec,
            "trajectory_rank": traj_rank, "of_teams": of_teams,
            "sellable": players, "tradeable_surplus": []}


def _aging(name, value, redraft, pos="RB", runway=-1.0):
    """`years_to_decline` is on every real `classify` entry and was missing here, which let the
    persuasion gate's runway branch pass unconditionally (a missing value reads as 0, i.e. on a
    clock). Third fixture this session found to be thinner than production data."""
    return {"name": name, "position": pos, "value": value, "redraft_value": redraft,
            "bucket": "declining", "is_starter": True, "years_to_decline": runway}


def _prior(finish, champion=False, made_playoffs=True, continuity=1.0):
    return {"season": "2025", "finish": finish, "wins": 10, "losses": 4,
            "points_for": 2000.0, "champion": champion, "made_playoffs": made_playoffs,
            "continuity": continuity, "describes_this_team": continuity >= 0.6,
            "note": f"2025: finished {finish}."}


# The real p90 of redraft/dynasty per position, measured on both live leagues. The
# spread is the point: an absolute bar that looks strict for RB is unreachable for TE.
BARS = {"QB": 1.31, "RB": 1.05, "WR": 0.89, "TE": 0.81}

ME = {"owner_id": "me", "owner": "Me", "window": "Push"}
NEED_RB = {"RB": {"level": "critical", "weakest_starter": 0, "note": "", "rank": 10, "of": 12, "startable": 0, "slots": 1}}


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
        ME, _board(states=[holder], thresholds={"RB": 100},
                   prior={"aging": _prior(1, champion=True)}, premium_bars=BARS), NEED_RB)
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
        ME, _board(states=[holder], thresholds={"RB": 100},
                   prior={"young": _prior(3)}, premium_bars=BARS), NEED_RB)
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
        ME, _board(states=[holder], thresholds={"RB": 100},
                   prior={"young": _prior(3)}, premium_bars=BARS), NEED_RB)
    assert out == []


def test_persuasion_bar_is_relative_to_the_players_own_position():
    """The bug this replaced, stated as a test. Dynasty and redraft are unnormalized scales
    whose relationship differs sharply by position: measured p90s are RB 1.05 but TE 0.81,
    and the *entire* TE pool tops out at 1.01. The old absolute 1.0 bar therefore excluded
    every tight end in every league while looking like an ordinary strictness setting.

    Real pair, identical ratio, opposite verdicts: a 36.9-year-old TE at 0.83 is top-decile
    now-weighted for a TE, while an RB at the same 0.83 is unremarkable.

    The bar decides one CLAUSE now, not membership. Used as a gate it made whether an owner had
    any reason at all to move a player turn on the fourth decimal place - DeVonta Smith's 0.8790
    against a WR bar of 0.8790 - so the mismatch argument stands on the runway and says outright
    whether a discount is there to harvest. The per-position distinction is what must survive
    that move, so it is asserted on the wording rather than on the list."""
    te = _aging("Kelce", 1810, 1504, pos="TE")   # 0.83 - clears the 0.81 TE bar
    rb = _aging("Jacobs", 2770, 2300)            # 0.83 - misses the 1.05 RB bar
    needs = {"RB": NEED_RB["RB"], "TE": NEED_RB["RB"]}
    holder = _holder("kk", "Contend", "rising", [te, rb], asc=30, dec=15)
    out = trade_targets._persuasion_targets(
        ME, _board(states=[holder], thresholds={"RB": 100, "TE": 100},
                   prior={"kk": _prior(3)}, premium_bars=BARS), needs)
    why = {t["name"]: t["why_they_might_listen"] for t in out}
    assert set(why) == {"Kelce", "Jacobs"}, "both are window mismatches on a rising holder"
    assert "priced as though his remaining years are gone" in why["Kelce"], (
        "0.83 is top-decile now-weighted for a TE - there is a discount to harvest")
    assert "not discounted for it" in why["Jacobs"], (
        "the same 0.83 is unremarkable for an RB, and the sentence must not claim otherwise")


def test_a_rising_middling_team_sells_its_age_and_not_its_youth():
    """Seller-ness is a property of the (owner, player) pair, and treating it as a team fact hid
    the best target on the board: James Cook, 6,027 of production at 1.21x production per unit of
    cost, sat behind a `window == "Rebuild"` test because kbmckenna is Middling - while being 0.1
    years from his cutoff on a roster 47% ascending against 0% declining.

    The clock is what keeps it honest. Without it this would offer up the very young core a
    rising team is accumulating."""
    rising = {"owner_id": "kb", "owner": "kb", "window": "Middling", "trajectory": "rising"}
    aging = {"name": "Cook", "years_to_decline": 0.1}
    young = {"name": "Kid", "years_to_decline": 5.0}
    assert trade_targets._sells_him(rising, aging), "his window is not the one they're building for"
    assert not trade_targets._sells_him(rising, young), "the young core is emphatically not for sale"

    steady = {**rising, "trajectory": "steady"}
    assert not trade_targets._sells_him(steady, aging), (
        "only a RISING middling team is accumulating seasons this player won't be there for")

    rebuilding = {"owner_id": "r", "owner": "r", "window": "Rebuild", "trajectory": "steady"}
    assert trade_targets._sells_him(rebuilding, young), "a rebuilder is selling everything"


def test_a_persuasion_target_has_to_beat_who_you_already_start():
    """The weakest-starter floor used to apply only to `weak` needs, on the theory that at a
    critical one any body helps. Bodies are what `depth_adds` is for, at a nominal price and no
    persuasion - so a critical need was asking an owner to change direction for players who would
    not crack the lineup. Live, under a note promising "aging production the market discounts":
    Tyrone Tracy at 255 against a weakest RB starter of 638, 0.18x production per unit of cost,
    which is the market pricing him far ABOVE what he produces."""
    need = {"level": "critical", "weakest_starter": 638, "note": "", "rank": 12, "of": 12, "startable": 0, "slots": 1}
    holder = _holder("kk", "Push", "falling", [_aging("Tracy", 1386, 255),
                                               _aging("Harvey", 2032, 865)], traj_rank=11)
    out = trade_targets._persuasion_targets(
        ME, _board(states=[holder], thresholds={"RB": 100},
                   prior={"kk": _prior(9, made_playoffs=False)}, premium_bars=BARS), {"RB": need})
    assert [t["name"] for t in out] == ["Harvey"], (
        "255 does not beat the 638 already starting there; 865 does")


def test_one_definition_of_needs_a_pivot_across_both_blocks():
    """`needs_a_pivot` had two definitions in one module: the persuasion tier meant "no hole of
    theirs I can fill", `_why_they_would_move_him` meant "not a rebuilder". They then printed
    opposite verdicts about the same player in the same run - Travis Kelce on Paulyt101 read
    "nearer a fit than a pitch" in one block and "asks them to change direction" in the other.

    `_counterparty_fit`'s `fills_a_hole` is the single test. Asserted on the function that used
    to invent its own answer, both ways round."""
    other = {"owner_id": "kk", "owner": "kk", "window": "Contend", "trajectory": "falling",
             "trajectory_rank": 11, "of_teams": 12, "ascending_pct": 20, "declining_pct": 30}
    player = _aging("Vet", 3000, 4000)
    fits = trade_targets._why_they_would_move_him(player, other, None, BARS, fills_a_hole=True)
    pivots = trade_targets._why_they_would_move_him(player, other, None, BARS, fills_a_hole=False)
    assert [f["flavor"] for f in fits["friction"]] == [], (
        "an owner with a hole this roster can fill is taking a trade that serves his own plan")
    assert [f["flavor"] for f in pivots["friction"]] == ["holds_to_win"], (
        "this holder is a contender, so no-hole friction is holds_to_win, not a pivot")
    assert fits["their_reason"] == pivots["their_reason"], (
        "why they'd listen is a fact about their team and must not move with the pivot flavor")


def test_the_mixed_price_basis_does_not_claim_to_be_upside():
    """value_basis says "mixed" for a prime player and the CLI phrasebook rendered that as
    "upside-priced" - a sentence claiming a different classification than the one computed
    (the owner, of two prime receivers: "I don't think Smith and Waddle are necessarily
    upside priced"). Each phrase may only describe its own basis."""
    from analysis.trade_targets import report
    assert "upside" not in report.BUY_PRICE_NOTE["mixed"]
    assert "production" in report.BUY_PRICE_NOTE["production"]
    assert "future" in report.BUY_PRICE_NOTE["upside"]


def test_a_contender_with_no_hole_holds_to_win_rather_than_needing_a_pivot():
    """The owner's read of a live report, and the second time he said it: a #1 lineup
    carrying [needs_a_pivot] on A.J. Brown - "that team is just nasty and competing. I
    would not say it needs a pivot... could sell some aging people and still be winning,
    but probably hangs onto them to win now." The no-hole ask has three shapes, split by
    the seller's window: a fit (has a hole), a pivot (mid-table, a direction to change),
    and holds-to-win (a contender, where 'change direction' was never the honest claim)."""
    contender = {"owner_id": "kk", "owner": "kk", "window": "Contend", "trajectory": "steady",
                 "ascending_pct": 37, "declining_pct": 12}
    middling = {**contender, "window": "Middling"}
    holds = trade_targets._why_they_would_move_him(_aging("Vet", 3000, 4000), contender,
                                                   None, BARS, fills_a_hole=False)
    pivot = trade_targets._why_they_would_move_him(_aging("Vet", 3000, 4000), middling,
                                                   None, BARS, fills_a_hole=False)
    assert holds["friction"][0]["flavor"] == "holds_to_win"
    assert "not to change direction" in holds["friction"][0]["why"]
    assert "stay a contender" in holds["friction"][0]["why"]
    assert pivot["friction"][0]["flavor"] == "needs_a_pivot"
    assert "change direction" in pivot["friction"][0]["why"]

    # And the persuasion tier says the same thing about the same seller: cost_note carries
    # the holds-to-win price and the entry still prices it ("not currently a seller").
    holder = _holder("kk", "Contend", "steady", [_aging("Stud", 4000, 6000)], asc=26, dec=16)
    out = trade_targets._persuasion_targets(
        ME, _board(states=[holder], thresholds={"RB": 100},
                   prior={"kk": _prior(3)}, premium_bars=BARS), NEED_RB)
    assert "probably holds" in out[0]["cost_note"] and "not currently a seller" in out[0]["cost_note"]
    assert out[0]["seller_window"] == "Contend", "the CLI marker derives from this, not a guess"
    assert [f["flavor"] for f in out[0]["friction"]] == ["holds_to_win"]


def test_persuasion_includes_a_falling_contender_that_has_not_won():
    """The mirror: same window, same kind of asset, but the roster is aging out and the
    core has not delivered. That team has a real reason to listen."""
    falling = _holder("kk", "Push", "falling", [_aging("Aging", 4000, 6000)], traj_rank=11)
    out = trade_targets._persuasion_targets(
        ME, _board(states=[falling], thresholds={"RB": 100},
                   prior={"kk": _prior(9, made_playoffs=False)}, premium_bars=BARS), NEED_RB)
    assert [t["name"] for t in out] == ["Aging"]
    why = out[0]["why_they_might_listen"]
    assert "11 of 12 on trajectory" in why and "hasn't delivered" in why, (
        "the decline argument must quote its league-relative rank, because 'falling' is a "
        "tertile - it once asserted a falling roster off a two-point gap")
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
        short_at_qb, {"QB": {"level": "critical", "startable": 1, "slots": 2}}, offers)
    assert fit["offer_any_one_of"] == ["SpareQB"] and "critical need at QB" in fit["why_it_fits"]

    # No hole, but rising while starting aging players - wants now-and-later value.
    rising = _holder("rising", "Contend", "steady", [], asc=26, dec=16)
    fit = trade_targets._counterparty_fit(rising, {}, offers)
    assert fit["offer_any_one_of"] == ["SpareQB", "AboutToTurn"], (
        "runway RANKS this pool and no longer empties it - the long-runway piece leads, the "
        "0.3-year one is still a real offer, and only a player past his own cliff is dropped. "
        "Filler is excluded on being below replacement, not on his runway")
    assert "no positional hole" in fit["why_it_fits"]
    assert "0.3 years from his own decline cutoff" in fit["why_it_fits"], (
        "the bar decides what the sentence CLAIMS: with a 0.3-runway piece in the offer it must "
        "not promise value 'still there in two', which that player's own number contradicts")

    # Everything offered clears the bar, so the strong claim is the honest one.
    fit = trade_targets._counterparty_fit(rising, {}, offers[:1])
    assert "the trade he should be making anyway" in fit["why_it_fits"]

    # Aging into its own window with nothing to fill: no obvious fit, and say so.
    assert trade_targets._counterparty_fit(
        _holder("aging", "Contend", "steady", [], asc=21, dec=23), {}, offers) is None


def test_a_buy_target_says_what_that_owner_would_take_back():
    """Half the trade was missing from the buy block: it named who to ring and, in a
    separate list, everything this team could give up - with no join. The reader had to
    invent the pairing, and a live answer offered a 0.3-runway 28-year-old WR to a
    55%-ascending team ("buttboi would not want DK Metcalf"). `_counterparty_fit` already
    answered this for the persuasion tier; it just never ran here.

    Two filters, both mirroring rules that already exist elsewhere: a team ACCUMULATING is
    not offered a piece inside its final year (the same `INSIDE_FINAL_YEAR` clock
    `_sells_him` uses to say that team is SELLING such pieces), and no piece worth wildly
    more than the target is proposed (the give-side mirror of `beyond_your_best_chip`,
    which stopped a 7,321 cornerstone being offered for a 2,006 back)."""
    aging = {"name": "Aging", "position": "WR", "value": 1900, "redraft_value": 900,
             "bucket": "prime", "years_to_decline": 0.3, "value_over_replacement": 100}
    young = {"name": "Young", "position": "WR", "value": 2000, "redraft_value": 800,
             "bucket": "ascending", "years_to_decline": 6.0, "value_over_replacement": 200}
    star = {"name": "Star", "position": "WR", "value": 9000, "redraft_value": 8000,
            "bucket": "prime", "years_to_decline": 5.0, "value_over_replacement": 5000}
    needs = {"WR": {"level": "weak", "startable": 2, "slots": 2}}
    target = {"name": "Wanted", "position": "RB", "value": 2500}

    rising = _holder("rising", "Middling", "rising", [], asc=55, dec=8)
    fit = trade_targets._counterparty_fit(rising, needs, [aging, young], target=target)
    assert fit["offer_any_one_of"] == ["Young"], (
        "a team accumulating youth is being sent the piece it would itself be selling")
    assert "accumulating" in fit["why_it_fits"]

    # The same aging piece is a fine offer to a team that is NOT accumulating.
    falling = _holder("falling", "Middling", "falling", [], asc=8, dec=40)
    assert "Aging" in trade_targets._counterparty_fit(
        falling, needs, [aging, young], target=target)["offer_any_one_of"]

    # Over-ceiling pieces are NAMED with the different-trade sentence, never silently
    # dropped: Jalen Hurts missed a hard 1.5x line by 13 units (0.25%) and "why Goff
    # instead of Hurts?" had no answer in the payload. The bar picks the sentence.
    fit = trade_targets._counterparty_fit(falling, needs, [aging, young, star], target=target)
    assert "Star" not in fit["offer_any_one_of"]
    assert "Star (9,000, 5.0 yrs)" in fit["why_it_fits"], (
        "the bigger piece carries its runway, so keep-the-years connects to it")
    assert "sale in its own right" in fit["why_it_fits"]
    assert "Star" in trade_targets._counterparty_fit(
        falling, needs, [aging, young, star], target={"value": 8000})["offer_any_one_of"]

    # Parity opens spend the FEWEST years that get the deal done - keep-the-years is the
    # seller's half of the same doctrine, and pool order led with the 6.1-year piece.
    assert fit["offer_any_one_of"] == ["Aging", "Young"], (
        "shortest runway first among fitting pieces (0.3 before 6.0)")


def test_a_named_player_gets_an_answer_not_a_verdict_about_list_membership():
    """The first friend-tester's actual complaint: "how do I trade for Rashee Rice" got
    "Rice isn't a trade target", repeatedly - absence from a ranked, capped, need-filtered
    list read back as unavailability. Absence has five meanings and none of them is that.
    `player_outlook` answers about the NAMED player: who owns him, whether that owner
    sells, and which single pieces of mine that owner would want back. Live, the real
    answer landed on the friend's own plan (Fitz sells Rice; send back Goff or Darnold).
    """
    players = {
        "rice": {"name": "Rice", "position": "WR", "value": 3500, "redraft_value": 2600,
                 "age": 26.3},
        "goff": {"name": "Goff", "position": "QB", "value": 3400, "redraft_value": 4600,
                 "age": 31.8},
    }
    rosters = [{"owner_id": "fitz", "players": ["rice"]},
               {"owner_id": "me", "players": ["goff"]}]
    ctx = _Ctx(players, rosters, {"fitz": {"rice"}, "me": set()})
    ctx.pick_owner = lambda q, rows: next(r for r in rows if q in r["owner"])
    rice_entry = {"name": "Rice", "position": "WR", "value": 3500, "redraft_value": 2600,
                  "bucket": "prime", "years_to_decline": 2.7, "is_starter": True}
    fitz = {"owner_id": "fitz", "owner": "fitz", "window": "Rebuild", "state": "Rebuilding",
            "flavor": "stalled", "window_note": "n", "trajectory": "steady",
            "ascending_pct": 26, "declining_pct": 8,
            "sellable": [rice_entry], "tradeable_surplus": []}
    me = {"owner_id": "me", "owner": "me", "window": "Rebuild", "state": "Rebuilding",
          "flavor": "convertible", "window_note": "n", "trajectory": "steady",
          "ascending_pct": 23, "declining_pct": 2,
          "sellable": [{"name": "Goff", "position": "QB", "value": 3400,
                        "redraft_value": 4600, "bucket": "prime", "years_to_decline": 6.1,
                        "is_starter": False}],
          "tradeable_surplus": []}
    board = _board(ctx, [fitz, me],
                   needs_by_owner_id={"fitz": {"QB": {"level": "critical", "startable": 1,
                                                      "slots": 2}}},
                   thresholds={"QB": 0, "WR": 0}, trade_counts={"fitz": 2, "me": 1})

    out = trade_targets.outlook_from_board(board, "rice", "me")
    assert out["found"] and out["owner"] == "fitz"
    assert "seller of exactly this kind of piece" in out["availability"], (
        "a Rebuild owner sells - the call is a price conversation, not a persuasion")
    assert out["your_fit"]["offer_any_one_of"] == ["Goff"], (
        "the fit names MY single pieces that fill THEIR hole")
    assert "not a bundle" not in out["availability"], "the no-bundle rule lives in the note"

    # A player nobody in the data matches is said plainly, with candidates on ambiguity.
    assert trade_targets.outlook_from_board(board, "zzz")["found"] is False
    # Asking about your own player redirects instead of pretending there is a call to make.
    assert trade_targets.outlook_from_board(board, "goff", "me").get("already_yours")


def test_the_package_tripwire_fires_on_real_bundles_and_not_on_real_alternatives():
    """The detector for `case_never_builds_a_package`, checked against the actual live
    answers rather than invented strings - because two earlier versions were confidently
    wrong in OPPOSITE directions (a false pass on a real bundle, then false failures on
    two correct answers), and each wrong version cost a live API call to discover.

    Offline, so the tripwire itself is verified for free and can never go vacuous."""
    from agent.evals import packaged_pieces

    mine = {"Harold Fannin", "Tyler Shough", "DK Metcalf", "Jalen Nailor"}

    # The defect, verbatim from the answer Caleb caught on his phone.
    assert packaged_pieces(
        "**Offer:** Harold Fannin (3,650) + Tyler Shough (3,379). This serves both sides.",
        mine), "the original bundle must still be caught"
    assert packaged_pieces("Offer for Barkley: Tyler Shough and DK Metcalf.", mine)

    # Correct behaviour that earlier versions failed: alternatives, and one piece each to
    # two different targets.
    assert not packaged_pieces(
        "Offer: **Tyler Shough** (or **DK Metcalf** if they've heard about Shough).", mine)
    assert not packaged_pieces(
        "Target 2: Jaylen Warren. Offer: DK Metcalf (1,985).\n"
        "Target 3: Kyren Williams. Offer: Tyler Shough (3,378).", mine)
    assert not packaged_pieces("You could offer Harold Fannin, DK Metcalf, or Jalen Nailor "
                               "- one of them, not several.", mine)


def test_every_list_of_offerable_pieces_says_it_is_not_a_package():
    """A bare list of names invites the one construction this project forbids, and a live
    answer built it: "Offer: Harold Fannin (3,650) + Tyler Shough (3,379)" against a 4,473
    target - a priced bundle whose halves were three ALTERNATIVES the tool had listed. The
    rule was already in the system prompt twice (principle D and rule 8) and still leaked,
    so it now lives in the data: the field is NAMED for what it is, and both list sites
    carry the no-bundle sentence."""
    from analysis.trade_targets import buy, counterparty

    offers = [{"name": "SpareQB", "position": "QB", "redraft_value": 3349, "bucket": "prime",
               "years_to_decline": 7.1, "value_over_replacement": 700}]
    fit = trade_targets._counterparty_fit(
        _holder("needy", "Push", "falling", [], asc=10, dec=30),
        {"QB": {"level": "critical", "startable": 1, "slots": 2}}, offers)
    assert "offer_any_one_of" in fit, "the key itself must say one-of, not a shopping list"

    for note in (counterparty.PERSUASION_NOTE, buy.MY_OFFERS_NOTE):
        assert "bundle" in note.lower() or "package" in note.lower(), (
            "the note has to name the forbidden construction, not imply it")
        assert "does not add across players" in note


def test_persuasion_ranks_by_production_per_cost_not_by_value():
    """The trap this exists to avoid. Ranking by dynasty value puts the *more valuable*
    player first, which is backwards for a win-now buyer: the cheaper name delivers more
    current production per unit paid, because the market discounts him for seasons a
    pushing team isn't buying. Modelled on the real pair - Barkley 3,746/5,081 (1.36x)
    against Taylor 5,240/6,649 (1.27x)."""
    holder = _holder("kk", "Push", "falling",
                     [_aging("Taylor", 5240, 6649), _aging("Barkley", 3746, 5081)])
    out = trade_targets._persuasion_targets(
        ME, _board(states=[holder], thresholds={"RB": 100},
                   prior={"kk": _prior(9, made_playoffs=False)}, premium_bars=BARS), NEED_RB)
    assert [t["name"] for t in out] == ["Barkley", "Taylor"], "cheaper but better ratio leads"
    assert out[0]["production_per_cost"] > out[1]["production_per_cost"]


def test_the_persuasion_tier_is_defined_by_runway_not_by_the_price_ratio():
    """It is "aging production held by a non-seller", and the project's canonical test for aging
    is the clock - the same `MIN_MEANINGFUL_RUNWAY` correction already made in `classify` and
    `_pivot_path`. Gating on the price ratio instead got it wrong in both directions on live
    data: Travis Etienne (27.6, runway -0.6, a +1,578 upgrade on the asking team's RB2) was
    unreachable at ratio 0.85 against a 1.05 bar, while dropping the bar admitted Bijan Robinson
    and Ashton Jeanty with 2.5 and 4.3 years of runway left.

    Both players here have a team reason to listen, so runway is the only thing separating them
    - and their ratios point the OPPOSITE way to the verdict, which is the whole point."""
    holder = _holder("kk", "Push", "falling", [
        _aging("OnAClock", 3000, 2000, runway=-0.6),       # ratio 0.67, well under the 1.05 bar
        _aging("YearsLeft", 3000, 4000, runway=3.0),       # ratio 1.33, comfortably over it
    ])
    out = trade_targets._persuasion_targets(
        ME, _board(states=[holder], thresholds={"RB": 100},
                   prior={"kk": _prior(9, made_playoffs=False)}, premium_bars=BARS), NEED_RB)
    assert [t["name"] for t in out] == ["OnAClock"], (
        "the clock decides, and the richer ratio does not rescue a player with years left")


def test_persuasion_ignores_last_season_when_the_roster_turned_over():
    """Last season's result is only allowed to speak about the roster that produced it.
    "This core hasn't won" is a real second reason to listen when the team still has that
    core, and meaningless once it has been torn down - so continuity gates the reason
    without changing whether the player surfaces at all."""
    holder = _holder("kk", "Push", "falling", [_aging("Stud", 3000, 4500)])
    intact = trade_targets._persuasion_targets(
        ME, _board(states=[holder], thresholds={"RB": 100},
                   prior={"kk": _prior(9, made_playoffs=False, continuity=1.0)}, premium_bars=BARS), NEED_RB)
    turned_over = trade_targets._persuasion_targets(
        ME, _board(states=[holder], thresholds={"RB": 100},
                   prior={"kk": _prior(9, made_playoffs=False, continuity=0.2)}, premium_bars=BARS), NEED_RB)
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
    floor = {"RB": 100}
    assert [c["name"] for c in
            trade_targets._conversion_candidates(rising, _board(premium_bars=BARS, thresholds=floor))] == ["Old Star"]
    assert trade_targets._conversion_candidates(aging, _board(premium_bars=BARS, thresholds=floor)) == []


def test_the_premium_bar_picks_the_conversion_sentence_not_the_list():
    """The mirror of the `_cliff_case` correction: other managers are told about a
    short-runway starter whether or not the market discounts him - the bar only changes
    which clause describes the price - so his own manager has to hear the same names.
    Before this, a starter at 0.75x (below the RB bar of 1.05) was pitched to the league
    and absent from his own report."""
    discounted = _aging("Sell High", 4000, 6000)          # 1.50x, over the 1.05 RB bar
    merely_mismatched = _aging("Wrong Window", 4000, 3000)  # 0.75x, under it
    me = _holder("rising", "Contend", "steady", [discounted, merely_mismatched],
                 asc=26, dec=16)
    out = trade_targets._conversion_candidates(me, _board(premium_bars=BARS, thresholds={"RB": 100}))
    notes = {c["name"]: c["note"] for c in out}
    assert set(notes) == {"Sell High", "Wrong Window"}
    assert "writing off the rest" in notes["Sell High"]
    assert "not discounted" in notes["Wrong Window"], \
        "an undiscounted price must not be described as a premium to harvest"


def test_conversion_candidates_apply_the_same_relevance_floor_as_the_persuasion_tier():
    """These are the names the persuasion tier shows the rest of the league, and it floors
    them on `clears_relevance_floor` first - a 300-value declining body is not a call anyone
    makes. The mirror has to use the same floor or the two lists disagree about who is worth
    ringing about."""
    fringe = _aging("Waiver Fodder", 300, 250)
    me = _holder("rising", "Contend", "steady", [fringe], asc=26, dec=16)
    assert trade_targets._conversion_candidates(me, _board(premium_bars=BARS, thresholds={"RB": 2000})) == []


def test_persuasion_never_searches_teams_that_are_already_sellers():
    """Rebuild teams are what the normal buy path covers. Including them here would
    double-list the same player under a framing that says it's a hard ask."""
    seller = _holder("reb", "Rebuild", "falling", [_aging("Cheap", 3000, 4500)])
    out = trade_targets._persuasion_targets(
        ME, _board(states=[seller], thresholds={"RB": 100},
                   prior={"reb": _prior(12, made_playoffs=False)}, premium_bars=BARS), NEED_RB)
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


# ------------------------------------------------------- pivot urgency by commitment

def test_a_middling_teams_sell_list_does_not_order_it_to_sell():
    """A live contradiction inside one report: mgibbons612's timing note said waiting for
    real results was legitimate, and four lines later the same output called eight aging
    starters `real urgency to move it`. `_pivot_path` serves both modes and its copy was
    written for the committed one. The runway is identical either way - what changes is
    whether it is an instruction or a deadline, so the distinction ships as
    `sell_clock_note` in the data rather than as printer-only wording the agent never sees."""
    kelce = {"name": "Travis Kelce", "position": "TE", "bucket": "declining",
             "value": 2000, "redraft_value": 1800, "years_to_decline": 0.5}
    me = {"sellable": [kelce], "owner_id": "me", "roster_id": 1}
    thresholds = {"TE": 0}

    committed = trade_targets._pivot_path(me, _board(thresholds=thresholds))
    assert committed["sell_candidates"] == [kelce]
    assert "real urgency to move it" in committed["sell_clock_note"]

    optional = trade_targets._pivot_path(me, _board(thresholds=thresholds), committed=False)
    assert optional["sell_candidates"] == [kelce], "the clock and the list are unchanged"
    assert "real urgency" not in optional["sell_clock_note"], \
        "told a team that may still wait that it has to sell now"
    assert "deadline" in optional["sell_clock_note"]


def test_a_data_gap_reaches_the_tool_result_not_only_stderr():
    """Graceful degradation that only reaches stderr is invisible to the person asking. Both
    nflverse call sites fall back rather than crash and both warn on stderr, which serves the
    author running the CLI and nobody else. It is not cosmetic: with usage roles missing every
    age curve falls back to its position default, and on a live roster that moved Jared Goff from
    6.2 years of runway to 2.1 - reversing which quarterback a rebuilding team should trade."""
    from sources import degraded
    degraded._MISSING.clear()
    assert degraded.note() is None, "nothing to say when every feed loaded"

    degraded.record("usage roles", "every age curve fell back to its position default")
    note = degraded.note()
    assert note and "usage roles" in note and "age curve" in note
    assert "say this in your answer" in note, (
        "the note is addressed to the model, because the reader can see it no other way")
    degraded._MISSING.clear()


# ------------------------------------------------------- trade evaluation (trade_eval)

from analysis import trade_eval


def _eval_fixture():
    """Two-team stub league (plus needs enough shape to assess). Team A has a spare QB;
    team B has no QB at all - the cleanest possible need to open and close."""
    players = {
        "qb1": {"name": "StarQB", "position": "QB", "value": 5000, "redraft_value": 4000, "age": 25},
        "qb2": {"name": "SpareQB", "position": "QB", "value": 2000, "redraft_value": 1500, "age": 26},
        "wr0": {"name": "OkWR", "position": "WR", "value": 900, "redraft_value": 500, "age": 24},
        "wr1": {"name": "BigWR", "position": "WR", "value": 6000, "redraft_value": 3000, "age": 23},
        "wr2": {"name": "SmallWR", "position": "WR", "value": 800, "redraft_value": 700, "age": 24},
        "rb1": {"name": "BackA", "position": "RB", "value": 1000, "redraft_value": 900, "age": 24},
        "rb2": {"name": "OldBack", "position": "RB", "value": 1000, "redraft_value": 900, "age": 28},
        "te1": {"name": "TightA", "position": "TE", "value": 700, "redraft_value": 600, "age": 25},
        "te2": {"name": "TightB", "position": "TE", "value": 700, "redraft_value": 600, "age": 25},
    }
    rosters = [{"owner_id": "a", "players": ["qb1", "qb2", "wr0", "rb1", "te1"]},
               {"owner_id": "b", "players": ["wr1", "wr2", "rb2", "te2"]}]
    ctx = _Ctx(players, rosters, {})
    ctx.needs_slots = {"QB": 1, "RB": 1, "WR": 1, "TE": 1}
    ctx.start_thresholds = {"QB": 0, "RB": 0, "WR": 0, "TE": 0}
    ctx.lineup_dedicated = ctx.needs_slots
    ctx.lineup_flex = []
    ctx.pick_owner = lambda q, rows: next(r for r in rows if q in r["owner"])
    states = [{"owner_id": "a", "owner": "a", "window": "Contend",
               "trajectory": "steady", "ascending_pct": 10, "declining_pct": 20},
              {"owner_id": "b", "owner": "b", "window": "Rebuild",
               "trajectory": "steady", "ascending_pct": 30, "declining_pct": 5}]
    return _board(ctx, states)


def test_a_trade_is_judged_per_side_never_priced():
    """SpareQB for BigWR: b closes its QB hole against the recomputed league bar, a gets
    the best single piece. No side carries a summed package price or a winner."""
    out = trade_eval.evaluate_from_board(_eval_fixture(), "a", ["spareqb"], "b", ["bigwr"])
    assert out["ok"] and out["best_piece"]["name"] == "BigWR" and out["best_piece"]["to"] == "a"
    a, b = out["sides"]
    assert any("closes the QB need (critical -> ok)" in r for r in b["read"])
    assert any("gets the best single piece" in r for r in a["read"])
    assert any("sends the best single piece" in r for r in b["read"])
    # The lineup delta comes from fill_lineup, not from the traded pieces' raw values:
    # BigWR (3000) replaces OkWR (500) in a's one WR slot; SpareQB never started for a.
    assert a["lineup_production_delta"] == 2500
    # No summed package price anywhere, and no read declares a winner.
    for side in out["sides"]:
        assert "total" not in side and "wins" not in " ".join(side["read"])


def test_a_rebuilder_taking_short_runway_production_is_flagged():
    """b (Rebuild) takes back OldBack-class runway: the read says those seasons won't be
    part of the next competitive team. The bar is the buyer's two-season horizon for a
    true Rebuild, not the seller's final-year clock."""
    # a (Contend, not accumulating) receiving OldBack: no flag - runway is his problem
    # only on a roster whose window is further out than the player's remaining seasons.
    out = trade_eval.evaluate_from_board(_eval_fixture(), "b", ["oldback"], "a", ["spareqb"])
    a_side = next(s for s in out["sides"] if s["owner"] == "a")
    assert not any("runway" in r for r in a_side["read"])

    # The real case: b (Rebuild) receives OldBack.
    board = _eval_fixture()
    board.ctx.rosters[0]["players"].append("rb2")
    board.ctx.rosters[1]["players"].remove("rb2")
    out2 = trade_eval.evaluate_from_board(board, "a", ["oldback"], "b", ["smallwr"])
    b_side = next(s for s in out2["sides"] if s["owner"] == "b")
    assert any("won't be part of it" in r for r in b_side["read"]), (
        "a Rebuild receiving a sub-two-season piece gets the horizon flag")


def test_trade_eval_says_plainly_when_a_name_is_not_on_the_roster():
    out = trade_eval.evaluate_from_board(_eval_fixture(), "a", ["ghost"], "b", ["bigwr"])
    assert out["ok"] is False and "'ghost' is not on a's roster" in out["problem"]


def test_picks_ride_as_pieces_with_the_right_timeline_reads():
    """A pick is the longest-dated asset there is: it must never trip the short-runway
    flag (no `years_to_decline` is not 0 years), and a win-now team taking one back is
    told the value pays after its window."""
    board = _eval_fixture()
    board.ctx.rosters[0]["roster_id"] = 1
    board.ctx.rosters[1]["roster_id"] = 2
    board.picks_by_owner = {
        1: [{"pick": "2027 1st (Early)", "value": 9000, "round": 1, "season": 2027,
             "originally": 1, "slot_basis": "expected early"}],
        2: [{"pick": "2026 1st (Mid)", "value": 3000, "round": 1, "season": 2026,
             "originally": 2, "slot_basis": "expected mid"}]}
    out = trade_eval.evaluate_from_board(board, "a", ["2027 1st"], "b", ["2026 1st"])
    assert out["ok"] and out["best_piece"]["name"] == "2027 1st (Early)"

    b_side = next(s for s in out["sides"] if s["owner"] == "b")
    assert not any("runway" in r for r in b_side["read"]), (
        "a Rebuild receiving a pick is buying exactly its own timeline - no flag")
    a_side = next(s for s in out["sides"] if s["owner"] == "a")
    assert any("value that pays after the window" in r for r in a_side["read"]), (
        "a Contend team taking back futures gets the mirror warning")
