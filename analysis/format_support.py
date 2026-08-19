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
            "format": format_line(fmt), **fmt,
        }

    return {"tier": "full", "reason": "Standard dynasty league, no caveats.", "format": format_line(fmt), **fmt}


def format_line(fmt: dict) -> str:
    """One sentence the agent can SAY - a tester's top note was that the assistant never
    mentioned the league was superflex or TE premium, even though every number under it
    was already priced for that format. The facts were computed and never spoken."""
    ppr = {1.0: "full PPR", 0.5: "half PPR", 0: "standard (non-PPR)"}.get(fmt["ppr"], f"{fmt['ppr']} PPR")
    tep = {"tep": ", TE premium", "teppp": ", heavy TE premium (TEP++)"}.get(fmt["tep_tier"], "")
    qb = "superflex (2 QB starters)" if fmt["is_superflex"] else "single-QB"
    return f"{fmt['num_teams']}-team {qb} dynasty, {ppr}{tep}"


if __name__ == "__main__":
    print(assess_format(sys.argv[1]))
