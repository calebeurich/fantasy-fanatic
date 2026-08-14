"""Each team's strategic window, from two measured axes cut into league tertiles:
**contention** (the lineup's total REDRAFT value, ranked - dynasty value prices future
years that score nothing this season) and **trajectory** (ascending minus declining share
of that production).

THREE states, each with flavors - `state` is the base to reason with, `window` the flavor
most of the codebase keys on:

- **Contending** - top third in current production. Only the clock splits the flavors:
  `Push` if falling (waiting costs value), `Contend` if not (never pay a premium).
- **Middling** - the middle, either trajectory. Both paths are shown; trajectory sets the
  note, because patience is only free for a rising roster.
- **Rebuilding** - bottom third. Sell what is declining, accumulate youth and picks.

Contending and rebuilding complement each other in both directions; same-state pairs do
not; a Middling team is a "maybe" until it picks a side (`_sells_him` in trade_targets).
Owning your own next 1st is a constraint on the pivot, not a fourth tier. A label within
refresh noise of a tertile line carries `window_edge` - the adjacent tier's advice is
live too, and a flip between runs is pricing noise, not news. History and the models
this replaced: LOGIC.md, "Team windows".

Smoke test: python -m analysis.team_state <league_id>
"""

import sys

from sources import sleeper, fantasycalc
from . import trade_activity
from .team_values import (age_bucket, years_to_decline, MIN_MEANINGFUL_RUNWAY,
                          INSIDE_FINAL_YEAR, NOISE_BAND,
                          get_players_with_roles, rank_map,
                          owned_picks, pick_capital,
                          split_starters_bench, tertile)

CORNERSTONE_PERCENTILE = 0.10  # top 10% of the format's value pool

# Tertile names per axis. Both are relative to this league, which is the only frame in
# which either question means anything - "can I compete" is always against these 11 teams.
# Below this, "top third" is one or two teams and the comparison stops meaning anything -
# same reason roster_needs refuses to assess quality in a tiny league.
MIN_TEAMS_FOR_LEVERAGE = 6

CONTENTION_TIER = {"top": "contender", "middle": "fringe", "bottom": "also-ran"}
TRAJECTORY_TIER = {"top": "rising", "middle": "steady", "bottom": "falling"}

# What a bucket's dynasty value is actually made of, and how much of positional
# replacement level a player needs to clear to be a real trade chip rather than
# waiver-wire filler. One shared source of truth for anything that needs to explain or
# filter trade value by age - buy-side pricing, sell-side give-up cost, and the
# minimum-relevance floor all derive from this instead of each keeping its own rule.
VALUE_BASIS = {"declining": "production", "prime": "mixed", "ascending": "upside", "unknown": "mixed"}
MIN_RELEVANCE_FRACTION = {"production": 0.5, "mixed": 0.5, "upside": 0.25}


def value_basis(entry: dict) -> str:
    """Is this player's price about production already delivered, or seasons still to come?
    A player inside his final year is at his own edge whatever his bucket says (the bucket
    is only the sign of `years_to_decline`); above that the bucket answers.
    `INSIDE_FINAL_YEAR`, not the buyer's two-season horizon - on the RB curve that horizon
    would call any back over 25 production-priced. Does not touch the relevance floor."""
    runway = entry.get("years_to_decline")
    if runway is not None and runway < INSIDE_FINAL_YEAR:
        return "production"
    return VALUE_BASIS[entry["bucket"]]


def window_for(contention: str, trajectory: str) -> str:
    """The two axes collapsed into what the team should do. The middle tier is a window,
    not a leftover - `Rebuild` used to be the else branch, and told a fringe team with the
    league's best QB room to sell decline it didn't have. Trajectory decides only what the
    middle tier is told (`WINDOW_NOTE`), never which paths it sees."""
    if contention == "contender":
        # Good now. The only question is whether there's a clock on it.
        return "Push" if trajectory == "falling" else "Contend"
    if contention == "fringe":
        return "Middling"
    return "Rebuild"


# Three states, not four: Push and Contend are flavors of ONE state, and four peer labels
# made readers count four. `state` is the base; `window` stays the flavor.
STATE = {"Push": "Contending", "Contend": "Contending",
         "Middling": "Middling", "Rebuild": "Rebuilding"}

# "Steady" is a NET number, and a barbell roster nets to zero: hard-aging assets
# cancelled out by arriving youth. That is not stability - it is an efficiency loss
# with an action attached (convert one side into the other's timeline), where genuine
# steadiness makes inaction efficient. The floor: at least a quarter of started
# production moving in EACH direction before the cancellation is called real.
BARBELL_MIN_PCT = 25

FLAVOR_NOTE = {
    "Push": "on a clock - the roster declines if you wait",
    "Contend": "no clock - good now and not declining",
    "convertible": "weak lineup, top-third war chest - an unspent option, not simply bad",
    "rising": "patience is free - next season's production is already here",
    "falling": "waiting costs something - this roster does not improve on its own",
    "steady": "flat - neither arriving nor aging out on its own",
    "misaligned": "flat in net only - real aging production and real arriving production "
                  "are cancelling out. Not stability: holding both sides is an efficiency "
                  "loss, and converting one side into the other's timeline is the action "
                  "a genuinely steady roster doesn't have",
    "ascending": "the rebuild is working - young production is arriving",
    "stalled": "the rebuild is not delivering - no ascending tilt that is actually "
               "accumulating, and no war chest to convert",
}


