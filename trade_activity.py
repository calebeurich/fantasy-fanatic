"""How many trades each owner has actually made, across the league's full dynasty
history - a signal for who's likely to engage with a trade offer at all, separate from
whether the trade itself makes sense. Sleeper doesn't expose a "trade block" feature,
so realized trade history is the best proxy available.

Smoke test: python trade_activity.py <league_id>
"""

import sys
from collections import Counter

import sleeper

MAX_WEEK = 18


def get_trade_counts(league_id: str) -> dict[str, int]:
    """Completed trades per user_id, summed across every season of this dynasty league."""
    counts = Counter()
    for season_league_id in sleeper.get_season_chain(league_id):
        roster_to_user = {r["roster_id"]: r["owner_id"] for r in sleeper.get_rosters(season_league_id)}
        for week in range(1, MAX_WEEK + 1):
            for txn in sleeper.get_transactions(season_league_id, week):
                if txn["type"] != "trade" or txn["status"] != "complete":
                    continue
                for roster_id in txn["roster_ids"]:
                    user_id = roster_to_user.get(roster_id)
                    if user_id:
                        counts[user_id] += 1
    return dict(counts)


def main(league_id: str) -> None:
    counts = get_trade_counts(league_id)
    users = {u["user_id"]: u["display_name"] for u in sleeper.get_users(league_id)}
    seasons = len(sleeper.get_season_chain(league_id))

    rows = [(users.get(uid, uid), counts.get(uid, 0)) for uid in users]
    rows.sort(key=lambda r: -r[1])

    print(f"trade counts across {seasons} season(s) of history:")
    for owner, count in rows:
        print(f"  {owner}: {count} trade(s)")


if __name__ == "__main__":
    main(sys.argv[1])
