"""The trade-ideas rules (analysis/trade_ideas.py), each pinned by name. Plain-dict
fixtures, no network. The owner quotes behind every rule: LOGIC.md "Trade ideas"."""

from analysis import trade_ideas as ti


def _pair(**over):
    base = dict(league_id="L", owners={"a": "A", "b": "B"}, stances={"a": None, "b": None},
                results={"a": {"mode": "buy"}, "b": {"mode": "buy"}},
                starters={"a": [], "b": []}, movable={"a": set(), "b": set()},
                wish={"a": set(), "b": set()}, picks={"a": [], "b": []},
                wants_picks={"a": True, "b": True}, value={}, eppg={}, position={}, runway={},
                bars={"QB": 0, "RB": 0, "WR": 0, "TE": 0}, players={}, qb_stock={}, qb_slots=2)
    base.update(over)
    return ti._Pair(**base)


def _idea(partner, a_sends, b_sends, lens="buy", delta=1.0, **extra):
    return {"partner": partner, "a_sends": a_sends, "b_sends": b_sends, "lens": lens,
            "buyer_delta": delta, "why": "", **extra}


def test_band_is_directional_in_the_buyers_view():
    """The buyer sends 0.9x-1.2x of what he gets (plus a hair of tolerance): Ward alone
    for Taylor at 0.69x is out; overpaying to 1.2x is what contenders do. 'sell' means
    side a is the SELLER, so side b's total is the buyer's."""
    assert ti.in_band("buy", 90, 100) and ti.in_band("buy", 124, 100)
    assert not ti.in_band("buy", 69, 100) and not ti.in_band("buy", 130, 100)
    assert ti.in_band("sell", 100, 90) and not ti.in_band("sell", 100, 69)
    assert not ti.in_band("buy", 50, 0)


def test_a_buyer_sweetens_with_1sts_and_2nds_nearest_year_first():
    """3rds and 4ths aren't currency for a real chip ("at least a 2nd"); same-round
    picks cost the same, so the NEAREST year is offered first ('27 1st, not '29)."""
    p = _pair(picks={"a": [("2027 1st", 3000), ("2029 1st", 2600), ("2027 2nd", 1000),
                           ("2027 3rd", 300)], "b": []})
    offered = [combo[0][0] for combo in ti.sweeteners(p, "a", [], light_is_buyer=True) if len(combo) == 1]
    assert offered == ["2027 2nd", "2027 1st", "2029 1st"]
    assert not any(n == "2027 3rd" for combo in ti.sweeteners(p, "a", [], True) for n, _ in combo)


def test_no_picks_top_up_a_side_that_does_not_take_picks():
    """A contender wants production, not your '28 1st."""
    p = _pair(picks={"a": [("2027 1st", 3000)], "b": []}, wants_picks={"a": True, "b": False})
    assert ti.sweeteners(p, "a", [], light_is_buyer=True) == []


def test_a_seller_sweetens_only_with_a_smaller_piece_it_would_move():
    """Evans on top of McLaurin, never a piece bigger than the one it started with, and
    never one under the trade bar."""
    p = _pair(movable={"a": set(), "b": {"Evans", "Bigger", "Filler"}},
              value={"McLaurin": 5000, "Evans": 2000, "Bigger": 9000, "Filler": 50},
              position={"McLaurin": "WR", "Evans": "WR", "Bigger": "WR", "Filler": "WR"},
              bars={"QB": 0, "RB": 0, "WR": 100, "TE": 0}, wants_picks={"a": False, "b": True})
    assert ti.sweeteners(p, "b", ["McLaurin"], light_is_buyer=False) == [[("Evans", 2000)]]


def test_a_rebuild_receives_only_its_wish_list_or_a_year_more_runway():
    """Tyson (7.0) for Zay Flowers (3.0) is what a rebuild does; Wilson (2.9) for Olave
    (2.8) is a lateral that does nothing for it - unless he's on its wish list."""
    p = _pair(results={"a": {"mode": "buy"}, "b": {"mode": "rebuild"}},
              movable={"a": {"Tyson", "Wilson"}, "b": {"Flowers", "Olave"}},
              position={n: "WR" for n in ("Tyson", "Wilson", "Flowers", "Olave")},
              runway={"Tyson": 7.0, "Flowers": 3.0, "Wilson": 2.9, "Olave": 2.8})
    assert ti._available(p, _idea("B", ["Tyson"], ["Flowers"]))
    assert not ti._available(p, _idea("B", ["Wilson"], ["Olave"]))
    p.wish["b"] = {"Wilson"}
    assert ti._available(p, _idea("B", ["Wilson"], ["Olave"]))