def _assets_bottom(asset_rank: int, num_teams: int) -> bool:
    """Bottom third of the league in total tradeable value - the same small-league guard
    as `leverage`, because below it the tertile is one team and means nothing."""
    return num_teams >= MIN_TEAMS_FOR_LEVERAGE and tertile(asset_rank, num_teams) == "bottom"


def flavor_for(window: str, trajectory: str, leverage: str | None,
               ascending_pct: float = 0, declining_pct: float = 0,
               assets_bottom: bool = False) -> str:
    """The sub-flavor of the state, from fields already computed. For Contending the clock
    is the flavor (`window` already encodes it); `convertible` outranks trajectory
    everywhere else, because a weak lineup on a top-third war chest is not described by
    its trajectory."""
    if window in ("Push", "Contend"):
        return window
    if leverage == "convertible":
        return "convertible"
    if window == "Rebuild":
        # The tilt is absolute, not the tertile: "is the rebuild working" is about this
        # roster, not its rank - a league full of ascending rebuilds once made the tertile
        # call the clearest working rebuild "stalled". But the tilt alone is scale-free, and
        # a roster whose young players are all bad reads ascending on ratio (live: 25/7
        # while dead last in both lineup and assets - "his ascending assets are just bad").
        # `assets_bottom` is the floor: arriving production that isn't accumulating into a
        # war chest is not a rebuild that is working. Every working rebuild measured sat
        # mid-table or better in assets; the relevance floor could not separate them at all.
        return ("ascending" if ascending_pct > declining_pct and not assets_bottom
                else "stalled")
    # Middling keeps the tertile: whether waiting is free RELATIVE TO THIS LEAGUE is what
    # decides between pushing and pivoting.
    if trajectory == "steady" and min(ascending_pct, declining_pct) >= BARBELL_MIN_PCT:
        return "misaligned"
    return trajectory


# The three-tier read (owner-designed, 2026-08-15): tier 1 is the contention tertile
# (shown by rank order, not a tag), tier 2 asks whether the roster's composition AGREES
# with the game that rank implies, tier 3 is the path - a continue verb when aligned,
# both paths with a lean when not. The lean comes FROM TIER 1: the side of the roster
# already delivering rank is the side to keep. Middle rank = genuinely no lean, which
# is the old Middling doctrine falling out of the structure instead of being a window.
ARRIVING_MARGIN_PCT = 15   # asc must beat dec by this much before "young and good"
                           # fires - without it the flag hit 11 of 19 contenders (wallpaper)
PICKS_HEAVY_PCT = 25       # a quarter of asset value in picks = a real war chest
AGING_WORTH_NOTING_PCT = 10  # below this, "nothing aging out" is honest wording


def _aging_clause(aging_pct: float) -> str:
    """Aligned is 'no forced move', not perfection - a team can always trim toward
    better alignment. Measured on the runway bar (started production inside two
    seasons), not the declining bucket - a 27.8 WR with 1.2 years is late-prime by
    bucket but expiring by any honest read. No names here: an aligned team's
    specifics are the agent's job; the row just keeps the trim visible (live: a
    roster starting a 38-year-old QB was told nothing was aging out)."""
    if aging_pct < AGING_WORTH_NOTING_PCT:
        return ""
    return (f". One trim available: {round(aging_pct)}% of the starting lineup's "
            f"production is aging out over the next couple seasons - not enough to "
            f"force anything, enough to keep converting at the right price")


def alignment_for(contention: str, asc_pct: float, dec_pct: float, pick_share: float,
                  assets_bottom: bool, aging_pct: float = 0) -> tuple[str, str, str]:
    """(alignment, path, reason) for one team. Production tilt for the players, pick
    share as its own signal - measuring the sides in dynasty value instead washed out
    completely (44 of 46 teams read 'arriving', the market's youth premium reported
    back as a roster fact)."""
    barbell = min(asc_pct, dec_pct) >= BARBELL_MIN_PCT
    arriving = (not barbell and asc_pct >= BARBELL_MIN_PCT
                and asc_pct - dec_pct >= ARRIVING_MARGIN_PCT)
    leaving = not barbell and not arriving and dec_pct >= BARBELL_MIN_PCT
    picks_heavy = pick_share >= PICKS_HEAVY_PCT

    if contention == "contender":
        if barbell:
            return ("unaligned", "press",
                    "real aging production AND real arriving production cancelling out, "
                    "at a rank that already delivers - the delivering side is the one to keep")
        if arriving:
            # NOT a conflict: an ascending starter is delivering now AND later - a young
            # contender is the best spot on the board, and "decide something" advice
            # here would tell a team already competing to unwind the point of competing.
            return ("aligned", "contend",
                    "young and good - the production is arriving and already winning at "
                    "the same time. The best spot on the board" + _aging_clause(aging_pct))
        if leaving:
            return ("aligned", "contend - on a clock",
                    "a contender whose production is aging out: the window is open and "
                    "closing on its own, so buying now is the aligned move")
        return ("aligned", "contend",
                "good now with no wave aging out - nothing needs buying at a premium, "
                "nothing needs selling" + _aging_clause(aging_pct))
    if contention == "fringe":
        if leaving or barbell:
            why = ("holding decline a middle rank isn't cashing"
                   if leaving else "cancellation, not calm")
            return ("unaligned", "decide",
                    f"{why} - both paths are live and the middle rank gives no lean, "
                    "so letting the season pick the direction is legitimate")
        if picks_heavy and not arriving:
            return ("unaligned", "decide",
                    "an unspent war chest on an undecided roster - the option is real "
                    "and unexercised, and the middle rank gives no lean")
        if arriving:
            return ("aligned", "wait - production is arriving",
                    "next season's production is already on the roster - patience is free"
                    + _aging_clause(aging_pct))
        return ("aligned", "wait",
                ("nothing arriving and nothing aging out - waiting costs nothing here"
                 if aging_pct < AGING_WORTH_NOTING_PCT else
                 "no wave big enough to force a move - waiting is cheap here")
                + _aging_clause(aging_pct))
    # also-ran: the rank itself urges the rebuild; unaligned just means it isn't underway.
    # A real war chest counts as arriving even before the production shows - picks are
    # pure future - but the assets_bottom guard applies to both: neither an ascending
    # tilt nor a pick pile that isn't accumulating into actual value is a working rebuild.
    if (arriving or picks_heavy) and not assets_bottom:
        return ("aligned", "build",
                "the rebuild is working - young production arriving and value accumulating"
                + _aging_clause(aging_pct))
    if arriving:  # ascending tilt that isn't accumulating into a war chest
        return ("unaligned", "sell",
                "an ascending tilt that is not accumulating value - the young pieces "
                "are not good enough yet to be the plan, so keep converting")
    if leaving or barbell:
        return ("unaligned", "sell",
                "aging value at a rebuild rank - selling has a deadline, and the "
                "arriving side (if any) is the side to keep")
    return ("unaligned", "sell",
            "uncommitted - nothing arriving and no war chest, so the first trade is "
            "for a direction")


