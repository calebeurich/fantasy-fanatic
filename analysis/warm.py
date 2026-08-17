"""Pre-fetch everything a league's tools need, so no request pays for it. Cloud Run
scales to zero between visits, and a cold process pays ~10s of fetches (nflverse
alone is ~5s). The API process and every session's MCP-server subprocess each have
their own in-memory cache, so BOTH call this at boot - a warm API did nothing for the
agent's first tool call until the MCP server warmed itself too."""

import os
import sys
import threading

from . import roster_needs, team_state


def warm(league_ids: list[str]) -> None:
    for league_id in league_ids:
        try:
            team_state.classify_league(league_id)
            roster_needs.league_needs(league_id)
        except Exception as e:  # noqa: BLE001 - best effort, the request path will retry
            print(f"warm-up failed for {league_id}: {type(e).__name__}: {e}", file=sys.stderr)


def start_from_env() -> None:
    """Off the main thread: a failure here costs nothing but the warm-up."""
    league_ids = [l for l in os.environ.get("WARM_LEAGUES", "").split(",") if l]
    if league_ids:
        threading.Thread(target=warm, args=(league_ids,), daemon=True).start()