def test_every_piece_is_something_its_own_team_would_move():
    """A rebuild wanting kb's Brian Thomas doesn't make Thomas available - except the
    starter an upgrade swap replaces, who goes because a better one arrives."""
    p = _pair(movable={"a": set(), "b": {"Goff"}}, position={"Young": "QB", "Goff": "QB"})
    assert not ti._available(p, _idea("B", ["Young"], ["Goff"]))
    assert ti._available(p, _idea("B", ["Young"], ["Goff"], upgrade="Young"))
    assert ti._available(p, _idea("B", ["2027 1st"], ["Goff"]))   # picks are always movable


def test_consolidations_pair_a_starter_never_two_depth_pieces():
    starts = {"Evans": True, "Warren": True, "White": False, "Cover": False}
    depth = {"Evans": False, "Warren": False, "White": True, "Cover": True}
    pairs = ti.consolidations(list(starts), starts.get, depth.get)
    assert ("Evans", "Warren") in pairs and ("Evans", "White") in pairs
    assert ("White", "Cover") not in pairs


def test_a_third_young_qb_on_a_rebuild_is_friction_not_a_veto():
    """With a young QB chip per QB slot already held, the next can't start and is slow to
    resell - the idea says so; it is not removed."""
    p = _pair(owners={"a": "A", "b": "Reb"}, qb_stock={"Reb": 2}, qb_slots=2,
              position={"Shough": "QB"}, runway={"Shough": 7.1})
    note = ti.qb_stockpile(p, _idea("Reb", ["Shough"], ["Etienne"]))
    assert note and "already holds 2 young QB chips" in note
    p.qb_stock = {"Reb": 1}
    assert ti.qb_stockpile(p, _idea("Reb", ["Shough"], ["Etienne"])) is None


def test_ideas_rank_buys_by_lineup_gain_and_dips_last():
    """A buyer's currency is his lineup (Etienne at +2.8 over a pricier Waddle at +1.2);
    a dip is shown but below every gain."""
    value = {"Etienne": 2900, "Waddle": 4000, "Hurts": 5000, "Shough": 3000, "Fannin": 3600, "Ward": 3500}
    cands = [_idea("Vic", ["Shough"], ["Waddle"], delta=1.2),
             _idea("Big", ["Fannin"], ["Etienne"], delta=2.8),
             _idea("Bart", ["Ward"], ["Hurts"], delta=-0.9)]
    assert [c["partner"] for c in ti.pick_ideas(cands, value, 3)] == ["Big", "Vic", "Bart"]


def test_one_idea_per_partner_the_rest_ride_along_as_also():
    """kieran would answer every RB deal the same way: one card, the alternates named on it."""
    value = {"Fannin": 3600, "Shough": 3000, "Taylor": 7000, "Kyren": 3200}
    cands = [_idea("kieran", ["Fannin"], ["Taylor"], delta=4.6),
             _idea("kieran", ["Shough"], ["Kyren"], delta=2.8)]
    out = ti.pick_ideas(cands, value, 3)
    assert len(out) == 1 and out[0]["b_sends"] == ["Taylor"] and out[0]["also"] == ["Kyren"]


def test_a_thin_roster_refills_to_the_limit_by_repeating_pieces():
    """Three offerable pieces can't make three unique ideas forever - once the strict
    pass runs dry, a repeated outgoing piece is fine, unseen partners first."""
    value = {"Shough": 3000, "Etienne": 2900, "Waddle": 4000, "Kyren": 3200}
    cands = [_idea("Big", ["Shough"], ["Etienne"], delta=2.8),
             _idea("Vic", ["Shough"], ["Waddle"], delta=1.2),
             _idea("Kie", ["Shough"], ["Kyren"], delta=2.0)]
    assert [c["partner"] for c in ti.pick_ideas(cands, value, 3)] == ["Big", "Kie", "Vic"]


def test_a_pick_shape_shows_once():
    """"Starter + two 1sts for a stud" is real, but once is enough."""
    value = {"Pickens": 6000, "Chase": 9000, "Nabers": 8800, "Ward": 3500}
    cands = [_idea("jq", ["Pickens", "2027 1st", "2028 1st"], ["Chase"], delta=3.0),
             _idea("rj", ["Ward", "2027 1st", "2028 1st"], ["Nabers"], delta=2.0)]
    assert [c["partner"] for c in ti.pick_ideas(cands, value, 3)] == ["jq"]
