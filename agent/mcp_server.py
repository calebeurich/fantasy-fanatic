"""MCP server exposing read-only dynasty fantasy football analysis as tools for an
agent. Every tool is a thin wrapper over an already-validated module - no new business
logic lives here. See LOGIC.md for the reasoning behind what each one computes.

Local stdio transport only - single user, no network exposure. See LOGIC.md's "The agent stack"
section for why that keeps this phase pure plumbing with no new risk surface.

Run: python -m agent.mcp_server
"""

import functools
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
from sources import sleeper, degraded


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

_tool = mcp.tool


def _with_data_gaps(fn):
    """Attach `data_gap` to any tool result computed while a reference feed was unreachable.

    **Graceful degradation that only reaches stderr is invisible to the person asking.** Both
    nflverse call sites already fall back rather than crash and both warn on stderr, which serves
    the author running the CLI and nobody else - a friend asking the agent sees a confident answer
    and no warning at all. It is not a cosmetic gap: with usage roles missing, every age curve
    falls back to its position default, and on one live roster that moved Jared Goff from 6.2
    years of runway to 2.1, inverting which quarterback a rebuilding team should trade.

    Wrapped once here rather than edited into seven tools, because the next tool added would have
    forgotten it. The note is only known AFTER a call loads players, so it is read on the way out.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        result = fn(*args, **kwargs)
        gap = degraded.note()
        if gap and isinstance(result, dict):
            return {**result, "data_gap": gap}
        return result
    return wrapper


def tool_with_gaps():
    """`@mcp.tool()` plus the degradation note, so no tool can be registered without it."""
    def decorator(fn):
        return _tool()(_with_data_gaps(fn))
    return decorator


mcp.tool = tool_with_gaps


@mcp.tool()
def check_league_format(league_id: str) -> dict:
    """Call this first for any league_id not yet checked this conversation. Returns
    'full' (proceed normally), 'degraded' (shallow league - proceed but caveat any
    percentile-based numbers), or 'unsupported' (not a dynasty league - explain why
    and do not call any other analysis tool for this league)."""
    return format_support.assess_format(league_id)


@mcp.tool()
def get_team_state(league_id: str, owner_name: str = None) -> dict:
    """Strategic window per team - THE authoritative classification (never re-derive a
    window from get_roster_detail) - with cornerstones, sellable players and tradeable
    surplus. Pass owner_name for one team; omit only when you need every team.

    `window`, from two measured axes (current starting production ranked against the
    league, and ascending vs declining share of it):
      Push    - contender that declines if it waits. Buy production, spend picks.
      Contend - contender with no clock. Don't pay premiums.
      Middling- the middle. Both paths live; `window_note` says whether patience is free.
      Rebuild - bottom third. Sell decline, accumulate youth and picks.

    Hard rules:
    - Use `window_note`'s wording: these are roster-composition measures - never describe
      them as records, wins or points, because no games have been played.
    - `years_to_decline` decides WHO to sell, never age: the curves give a 31.8-year-old
      pocket passer 6.2 years and a 28.0-year-old running QB 4.0. It cuts across lists - a
      short-runway cornerstone is often a rebuilder's right sale.
    - A cornerstone is ALSO a trade chip: being the foundation raises his price, it never
      removes him from the table.
    - Read `lineup` before saying how many of a position a team can start.
    - `leverage`/`leverage_note`, when present, say what a team could BECOME (convertible:
      weak lineup, top-third war chest; mortgaged: the reverse) - raise it whenever asked
      whether a team is good, because a Rebuild label on a convertible team understates it."""
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
    """Positional needs for every team, keyed by owner_id. A need's `level` names the
    SHAPE of the problem - critical (bodies and quality), top-heavy (bodies, NOT an
    upgrade), weak (an upgrade, NOT more depth) - and each entry's `note` states the
    finding in words: use that wording, never a generic "thin". Fine positions are
    omitted; mid-league is not a need.

    `drop_if_injured`/`exposure`/`position_miss_rate` measure injury exposure, which is
    NOT a need - raise it when asked about depth or risk, never as a hole in the lineup.
    Exposure already accounts for flex refills, so in superflex two good QBs plus a cheap
    third is a sound build rather than a gap."""
    return roster_needs.league_needs(league_id)


@mcp.tool()
def get_trade_targets(league_id: str, owner_name: str, max_per_position: int = 3) -> dict:
    """Trade recommendations for one team, shaped by its window (see get_team_state):
    buy blocks for Push/Contend, sell blocks for Rebuild, or BOTH (keyed push/pivot) for
    Middling.

    Every block ships with its own note ("..._note", plus per-entry fields like
    "cost_note", "why_they_might_listen", "their_reason", "starter_caveat", "friction").
    THE NOTES ARE THE INSTRUCTIONS: each states what its block means, what to lead with,
    and what must not be claimed - repeat that framing rather than inventing your own,
    and never present entries from differently-framed blocks as interchangeable.

    Rules the notes cannot carry for you:
    - Present blocks in this order of strength: stranded first when present, then
      value_upgrades (lead with any "already_mine" return - it needs no trade), then
      targets before long_shots before persuasion_targets, then depth.
    - "friction" is one vocabulary on both sides of the table; an empty list means easy.
      State the `why` whenever you name a player carrying friction, and lead with
      no-friction entries. A `cornerstone` flavor is a price, never a veto.
    - A "kind" on a value-upgrade return changes the claim: upgrade (more production for
      less value), value_decision (lineup unchanged; the gain is the value released),
      conversion (GIVES UP real production - state the loss with the gain).
    - Never add player values or price a package (system rule 8); "this holding beats
      that one" is the entire claim. Say the age - cheaper usually means older.
    - An empty list is a meaningful answer (a young rebuild has nothing declining to
      sell; a covered roster needs no depth). Do not pad it from other blocks.
    - max_per_position caps each list - call again with a higher number for more."""
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