def next_first_note(owns_next_first: bool, window: str) -> str:
    """What not owning your own next 1st means, which depends entirely on the window: a
    real constraint for a rebuilder (tanking pays nothing), the window working as intended
    for a contender. As a bare boolean it got read as universally bad."""
    if owns_next_first:
        return "Owns its own next 1st."
    if window == "Rebuild":
        return ("Does NOT own its own next 1st - a real constraint while rebuilding, since "
                "a worse record now just hands a better pick to whoever holds it. Young "
                "assets have to be traded for, not earned by losing.")
    return ("Does NOT own its own next 1st - expected and appropriate for a team competing "
            "now, which is what spending future picks on current production looks like. "
            "Not a concern to fix, and not a reason to trade back for one. It also lowers "
            "the return on pivoting: without that pick, a bad season pays nothing back, so "
            "the case for selling has to stand on the trade returns alone.")


def cornerstone_threshold(players: dict[str, dict], metric: str = "value") -> float:
    values = sorted((info.get(metric) or 0 for info in players.values()), reverse=True)
    return values[int(len(values) * CORNERSTONE_PERCENTILE)]


def clears_relevance_floor(entry: dict, thresholds: dict[str, float]) -> bool:
    fraction = MIN_RELEVANCE_FRACTION[VALUE_BASIS[entry["bucket"]]]
    return entry["value"] >= thresholds[entry["position"]] * fraction


