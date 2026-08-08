"""Classify each team's strategic window (Win-Now / Middling / Rebuilding) from its
starting lineup's age composition - not the whole roster, since deep-bench dart-throws
don't define a team's direction the way starters do. Thresholds are organic breakpoints
found by inspecting a real league's asc%-dec% distribution, not a forced even split -
see the CLAUDE.md discussion this came from for the reasoning.

Smoke test: python -m analysis.team_state <league_id>
"""

import sys

from sources import sleeper
from .team_values import NUM_QBS, age_bucket, get_players_with_roles, split_starters_bench

CORNERSTONE_PERCENTILE = 0.10  # top 10% of the format's value pool

# Win-Now if the starting lineup's value skews toward declining (aging-but-productive)
# players; Rebuilding if it skews toward ascending (still-developing) players.
WIN_NOW_MAX_DIFF = -10
REBUILD_MIN_DIFF = 30

# A team can just be weak, independent of its age split - bottom-third starter value
# with barely any real difference-makers isn't "Middling with options," it's thin.
THIN_ROSTER_MAX_CORNERSTONES = 1

# What a bucket's dynasty value is actually made of, and how much of positional
# replacement level a player needs to clear to be a real trade chip rather than
# waiver-wire filler. One shared source of truth for anything that needs to explain or
# filter trade value by age - buy-side pricing, sell-side give-up cost, and the
# minimum-relevance floor all derive from this instead of each keeping its own rule.
VALUE_BASIS = {"declining": "production", "prime": "mixed", "ascending": "upside", "unknown": "mixed"}
MIN_RELEVANCE_FRACTION = {"production": 0.5, "mixed": 0.5, "upside": 0.25}


def cornerstone_threshold(players: dict[str, dict]) -> float:
    values = sorted((info["value"] for info in players.values()), reverse=True)
    return values[int(len(values) * CORNERSTONE_PERCENTILE)]


def clears_relevance_floor(entry: dict, thresholds: dict[str, float]) -> bool:
    fraction = MIN_RELEVANCE_FRACTION[VALUE_BASIS[entry["bucket"]]]
    return entry["value"] >= thresholds[entry["position"]] * fraction


def classify(roster: dict, players: dict[str, dict], threshold: float) -> dict:
    starter_ids = [pid for pid in (roster["starters"] or []) if pid != "0"]
    all_ids = roster["players"] or []

    buckets = {"ascending": 0, "prime": 0, "declining": 0, "unknown": 0}
    for pid in starter_ids:
        info = players.get(pid)
        if info:
            buckets[age_bucket(info["position"], info["age"], info.get("usage_role"))] += info["value"]
    starter_total = sum(buckets.values())
    asc_pct = buckets["ascending"] / starter_total * 100 if starter_total else 0
    dec_pct = buckets["declining"] / starter_total * 100 if starter_total else 0
    diff = asc_pct - dec_pct

    if diff <= WIN_NOW_MAX_DIFF:
        state = "Win-Now"
    elif diff >= REBUILD_MIN_DIFF:
        state = "Rebuilding"
    else:
        state = "Middling"

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
        entry = {"name": info["name"], "position": info["position"], "value": info["value"],
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

    return {"state": state, "diff": round(diff), "cornerstones": cornerstones, "win_now_core": win_now_core,
            "tradeable_surplus": tradeable_surplus[:5], "sellable": sellable}


def classify_league(league_id: str) -> list[dict]:
    """Full team-window report for every roster in the league, ranked by starter value.
    Reused by anything downstream that needs to know each team's strategic posture
    (e.g. matching trade targets across win-now/rebuild teams)."""
    league = sleeper.get_league(league_id)
    fmt = sleeper.describe_format(league)
    num_qbs = NUM_QBS[fmt["is_superflex"]]

    players = get_players_with_roles(num_qbs, fmt["num_teams"], fmt["ppr"], fmt["is_dynasty"])
    threshold = cornerstone_threshold(players)

    rosters = sleeper.get_rosters(league_id)
    owner_names = {user["user_id"]: user["display_name"] for user in sleeper.get_users(league_id)}

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
        starter_value, _ = split_starters_bench(roster, players)
        result = classify(roster, players, threshold)
        rows.append({
            "owner": owner_names.get(roster["owner_id"], "Unknown"),
            "owner_id": roster["owner_id"],
            "roster_id": roster["roster_id"],
            "starter_value": starter_value,
            "owns_next_first": roster["roster_id"] not in lost_own_first,
            **result,
        })

    rows.sort(key=lambda r: r["starter_value"], reverse=True)
    num_teams = len(rows)
    bottom_third_rank = num_teams - num_teams // 3  # rank strictly greater than this = bottom third
    top_third_rank = num_teams // 3  # rank at or below this = top third

    for rank, row in enumerate(rows, start=1):
        row["starter_value_rank"] = rank
        row["is_thin"] = rank > bottom_third_rank and len(row["cornerstones"]) <= THIN_ROSTER_MAX_CORNERSTONES
        # The mirror case of thin: a team can be the best roster in the league by
        # starter value and still trip the age-composition "Rebuilding" read purely
        # because it also happens to have a lot of great young talent mixed in - that's
        # not a fire-sale seller, it's a loaded team. Validated on a real case: the
        # #1 starter-value team in a real league read "Rebuilding" and its actually-
        # good WR got suggested as an easy buy-low to a Win-Now team, which no real
        # #1 team would actually just give away.
        row["is_loaded"] = rank <= top_third_rank and row["state"] == "Rebuilding"
        if row["is_thin"]:
            row["effective_strategy"] = "Rebuilding"
        elif row["is_loaded"]:
            row["effective_strategy"] = "Middling"
        else:
            row["effective_strategy"] = row["state"]

    return rows


def main(league_id: str) -> None:
    league_name = sleeper.get_league(league_id)["name"]
    rows = classify_league(league_id)

    print(f"{league_name} - team windows:")
    for row in rows:
        # Headline is always effective_strategy - that's what every downstream decision
        # actually uses. The raw age-mix state is shown as context, not the label
        # itself, since on a thin roster it can read backwards (e.g. "Win-Now" from
        # having almost no ascending value to offset a little declining value, not
        # from an actual aging contender core).
        context = f"raw age-mix reads {row['state']}"
        if row["is_thin"]:
            context += ", but too thin to act on it"
        elif row["is_loaded"]:
            context += ", but too loaded to treat as a real seller"
        tank_note = ""
        if row["effective_strategy"] == "Rebuilding" and not row["owns_next_first"]:
            tank_note = " [doesn't own next 1st - tanking wouldn't even help them]"
        print(f"  {row['starter_value_rank']}. {row['owner']}: {row['effective_strategy']}{tank_note}  "
              f"[{context}, starter value rank {row['starter_value_rank']}/{len(rows)}, asc-dec diff={row['diff']}]")
        names = lambda entries: ", ".join(e["name"] for e in entries)
        print(f"       cornerstones: {names(row['cornerstones']) if row['cornerstones'] else 'none'}")
        if row["win_now_core"]:
            print(f"       win-now core / sell candidates: {names(row['win_now_core'])}")
        if row["tradeable_surplus"]:
            print(f"       tradeable surplus: {names(row['tradeable_surplus'])}")


if __name__ == "__main__":
    main(sys.argv[1])
