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

The four windows and what separates them:

- `Push`    - contender whose roster is falling. The window is open and closing on its own,
              so waiting costs value. Buy production, spend picks.
- `Contend` - contender that is steady or rising. Good now with no clock, so there is no
              reason to pay a premium for anything.
- `Ascend`  - fringe now, rising. Can push, but its own ascending players supply next
              season's production for free; pushing now means paying a premium for what
              patience delivers. Both paths are shown with that cost stated.
- `Rebuild` - anything else. Sell what is declining, accumulate youth and picks.

**Owning your own next 1st is a constraint on the pivot option, not a fourth tier.**
Tanking only pays if you hold the pick your bad season earns; without it, a losing season
buys nothing. That does not change the window (a rebuild is still a rebuild - you acquire
young assets by trade rather than by finishing last), so it ships as a note rather than
another label.

Smoke test: python -m analysis.team_state <league_id>
"""

import sys

from sources import sleeper
from . import trade_activity
from .team_values import (age_bucket, get_players_with_roles, rank_map,
                          split_starters_bench, tertile)

CORNERSTONE_PERCENTILE = 0.10  # top 10% of the format's value pool

# Tertile names per axis. Both are relative to this league, which is the only frame in
# which either question means anything - "can I compete" is always against these 11 teams.
CONTENTION_TIER = {"top": "contender", "middle": "fringe", "bottom": "also-ran"}
TRAJECTORY_TIER = {"top": "rising", "middle": "steady", "bottom": "falling"}

# What a bucket's dynasty value is actually made of, and how much of positional
# replacement level a player needs to clear to be a real trade chip rather than
# waiver-wire filler. One shared source of truth for anything that needs to explain or
# filter trade value by age - buy-side pricing, sell-side give-up cost, and the
# minimum-relevance floor all derive from this instead of each keeping its own rule.
VALUE_BASIS = {"declining": "production", "prime": "mixed", "ascending": "upside", "unknown": "mixed"}
MIN_RELEVANCE_FRACTION = {"production": 0.5, "mixed": 0.5, "upside": 0.25}


def window_for(contention: str, trajectory: str) -> str:
    """The two axes collapsed into what the team should actually do."""
    if contention == "contender":
        # Good now. The only question is whether there's a clock on it.
        return "Push" if trajectory == "falling" else "Contend"
    if contention == "fringe" and trajectory == "rising":
        return "Ascend"
    return "Rebuild"


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
        if bucket == "declining":
            win_now_core.append(entry)
            sellable.append(entry)  # valuable and declining - still sellable, just pricier
        else:
            cornerstones.append(entry)
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
    "Ascend": ("Not top-third yet, but the roster rises on its own - your own ascending "
               "players supply next season's production for free. Pushing now means "
               "paying a market premium for what patience delivers, so both paths are "
               "shown: push only where the price is right, otherwise keep accumulating."),
    "Rebuild": ("Not in contention this season and not rising fast enough to change that. "
                "Sell what's declining while it still has value, and accumulate youth "
                "and picks."),
}


def window_note(window: str, contention_rank: int, num_teams: int, pct_of_best: int,
                asc_pct: int, dec_pct: int) -> str:
    """The measurements that produced the window, in words, alongside it.

    Same rule that `roster_needs` follows and for the same reason: an unlabelled number
    in a tool result gets a meaning invented for it. The predecessor of this field shipped
    a bare `{"diff": -11}` and the model reliably described teams as "below their expected
    win total" or "underperforming by 25 points" - neither of which exists, least of all
    in a preseason with no games played."""
    return (f"{WINDOW_NOTE[window]} Current starting production ranks {contention_rank} of "
            f"{num_teams} ({pct_of_best}% of the league's best lineup); {asc_pct}% of that "
            f"production comes from ascending players and {dec_pct}% from declining ones. "
            f"Both are roster-composition measures - there are no wins or points scored "
            f"behind them.")


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

    rows = []
    for roster in rosters:
        starter_ids = ctx.starters_for(roster)
        starter_value, _ = split_starters_bench(roster, players, starter_ids)
        result = classify(roster, players, threshold, starter_ids)
        rows.append({
            "owner": owner_names.get(roster["owner_id"], "Unknown"),
            "owner_id": roster["owner_id"],
            "roster_id": roster["roster_id"],
            "starter_value": starter_value,
            "owns_next_first": roster["roster_id"] not in lost_own_first,
            "no_trade_history": no_trade_history,
            **result,
        })

    # Two independent rankings over the same rosters. `starter_value` (dynasty) is still
    # reported because "what is my roster worth" is a real question - but it is no longer
    # what decides the window, because it prices future seasons.
    num_teams = len(rows)
    contention_rank = rank_map({r["owner_id"]: r["starting_production"] for r in rows})
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
        row["window"] = window
        row["window_note"] = window_note(window, c_rank, num_teams, row["pct_of_best"],
                                         row["ascending_pct"], row["declining_pct"])
        row["next_first_note"] = next_first_note(row["owns_next_first"], window)

    rows.sort(key=lambda r: r["contention_rank"])
    return rows


def main(league_id: str) -> None:
    league_name = sleeper.get_league(league_id)["name"]
    rows = classify_league(league_id)

    print(f"{league_name} - team windows:")
    if rows and rows[0]["no_trade_history"]:
        print("  (no trades in this league's history yet - labels below are less reliable this early)")
    print(f"  {'#':>2} {'owner':18} {'window':8} {'production':>10} {'%best':>5} "
          f"{'asc/dec':>9}  {'dynasty':>8} {'rk':>3}")
    for row in rows:
        tank_note = "" if row["owns_next_first"] else "  [no next 1st]"
        # Dynasty rank shown beside the production rank on purpose: where they disagree is
        # exactly where the old age-only model was wrong, and it's the difference between
        # "old and bad" and "old and close".
        dyn_rank = sorted(rows, key=lambda r: -r["starter_value"]).index(row) + 1
        print(f"  {row['contention_rank']:2} {row['owner'][:18]:18} {row['window']:8} "
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