def classify(roster: dict, players: dict[str, dict], threshold: float,
             starter_ids: set[str], redraft_threshold: float = float("inf")) -> dict:
    """One roster's raw measurements; the league-relative parts are added by
    `classify_league`. `starter_ids` is the value-derived lineup. Trajectory is measured
    on current production, not dynasty value - dynasty prices the growth being asked
    about, so weighting by it would report the market's opinion back as a roster fact."""
    all_ids = roster["players"] or []

    buckets = {"ascending": 0, "prime": 0, "declining": 0, "unknown": 0}
    for pid in starter_ids:
        info = players.get(pid)
        if info:
            bucket = age_bucket(info["position"], info["age"], info.get("usage_role"))
            buckets[bucket] += info.get("redraft_value") or 0
    production = sum(buckets.values())
    asc_pct = buckets["ascending"] / production * 100 if production else 0
    dec_pct = buckets["declining"] / production * 100 if production else 0

    cornerstones, win_now_core, tradeable_surplus, sellable = [], [], [], []
    for pid in all_ids:
        info = players.get(pid)
        if info is None:
            continue
        # Both currencies plus the age fields ride on every entry so downstream pricing
        # never has to re-derive them - bucket for the kind of value, years_to_decline for
        # the distance the bucket throws away.
        entry = {"name": info["name"], "position": info["position"], "value": info["value"],
                 "redraft_value": info.get("redraft_value"),
                 "is_starter": pid in starter_ids}
        bucket = age_bucket(info["position"], info["age"], info.get("usage_role"))
        entry["bucket"] = bucket
        entry["years_to_decline"] = years_to_decline(info["position"], info["age"],
                                                     info.get("usage_role"))
        # A piece is core-sized if EITHER currency clears the league's top-10% bar.
        # Dynasty value alone made the deepest producers invisible: Derrick Henry's
        # price has already collapsed, so he missed win_now_core - and every note built
        # on it - while carrying more of his team's production than anyone (the
        # Walker-but-not-Henry note). Production is the other honest way to be core.
        production_core = (info.get("redraft_value") or 0) >= redraft_threshold
        if info["value"] < threshold and not production_core:
            # Not a foundational piece either way, but the two cases mean different
            # things: ascending-but-small is a lottery ticket you'd offer as filler;
            # prime/declining-but-small is a real (if modest) win-now contributor that
            # just isn't this team's identity - still a findable trade target.
            if bucket == "ascending":
                tradeable_surplus.append(entry)
            elif bucket in ("prime", "declining"):
                sellable.append(entry)
            continue
        # Runway, not bucket: a cornerstone is a piece with several seasons, not a side of
        # a birthday. And a cornerstone stays in `sellable` - the hardest ask is a price,
        # not a veto, and leaving him out made the best piece on a roster unaskable.
        # Production-only qualifiers can never be cornerstones - "build around" is a
        # dynasty-price claim the market is explicitly not making about them.
        if (entry["years_to_decline"] or 0) < MIN_MEANINGFUL_RUNWAY or info["value"] < threshold:
            # He missed the cornerstone tag on the CLOCK, not on value - so the ask must
            # not fall off a cliff with the label. Without this, a roster read
            # "cornerstones: none" while holding a top-6 receiver, and the seller's own
            # tool framed its premium asset as an ordinary piece.
            # price_notes are PRICING facts - what the ask is IF a piece moves - never
            # an instruction to move him. Unconditional "now is the selling window"
            # wording made a live answer tell a contend-on-a-clock team to liquidate
            # its own RB room: three pieces each carried the sentence, and the model
            # assembled them into a "Must Sell" plan that inverted the team's path.
            if info["value"] >= threshold and entry["years_to_decline"] is not None:
                entry["price_note"] = (
                    f"cornerstone-priced: his {entry['value']:,} clears the same top-10% "
                    f"bar this league's cornerstones do, and only the clock "
                    f"({entry['years_to_decline']} years) keeps the tag off. The market "
                    f"has not discounted the remaining years yet - so IF this team's "
                    f"path calls for converting him, now prices best and the ask is a "
                    f"cornerstone's ask. A pricing fact, not an instruction: whether he "
                    f"moves at all is the team's path's call.")
            elif info["value"] < threshold and (entry["years_to_decline"] or 0) < MIN_MEANINGFUL_RUNWAY:
                entry["price_note"] = (
                    f"production-priced: his {entry['redraft_value']:,} this-season value "
                    f"clears the league's top-10% production bar while his dynasty price "
                    f"({entry['value']:,}) no longer does - the market has already "
                    f"discounted the future, so whoever buys him is buying this season "
                    f"only. His market is any team whose path says buy, and it peaks at "
                    f"the deadline. A pricing fact, not an instruction: a team consuming "
                    f"his production itself (contend/press) rides him, it doesn't sell him.")
            elif info["value"] < threshold:
                # The Goff shape: top-10% production, discount price, YEARS of runway.
                # The market doubts the asset, not this season - cheap real production
                # that is fine to hold through a wait, not a rental.
                entry["price_note"] = (
                    f"production-priced with runway: his {entry['redraft_value']:,} "
                    f"this-season value clears the league's top-10% production bar at a "
                    f"discount dynasty price ({entry['value']:,}), with "
                    f"{entry['years_to_decline']} years before his decline - the market "
                    f"doubts the asset, not this season. Cheap production that is fine "
                    f"to hold; nothing here forces a sale.")
            win_now_core.append(entry)
            sellable.append(entry)  # valuable but short - still sellable, just pricier
        else:
            entry["is_cornerstone"] = True
            cornerstones.append(entry)
            sellable.append(entry)
    tradeable_surplus.sort(key=lambda e: -e["value"])
    sellable.sort(key=lambda e: -e["value"])

    # `_cliff_case` pointed at one's own roster: an ascending-tilt team STARTING a
    # short-runway premium piece is holding seasons it isn't built for, and the answer
    # to "what window am I in" kept describing the young core without naming the one
    # starter whose clock disagrees with it (live: a 45%-ascending Middling roster
    # starting a 0.1-year RB the market still prices as a cornerstone). The runway
    # check is explicit, NOT inherited from win_now_core membership: since core went
    # either-currency, win_now_core also holds long-runway production-priced pieces
    # (a 31.9 pocket QB with 5.1 years was flagged as "seasons he won't be part of" -
    # he will be part of all of them; he is fine for the wait).
    clock_mismatch = ([e for e in win_now_core if e["is_starter"]
                       and (e["years_to_decline"] or 0) < MIN_MEANINGFUL_RUNWAY]
                      if asc_pct > dec_pct else [])

    # The clock can be POSITIONAL: a "no clock" roster whose entire RB room expires
    # (live: Walker 1.2 / Henry -5.6 / Swift -0.6 under durable QBs and WRs) reads
    # "Contend, steady" because the young positions' production swamps the tilt. Per
    # position: what share of STARTED production sits inside the buyer's two-season
    # bar. Majority = the room ages out together; clock_mismatch misses these when
    # the pieces are individually below the premium-value bar.
    position_clocks = []
    by_pos: dict[str, list] = {}
    for pid in starter_ids:
        info = players.get(pid)
        if info:
            by_pos.setdefault(info["position"], []).append(info)
    # Roster-wide version of the same bar: how much started production is inside the
    # buyer's two-season horizon. The declining BUCKET undercounts "aging out" - a
    # 27.8 WR with 1.2 years is late-prime by bucket but expiring by any honest read.
    expiring_starters = [
        i for infos in by_pos.values() for i in infos
        if (y := years_to_decline(i["position"], i["age"], i.get("usage_role"))) is not None
        and y < MIN_MEANINGFUL_RUNWAY]
    expiring_starters.sort(key=lambda i: -(i.get("redraft_value") or 0))
    expiring_total = sum(i.get("redraft_value") or 0 for i in expiring_starters)
    expiring_pct = round(100 * expiring_total / production) if production else 0
    for pos, infos in by_pos.items():
        total = sum(i.get("redraft_value") or 0 for i in infos)
        expiring = [i for i in infos
                    if (yrs := years_to_decline(i["position"], i["age"],
                                                i.get("usage_role"))) is not None
                    and yrs < MIN_MEANINGFUL_RUNWAY]
        exp_total = sum(i.get("redraft_value") or 0 for i in expiring)
        if total and exp_total / total > 0.5:
            position_clocks.append({
                "position": pos,
                "expiring_pct": round(100 * exp_total / total),
                "names": [i["name"] for i in
                          sorted(expiring, key=lambda i: -(i.get("redraft_value") or 0))]})
    def _slope_phrase(e):
        y = e["years_to_decline"]
        if y is None:
            return e["name"]
        if y >= 0:
            return f"{e['name']} ({y} yrs before his decline starts)"
        # Past the breakpoint is a slope, not a cliff - he still contributes, the
        # seasons ahead just aren't the ones this roster is accumulating.
        return (f"{e['name']} ({abs(y):.1f} yrs past his breakpoint - still productive "
                f"today, declining from here)")

    # Human-readable fact only - the instruction to raise it unprompted lives in
    # get_team_state's docstring, not here; this string is shown to people in the UI.
    mismatch_note = (
        "Built for later, starting now: " +
        ", ".join(_slope_phrase(e) for e in clock_mismatch) +
        f" - this roster's tilt is ascending ({round(asc_pct)}% vs {round(dec_pct)}%), "
        f"so it is accumulating seasons these starters won't be part of. They still "
        f"contend today, and that is exactly the window to convert them in - while "
        f"the price still says starter." if clock_mismatch else None)

    return {"clock_mismatch": clock_mismatch,
            "clock_mismatch_note": mismatch_note,
            "position_clocks": position_clocks,
            "expiring_pct": expiring_pct,
            "expiring_names": [i["name"] for i in expiring_starters[:3]],
            "starting_production": round(production),
            "trajectory_score": round(asc_pct - dec_pct),
            "ascending_pct": round(asc_pct), "declining_pct": round(dec_pct),
            "cornerstones": cornerstones, "win_now_core": win_now_core,
            "tradeable_surplus": tradeable_surplus[:5], "sellable": sellable}


