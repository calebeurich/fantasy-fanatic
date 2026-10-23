"""The season simulator's playoff rules (analysis/season_sim.py), each read off the
crawled brackets. Plain fixtures, no network."""

from analysis import season_sim as ss

SEEDS = ["s1", "s2", "s3", "s4", "s5", "s6"]


def _upsets(*winners):
    """A `play` where the listed teams win every game they're in and seeds hold otherwise."""
    return lambda me, rnd: 100 if me in winners else -SEEDS.index(me)


def test_six_team_bracket_gives_the_top_two_seeds_byes_and_plays_3v6_4v5():
    champ, byes, losers = ss._bracket(SEEDS, reseed=False, play=_upsets())
    assert champ == "s1" and byes == ["s1", "s2"]
    assert losers == [["s5", "s6"], ["s4", "s3"], ["s2"]]


def test_fixed_bracket_sends_the_1_seed_to_the_4v5_winner():
    """seed_type 0: s6 upsets s3, but s1 still meets the 4/5 winner - s2 meets s6."""
    games = []
    def play(me, rnd):
        games.append((rnd, me))
        return 100 if me == "s6" and rnd == 0 else -SEEDS.index(me)
    ss._bracket(SEEDS, reseed=False, play=play)
    round2 = [me for rnd, me in games if rnd == 1]
    assert round2 == ["s1", "s4", "s2", "s6"]


def test_reseeding_sends_the_1_seed_to_the_lowest_seed_left():
    """seed_type 1: s6 upsets s3, so s1 plays s6 and s2 plays the 4/5 winner."""
    games = []
    def play(me, rnd):
        games.append((rnd, me))
        return 100 if me == "s6" and rnd == 0 else -SEEDS.index(me)
    champ, _, _ = ss._bracket(SEEDS, reseed=True, play=play)
    round2 = [me for rnd, me in games if rnd == 1]
    assert round2[:2] == ["s1", "s6"] and champ == "s1"


def test_division_leaders_take_the_top_seeds_even_on_a_worse_record():
    teams = [{"owner_id": o, "division": d} for o, d in
             (("a", 1), ("b", 1), ("c", 1), ("d", 2))]
    wins = {"a": 10, "b": 9, "c": 8, "d": 6}
    pf = dict.fromkeys(wins, 0)
    assert ss._seed(teams, wins, pf, {"divisions": 2})[:2] == ["a", "d"]
    assert ss._seed(teams, wins, pf, {})[:2] == ["a", "b"]


def test_points_for_breaks_a_tie_in_wins():
    teams = [{"owner_id": o} for o in "ab"]
    assert ss._seed(teams, {"a": 7, "b": 7}, {"a": 1500, "b": 1600}, {}) == ["b", "a"]


def _league():
    teams = [dict(owner_id=f"t{i}", wins=3, ties=0,
                  points_for=840.0, eppg=120 + 4 * i, division=None) for i in range(12)]
    return teams, {"playoff_teams": 6, "playoff_week_start": 15}


def test_same_inputs_same_odds_and_the_totals_add_up():
    teams, settings = _league()
    one = ss.simulate(teams, settings, 6, runs=500)
    assert one == ss.simulate(teams, settings, 6, runs=500)
    assert round(sum(v["playoffs"] for v in one.values()), 6) == 6
    assert round(sum(v["bye"] for v in one.values()), 6) == 2
    assert round(sum(v["title"] for v in one.values()), 6) == 1
    assert one["t11"]["playoffs"] > one["t0"]["playoffs"]


def test_a_finished_regular_season_is_decided_by_the_standings():
    teams, settings = _league()
    for i, t in enumerate(teams):
        t["wins"] = i
    odds = ss.simulate(teams, settings, 14, runs=200)
    assert [o for o, v in odds.items() if v["playoffs"] == 1] == [f"t{i}" for i in range(6, 12)]
    assert odds["t11"]["bye"] == odds["t10"]["bye"] == 1


def test_draft_order_follows_the_xfl_constitution():
    """1-6: playoff misses, lowest max PF first (a 2-12 team with a higher max PF picks
    AFTER a 4-10 one - benching can't buy a pick); 7-8: first-round losers, worse record
    first, max PF breaking ties; 9/10: the 3rd-place game's loser, then its winner; 11:
    runner-up; 12: champion."""
    missed = ["m1", "m2", "m3"]
    mpf = {"m1": 1900, "m2": 1700, "m3": 2000, "r1": 2100, "r2": 2050, "s1": 2200, "s2": 2300,
           "ru": 2400, "ch": 2500}
    wins = {"m1": 2, "m2": 4, "m3": 6, "r1": 8, "r2": 8, "s1": 9, "s2": 10, "ru": 11, "ch": 12}
    third = lambda me, rnd: 100 if me == "s1" else 0          # s1 wins the 3rd-place game
    order = ss.draft_order(missed, "ch", [["r1", "r2"], ["s1", "s2"], ["ru"]], wins, mpf, third)
    assert order == ["m2", "m1", "m3", "r2", "r1", "s2", "s1", "ru", "ch"]


def test_pick_odds_cover_every_slot_and_follow_max_pf():
    teams, settings = _league()
    for i, t in enumerate(teams):
        t["max_pf"] = 900 + 10 * i
    odds = ss.simulate(teams, settings, 6, runs=500)
    assert all(round(sum(v["pick"]), 6) == 1 for v in odds.values())
    assert all(round(sum(v["pick"][k] for v in odds.values()), 6) == 1 for k in range(12))
    assert odds["t0"]["pick"][0] > odds["t5"]["pick"][0] > odds["t11"]["pick"][0]


def test_pick_text_says_settled_only_when_it_is():
    from analysis.team_state import pick_text
    assert pick_text([0.0, 0.3, 0.4, 0.3] + [0.0] * 8) == "~#3 (#2-4)"
    assert pick_text([0.98, 0.02] + [0.0] * 10) == "~#1"
    assert pick_text([1.0] + [0.0] * 11) == "#1"


def test_everyone_in_means_no_odds():
    teams, settings = _league()
    assert ss.simulate(teams, {**settings, "playoff_teams": 12}, 6) == {}
