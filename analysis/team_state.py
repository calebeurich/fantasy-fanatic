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
Owning your own next 1st is a constraint on the pivot, not a fourth tier. History and the
models this replaced: LOGIC.md, "Team windows".

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
    """The sub-flavor of the state, from fields already computed. For Contending the clock
    is the flavor (`window` already encodes it); `convertible` outranks trajectory
    everywhere else, because a weak lineup on a top-third war chest is not described by
    its trajectory."""
    if window in ("Push", "Contend"):
        return window
    if leverage == "convertible":
        return "convertible"
    if window == "Rebuild":
        # Absolute, not the tertile: "is the rebuild working" is about this roster, not its
        # rank - a league full of ascending rebuilds once made the tertile call the clearest
        # working rebuild "stalled".
        return "ascending" if ascending_pct > declining_pct else "stalled"
    # Middling keeps the tertile: whether waiting is free RELATIVE TO THIS LEAGUE is what
    # decides between pushing and pivoting.
    return trajectory


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


def cornerstone_threshold(players: dict[str, dict]) -> float:
    values = sorted((info["value"] for info in players.values()), reverse=True)
    return values[int(len(values) * CORNERSTONE_PERCENTILE)]


def clears_relevance_floor(entry: dict, thresholds: dict[str, float]) -> bool:
    fraction = MIN_RELEVANCE_FRACTION[VALUE_BASIS[entry["bucket"]]]
    return entry["value"] >= thresholds[entry["position"]] * fraction


def classify(roster: dict, players: dict[str, dict], threshold: float,
             starter_ids: set[str]) -> dict:
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
        # Runway, not bucket: a cornerstone is a piece with several seasons, not a side of
        # a birthday. And a cornerstone stays in `sellable` - the hardest ask is a price,
        # not a veto, and leaving him out made the best piece on a roster unaskable.
        if (entry["years_to_decline"] or 0) < MIN_MEANINGFUL_RUNWAY:
            win_now_core.append(entry)
            sellable.append(entry)  # valuable but short - still sellable, just pricier
        else:
            entry["is_cornerstone"] = True
            cornerstones.append(entry)
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


def classify_league(league_id: str) -> list[dict]:
    """Full team-window report for every roster in the league, ranked by starter value.
    Reused by anything downstream that needs to know each team's strategic posture
    (e.g. matching trade targets across win-now/rebuild teams)."""
    from .league import context
    ctx = context(league_id)
    league, players = ctx.league, ctx.players
    threshold = cornerstone_threshold(players)
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
            # How much of the war chest is in picks - the liquid, position-agnostic part.
            # Reported, not weighted: by how much a pick converts more easily is nothing
            # this project can calibrate, and an honest number beats a guessed multiplier.
            "pick_share": round(100 * capital.get(roster["roster_id"], 0)
                                / (roster_value + capital.get(roster["roster_id"], 0)))
            if roster_value + capital.get(roster["roster_id"], 0) else 0,
            "owns_next_first": roster["roster_id"] not in lost_own_first,
            "no_trade_history": no_trade_history,
            **result,
        })

    # `starter_value` (dynasty) is still reported - "what is my roster worth" is a real
    # question - but the window is decided by current production only.
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