WINDOW_NOTE = {
    "Push": ("Top-third in current production, with a roster that declines if you wait - "
             "the window is open and closing on its own. Buying production is worth a "
             "premium here, and future picks are the right currency to pay with. Pivoting "
             "is still *available*, it just returns poorly: the current production that "
             "makes this team competitive is priced on already-realized value, so selling "
             "it converts a lot of what wins games into comparatively little dynasty "
             "value. Being decent now is itself the argument against tearing down."),
    "Contend": ("Top-third in current production and not declining, so there's no clock. "
                "Nothing needs to be bought at a premium, and nothing needs selling."),
    "Middling": ("In the middle of the league and not committed in either direction - which "
                 "is a position, not an unmade decision. Both paths are shown because either "
                 "is defensible from here, and waiting to see how the season actually starts "
                 "is a legitimate choice. What this roster does not have is the free option a "
                 "rising one gets: it is not supplying next season's production by itself, so "
                 "pushing later will not be cheaper than pushing now."),
    "Rebuild": ("Not in contention this season and not rising fast enough to change that. "
                "Sell what's declining while it still has value, and accumulate youth "
                "and picks."),
}

# A rising middling team has something the others don't - its own ascending players deliver
# next season's production at no cost - so patience is genuinely free and a push has to clear
# a higher bar. Same window and the same two paths, a different reason to prefer waiting.
MIDDLING_RISING = (
    "Not top-third yet, but the roster rises on its own - your own ascending players supply "
    "next season's production for free. Pushing now means paying a market premium for what "
    "patience delivers, so both paths are shown: push only where the price is right, "
    "otherwise keep accumulating."
)

# The Rebuild line assumes something declining to sell, and the teams furthest into a
# rebuild - exactly the audience - have nothing. Advice has to be keyed on the roster it
# describes, not the window label.
REBUILD_NOTHING_DECLINING = (
    "Not in contention this season and not rising fast enough to change that. Nothing here "
    "is declining, so there is no aging value to cash in - this roster is already young. "
    "That makes the sell list short by nature, and what it has to trade is production that "
    "doesn't fit its timeline rather than players running out of time. Keep accumulating "
    "youth and picks, and convert anything the lineup cannot actually field."
)


def window_note(window: str, contention_rank: int, num_teams: int, pct_of_best: int,
                asc_pct: int, dec_pct: int, trajectory: str = "steady") -> str:
    """The measurements that produced the window, in words, alongside it - an unlabelled
    number in a tool result gets a meaning invented for it (a bare {"diff": -11} was once
    narrated as games underperformed, in the preseason)."""
    if window == "Rebuild" and dec_pct == 0:
        lead = REBUILD_NOTHING_DECLINING
    elif window == "Middling" and trajectory == "rising":
        lead = MIDDLING_RISING
    else:
        lead = WINDOW_NOTE[window]
    # The mirror of Push's clock, as a clause rather than a flavor: a rebuilder in the
    # league's falling tertile is holding value that is aging out - the SELLING has a
    # deadline. Not its own flavor because it changes the tempo of "convert", never
    # the verb - and the per-piece runway on every sell entry already prices the
    # urgency at the right resolution.
    deadline = (" The value still here is aging out faster than this league's - "
                "conversion has a deadline, not just a direction."
                if window == "Rebuild" and trajectory == "falling" else "")
    return (f"{lead} Current starting production ranks {contention_rank} of "
            f"{num_teams} ({pct_of_best}% of the league's best lineup); {asc_pct}% of that "
            f"production comes from ascending players and {dec_pct}% from declining ones."
            f"{deadline} "
            f"Both are roster-composition measures - there are no wins or points scored "
            f"behind them.")


