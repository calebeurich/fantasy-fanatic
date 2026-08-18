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

from analysis import format_support, team_state, roster_needs, trade_targets, waiver_wire, roster_detail, warm
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
    """Each team's read - THE authoritative classification (never re-derive one from
    get_roster_detail) - with cornerstones, sellable players and tradeable surplus. Pass
    owner_name for one team; omit only when you need every team.

    THE HEADLINE for any team is its three-tier read, on every row:
      `contention_rank` of `of_teams` - the tertile it sits in is tier 1.
      `alignment` - "aligned" (roster composition agrees with what its rank asks;
        the path is a continue verb) or "unaligned" (a real decision is pending).
      `path` - the action: "contend" (optionally "- on a clock") / "wait" (plain =
        nothing aging out; "- production is arriving" = a wave coming) / "build" when
        aligned; "press" / "decide" / "sell" when not. Waiting and building are earned
        by ASCENDING or by having no clock running at all; contending is earned by rank.
        A middle roster with pieces aging out and no wave arriving reads decide. The lean comes from rank: press = convert future into now, sell =
        convert aging value into future, and decide (a middle rank) has NO lean -
        letting the season decide is legitimate.
      `path_reason` - why, in words. OPEN with path + path_reason and support it with
        the measurements - never open with a label and walk it back.
      `posture_note` - what the path means for premiums and tempo, plus the measured
        numbers behind the read. Use its wording: these are roster-composition
        measures on PROJECTED points a game (`starting_production`, `pct_of_best` -
        Sleeper's season projections under this league's scoring), never records or
        wins, because no games have been played. Premium rules live here: "contend - on a clock" is the one
        path where buying production is worth a premium; plain "contend" and "press"
        buy at fair prices; "sell" converts aging value while it still has a price.
      `path_edge`, when present - the read sits within refresh noise of a tier line
        and names the path across it: present that path's advice as live alongside
        this one, and a different read on a re-ask is pricing noise, never the team
        changing direction.
      `aging_chips` - aging-out pieces that are still real trade chips, a FACT on
        every row: on a rebuild they are the "one clear sell" that made it unaligned;
        on a contender they are pieces it rides, never a sell list. `idle_youth` - a
        contender's cornerstones priced on runway who aren't in the lineup.

    Hard rules:
    - THE DIRECTION GATE: contenders acquire production and part with future pieces;
      rebuilds acquire future value and part with production; only the middle swings
      both ways. The tools already cut cross-direction suggestions - never reinvent
      one (no pitching a contender's producers to anyone, no offering rentals to a
      rebuild), whatever a roster hole seems to suggest.
    - `years_to_decline` is an age-curve clock, NOT a contract - never dress runway
      as an "expiring contract" (a live answer did). Real contract terms exist only
      in get_roster_detail's `contract` field; cite them from there or not at all.
    - The team's `path` OUTRANKS every piece-level note. A `price_note` describes the
      ask IF that piece moves - it is never a reason to move him. Never assemble a
      sell plan for a team whose path is contend/press-to-buy out of its own pieces'
      pricing notes (a live answer told a contend-on-a-clock team, whose aging RBs
      ARE its rank, to liquidate all three - that is rebuild advice aimed at a
      contender; its actual move was buying).
    - Raise `clock_mismatch_note` whenever describing the team, not only when asked
      about selling - it names the starters whose clock disagrees with the roster.
      It is also the PATH-SANCTIONED exception to the rule above: converting the
      pieces it NAMES is consistent with contending - present those conversions as
      live moves, not as something to merely monitor.
    - `years_to_decline` decides WHO to sell, never age: the curves give a 31.8-year-old
      pocket passer 6.2 years and a 28.0-year-old running QB 4.0. It cuts across lists - a
      short-runway cornerstone is often a rebuilder's right sale.
    - A cornerstone is ALSO a trade chip: being the foundation raises his price, it never
      removes him from the table.
    - `sellable`/`tradeable_surplus`/`win_now_core` are lists of pieces that could move
      INDIVIDUALLY, never a set to combine. Naming two of them as one offer prices a
      bundle, and dynasty value does not add across players (a live answer paired a
      3,650 TE with a 3,379 QB against a 4,473 target). One piece against one piece.
    - Read `lineup` before saying how many of a position a team can start.
    - `leverage`/`leverage_note`, when present, say what a team could BECOME (convertible:
      weak lineup, top-third war chest; mortgaged: the reverse) - raise it whenever asked
      whether a team is good, because a "sell" path on a convertible team understates it."""
    from analysis.league import context
    teams = team_state.classify_league(league_id)
    if owner_name:
        # pick_owner matches handle OR custom team name and raises listing the real
        # options - the old handle-only substring filter answered a team-name query
        # with an empty list, which the model read as "no such team".
        teams = [context(league_id).pick_owner(owner_name, teams)]
    # The lineup shape ships with every team, not only from check_league_format. A live run
    # called check_league_format, got superflex, and still wrote "three QBs in a league that
    # only starts one" a few hundred tokens later - then built its whole recommendation on
    # that. Format read once at the top of a conversation does not survive to the point where
    # it matters; attached to the roster it is being reasoned about, it does.
    fmt = sleeper.describe_format(context(league_id).league)
    # One dialect: window/state/flavor are internal measurement labels (window still
    # dispatches the trade paths); the model reads the tier trio + posture_note only.
    # Two vocabularies in one payload is how a model ends up saying "Middling window"
    # about a team whose chip says decide (LOGIC.md, "The window-label retirement").
    teams = [{k: v for k, v in t.items()
              if k not in ("window", "state", "flavor", "flavor_note", "trajectory",
                           "trajectory_rank")} for t in teams]
    # Wrapped in a dict rather than returned as a bare list - this MCP SDK version
    # splits a top-level list return into one content block per item instead of one
    # JSON array, which is fragile to rely on. A dict always serializes as one block.
    return {"teams": teams, "lineup": _lineup_note(fmt)}


