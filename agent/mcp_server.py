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
    roster worth"); `starting_production` is what decides the window.

    `leverage` and `leverage_note`, when present, describe what a team could BECOME rather
    than what it is, by comparing `asset_rank` (every player plus every pick it owns) against
    `contention_rank` (what it starts). "convertible" = a weak lineup sitting on a top-third
    war chest; that team is not simply bad, it has an unspent option and is usually right to
    hold and see how the season opens before committing. "mortgaged" = a strong lineup with
    little behind it; this season is close to the whole return and reloading will be hard.
    Raise this whenever asked whether a team is good, or what it should do - a `Rebuild`
    label on a convertible team badly understates it. Most teams get neither, which means
    their two ranks agree."""
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
    that wording rather than describing every need as "thin".

    Every position ALSO carries `drop_if_injured` (production lost if the last starter
    there goes down, before a replacement takes over), `exposure_rank` and `exposure`
    (high/typical/low against the league). **Exposure is not a need** - a team whose
    starting lineup is entirely fine can still be one injury from disaster, and those are
    different problems with different fixes. Raise it when asked about depth, injuries or
    roster risk; do not report it as a hole in the lineup.

    Exposure is still the magnitude *if* an injury happens rather than an expected loss, but
    the likelihood half is now measured: `position_miss_rate` is the share of roster weeks
    players at that position have actually missed over the last three seasons (QB ~0.11 vs
    RB ~0.19), so an equal drop-off at QB and RB is genuinely not equally worrying and you
    can now say why with a number. Exposure already accounts for flex slots: in superflex a
    lost QB is backfilled by the best remaining player of any position, so two good QBs plus
    a cheap third is a sound build rather than a gap to fix."""
    return roster_needs.league_needs(league_id)


@mcp.tool()
def get_trade_targets(league_id: str, owner_name: str, max_per_position: int = 3) -> dict:
    """Trade recommendations for one team, shaped by its window (see get_team_state):
    buy targets for Push/Contend, sell and acquire targets for Rebuild, or BOTH paths
    (labeled push/pivot) for Ascend. An Ascend result also carries "timing_note",
    explaining why the two paths cost differently - surface that reasoning rather than
    just listing both sets of names.

    A Rebuild result returns "sell_candidates" (under two years before their decline cutoff,
    so urgent - this includes prime-age players, not just declining ones), "situational" (everything
    else worth selling, ordered by how much of the player's price is CURRENT production - the
    top entry is the best sell-high, not the most valuable player) and "acquire_targets".
    A young rebuilding roster legitimately has an empty "sell_candidates"; that is not an
    error, and its window_note will say so rather than telling it to sell decline it lacks.

    Also returns draft picks, in the direction that team should be moving them:
    "picks_to_trade_away" for a contender and "picks_to_acquire" for a rebuilder.
    Picks are currency, not production - a first becomes a rookie at the next offseason's
    draft, which is another upside asset and the opposite of what a contender needs, so
    their value to a contender is only in what they can be traded FOR. Player entries carry
    "tier" (core piece vs depth) and "pick_equivalent" - depth is real but shouldn't
    anchor an offer. An entry in "my_offers" carrying "lineup_cost" is a current STARTER
    who is nonetheless offerable, and the number is how much current production the lineup
    loses if he goes, after refilling itself optimally. Say that number when you name him -
    it is a cost, not a reason to omit him, and for a Push team an ascending starter is
    often the single biggest chip available precisely because his value is future. "efficiency_swaps", when present, are value decisions rather than
    lineup upgrades: the two players produce near-identically this season, and
    "efficiency_swap_framing" says why that matters for THIS team's window - a closing
    window converts future premium into capital, a healthy contender is just taking
    profit with no urgency.

    "persuasion_targets", when present, are a DIFFERENT KIND of suggestion and must be
    presented as such: aging production held by teams that are NOT currently sellers.
    Each carries "why_they_might_listen" - a falling roster, a core that hasn't won, or a
    mismatch between that owner's window and the player's (a team contending now AND later
    can afford to move an aging starter; one aging into its own window cannot) - plus a
    "cost_note". Never describe these as available or as fits - acquiring one means
    persuading that manager to change direction, which prices above market. They are
    ranked by "production_per_cost" (current production per unit of trade value), so the
    top entry is often NOT the most valuable player - that is deliberate and worth saying,
    because the cheaper name delivers more of what a contending team is buying.

    Where a persuasion target carries "you_could_offer" and "why_it_fits", LEAD WITH THAT -
    it names what this team holds that the other owner actually wants, which is the
    difference between a trade idea and a wish. Note an owner with no positional hole can
    still be a real partner: a team contending now and tilting ascending wants value that
    scores this season and lasts, so it may well move an aging starter for it.

    The buy targets above need no persuasion at all. Do not let a bigger persuasion name
    crowd them out - a smaller player from a team already selling is a far easier trade than
    talking a contender into changing direction.

    "conversion_candidates" + "choice_note" appear only for a contender whose production is
    still tilting ascending. That team has TWO live paths and the answer must present both:
    stack more current production, or convert those aging starters into value matching the
    seasons the rest of its roster is built for. Its `window` is still "Contend" and that is
    correct - it contends either way, so this is a choice about HOW, not whether. Do not
    report it as indecision or as a downgrade, and do not recommend one path as the answer
    without giving the cost of the other.

    "stranded" + "stranded_note", when present, is the FIRST thing to raise. These players
    out-produce the weakest man in this team's own starting lineup and cannot be played, held
    out purely by positional capacity - a superflex roster's QB3, say. Their whole value to
    this team is what they fetch, in any window. Never describe them as depth or as bench
    players who aren't good enough; say what they produce and what the lineup starts instead.

    "depth_adds" + "depth_note" are NOT needs and must never be presented as fits. Each is a
    cheap body who would step into this lineup only if a starter at his position were out -
    real insurance, since byes are certain and injuries likely, but every one of them sits
    below the trade-relevance floor. Worth a late pick or a spare body, never a real asset.
    An empty list is a meaningful answer: it means the roster already covers itself.

    A target carrying "starter_caveat" starts for a REBUILDING team. Do not describe him as
    someone that owner is relying on - that reflects his value on the roster, not intent,
    and those players are exactly what a rebuilder wants to convert into picks.

    max_per_position caps results per position - call again with a higher number if
    asked for more, rather than assuming this is the full list."""
    return trade_targets.find_targets(league_id, owner_name, max_per_position)


