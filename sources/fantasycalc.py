"""Minimal FantasyCalc API client. Smoke test: python -m sources.fantasycalc"""

import requests

from .cache import ttl_cache, MARKET_TTL

BASE = "https://api.fantasycalc.com/values/current"


@ttl_cache(MARKET_TTL)
def get_players(num_qbs: int, num_teams: int, ppr: float, is_dynasty: bool = True) -> dict[str, dict]:
    """Dynasty value + age + position for this league's format, keyed by Sleeper player_id."""
    params = {
        "isDynasty": str(is_dynasty).lower(),
        "numQbs": num_qbs,
        "numTeams": num_teams,
        "ppr": ppr,
    }
    resp = requests.get(BASE, params=params)
    resp.raise_for_status()
    entries = resp.json()

    players = {}
    for entry in entries:
        if entry["player"]["position"] == "PICK":
            continue
        sleeper_id = entry["player"].get("sleeperId")
        if sleeper_id:
            players[sleeper_id] = {
                "value": entry["value"],
                "age": entry["player"].get("maybeAge"),
                "position": entry["player"].get("position"),
                "name": entry["player"].get("name"),
            }
    return players


@ttl_cache(MARKET_TTL)
def get_pick_values(num_qbs: int, num_teams: int, ppr: float, is_dynasty: bool = True) -> dict[str, int]:
    """Rookie pick values keyed by name, e.g. '2027 1st'. Only the current draft class
    (this season, before it happens) gets an exact slot like '2026 Pick 1.01' - future
    classes are valued as a flat round average since the slot isn't known yet."""
    params = {
        "isDynasty": str(is_dynasty).lower(),
        "numQbs": num_qbs,
        "numTeams": num_teams,
        "ppr": ppr,
    }
    resp = requests.get(BASE, params=params)
    resp.raise_for_status()
    entries = resp.json()

    return {
        entry["player"]["name"]: entry["value"]
        for entry in entries
        if entry["player"]["position"] == "PICK"
    }


if __name__ == "__main__":
    players = get_players(num_qbs=2, num_teams=12, ppr=1.0)
    top_5 = sorted(players.items(), key=lambda kv: kv[1]["value"], reverse=True)[:5]
    print(f"{len(players)} players with values")
    print("top 5 by value:", top_5)

    picks = get_pick_values(num_qbs=2, num_teams=12, ppr=1.0)
    print(f"\n{len(picks)} pick values, e.g. 2027 1st = {picks.get('2027 1st')}")
