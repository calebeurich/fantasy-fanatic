"""Classify each team's strategic window (Win-Now / Middling / Rebuilding) from its
starting lineup's age composition - not the whole roster, since deep-bench dart-throws
don't define a team's direction the way starters do. Thresholds are organic breakpoints
found by inspecting a real league's asc%-dec% distribution, not a forced even split -
see the CLAUDE.md discussion this came from for the reasoning.

Smoke test: python team_state.py <league_id>
"""

import sys

import sleeper
from team_values import NUM_QBS, age_bucket, get_players_with_roles, split_starters_bench

CORNERSTONE_PERCENTILE = 0.10  # top 10% of the format's value pool

# Win-Now if the starting lineup's value skews toward declining (aging-but-productive)
# players; Rebuilding if it skews toward ascending (still-developing) players.
WIN_NOW_MAX_DIFF = -10
REBUILD_MIN_DIFF = 30

# A team can just be weak, independent of its age split - bottom-third starter value
# with barely any real difference-makers isn't "Middling with options," it's thin.
THIN_ROSTER_MAX_CORNERSTONES = 1


def cornerstone_threshold(players: dict[str, dict]) -> float:
    values = sorted((info["value"] for info in players.values()), reverse=True)
    return values[int(len(values) * CORNERSTONE_PERCENTILE)]


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

    cornerstones, win_now_core = [], []
    for pid in all_ids:
        info = players.get(pid)
        if info is None or info["value"] < threshold:
            continue
        bucket = age_bucket(info["position"], info["age"], info.get("usage_role"))
        if bucket == "declining":
            win_now_core.append(info["name"])
        else:
            cornerstones.append(info["name"])

    return {"state": state, "diff": round(diff), "cornerstones": cornerstones, "win_now_core": win_now_core}


def main(league_id: str) -> None:
    league = sleeper.get_league(league_id)
    fmt = sleeper.describe_format(league)
    num_qbs = NUM_QBS[fmt["is_superflex"]]

    players = get_players_with_roles(num_qbs, fmt["num_teams"], fmt["ppr"], fmt["is_dynasty"])
    threshold = cornerstone_threshold(players)

    rosters = sleeper.get_rosters(league_id)
    users = sleeper.get_users(league_id)
    owner_names = {user["user_id"]: user["display_name"] for user in users}

    rows = []
    for roster in rosters:
        owner = owner_names.get(roster["owner_id"], "Unknown")
        starter_value, _ = split_starters_bench(roster, players)
        result = classify(roster, players, threshold)
        rows.append((owner, starter_value, result))

    rows.sort(key=lambda r: r[1], reverse=True)
    num_teams = len(rows)
    bottom_third_rank = num_teams - num_teams // 3  # rank strictly greater than this = bottom third

    print(f"{league['name']} - team windows (cornerstone threshold: value >= {threshold:.0f}):")
    for rank, (owner, starter_value, result) in enumerate(rows, start=1):
        is_thin = rank > bottom_third_rank and len(result["cornerstones"]) <= THIN_ROSTER_MAX_CORNERSTONES
        label = result["state"] + (" (thin roster)" if is_thin else "")
        print(f"  {rank}. {owner}: {label}  [starter value rank {rank}/{num_teams}, asc-dec diff={result['diff']}]")
        if result["cornerstones"]:
            print(f"       cornerstones: {', '.join(result['cornerstones'])}")
        else:
            print("       cornerstones: none")
        if result["win_now_core"]:
            print(f"       win-now core / sell candidates: {', '.join(result['win_now_core'])}")


if __name__ == "__main__":
    main(sys.argv[1])
