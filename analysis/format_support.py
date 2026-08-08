"""Whether this tool's dynasty-specific analysis actually applies to a given league.
Built and validated before any agent exists, so format-safety is a deterministic code
fact the agent inherits later rather than something it has to reason its way into
respecting on its own.

Smoke test: python -m analysis.format_support <league_id>
"""

import sys

from sources import sleeper

# Below this, percentile-based math (cornerstone/replacement thresholds) gets noisy -
# a 4-team league's "90th percentile" is one player. Not validated against a real
# shallow league yet (none of the leagues checked this project are that small) -
# flagged as an open item, revisit the exact cutoff once one is available.
MIN_TEAMS_FOR_FULL_SUPPORT = 8


def assess_format(league_id: str) -> dict:
    league = sleeper.get_league(league_id)
    fmt = sleeper.describe_format(league)

    if not fmt["is_dynasty"]:
        return {
            "tier": "unsupported",
            "reason": "This is a redraft/keeper league, not dynasty. Dynasty trade "
                      "values, age-curve win-window classification, and pick capital "
                      "don't mean the same thing (or anything) outside a dynasty "
                      "league - this tool's analysis doesn't apply here.",
        }

    if fmt["num_teams"] < MIN_TEAMS_FOR_FULL_SUPPORT:
        return {
            "tier": "degraded",
            "reason": f"Only {fmt['num_teams']} teams - percentile-based thresholds "
                      "(cornerstone value, positional replacement level) get noisy in "
                      "a shallow league. Numbers are still computed, but treat them "
                      "as rougher estimates than in a standard-sized league.",
        }

    return {"tier": "full", "reason": "Standard dynasty league, no caveats."}


if __name__ == "__main__":
    print(assess_format(sys.argv[1]))
