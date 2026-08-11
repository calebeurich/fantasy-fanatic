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
    """Strategic window per team, with each team's cornerstones
    (long-term foundation), sellable players (real trade chips, split declining=urgent
    vs prime=situational), and tradeable surplus (young non-core depth). Pass
    owner_name for a question about one team (much smaller result, and this IS the
    authoritative classification - don't re-derive a team's window from
    get_roster_detail instead). Omit owner_name only when you actually need every
    team, e.g. comparing teams or finding trade partners.

    The `window` is one of four, derived from two measured axes - `contention` (this
    team's CURRENT starting production, ranked against the league) and `trajectory`
    (how much of that production comes from ascending vs declining players):

      Push    - contender whose roster declines if it waits. Buy production, spend picks.
      Contend - contender that is steady or rising. No clock; don't pay premiums.
      Ascend  - fringe now but rising. Can push, but its own young players supply next
                season's production for free, so pushing costs a premium. Both paths.
      Rebuild - anything else. Sell decline, accumulate youth and picks.

    Every row carries `window_note`, which states the measurements behind the label
    (rank, % of the league's best lineup, ascending/declining shares). Use that wording -
    do NOT describe these as records, wins, or points scored; no games have been played.
    `starter_value` is dynasty value and answers a different question ("what is this
    roster worth"); `starting_production` is what decides the window."""
    teams = team_state.classify_league(league_id)
    if owner_name:
        teams = [t for t in teams if owner_name.lower() in t["owner"].lower()]
    # Wrapped in a dict rather than returned as a bare list - this MCP SDK version
    # splits a top-level list return into one content block per item instead of one
    # JSON array, which is fragile to rely on. A dict always serializes as one block.
    return {"teams": teams}


@mcp.tool()
def get_roster_needs(league_id: str) -> dict:
    """Positional needs for every team, keyed by owner_id, with the shape of each need.

    A need is one of three different problems, and they call for opposite fixes:
    "critical" (too few startable players AND a bottom-of-the-league group - needs both
    bodies and quality), "top-heavy" (too few startable players but the ones there are
    good - needs a body, NOT an upgrade), and "weak" (the slots are fillable but the
    group ranks poorly - needs an upgrade, i.e. consolidating depth into one better
    starter, NOT more depth). Positions that are fine are omitted; a team ranking
    mid-league at a position is not a need. Each entry carries the rank, the starting
    production, the league median, and a `note` that states the finding in words - use
    that wording rather than describing every need as "thin"."""
    return roster_needs.league_needs(league_id)


@mcp.tool()
def get_trade_targets(league_id: str, owner_name: str, max_per_position: int = 3) -> dict:
    """Trade recommendations for one team, shaped by its window (see get_team_state):
    buy targets for Push/Contend, sell and acquire targets for Rebuild, or BOTH paths
    (labeled push/pivot) for Ascend. An Ascend result also carries "timing_note",
    explaining why the two paths cost differently - surface that reasoning rather than
    just listing both sets of names.

    Also returns draft picks, in the direction that team should be moving them:
    "picks_to_trade_away" for a contender and "picks_to_acquire" for a rebuilder.
    Picks are currency, not production - a first becomes a rookie at the next offseason's
    draft, which is another upside asset and the opposite of what a contender needs, so
    their value to a contender is only in what they can be traded FOR. Player entries carry
    "tier" (core piece vs depth) and "pick_equivalent" - depth is real but shouldn't
    anchor an offer. "efficiency_swaps", when present, are value decisions rather than
    lineup upgrades: the two players produce near-identically this season.

    "persuasion_targets", when present, are a DIFFERENT KIND of suggestion and must be
    presented as such: aging production held by teams that are NOT currently sellers.
    Each carries "why_they_might_listen" (a falling roster, a core that hasn't won) and
    "cost_note". Never describe these as available or as fits - acquiring one means
    persuading that manager to change direction, which prices above market. They are
    ranked by "production_per_cost" (current production per unit of trade value), so the
    top entry is often NOT the most valuable player - that is deliberate and worth saying,
    because the cheaper name delivers more of what a contending team is buying.

    max_per_position caps results per position - call again with a higher number if
    asked for more, rather than assuming this is the full list."""
    return trade_targets.find_targets(league_id, owner_name, max_per_position)


@mcp.tool()
def get_mutual_swaps(league_id: str, owner_name: str) -> dict:
    """Two-way trade fits between this team and another team still trying to win, where
    each side has a positional surplus (real spare starting-caliber depth) that's the
    other's need - both teams improve, neither gives up a core piece. Different from
    get_trade_targets, which only matches this team against rebuilding teams' sell
    candidates in one direction. Each swap carries a "balance" block with both sides'
    totals - the two are checked to be of comparable value, but this is a shape that
    could work, not a priced offer. Use this when the question is about trading with
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
