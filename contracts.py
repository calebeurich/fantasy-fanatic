"""NFL contract data via nflverse (sourced from OverTheCap, redistributed under nflverse's
open data project - not a direct scrape of overthecap.com, which forbids scraping in its ToS).

Smoke test: python contracts.py
"""

from datetime import date

import nflreadpy as nfl


def get_contracts() -> dict[str, dict]:
    """Active NFL contracts keyed by Sleeper player_id."""
    contracts = nfl.load_contracts().filter(nfl.load_contracts()["is_active"])
    playerids = nfl.load_ff_playerids()

    gsis_to_sleeper = {
        row["gsis_id"]: str(row["sleeper_id"])
        for row in playerids.iter_rows(named=True)
        if row["gsis_id"] and row["sleeper_id"]
    }

    current_year = date.today().year
    result = {}
    # Sorted oldest-to-newest so a duplicate active contract for the same player
    # (rare, ~2 cases) resolves to the most recently signed one.
    for row in contracts.sort("year_signed").iter_rows(named=True):
        sleeper_id = gsis_to_sleeper.get(row["gsis_id"])
        if not sleeper_id:
            continue
        result[sleeper_id] = {
            "years_remaining": row["year_signed"] + row["years"] - current_year,
            "guaranteed": row["guaranteed"],
            "apy": row["apy"],
        }
    return result


if __name__ == "__main__":
    contracts = get_contracts()
    print(f"{len(contracts)} active contracts matched to a Sleeper player_id")
    burrow_id = "6770"
    print("Joe Burrow (sleeper_id 6770):", contracts.get(burrow_id))
