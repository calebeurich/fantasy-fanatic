"""Each team's strategic window, from two measured axes rather than age alone.

**Contention** - can this team compete *this season*? Its starting lineup's total
**redraft** value, ranked against the league. **Trajectory** - where does the roster go on
its own? Ascending minus declining share of that same current production. Both are cut
into league tertiles (`team_values.tertile`), so nothing here is a hardcoded constant that
only fits one league.

**This replaced an age-only model plus two patches.** The old classifier read Win-Now /
Middling / Rebuilding purely off the age split, then bolted on `is_thin` ("bottom-third
starter value with barely any cornerstones") and `is_loaded` ("top-third but reads
Rebuilding") to override the answers age got wrong. Both patches were crude proxies for a
missing contention axis, and measuring contention properly subsumes them - two special
cases deleted, one honest axis added.

**And the old contention proxy was the recurring bug in this project one more time.** It
ranked the league by *dynasty* starter value, which prices future years that score no
points this season. Measured on two real leagues, swapping to redraft moved teams four and
five places: one team sat 8th of 12 in dynasty value and 4th in current production - old,
but genuinely close, and being told it was mid-pack. Its mirror ranked 2nd in dynasty and
6th in production: a young roster whose price is mostly potential.

**THREE states, each with flavors.** `state` is the base and is the thing to reason with;
`window` is the flavor and is what most of this codebase keys on for historical reasons.
Keeping them apart matters: four peer `window` labels read as four base states, and that
misreading has now cost both a reader and this file's own author the model.

- **Contending** - top third in current production. Two flavors, and only the clock
  separates them: `Push` if the roster is falling (waiting costs value - buy production,
  spend picks) and `Contend` if it is steady or rising (no clock, so never pay a premium).
- **Middling** - middle of the league, either trajectory. In dynasty you are winning now or
  rebuilding; in between, both paths are shown and waiting to see how the season starts is
  legitimate. Trajectory sets the *note*, not the state: a rising roster gets next season's
  production free, a falling one does not, so patience is only free for one of them.
- **Rebuilding** - bottom third in current production. Sell what is declining, accumulate
  youth and picks.

What makes a trade easy is one comparison across those states: **contending and rebuilding
complement each other in both directions** (one spares future years, the other spares
production now), same-state pairs do not, and a Middling team is a "maybe" until it picks a
side. `IS_SELLER`/`NOT_SELLER` in `trade_targets` is that comparison.

**Owning your own next 1st is a constraint on the pivot option, not a fourth tier.**
Tanking only pays if you hold the pick your bad season earns; without it, a losing season
buys nothing. That does not change the window (a rebuild is still a rebuild - you acquire
young assets by trade rather than by finishing last), so it ships as a note rather than
another label.

Smoke test: python -m analysis.team_state <league_id>
"""

import sys