@mcp.tool()
def get_mutual_swaps(league_id: str, owner_name: str) -> dict:
    """Two-way trade fits between this team and another team still trying to win, where
    each side has spare depth - a player NOT in its own starting lineup who still has real
    trade value - that happens to be the other's need - both teams improve, neither gives up a core piece. Different from
    get_trade_targets, which only matches this team against rebuilding teams' sell
    candidates in one direction. Each swap carries a "balance" block with both sides'
    totals - the two are checked to be of comparable value, but this is a shape that
    could work, not a priced offer. An empty list is common and meaningful: it needs BOTH
    teams to hold spare depth the other is short at, so a team with no needs of its own can
    never appear here even when it holds exactly the piece someone wants. Use this when the question is about trading with
    another specific contender, or "how do I improve without giving up my best guys."""
    return trade_targets.find_mutual_swaps(league_id, owner_name)


@mcp.tool()
def get_waiver_upgrades(league_id: str, owner_name: str = None) -> dict:
    """Unrostered players with real dynasty value that would upgrade a team, plus FAAB
    budget remaining per team. Pass owner_name to filter to one team; omit for the
    whole league."""
    return waiver_wire.league_upgrades(league_id, owner_name)


@mcp.tool()
def get_optimal_lineup(league_id: str, owner_name: str, without: list[str] = None) -> dict:
    """This team's best legal lineup, and what it becomes if `without` players are removed
    (player names, e.g. ["Jonathan Taylor"]).

    **Use this instead of working a lineup out yourself.** Filling FLEX and SUPER_FLEX
    slots is a deterministic optimisation with exactly one right answer, and reasoning
    about it in prose gets it subtly wrong. Any question of the form "what if X gets
    hurt", "what would I start", "who replaces Y", or "how much does losing Z cost"
    should call this rather than being reasoned through.

    Returns each starter with the slot they occupy, "production_lost", "promoted" (who
    enters the lineup) and "moved_slots" (who shifts slot). The cascade is the point: on a
    real roster, losing the RB2 slid the FLEX back into RB2 and pulled a TIGHT END into
    the vacated FLEX - not the backup WR the manager assumed - because FLEX accepts
    RB/WR/TE and the bench TE simply produced more."""
    return roster_detail.optimal_lineup(league_id, owner_name, without)


@mcp.tool()
def get_roster_detail(league_id: str, owner_name: str) -> dict:
    """Full player-by-player breakdown for one team: value, age, win-window bucket,
    starter/bench status, contract detail where available, and `miss_rate` - the share of
    roster weeks that player has actually missed over the last three seasons.

    `miss_rate` is **None for unknown, which is not zero**. It needs two seasons of history,
    so most rookies carry None; never read a missing rate as durability. Use it when asked
    about injury risk or depth - a starting lineup that is fine on paper reads differently
    when two of its starters have missed a third of their weeks.

    **Only material for players who could plausibly reach the lineup.** A high rate on a deep
    bench player is noise: he wasn't going to play anyway, so his availability changes
    nothing. Weigh it for starters and for the bodies immediately behind them, and don't
    report a fringe player's injury history as a roster risk."""
    return roster_detail.get_roster_rows(league_id, owner_name)


if __name__ == "__main__":
    mcp.run()
