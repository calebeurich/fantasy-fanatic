"""Minimal Sleeper API client. Smoke test: python sleeper.py <username> [year] [league_id]"""

import sys
import requests

BASE = "https://api.sleeper.app/v1"

# Sleeper's own league type flag: 0 = redraft, 1 = keeper, 2 = dynasty
DYNASTY_TYPE = 2


def get_user_id(username: str) -> str:
    resp = requests.get(f"{BASE}/user/{username}")
    resp.raise_for_status()
    return resp.json()["user_id"]


def get_leagues(user_id: str, year: str) -> list[dict]:
    resp = requests.get(f"{BASE}/user/{user_id}/leagues/nfl/{year}")
    resp.raise_for_status()
    return resp.json()


def get_league(league_id: str) -> dict:
    resp = requests.get(f"{BASE}/league/{league_id}")
    resp.raise_for_status()
    return resp.json()


def get_rosters(league_id: str) -> list[dict]:
    resp = requests.get(f"{BASE}/league/{league_id}/rosters")
    resp.raise_for_status()
    return resp.json()


def get_users(league_id: str) -> list[dict]:
    resp = requests.get(f"{BASE}/league/{league_id}/users")
    resp.raise_for_status()
    return resp.json()


def get_traded_picks(league_id: str) -> list[dict]:
    """Future picks that have changed hands at least once. A pick not listed here is
    still owned by the roster whose original pick it is."""
    resp = requests.get(f"{BASE}/league/{league_id}/traded_picks")
    resp.raise_for_status()
    return resp.json()


def get_transactions(league_id: str, week: int) -> list[dict]:
    resp = requests.get(f"{BASE}/league/{league_id}/transactions/{week}")
    resp.raise_for_status()
    return resp.json()


def get_season_chain(league_id: str) -> list[str]:
    """This league's own league_id plus every prior season's, oldest dynasty history
    included, most recent first."""
    chain = []
    while league_id:
        chain.append(league_id)
        league_id = get_league(league_id).get("previous_league_id")
    return chain


def describe_format(league: dict) -> dict:
    """Pull out the format details that drive dynasty value lookups."""
    positions = league["roster_positions"]
    scoring = league["scoring_settings"]
    return {
        "is_dynasty": league["settings"]["type"] == DYNASTY_TYPE,
        "num_teams": league["settings"]["num_teams"],
        "is_superflex": "SUPER_FLEX" in positions,
        "ppr": scoring.get("rec", 0),
        "is_te_premium": scoring.get("bonus_rec_te", 0) > 0,
    }


if __name__ == "__main__":
    username = sys.argv[1]
    year = sys.argv[2] if len(sys.argv) > 2 else "2026"

    user_id = get_user_id(username)
    leagues = get_leagues(user_id, year)

    print(f"user_id: {user_id}")
    print(f"{len(leagues)} league(s) for {year}:")
    for league in leagues:
        print(f"  - {league['name']} (league_id={league['league_id']})")

    if len(sys.argv) > 3:
        league = get_league(sys.argv[3])
        print(f"\nformat for {league['name']}: {describe_format(league)}")