from sources import sleeper, fantasycalc
from . import trade_activity
from .team_values import (age_bucket, years_to_decline, MIN_MEANINGFUL_RUNWAY,
                          INSIDE_FINAL_YEAR,
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
    """Is this player's price about production already delivered, or about seasons still to come?

    **`bucket` is only the SIGN of `years_to_decline`** - both come from the same age against the
    same cutoff, and `age_bucket`'s own docstring says it "throws away the distance to the
    boundary". Reading the sign alone priced DK Metcalf, **0.3 years** from his cliff, as
    `prime` -> `mixed` -> "moderate - some future value baked in too", while Dallas Goedert one
    year further on read "low - value is mostly already-realized production". Nothing about
    those two prices differs in kind.

    A player inside his final year (`INSIDE_FINAL_YEAR`) is at his own edge, so what you pay for
    him is production. Above that, the bucket answers as before.

    **Not `MIN_MEANINGFUL_RUNWAY`.** That 2.0 is a buyer's planning horizon, and borrowing it
    here priced Zach Charbonnet - 25.6, 1,684 of dynasty value for 434 of production, a ratio of
    **0.26** - as "production-priced", because the RB curve is (24, 27) and two years of runway
    means any RB over 25. One year keeps Metcalf at 0.3 and drops Charbonnet back to upside.

    Deliberately does NOT touch `MIN_RELEVANCE_FRACTION`, where `production` and `mixed` are both
    0.5, so this changes what the two pricing labels say and nothing about who clears the floor."""
    runway = entry.get("years_to_decline")
    if runway is not None and runway < INSIDE_FINAL_YEAR:
        return "production"
    return VALUE_BASIS[entry["bucket"]]


def window_for(contention: str, trajectory: str) -> str:
    """The two axes collapsed into what the team should actually do.

    **The middle tier is a window, not a leftover.** In dynasty you are winning now or
    rebuilding; a team in between should see both directions and is entitled to wait and
    see how the season starts. `Rebuild` used to be the else branch, so a `fringe` team that
    merely wasn't *rising* fell into it - and the one team in a real league that hit that
    case was 3rd of 12 in total dynasty value, 5th on an outside dynasty site, average age
    26.0, with the best QB room in the league. Telling it to sell what's declining was
    advice for a roster it didn't have.

    Trajectory no longer decides whether the middle tier gets both paths. It still decides
    what that team is *told*, in `WINDOW_NOTE` - rising means patience is free, falling
    means waiting costs something."""
    if contention == "contender":
        # Good now. The only question is whether there's a clock on it.
        return "Push" if trajectory == "falling" else "Contend"
    if contention == "fringe":
        return "Middling"
    return "Rebuild"


# There are THREE states, not four. `window` has always returned four labels, and `window_for`
# knows better - its own comment reads "Good now. The only question is whether there's a clock on
# it" - so Push and Contend are two flavors of ONE state, exactly as rising/falling are flavors
# of Middling. Returning four peer labels made a reader (and the author of this file) count four
# base states and lose the model. `state` is the base; `window` stays the flavor.
STATE = {"Push": "Contending", "Contend": "Contending",
         "Middling": "Middling", "Rebuild": "Rebuilding"}

FLAVOR_NOTE = {
    "Push": "on a clock - the roster declines if you wait",
    "Contend": "no clock - good now and not declining",
    "convertible": "weak lineup, top-third war chest - an unspent option, not simply bad",
    "rising": "patience is free - next season's production is already here",
    "falling": "waiting costs something - this roster does not improve on its own",
    "steady": "flat - neither arriving nor aging out on its own",
    "ascending": "the rebuild is working - young production is arriving",
    "stalled": "nothing arriving and nothing to convert - genuinely stuck",
}


def flavor_for(window: str, trajectory: str, leverage: str | None,
               ascending_pct: float = 0, declining_pct: float = 0) -> str:
    """The sub-flavor of the state, from fields already computed. Every one of these has been
    described in LOGIC.md for a while and none was ever labelled, so the flavor column repeated
    the state for two of the three - which is how a documented distinction stays invisible.

    Contending: the clock is the only flavor that matters, which is what `window` already
    encodes. Otherwise `convertible` wins - a weak lineup on a top-third war chest is not
    described by its trajectory - and below that it is trajectory, named for the state, because
    "rising" means *patience is free* to a Middling team and *the plan is working* to a
    rebuilding one."""
    if window in ("Push", "Contend"):
        return window
    if leverage == "convertible":
        return "convertible"
    if window == "Rebuild":
        # **Absolute, not the tertile**, and this is the one place the two genuinely differ.
        # "Is the rebuild working" is a question about this roster, not about its rank: young
        # production is either arriving faster than the rest is aging out, or it isn't.
        # BartolosHeroes is 40% ascending against 3% declining and lands in the MIDDLE tertile
        # only because that league is full of ascending rebuilds - so the tertile called it
        # "steady" and this called it "stalled", i.e. "nothing arriving and nothing to convert",
        # about the clearest working rebuild on the board. The label has to be true of the
        # roster it names.
        return "ascending" if ascending_pct > declining_pct else "stalled"
    # Middling keeps the tertile, because there the question really is comparative - whether
    # waiting is free *relative to this league* is what decides between pushing and pivoting,
    # and `WINDOW_NOTE` has always read it that way.
    return trajectory


def next_first_note(owns_next_first: bool, window: str) -> str:
    """What not owning your own next 1st actually means, which depends entirely on the
    window - shipped as a bare boolean it got read as universally bad. A live run told a
    contender it was "concerning" and to "reclaim a first-round pick", in the same answer
    that correctly advised spending picks aggressively. Having spent that pick is the
    window working as intended.

    For a rebuilder it's a genuine constraint rather than a blemish: tanking only pays if
    you hold the pick your bad season earns. Without it the pivot has no consolation
    prize, so the only route to young assets is trading for them. Both real leagues here
    have several teams in this spot, including two of the same manager's."""
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


def cornerstone_threshold(players: dict[str, dict]) -> float:
    values = sorted((info["value"] for info in players.values()), reverse=True)
    return values[int(len(values) * CORNERSTONE_PERCENTILE)]


def clears_relevance_floor(entry: dict, thresholds: dict[str, float]) -> bool:
    fraction = MIN_RELEVANCE_FRACTION[VALUE_BASIS[entry["bucket"]]]
    return entry["value"] >= thresholds[entry["position"]] * fraction


def classify(roster: dict, players: dict[str, dict], threshold: float,
             starter_ids: set[str]) -> dict:
    """One roster's raw measurements. The league-relative parts - ranks, tiers, and the
    window they imply - are added by `classify_league`, which is the only place that can
    see the rest of the league.

    `starter_ids` is the value-derived lineup (`LeagueContext.starters`), not Sleeper's
    current-week snapshot, and both the trajectory buckets and every entry's `is_starter`
    flag come from it.

    **Trajectory is measured on current production, not dynasty value.** The question is
    "will my lineup get better or worse on its own", so the currency has to be the one
    that scores points. Dynasty value would double-count the effect it's trying to
    measure: ascending players are *priced* on the growth being asked about, so weighting
    by it inflates the ascending share of every young roster and reports the market's
    opinion back as if it were a roster fact."""
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
        # bucket: lets trade_targets.py flag "production-priced" (declining) vs
        # "upside-priced" (prime) buys - a prime player's dynasty value bakes in
        # future growth a win-now buyer doesn't need, so it costs more per unit of
        # current production than a declining player's value does.
        # is_starter: a valuable-but-non-cornerstone starter (e.g. your QB2) isn't
        # real surplus even though it clears the sellable bar - only bench value at
        # this tier is safely offerable without weakening your actual lineup.
        # redraft_value / future_premium (see team_values.get_players_with_roles) let a
        # win-now team see what it's actually paying for: two players can produce the
        # same this season while one costs far more in dynasty value. Carried through so
        # trade_targets and the agent can reason about that, instead of ranking by
        # dynasty value alone and never noticing.
        entry = {"name": info["name"], "position": info["position"], "value": info["value"],
                 "redraft_value": info.get("redraft_value"),
                 "is_starter": pid in starter_ids}
        bucket = age_bucket(info["position"], info["age"], info.get("usage_role"))
        entry["bucket"] = bucket
        # The distance to the decline cutoff, not just which side of it he's on. `bucket`
        # alone called a receiver four months from declining "prime", and a caller reading
        # that as "has a future" offered him as a piece that would "still be there later".
        entry["years_to_decline"] = years_to_decline(info["position"], info["age"],
                                                     info.get("usage_role"))
        if info["value"] < threshold:
            # Not a foundational piece either way, but the two cases mean different
            # things: ascending-but-small is a lottery ticket you'd offer as filler;
            # prime/declining-but-small is a real (if modest) win-now contributor that
            # just isn't this team's identity - still a findable trade target.
            if bucket == "ascending":
                tradeable_surplus.append(entry)
            elif bucket in ("prime", "declining"):
                sellable.append(entry)
            continue
        # **Runway, not bucket.** A cornerstone is a piece to build the next several seasons
        # around, so the test is whether he has several seasons - not which side of a birthday
        # he sits on. Live: an elite back 0.1 years from his cutoff was a cornerstone and
        # therefore unreachable, while the same player one month older would have been a sell
        # candidate; another at 1.2 years sat on the one team the tool was already telling to
        # convert aging production, invisible to that path. Both are exactly who a contender
        # should be asking about.
        if (entry["years_to_decline"] or 0) < MIN_MEANINGFUL_RUNWAY:
            win_now_core.append(entry)
            sellable.append(entry)  # valuable but short - still sellable, just pricier
        else:
            entry["is_cornerstone"] = True
            cornerstones.append(entry)
            # A cornerstone is the hardest ask on the roster, which is a PRICE, not a veto.
            # Leaving them out of `sellable` made them literally unaskable: an owner deciding
            # what he'd move names them first and gets told to expect over-market or no.
            # `win_now_core` above already sets the precedent of one piece in two lists.
            sellable.append(entry)
    tradeable_surplus.sort(key=lambda e: -e["value"])
    sellable.sort(key=lambda e: -e["value"])

    return {"starting_production": round(production),
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

# The Rebuild line above assumes there is something declining to sell, and on a young
# rebuilding roster there isn't. A real team read "sell what's declining" with **0% of its
# production from declining players** and an empty sell list - advice keyed on the window
# rather than on the roster it was describing, which is the tell of a template. The teams
# most likely to hit this are the ones furthest into a rebuild, i.e. exactly the audience.
REBUILD_NOTHING_DECLINING = (
    "Not in contention this season and not rising fast enough to change that. Nothing here "
    "is declining, so there is no aging value to cash in - this roster is already young. "
    "That makes the sell list short by nature, and what it has to trade is production that "
    "doesn't fit its timeline rather than players running out of time. Keep accumulating "
    "youth and picks, and convert anything the lineup cannot actually field."
)


def window_note(window: str, contention_rank: int, num_teams: int, pct_of_best: int,
                asc_pct: int, dec_pct: int, trajectory: str = "steady") -> str:
    """The measurements that produced the window, in words, alongside it.

    Same rule that `roster_needs` follows and for the same reason: an unlabelled number
    in a tool result gets a meaning invented for it. The predecessor of this field shipped
    a bare `{"diff": -11}` and the model reliably described teams as "below their expected
    win total" or "underperforming by 25 points" - neither of which exists, least of all
    in a preseason with no games played."""
    if window == "Rebuild" and dec_pct == 0:
        lead = REBUILD_NOTHING_DECLINING
    elif window == "Middling" and trajectory == "rising":
        lead = MIDDLING_RISING
    else:
        lead = WINDOW_NOTE[window]
    return (f"{lead} Current starting production ranks {contention_rank} of "
            f"{num_teams} ({pct_of_best}% of the league's best lineup); {asc_pct}% of that "
            f"production comes from ascending players and {dec_pct}% from declining ones. "
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
    """Whether a team's convertible assets and its starting lineup tell different stories.

    **The state the window model could not express.** A real rebuilding roster ranked 9th of
    12 in starting production and **2nd** in total tradeable value - it was labelled
    `also-ran`, which reads as "bad", when the true statement was "bad right now, holding
    the second-largest war chest in the league". Its owner's own description was that he
    doesn't expect to win, but if the season opens well he has the assets to convert. That
    is an option with real value and the model priced it at zero.

    Both directions come from one comparison, and the mirror is just as real: the same league
    had a team 1st in production and 8th in assets, i.e. winning now on borrowed time with
    nothing to reload from.

    Tertiles rather than a tuned gap, matching how contention and trajectory are already
    read - top third on one axis and not the other. This is deliberately *not* a fifth
    window: `window` answers what a team should do with the roster it has, and this answers
    how much rope it has to change that roster. Making it a window would force one number to
    carry both."""
    if num_teams < MIN_TEAMS_FOR_LEVERAGE:
        return None
    assets_top = tertile(asset_rank, num_teams) == "top"
    production_top = tertile(contention_rank, num_teams) == "top"
    if assets_top and not production_top:
        return "convertible"
    if production_top and not assets_top:
        return "mortgaged"
    return None


def classify_league(league_id: str) -> list[dict]:
    """Full team-window report for every roster in the league, ranked by starter value.
    Reused by anything downstream that needs to know each team's strategic posture
    (e.g. matching trade targets across win-now/rebuild teams)."""
    from .league import context
    ctx = context(league_id)
    league, players = ctx.league, ctx.players
    threshold = cornerstone_threshold(players)
    rosters, owner_names = ctx.rosters, ctx.owner_names

    # Win-Now/Middling/Rebuilding reads a team's *current* age composition, but real
    # dynasty identity is built through trades over time - a fresh league (or one that
    # just hasn't traded yet) hasn't had the chance to actually differentiate, so the
    # labels are at their least meaningful right when a league is newest. Zero trades
    # in the league's whole history is a clean, directly-knowable proxy for "hasn't
    # differentiated yet" - simpler and more honest than trying to detect "low
    # separation" from the age-diff numbers themselves without real fresh-league data
    # on hand to calibrate a threshold against.
    no_trade_history = sum(trade_activity.get_trade_counts(league_id).values()) == 0

    # Tanking for a better pick only helps a team if it still owns its own next 1st -
    # if that pick's already been traded away, playing for a worse record just hands
    # the upside to whoever holds it.
    next_season = int(league["season"]) + 1
    traded_picks = sleeper.get_traded_picks(league_id)
    lost_own_first = {
        p["roster_id"] for p in traded_picks
        if p["round"] == 1 and int(p["season"]) == next_season and p["owner_id"] != p["roster_id"]
    }

    # Everything this team could put on the table: every player it owns plus every pick.
    # Priced WITHOUT `strategy_by_roster`, deliberately - that argument prices a pick by the
    # window of the team it originated from, and the window is what this measure is about to
    # help describe. Letting it in would make the label feed its own input.
    pick_values = fantasycalc.get_pick_values(ctx.fmt["num_qbs"], ctx.fmt["num_teams"],
                                              ctx.fmt["ppr"], ctx.fmt["is_dynasty"])
    capital = pick_capital(owned_picks(league_id, int(league["season"]),
                                       league["settings"]["draft_rounds"],
                                       [r["roster_id"] for r in rosters], pick_values))

    rows = []
    for roster in rosters:
        starter_ids = ctx.starters_for(roster)
        starter_value, _ = split_starters_bench(roster, players, starter_ids)
        result = classify(roster, players, threshold, starter_ids)
        roster_value = sum(players[pid]["value"] or 0
                           for pid in (roster["players"] or []) if pid in players)
        rows.append({
            "owner": owner_names.get(roster["owner_id"], "Unknown"),
            "owner_id": roster["owner_id"],
            "roster_id": roster["roster_id"],
            "starter_value": starter_value,
            "roster_value": roster_value,
            "pick_capital": capital.get(roster["roster_id"], 0),
            "asset_value": roster_value + capital.get(roster["roster_id"], 0),
            # How much of the war chest is in picks. **Reported, not weighted.** Picks
            # convert more easily than players of equal price - position-agnostic, so they
            # fit any deal instead of needing a positionally-matched counterparty, and
            # insulated from the age, injury and role decay a player carries. By *how much*
            # is not something this project can calibrate, and a guessed multiplier buried
            # inside the ranking would be worse than an honest number printed beside it. The
            # observed spread is wide enough to matter unaided: 3% to 41% across three real
            # leagues, with the most mortgaged contender at the bottom of it.
            "pick_share": round(100 * capital.get(roster["roster_id"], 0)
                                / (roster_value + capital.get(roster["roster_id"], 0)))
            if roster_value + capital.get(roster["roster_id"], 0) else 0,
            "owns_next_first": roster["roster_id"] not in lost_own_first,
            "no_trade_history": no_trade_history,
            **result,
        })

    # Two independent rankings over the same rosters. `starter_value` (dynasty) is still
    # reported because "what is my roster worth" is a real question - but it is no longer
    # what decides the window, because it prices future seasons.
    num_teams = len(rows)
    contention_rank = rank_map({r["owner_id"]: r["starting_production"] for r in rows})
    asset_rank = rank_map({r["owner_id"]: r["asset_value"] for r in rows})
    trajectory_rank = rank_map({r["owner_id"]: r["trajectory_score"] for r in rows})
    best_production = max(r["starting_production"] for r in rows) or 1

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

        # What a team could *become*, alongside what it currently is. Additive, and not a
        # fifth window - see `leverage`.
        row["asset_rank"] = asset_rank[row["owner_id"]]
        row["leverage"] = leverage(c_rank, row["asset_rank"], num_teams)
        # After `leverage`, because `convertible` outranks trajectory as the flavor.
        row["flavor"] = flavor_for(window, trajectory, row["leverage"],
                                   row["ascending_pct"], row["declining_pct"])
        row["flavor_note"] = FLAVOR_NOTE[row["flavor"]]
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
        names = lambda entries: ", ".join(e["name"] for e in entries)
        print(f"       cornerstones: {names(row['cornerstones']) if row['cornerstones'] else 'none'}")
        if row["win_now_core"]:
            print(f"       win-now core / sell candidates: {names(row['win_now_core'])}")
        if row["tradeable_surplus"]:
            print(f"       tradeable surplus: {names(row['tradeable_surplus'])}")


if __name__ == "__main__":
    main(sys.argv[1])
