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


if __name__ == "__main__":
    mapping = gsis_to_sleeper()
    print(f"{len(mapping)} gsis_id -> sleeper_id mappings")
