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
from sources import sleeper


def _lineup_note(fmt: dict) -> str:
    """How many of each position this league actually starts, in words, so an answer can't
    contradict the format it is reasoning about."""
    if fmt["is_superflex"]:
        return (f"SUPERFLEX: {fmt['num_qbs']} QB slots start every week. A second and even "
                f"third quarterback is a startable asset here, not a backup, and QBs are the "
                f"scarcest commodity in the format - price them accordingly.")
    return (f"{fmt['num_qbs']} QB slot starts each week (not superflex), so a backup "
            f"quarterback is worth little to anyone.")

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
    vs prime=situational), and tradeable surplus (young non-core depth). A cornerstone is
    ALSO a trade chip - never say a player is untouchable or unavailable because he is one.
    Being the foundation raises his price, it does not remove him from the table, and
    `get_trade_targets` marks him with `cornerstone` friction when it lists him. Pass
    owner_name for a question about one team (much smaller result, and this IS the
    authoritative classification - don't re-derive a team's window from
    get_roster_detail instead). Omit owner_name only when you actually need every
    team, e.g. comparing teams or finding trade partners.

    The `window` is one of four, derived from two measured axes - `contention` (this
    team's CURRENT starting production, ranked against the league) and `trajectory`
    (how much of that production comes from ascending vs declining players):

      Push    - contender whose roster declines if it waits. Buy production, spend picks.
      Contend - contender that is steady or rising. No clock; don't pay premiums.
      Middling- middle of the league, either trajectory. Both paths are shown and waiting
                to see how the season starts is legitimate. `window_note` says whether
                patience is free here: only a rising roster supplies next season's
                production by itself.
      Rebuild - bottom-third in current production. Sell decline, accumulate youth/picks.

    Also returns `lineup`, the league's actual starting requirements. Read it before saying
    anything about how many players at a position a team can start or how spare a backup is.

    **Every player entry carries `years_to_decline`, and it decides WHO to sell.** Age alone
    does not: this project's own curves give a 31.8-year-old pocket passer 6.2 years and a
    28.0-year-old running quarterback 4.0, because they decline for different reasons. Sort
    by that number, never by age, when choosing which of several similar players to move -
    and note it cuts across the lists. A `cornerstone` with the fewest years left is often the
    right sale for a rebuilding team even though he is not in `sell_candidates`, because he
    fetches the most now and fits the timeline least. Both a human expert and an earlier run
    of this agent got this backwards on the same roster, recommending the older man be traded
    while the numbers said keep him.

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
    # The lineup shape ships with every team, not only from check_league_format. A live run
    # called check_league_format, got superflex, and still wrote "three QBs in a league that
    # only starts one" a few hundred tokens later - then built its whole recommendation on
    # that. Format read once at the top of a conversation does not survive to the point where
    # it matters; attached to the roster it is being reasoned about, it does.
    from analysis.league import context
    fmt = sleeper.describe_format(context(league_id).league)
    # Wrapped in a dict rather than returned as a bare list - this MCP SDK version
    # splits a top-level list return into one content block per item instead of one
    # JSON array, which is fragile to rely on. A dict always serializes as one block.
    return {"teams": teams, "lineup": _lineup_note(fmt)}


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
    (labeled push/pivot) for Middling. A Middling result also carries "timing_note",
    explaining why the two paths cost differently - surface that reasoning rather than
    just listing both sets of names.

    A Rebuild result returns "sell_candidates" (under two years before their decline cutoff -
    this includes prime-age players, not just declining ones), "situational" (everything
    else worth selling, ordered by how much of the player's price is CURRENT production - the
    top entry is the best sell-high, not the most valuable player) and "acquire_targets".
    A young rebuilding roster legitimately has an empty "sell_candidates"; that is not an
    error, and its window_note will say so rather than telling it to sell decline it lacks.
    How hard to press the sale is NOT yours to infer from the runway - "sell_clock_note"
    alongside the list says it, and it differs by mode: a Rebuild team is told to move the
    piece, while the same names under a Middling team's "pivot" are the cost of waiting and
    the deadline on choosing, NOT players to sell today. Repeat that distinction; do not
    flatten a Middling pivot list into urgent sell advice.

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
    often the single biggest chip available precisely because his value is future.
    **"friction" is one vocabulary used on BOTH sides of the table**, and an empty list means
    easy. Each entry is {flavor, why}. On the sell side (my_offers) the flavors are
    `cornerstone` (you are building around him - the hardest ask on your own roster, a price
    and not a veto, so never silently drop him or call him untradeable; he is usually the
    biggest chip there is) and `costs_you_production` (moving him drops your own lineup by a
    stated amount, after it refills). On the buy side they are `cornerstone`, `never_trades`,
    `beyond_your_best_chip` and `needs_a_pivot`. State the `why` whenever you name a player
    that carries friction, and treat no-friction entries as the ones to lead with.

    Every "value_upgrades" return also carries "their_reason": why the CURRENT owner would
    part with him. This is the half of a trade that is easy to forget. A Rebuild owner is
    already selling that kind of production, so no persuasion is needed. Anyone else has to be
    argued into it, and then the entry carries `needs_a_pivot` friction and the reason says
    what the argument is - their window and the player's not lining up, or their roster falling,
    or in the weakest case that they have no reason to sell at all. NEVER present a player held
    by a contender as though he were available from a seller; say which it is.

    "value_upgrades", when present, are the strongest single finding this tool produces and
    should usually lead the answer for a team trying to win now. Each names a current starter
    and several players who are a better thing to own than he is, every one of them costing
    LESS in dynasty value. Each return carries "kind", and it changes what you should say:
      upgrade        - produces MORE this season too. Raises the lineup and frees trade capital
                       at once; the replaced starter drops to depth.
      value_decision - produces very slightly LESS (at least 98% of it). The lineup is
                       unchanged and the gain is purely the value released: worth doing at a
                       good price, never worth chasing. Never call this raising the lineup.
      conversion     - produces MEANINGFULLY less (down to 90%). This GIVES UP production to
                       free value. State the loss in the same breath as the gain. It only
                       appears for teams with no clock; never present it as free.
    A return carrying "already_mine" is on the asking team's OWN BENCH: no trade is required at
    all, just promote him and sell the starter above him. Lead with that one when it exists,
    even though its production line looks worst - it is the only move here that costs nothing
    to arrange. There is one entry per upgradeable starter, so the list is a map of where this
    lineup can be beaten. Do NOT price a package around them or add the pieces up - value is
    not additive across players, and "this holding beats that one" is the entire claim. Say the
    age: these are cheaper precisely because they are older, which is the trade-off the asking
    team has to weigh against its own timeline.

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

    "targets" and "long_shots" are two lists on purpose, and the order you present them in
    matters more than the values in them. "targets" is who to ring first: nothing structural
    is in the way. Every "long_shots" entry carries "friction" - a list of {flavor, why} saying
    what IS in the way -
    the owner has never traded, the player is a cornerstone on that roster, or he costs more
    than the asking team's biggest single chip. State the blocker whenever you name one, lead
    with "targets" even when a long shot is a bigger name, and never imply a long shot is
    merely expensive when the blocker says it is unreachable one-for-one.

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
