"""MCP server exposing read-only dynasty fantasy football analysis as tools for an
agent. Every tool is a thin wrapper over an already-validated module - no new business
logic lives here. See LOGIC.md for the reasoning behind what each one computes.

Local stdio transport only - single user, no network exposure. See LOGIC.md's "MCP"
section for why that keeps this phase pure plumbing with no new risk surface.

Run: python -m agent.mcp_server
"""

import sys
from pathlib import Path

# Make this file runnable by absolute path from any working directory. The agent
# spawns it as a subprocess, and McpStdioServerConfig has no `cwd` option (checked
# the SDK type: command/args/env only), so we can't guarantee what directory it
# starts in. Without this, `from analysis import ...` below fails whenever the CWD
# isn't the repo root - which is exactly what happened on the first Cloud Run deploy:
# the server never registered, the agent was left with zero tools, and the model
# confabulated an explanation for why it couldn't answer.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP

from analysis import format_support, team_state, roster_needs, trade_targets, waiver_wire, roster_detail

mcp = FastMCP("fantasy-fanatic")


@mcp.tool()
def check_league_format(league_id: str) -> dict:
    """Call this first for any league_id not yet checked this conversation. Returns
    'full' (proceed normally), 'degraded' (shallow league - proceed but caveat any
    percentile-based numbers), or 'unsupported' (not a dynasty league - explain why
    and do not call any other analysis tool for this league)."""
    return format_support.assess_format(league_id)


@mcp.tool()
def get_team_state(league_id: str, owner_name: str = None) -> dict:
    """Win-Now / Middling / Rebuilding classification, with each team's cornerstones
    (long-term foundation), sellable players (real trade chips, split declining=urgent
    vs prime=situational), and tradeable surplus (young non-core depth). Pass
    owner_name for a question about one team (much smaller result, and this IS the
    authoritative classification - don't re-derive a team's window from
    get_roster_detail instead). Omit owner_name only when you actually need every
    team, e.g. comparing teams or finding trade partners."""
    teams = team_state.classify_league(league_id)
    if owner_name:
        teams = [t for t in teams if owner_name.lower() in t["owner"].lower()]
    # Wrapped in a dict rather than returned as a bare list - this MCP SDK version
    # splits a top-level list return into one content block per item instead of one
    # JSON array, which is fragile to rely on. A dict always serializes as one block.
    return {"teams": teams}


@mcp.tool()
def get_roster_needs(league_id: str) -> dict:
    """Positional needs (critical or thin) for every team, keyed by owner_id."""
    return roster_needs.league_needs(league_id)


@mcp.tool()
def get_trade_targets(league_id: str, owner_name: str, max_per_position: int = 3) -> dict:
    """Trade recommendations for one team: buy targets if Win-Now, sell/acquire
    targets if Rebuilding, or both paths (labeled push/pivot) if Middling.

    Also returns draft picks, in the direction that team should be moving them:
    "picks_you_could_spend" for a Win-Now team (converting future value into current
    production is the point of the window) and "picks_to_acquire" for a Rebuilding one,
    listing picks held by contenders, to whom they are worth less. Player entries carry
    "tier" (core piece vs depth) and "pick_equivalent" - depth is real but shouldn't
    anchor an offer. "efficiency_swaps", when present, are value decisions rather than
    lineup upgrades: the two players produce near-identically this season.

    max_per_position caps results per position - call again with a higher number if
    asked for more, rather than assuming this is the full list."""
    return trade_targets.find_targets(league_id, owner_name, max_per_position)


@mcp.tool()
def get_mutual_swaps(league_id: str, owner_name: str) -> dict:
    """Two-way trade fits between this team and another Win-Now/Middling team, where
    each side has a positional surplus (real spare starting-caliber depth) that's the
    other's need - both teams improve, neither gives up a core piece. Different from
    get_trade_targets, which only matches this team against Rebuilding teams' sell
    candidates in one direction. Use this when the question is about trading with
    another specific contender, or "how do I improve without giving up my best guys."""
    return trade_targets.find_mutual_swaps(league_id, owner_name)


@mcp.tool()
def get_waiver_upgrades(league_id: str, owner_name: str = None) -> dict:
    """Unrostered players with real dynasty value that would upgrade a team, plus FAAB
    budget remaining per team. Pass owner_name to filter to one team; omit for the
    whole league."""
    return waiver_wire.league_upgrades(league_id, owner_name)


@mcp.tool()
def get_roster_detail(league_id: str, owner_name: str) -> dict:
    """Full player-by-player breakdown for one team: value, age, win-window bucket,
    starter/bench status, and contract detail where available."""
    return roster_detail.get_roster_rows(league_id, owner_name)


if __name__ == "__main__":
    mcp.run()
