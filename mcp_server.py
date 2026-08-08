"""MCP server exposing read-only dynasty fantasy football analysis as tools for an
agent. Every tool is a thin wrapper over an already-validated module - no new business
logic lives here. See LOGIC.md for the reasoning behind what each one computes.

Local stdio transport only - single user, no network exposure. See LOGIC.md's "MCP"
section for why that keeps this phase pure plumbing with no new risk surface.

Run: python mcp_server.py
"""

from mcp.server import MCPServer

import format_support
import team_state
import roster_needs
import trade_targets
import waiver_wire
import roster_detail

mcp = MCPServer("fantasy-fanatic")


@mcp.tool()
def check_league_format(league_id: str) -> dict:
    """Call this first for any league_id not yet checked this conversation. Returns
    'full' (proceed normally), 'degraded' (shallow league - proceed but caveat any
    percentile-based numbers), or 'unsupported' (not a dynasty league - explain why
    and do not call any other analysis tool for this league)."""
    return format_support.assess_format(league_id)


@mcp.tool()
def get_team_state(league_id: str) -> dict:
    """Win-Now / Middling / Rebuilding classification for every team in the league,
    with each team's cornerstones (long-term foundation), sellable players (real
    trade chips, split declining=urgent vs prime=situational), and tradeable surplus
    (young non-core depth)."""
    # Wrapped in a dict rather than returned as a bare list - this MCP SDK version
    # splits a top-level list return into one content block per item instead of one
    # JSON array, which is fragile to rely on. A dict always serializes as one block.
    return {"teams": team_state.classify_league(league_id)}


@mcp.tool()
def get_roster_needs(league_id: str) -> dict:
    """Positional needs (critical or thin) for every team, keyed by owner_id."""
    return roster_needs.league_needs(league_id)


@mcp.tool()
def get_trade_targets(league_id: str, owner_name: str, max_per_position: int = 3) -> dict:
    """Trade recommendations for one team: buy targets if Win-Now, sell/acquire
    targets if Rebuilding, or both paths (labeled push/pivot) if Middling.
    max_per_position caps results per position - call again with a higher number if
    asked for more, rather than assuming this is the full list."""
    return trade_targets.find_targets(league_id, owner_name, max_per_position)


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