LEVERAGE_NOTE = {
    "convertible": (
        "CONVERTIBLE. This lineup is not competitive, but the roster behind it is - it "
        "ranks {asset_rank} of {num_teams} in total tradeable value (players plus picks) "
        "against {contention_rank} of {num_teams} in what it actually starts. That gap is "
        "an option, not an oversight: a team here can buy its way into contention faster "
        "than its lineup suggests, so the right move is usually to hold and see how the "
        "season opens rather than commit now. Do NOT read this as a bad team - read it as "
        "a team that has not yet spent what it has. {pick_share}% of that value sits in "
        "draft picks, which is the part that converts most easily: a pick is "
        "position-agnostic, so it fits any deal rather than needing a partner who happens "
        "to want the position you are long in, and its value does not decay with age, "
        "injury or a lost role the way a player's does."
    ),
    "mortgaged": (
        "MORTGAGED. This lineup is competitive and there is little behind it - it ranks "
        "{contention_rank} of {num_teams} in what it starts against {asset_rank} of "
        "{num_teams} in total tradeable value (players plus picks). Whatever this season "
        "produces is close to the whole return; there is not much left to reload with, so "
        "a deadline addition costs more than it looks and losing a starter is harder to "
        "cover. Only {pick_share}% of what it holds is in picks - the liquid, "
        "position-agnostic part - so even the value it has is mostly players, and spending "
        "that means finding a partner who wants the exact positions it happens to be long in."
    ),
}


def leverage(contention_rank: int, asset_rank: int, num_teams: int) -> str | None:
    """Whether a team's convertible assets and its starting lineup tell different stories:
    `convertible` (weak lineup, top-third war chest - an unspent option, not simply bad)
    or `mortgaged` (strong lineup, little behind it). Deliberately not a fifth window -
    `window` says what to do with this roster, leverage says how much rope there is to
    change it."""
    if num_teams < MIN_TEAMS_FOR_LEVERAGE:
        return None
    assets_top = tertile(asset_rank, num_teams) == "top"
    production_top = tertile(contention_rank, num_teams) == "top"
    if assets_top and not production_top:
        return "convertible"
    if production_top and not assets_top:
        return "mortgaged"
    return None


# How close two trajectory scores sit before a refresh can swap their ranks. In points,
# not a share - the score crosses zero, where a relative band means nothing. Under the
# same +/-2% value jitter that calibrated NOISE_BAND, the score moved 1 point at the 95th
# percentile and 3 at most, so a pair within 2 points is one ordinary update from
# trading places (calibration: LOGIC.md, "Boundary noise").
TRAJECTORY_NOISE_POINTS = 2


def tertile_edges(ranks: dict, scores: dict, num_teams: int, same) -> dict:
    """The teams whose tertile is one value refresh from flipping: each pair straddling a
    tertile line whose scores `same` calls indistinguishable. Maps owner_id -> (the
    tertile across the line, the score gap). The label keeps the tertile - a cut at the
    largest GAP instead would relabel several teams at once whenever the gap moved
    (LOGIC.md, "Boundary noise") - this just says when the line runs through noise."""
    lines = [r for r in range(1, num_teams)
             if tertile(r, num_teams) != tertile(r + 1, num_teams)]
    out = {}
    for line in lines:
        above = next(o for o, r in ranks.items() if r == line)
        below = next(o for o, r in ranks.items() if r == line + 1)
        if same(scores[above], scores[below]):
            # One gap, one reference score for BOTH sides - each side quoting the gap
            # against its own total described the same 691-point gap as 1.8% and 1.9%
            # in one grid, and a shared number is the point of a shared line.
            gap = scores[above] - scores[below]
            out[above] = (tertile(line + 1, num_teams), gap, scores[above])
            out[below] = (tertile(line, num_teams), gap, scores[above])
    return out


EDGE_CONTENTION = (
    "This label is within refresh noise of the line: {gap_pct}% of starting production "
    "separates this team from the one across the tertile boundary, and a routine value "
    "refresh moves lineup totals about 1%, so an ordinary update can relabel it "
    "{alt_window}. That flip would be pricing noise, not the team changing direction - "
    "treat {alt_window}'s advice as live alongside this window's."
)

EDGE_CLOCK = (
    "The clock is within refresh noise: {gap} of trajectory separate{s} this team from "
    "the one across the line, so an ordinary update can relabel it {alt_window}. Whether "
    "this roster is declining is genuinely unclear at this margin - don't let {window} "
    "vs {alt_window} decide a premium on its own."
)

EDGE_FLAVOR = (
    "The trajectory read is within refresh noise: {gap} separate{s} this team from the "
    "one across the line, so '{flavor}' can read '{alt_flavor}' after an ordinary "
    "update. At this margin the tilt is a coin flip, not a measurement."
)


def _points(n: int) -> str:
    return f"{n} point" + ("" if n == 1 else "s")