@mcp.tool()
def get_roster_needs(league_id: str) -> dict:
    """Positional needs for every team as a list, each entry carrying the owner's NAME
    (use it - a live answer addressed a manager as "Owner 637083353878695936" because
    this payload was keyed by id alone). A need's `level` names the
    SHAPE of the problem - critical (a body is needed NOW; whether what's started is
    good rides in `body_solid` and the note, so never read critical alone as "their
    players are bad"), weak (an upgrade eventually, NOT more depth) - and each entry's
    `note` states the finding in words: use that wording, never a generic "thin".
    Fine positions are omitted; mid-league is not a need.

    `drop_if_injured`/`exposure`/`position_miss_rate` measure injury exposure, which is
    NOT a need - raise it when asked about depth or risk, never as a hole in the lineup.
    Exposure already accounts for flex refills, so in superflex two good QBs plus a cheap
    third is a sound build rather than a gap."""
    # Names attached HERE rather than in `league_needs`, whose {owner_id: {POS: ...}}
    # shape every analysis caller iterates by position - a sibling "owner" key would
    # read as a position. The model is the only reader that needs the name.
    from analysis.league import context
    ctx = context(league_id)
    return {"teams": [{"owner": ctx.owner_names.get(owner_id, "Unknown"),
                       "owner_id": owner_id, "needs": needs}
                      for owner_id, needs in roster_needs.league_needs(league_id).items()]}


# A tool result over ~50KB on the wire is not delivered: the harness replaces the whole
# thing with a 2KB preview and a file path the model cannot open. It is silent - no tool
# error, nothing in the log - and the model answers from that 2KB, which is how a live
# run built a three-player package out of the only names it could still see
# (LOGIC.md, "The tool result the model never saw"). Budget in JSON chars with margin:
# 43,225 chars measured 52.6KB on the wire, so ~1.22 bytes per char.
WIRE_BUDGET_CHARS = 36_000


