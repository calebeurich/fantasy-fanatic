"""Save Sleeper's rest-of-season weekly projections as they stand today, so next year's
playoff-odds replay can test real ePPG instead of ppg-to-date. Sleeper keeps each past
week's final line, but never what it projected for week 12 back in week 6 - that view is
what ePPG and the odds run on, and it only exists if it is saved at the time.

Run weekly in season: python -m research.save_projections
Writes data/projections/<season>/asof-<date>-week<N>.json.gz (~0.4 MB; data/ is not
committed - these are Sleeper's numbers, not ours to publish).
"""

import datetime
import gzip
import json
from pathlib import Path

from sources import sleeper

LAST_WEEK = 18


def main() -> None:
    state = sleeper.get_nfl_state()
    season, week = state["season"], int(state["week"] or 0)
    if state.get("season_type") != "regular" or not week:
        print(f"Not in the regular season ({state.get('season_type')}) - nothing saved.")
        return
    weeks = {wk: [{k: r.get(k) for k in ("player_id", "team", "opponent", "stats")}
                  for r in sleeper.projection_rows(season, wk)]
             for wk in range(week, LAST_WEEK + 1)}
    out = Path("data/projections") / season / f"asof-{datetime.date.today()}-week{week}.json.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(gzip.compress(json.dumps({"season": season, "as_of_week": week,
                                              "weeks": weeks}).encode()))
    print(f"Saved weeks {week}-{LAST_WEEK} ({sum(map(len, weeks.values()))} lines) -> {out}")


if __name__ == "__main__":
    main()