def window_edge(row: dict, contention_edges: dict, trajectory_edges: dict) -> str | None:
    """The stability of the label, next to the label. Which flip matters depends on the
    axis and the window: a contention flip always changes the window; a trajectory flip
    only matters where trajectory decides something (the Push/Contend clock, or a
    Middling flavor - never a Rebuild, whose flavor is absolute)."""
    notes = []
    edge = contention_edges.get(row["owner_id"])
    if edge:
        alt_tier, gap, ref = edge
        alt_window = window_for(CONTENTION_TIER[alt_tier], row["trajectory"])
        notes.append(EDGE_CONTENTION.format(
            gap_pct=round(100 * gap / ref, 1) if ref else 0,
            alt_window=alt_window))
    edge = trajectory_edges.get(row["owner_id"])
    if edge:
        alt_tier, gap, _ = edge
        alt_trajectory = TRAJECTORY_TIER[alt_tier]
        alt_window = window_for(row["contention"], alt_trajectory)
        alt_flavor = flavor_for(alt_window, alt_trajectory, row["leverage"],
                                row["ascending_pct"], row["declining_pct"],
                                _assets_bottom(row["asset_rank"], row["of_teams"]))
        s = "s" if gap == 1 else ""
        if alt_window != row["window"]:
            notes.append(EDGE_CLOCK.format(gap=_points(gap), s=s,
                                           window=row["window"], alt_window=alt_window))
        elif alt_flavor != row["flavor"]:
            notes.append(EDGE_FLAVOR.format(gap=_points(gap), s=s,
                                            flavor=row["flavor"], alt_flavor=alt_flavor))
    return " ".join(notes) or None


def classify_league(league_id: str) -> list[dict]:
    """Full team-window report for every roster in the league, ranked by starter value.
    Reused by anything downstream that needs to know each team's strategic posture
    (e.g. matching trade targets across win-now/rebuild teams)."""
    from .league import context
    ctx = context(league_id)
    league, players = ctx.league, ctx.players
    threshold = cornerstone_threshold(players)
    redraft_threshold = cornerstone_threshold(players, "redraft_value")
    rosters, owner_names = ctx.rosters, ctx.owner_names

    # Dynasty identity is built through trades, so a league with zero in its history
    # hasn't differentiated yet and the labels are at their least reliable - said with a
    # flag rather than guessed from the numbers.
    no_trade_history = sum(trade_activity.get_trade_counts(league_id).values()) == 0

    # Tanking only pays if the team still owns its own next 1st.
    next_season = int(league["season"]) + 1
    traded_picks = sleeper.get_traded_picks(league_id)
    lost_own_first = {
        p["roster_id"] for p in traded_picks
        if p["round"] == 1 and int(p["season"]) == next_season and p["owner_id"] != p["roster_id"]
    }

    # Pick capital priced WITHOUT `strategy_by_roster`: that argument prices a pick by the
    # originating team's window, and the window is what this measure helps produce -
    # letting it in would make the label feed its own input.
    pick_values = fantasycalc.get_pick_values(ctx.fmt["num_qbs"], ctx.fmt["num_teams"],
                                              ctx.fmt["ppr"], ctx.fmt["is_dynasty"])
    picks_by_roster = owned_picks(league_id, int(league["season"]),
                                  league["settings"]["draft_rounds"],
                                  [r["roster_id"] for r in rosters], pick_values)
    capital = pick_capital(picks_by_roster)

    rows = []
    for roster in rosters:
        starter_ids = ctx.starters_for(roster)
        starter_value, _ = split_starters_bench(roster, players, starter_ids)
        result = classify(roster, players, threshold, starter_ids, redraft_threshold)
        roster_value = sum(players[pid]["value"] or 0
                           for pid in (roster["players"] or []) if pid in players)
        rows.append({
            "owner": owner_names.get(roster["owner_id"], "Unknown"),
            # The custom Sleeper team name ("Where's the Lamb Sauce???") - half a league
            # refers to teams this way, so every reader of these rows gets both names.
            "team_name": ctx.team_names.get(roster["owner_id"]),
            "owner_id": roster["owner_id"],
            "roster_id": roster["roster_id"],
            "starter_value": starter_value,
            "roster_value": roster_value,
            "pick_capital": capital.get(roster["roster_id"], 0),
            "asset_value": roster_value + capital.get(roster["roster_id"], 0),
            # How much of the war chest is in picks - the liquid, position-agnostic part.
            # Reported, not weighted: by how much a pick converts more easily is nothing
            # this project can calibrate, and an honest number beats a guessed multiplier.
            "pick_share": round(100 * capital.get(roster["roster_id"], 0)
                                / (roster_value + capital.get(roster["roster_id"], 0)))
            if roster_value + capital.get(roster["roster_id"], 0) else 0,
            "owns_next_first": roster["roster_id"] not in lost_own_first,
            # Which future 1sts this team actually holds, by season - the detail behind
            # pick_capital's number. First-round only: that is the pick people track.
            "firsts": sorted(int(p["season"]) for p in
                             picks_by_roster.get(roster["roster_id"], [])
                             if p["round"] == 1),
            "no_trade_history": no_trade_history,
            **result,
        })

    # `starter_value` (dynasty) is still reported - "what is my roster worth" is a real
    # question - but the window is decided by current production only.
    num_teams = len(rows)
    production = {r["owner_id"]: r["starting_production"] for r in rows}
    trajectory_scores = {r["owner_id"]: r["trajectory_score"] for r in rows}
    contention_rank = rank_map(production)
    asset_rank = rank_map({r["owner_id"]: r["asset_value"] for r in rows})
    trajectory_rank = rank_map(trajectory_scores)
    best_production = max(production.values()) or 1
    contention_edges = tertile_edges(contention_rank, production, num_teams,
                                     lambda a, b: a - b <= a * NOISE_BAND)
    trajectory_edges = tertile_edges(trajectory_rank, trajectory_scores, num_teams,
                                     lambda a, b: a - b <= TRAJECTORY_NOISE_POINTS)

    for row in rows:
        c_rank = contention_rank[row["owner_id"]]
        t_rank = trajectory_rank[row["owner_id"]]
        contention = CONTENTION_TIER[tertile(c_rank, num_teams)]
        trajectory = TRAJECTORY_TIER[tertile(t_rank, num_teams)]
        window = window_for(contention, trajectory)

        row["contention"] = contention
        row["contention_rank"] = c_rank
        row["pct_of_best"] = round(100 * row["starting_production"] / best_production)
        row["trajectory"] = trajectory
        row["trajectory_rank"] = t_rank
        # Every rank on a row is meaningless without it, and prose built from these ranks was
        # having to guess the denominator or leave it out.
        row["of_teams"] = num_teams
        row["window"] = window
        row["state"] = STATE[window]
        row["window_note"] = window_note(window, c_rank, num_teams, row["pct_of_best"],
                                         row["ascending_pct"], row["declining_pct"],
                                         trajectory)
        row["next_first_note"] = next_first_note(row["owns_next_first"], window)

        # Only Contend gets this sentence: it is the one label that claims "no clock",
        # and a majority-expiring position room is the exception that claim needs.
        # Push already knows it is on a clock; a rebuilder's aging room is sell fodder
        # the seller machinery handles.
        if window == "Contend" and row["position_clocks"]:
            parts = [f"the {c['position']} room ages out together - {c['expiring_pct']}% "
                     f"of its started production ({', '.join(c['names'])}) is inside two "
                     f"seasons of runway" for c in row["position_clocks"]]
            row["position_clock_note"] = (
                "No roster-wide clock, with one positional exception: " + "; ".join(parts)
                + ". Contend everywhere else, but at "
                + "/".join(c["position"] for c in row["position_clocks"])
                + " act like a Push team: succession there is the action this window "
                  "actually requires, and 'no action needed' is not true of it.")

        # What a team could *become*, alongside what it currently is. Additive, and not a
        # fifth window - see `leverage`.
        row["asset_rank"] = asset_rank[row["owner_id"]]
        row["leverage"] = leverage(c_rank, row["asset_rank"], num_teams)
        # After `leverage`, because `convertible` outranks trajectory as the flavor.
        row["flavor"] = flavor_for(window, trajectory, row["leverage"],
                                   row["ascending_pct"], row["declining_pct"],
                                   _assets_bottom(row["asset_rank"], num_teams))
        row["flavor_note"] = FLAVOR_NOTE[row["flavor"]]
        row["alignment"], row["path"], row["path_reason"] = alignment_for(
            contention, row["ascending_pct"], row["declining_pct"], row["pick_share"],
            _assets_bottom(row["asset_rank"], num_teams), row["expiring_pct"])
        # Unaligned rows carry their specifics BY NAME - telling them how to become
        # aligned is the whole point of the bright chip. Aligned rows stay brief;
        # their specifics are the agent's job.
        if row["alignment"] == "unaligned" and row["expiring_names"]:
            row["path_reason"] += (
                ". The pieces on the clock: " + ", ".join(row["expiring_names"]))
        # After flavor: an edge is only an edge if crossing the line changes the message.
        row["window_edge"] = window_edge(row, contention_edges, trajectory_edges)
        row["leverage_note"] = (
            LEVERAGE_NOTE[row["leverage"]].format(
                asset_rank=row["asset_rank"], contention_rank=c_rank, num_teams=num_teams,
                pick_share=row["pick_share"])
            if row["leverage"] else None)

    rows.sort(key=lambda r: r["contention_rank"])
    return rows


