"""Shared crosswalk from nflverse's gsis_id to Sleeper's player_id. Used by any module
that pulls nflverse/OTC data (contracts, usage stats) and needs to join it to a roster.
Smoke test: python -m sources.nflverse_ids
"""

import nflreadpy as nfl

from .cache import ttl_cache, REFERENCE_TTL


@ttl_cache(REFERENCE_TTL)
def gsis_to_sleeper() -> dict[str, str]:
    playerids = nfl.load_ff_playerids()
    return {
        row["gsis_id"]: str(row["sleeper_id"])
        for row in playerids.iter_rows(named=True)
        if row["gsis_id"] and row["sleeper_id"]
    }


@ttl_cache(REFERENCE_TTL)
def sleeper_names() -> dict[str, dict]:
    """Name and position by Sleeper player_id, for players the value sources don't carry.

    FantasyCalc prices ~400 players and a dynasty bench runs past that, so a rostered
    player can have no entry there at all - which rendered on screen as the literal string
    "(unvalued player_id 13342)". He's John Michael Gyllenborg, a KC tight end, and the
    dataset that already backs `gsis_to_sleeper` knew his name the whole time."""
    playerids = nfl.load_ff_playerids()
    return {
        str(row["sleeper_id"]): {"name": row["name"], "position": row["position"]}
        for row in playerids.iter_rows(named=True)
        if row["sleeper_id"] and row["name"]
    }


if __name__ == "__main__":
    mapping = gsis_to_sleeper()
    print(f"{len(mapping)} gsis_id -> sleeper_id mappings")
    names = sleeper_names()
    print(f"{len(names)} sleeper_id -> name/position entries")
    print("sleeper_id 13342:", names.get("13342"))