def _within_wire_limit(league_id: str, owner_name: str, max_per_position: int,
                       stance: str = None) -> dict:
    """The full report if it fits, otherwise the same report with shorter lists.

    Shrinking `max_per_position` is the right lever because it is the one the tool
    already documents: every block survives, each just carries fewer entries, so no
    KIND of advice silently disappears - which is what dropping whole blocks would do.
    """
    import json

    for attempt in range(max_per_position, 0, -1):
        result = trade_targets.find_targets(league_id, owner_name, attempt, stance=stance)
        if len(json.dumps(result)) <= WIRE_BUDGET_CHARS or attempt == 1:
            break

    # The per-position cap does not bound the sell lists (a deep roster has as many
    # sellable pieces as it has), so a Middling team - which ships BOTH paths - can still
    # be over at one per position. Trim the longest list repeatedly until it fits: any
    # entry dropped is the lowest-ranked of its own block, and every block survives.
    dropped = False
    while len(json.dumps(result)) > WIRE_BUDGET_CHARS:
        lists = [(len(json.dumps(v)), k, v, d)
                 for d in (result, result.get("push") or {}, result.get("pivot") or {})
                 for k, v in d.items() if isinstance(v, list) and len(v) > 1]
        if not lists:
            break
        _, key, longest, owner_dict = max(lists)
        owner_dict[key] = longest[:max(1, len(longest) // 2)]
        dropped = True

    if attempt < max_per_position or dropped:
        result["truncation_note"] = (
            f"This report did not fit in one tool result, so the lists are shortened "
            f"(capped at {attempt} per position, longest lists trimmed further). Every "
            f"block is still here and each keeps its best-ranked entries - nothing is "
            f"missing except lower-ranked names. Do not describe these lists as the whole "
            f"market; if the answer needs more at one position, say so and ask again.")
    return result


@mcp.tool()
def get_trade_targets(league_id: str, owner_name: str, max_per_position: int = 3,
                      stance: str = None) -> dict:
    """Trade recommendations for one team, shaped by its path (see get_team_state):
    buy blocks for contend/press, sell blocks for sell/build, or BOTH (keyed push/pivot)
    for wait/decide.

    `stance`: pass ONLY when the user declares their own direction ("I want to press
    this season", "I'm selling") that disagrees with the team's label - one of
    press/contend/buy/decide/wait/sell/build. The manager outranks the default: the
    report runs their chosen side's paths, and `stance_note` says how to present the
    choice next to the measured read. Never pass it on your own initiative.

    For a buying path with no hole, `production_adds` is the rental market: sellers'
    production-priced pieces that would START for this team today, with the margin over
    the starter displaced - a 2nd for one of these is an ordinary, directionally sound
    trade, and it is the natural second leg after a consolidation move opens a slot.

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
    - max_per_position caps each list - call again with a higher number for more.
    - These lists answer "who fits best", not "who is available": if the user asks about
      a SPECIFIC player who isn't in them, that absence is not a verdict - call
      get_player_outlook for him instead of declaring him untradeable."""
    return _within_wire_limit(league_id, owner_name, max_per_position, stance)


@mcp.tool()
def get_player_outlook(league_id: str, player_name: str, your_team: str = None) -> dict:
    """Call this whenever the user names a SPECIFIC player - "how do I trade for X",
    "what would it take to get X", "should I go after X". Absence from
    get_trade_targets' lists is NEVER a verdict on a named player (those lists are
    ranked, capped and filtered to scored needs); this answers about him directly.

    Returns one player with both sides of the call: who owns him, that owner's path,
    `availability` (why the owner would or wouldn't move him - use its wording),
    `friction` (same vocabulary as everywhere), what the owner is short at, and - when
    your_team is given - `your_fit` with `offer_any_one_of`: single pieces THAT owner
    would want back, alternatives never a bundle. It does not price the deal; if the
    user believes the player is under- or over-valued, treat that as their thesis per
    your rules and reason conditionally on it."""
    return trade_targets.player_outlook(league_id, player_name, your_team)


@mcp.tool()
def evaluate_trade(league_id: str, owner_a: str, sends_a: list[str],
                   owner_b: str, sends_b: list[str],
                   stance_a: str = None, stance_b: str = None) -> dict:
    """Judge ONE concrete proposed trade from both seats. Call this whenever the user
    lays out a specific deal ("X for Y", "would you do A + a 2027 1st for B"). Names
    are matched fuzzily within what each sender actually holds - players by name, picks
    like "2027 1st" or "2026 Pick 1.03" - and `problem` says what didn't resolve.

    Each side carries the LENS its own path sets (`lens`): a buying path (contend,
    press) is judged on its STARTING LINEUP - `lineup_production_delta` after the lineup
    re-settles and `need_changes`, where a hole newly OPENED matters as much as one
    closed; a selling path (sell, build) is judged on DYNASTY VALUE overall with package
    concerns; wait/decide sees both. `goal` is that judgment in one sentence - lead with
    it for each side. `read` carries the rest: who holds the best single piece, timeline
    flags (a rebuild taking a rental; an aligned contender taking back futures), and
    `BALLPARK` lines - the measured consolidation premium for N-for-1 packages. Quote
    the ballpark as the tool's benchmark from real trades and STOP there: it is the one
    place this project sums values, and only because the benchmark was measured that
    way; never extend the arithmetic, never call a side the winner, never say "fair".
    When there is no BALLPARK line (a pick is the best piece; a plain swap) there is no
    benchmark - never write one. A trade can be right for both seats; say so when it is.

    `stance_a`/`stance_b`: pass ONLY when the user declares that side's branch ("kieran
    wants to pivot", "I'm pressing this year") - one of press/contend/buy/sell/build/
    pivot/wait/decide. It switches that side's lens and adds `stance_note`; the
    measured read still rides. Never pass it on your own initiative."""
    from analysis import trade_eval
    return trade_eval.evaluate_trade(league_id, owner_a, sends_a, owner_b, sends_b,
                                     stance_a, stance_b)


@mcp.tool()
def evaluate_trade_sequence(league_id: str, legs: list[dict]) -> dict:
    """Judge a PLAN of two or more trades in order - "I'd do X, and then Y". Each leg is
    {owner_a, sends_a, owner_b, sends_b} (plus optional stance_a/stance_b, same rule as
    evaluate_trade), and each is judged on the rosters the legs before it produced, so a
    later leg can close the hole an earlier one opened - a consolidation move plus its
    backfill is the common shape. `cumulative` is each team's net position against
    TODAY (lineup after everything re-settles; needs from before the first leg to
    after the last). Lead with the cumulative line for the team asking, then each
    leg's own reads. Use this whenever a user chains trades ("and then", "also",
    "after that"); use evaluate_trade for a single deal. Exactly two legs - a move
    and its backfill; a longer plan is judged two legs at a time, never as one chain."""
    from analysis import trade_eval
    return trade_eval.evaluate_sequence(league_id, legs)


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
    warm.start_from_env(delay=5.0)  # after the handshake, never competing with it
    mcp.run()