def main(league_id: str) -> None:
    league_name = sleeper.get_league(league_id)["name"]
    rows = classify_league(league_id)

    print(f"{league_name} - team windows:")
    if rows and rows[0]["no_trade_history"]:
        print("  (no trades in this league's history yet - labels below are less reliable this early)")
    print(f"  {'#':>2} {'owner':18} {'state':11} {'flavor':12} {'production':>10} {'%best':>5} "
          f"{'asc/dec':>9}  {'dynasty':>8} {'rk':>3}")
    for row in rows:
        tank_note = "" if row["owns_next_first"] else "  [no next 1st]"
        # Dynasty rank shown beside the production rank on purpose: where they disagree is
        # exactly where the old age-only model was wrong, and it's the difference between
        # "old and bad" and "old and close".
        dyn_rank = sorted(rows, key=lambda r: -r["starter_value"]).index(row) + 1
        print(f"  {row['contention_rank']:2} {row['owner'][:18]:18} "
              f"{row['state'][:11]:11} {row['flavor']:12} "
              f"{row['starting_production']:10,} {row['pct_of_best']:4}% "
              f"{row['ascending_pct']:4}/{row['declining_pct']:<4} "
              f"{row['starter_value']:8,} {dyn_rank:3}{tank_note}")
        print(f"       {row['contention']} + {row['trajectory']}")
        if row["window_edge"]:
            print(f"       borderline: {row['window_edge']}")
        names = lambda entries: ", ".join(e["name"] for e in entries)
        print(f"       cornerstones: {names(row['cornerstones']) if row['cornerstones'] else 'none'}")
        if row["win_now_core"]:
            core = ", ".join(e["name"] + (" [cornerstone-priced]" if e.get("price_note") else "")
                             for e in row["win_now_core"])
            print(f"       win-now core / sell candidates: {core}")
        if row["tradeable_surplus"]:
            print(f"       tradeable surplus: {names(row['tradeable_surplus'])}")


if __name__ == "__main__":
    main(sys.argv[1])
