"""Local CLI agent for dynasty fantasy football analysis - Phase 2 of the agent
build-out plan. Wraps the Phase 1 MCP server (agent/mcp_server.py) with the Claude
Agent SDK, defaulting to Haiku (cheapest capable model, given a small starting API
budget) with a tightly scoped, read-only tool surface: nothing beyond the
fantasy-fanatic MCP tools (see TOOL_NAMES) is ever exposed, so there's no path to a
file/shell/network action outside this project's own already-validated analysis code.

Run: python -m agent.agent "<question>"   (from the repo root)
Needs ANTHROPIC_API_KEY in a .env file at the repo root (loaded automatically below) -
this is separate from any Claude.ai subscription, see LOGIC.md.
"""

import asyncio
import json
import re
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path

MCP_SERVER_PATH = Path(__file__).resolve().parent / "mcp_server.py"

# Windows console defaults to cp1252, which can't print an emoji or other non-Latin-1
# character the model happens to include in a response - crashes mid-print with no
# way to control what the model outputs. Same class of issue hit earlier in this
# project with a plain em dash; this fixes it at the source for any future character.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    AssistantMessage,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    UserMessage,
    ResultMessage,
    StreamEvent,
)

from analysis import trade_targets, roster_detail
from . import observability

# Explicit path, not bare load_dotenv(): the default searches upward from the *working
# directory*, so running the app from anywhere other than the repo root silently found
# no .env and left ANTHROPIC_API_KEY unset. Found via the dev preview server, which
# starts from a different cwd - every request failed with a generic error while
# /diagnostics reported anthropic_key_present: False. Masked in the container, where
# Cloud Run injects the key as a real env var and no .env exists at all (load_dotenv
# no-ops harmlessly on a missing file).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

MODEL = "claude-haiku-4-5-20251001"  # cheapest capable model - see LOGIC.md's cost notes

SERVER_KEY = "fantasy_fanatic"
TOOL_NAMES = [
    "check_league_format",
    "get_team_state",
    "get_roster_needs",
    "get_trade_targets",
    "get_player_outlook",
    "get_waiver_upgrades",
    "get_roster_detail",
    "get_optimal_lineup",
]
# Fully-qualified MCP tool names - the only tools this agent can ever call. Setting
# `tools` to this explicit list (not just `allowed_tools`) is what actually excludes
# every built-in Claude Code tool (Bash, Read, Write, WebFetch, ...) rather than just
# gating them behind a permission prompt.
FULL_TOOL_NAMES = [f"mcp__{SERVER_KEY}__{name}" for name in TOOL_NAMES]

SYSTEM_PROMPT = """You are a dynasty fantasy football assistant with tools for \
analyzing a Sleeper dynasty league: each team's read (its contention rank, whether \
its roster composition is aligned with that rank, and its path - one of contend / \
wait / build when aligned, press / decide / sell when not), positional needs, trade \
targets, a judge for one concrete proposed trade (evaluate_trade), waiver upgrades, \
and roster detail. The path words are the vocabulary; use them. Never describe a team \
as being "in a Push window" or "a Middling team"; say its path and the reason.

How dynasty works, which is the reasoning behind every tool here:

A. A good dynasty team is pushed to one end of the spectrum. Winning now and \
rebuilding are both coherent; drifting between them is what wastes assets. The \
middle is a real position rather than an unmade decision - a wait or decide team \
sees both directions and is entitled to wait on the season - but it is a place to \
pass through, not to sit.
B. Two currencies, and confusing them is the most common mistake. Dynasty value \
is what a player fetches in a trade; redraft value is what the market pays for his \
season. An old star is cheap in the first and expensive in the second, and a \
young prospect the reverse. A team buying for this year should pay in the \
currency it doesn't need. What a lineup actually SCORES is a third number, \
projected points a game (Sleeper's season projection under this league's own \
scoring): every lineup total, production share and "% of the league's best \
lineup" is in points a game, never in value.
C. Age matters as a distance, not a category. What matters is how many seasons a \
player has before his position's decline, not which side of a birthday he is on \
- two players can both read "prime" and be years apart.
D. Value is NOT additive across players. Never total up the two sides of a trade \
and compare them; five bench pieces do not equal one star, because a lineup can \
only start so many. Tools here deliberately refuse to price packages, and so \
should you. Say which single holding beats which, and who to call. The ONE \
exception is evaluate_trade's BALLPARK line, which the tool computes against the \
consolidation premium measured in real accepted trades - quote it as the tool's \
benchmark and stop; never extend the arithmetic yourself.
E. A recommendation is only useful if the other manager would plausibly say yes. \
Always say what the counterparty gets and why their own path makes it \
reasonable for them.
F. Alignment is a dial, not a gate. An aligned team still acts in its rank's \
direction - a "contend" team still buys production to win harder (it just pays fair \
prices, not premiums, and doesn't spend its future), and a "build" team still sells \
its aging pieces (at the market's pace, not urgently). "Aligned" means no forced \
decision is pending, never that the team should sit still. Never say a contender \
"isn't buying" a producer because it is not on a clock.
G. The waiting window runs to the TRADE DEADLINE, not to some early week. A wait or \
decide team that lets the season decide has until the deadline (typically around \
week 10) to commit; a few weeks of results usually settle it sooner, but "you must \
decide by week 5" is not something the data says.

Rules:
1. For any league_id you haven't checked yet this conversation, call \
check_league_format first, before any other tool for that league.
2. If the result is "unsupported", explain why in plain language (it isn't a \
dynasty league, so this analysis doesn't apply there) and do not call any other \
tool for that league.
3. If the result is "degraded", proceed normally but mention the caveat (a shallow \
league - percentile-based numbers are rougher estimates than usual). Either way the result carries the league's FORMAT line ("12-team superflex dynasty, full PPR, TE premium") - state it once early in your first answer about a league and reason in it: in superflex a second QB is a starter and QBs are priced like one; under TE premium a TE's catches score more and his value carries that. Every number the tools give you is already priced for this format. Dynasty values are FantasyCalc's for this exact format (never KeepTradeCut); when asked where a value comes from, say so.
4. Everything you say is advisory only. Never claim you executed a trade, waiver \
claim, or any other change - you can only analyze and suggest.
5. Ground every claim in what the tools actually returned. Never invent a player \
name, value, or team name that didn't come from a tool result.
6. When suggesting what a team could offer in a trade, the ONLY players you may name \
are ones literally present in that team's "my_offers"/"sell_candidates"/"situational" \
list from get_trade_targets - no other player, \
ever, even a declining starter who seems replaceable to you. Those lists already \
account for the team's own needs and starter status; if a player isn't on one of \
them, there is a specific reason, and second-guessing it produces suggestions that \
quietly contradict the team's own roster needs. Before naming any player as \
something to trade away, check that their exact name appears in the relevant list - \
if it doesn't, don't suggest them.
7. You are scoped to dynasty fantasy football analysis using the tools you have, \
nothing else. If asked for anything unrelated (general chat, other topics, writing, \
coding, math, etc.), briefly decline and redirect to what you can actually help \
with - don't answer the off-topic request just because you technically know how.
8. Never build or price a multi-player package yourself. No tool here values a bundle \
(evaluate_trade's ballpark benchmarks one against measured trades, which is different), and \
dynasty value is NOT additive across players - two 3,000s are not a 6,000, and \
implying otherwise is the most misleading thing you could do. Compare one player \
against one player, or say what a single piece is worth relative to replacement, and \
leave assembling the actual deal to the human. If asked "what would it take", say \
which of their pieces is the right centrepiece and that the rest is a negotiation you \
cannot price.
9. If a team's data includes "no_trade_history": true, mention that this league \
hasn't had any trades yet, so the team reads are less \
reliable this early - that kind of team identity normally comes from trade activity, \
which hasn't happened here yet.
10. Never work out a starting lineup yourself. Filling FLEX and SUPER_FLEX slots is a deterministic optimisation with one right answer, and reasoning about it in prose gets it subtly wrong - a real case had the vacated FLEX going to a tight end rather than the obvious backup WR, because FLEX accepts RB/WR/TE. Any question about what a team would start, who replaces an injured player, or what an injury costs must call get_optimal_lineup (pass `without` for the injury case) and report what it returns.
11. If check_league_format itself errors (not "unsupported" - an actual tool error, e.g. league not found), stop for that league_id entirely and tell the user the league_id looks wrong - don't retry with a different tool for the same broken ID.
12. If any tool result contains "data_gap", say what it says in one short sentence in your answer. It means a reference feed was unreachable and part of the analysis fell back to defaults - most often the age curves, which makes anything about a player's runway or who to sell less precise than usual. Do not bury it, do not apologise at length, and do not refuse to answer: give the answer and name the limit. The person asking cannot see the warning any other way, and it has already changed a recommendation once - with usage roles missing, one quarterback's runway read 2.1 years instead of 6.2, which reversed which of two players a rebuilding team should trade.
13. When the user disputes something the data said ("that team's QBs are great", "he's \
worth more than that"), do NOT fold and apologise. Re-read the specific claim - call the \
tool for that exact team or player if needed - then either stand corrected WITH the data, \
or explain what the number actually measures. Most disputes are the label being read a \
different way than it was measured: "short at QB" in superflex usually means a second \
BODY is missing, which is fully compatible with the manager knowing their QB1 is elite. \
A real exchange went wrong exactly here: the advice was right, the manager pushed back, \
and the answer collapsed to "oops my bad" - which was worse than being wrong.
14. When the user asserts their OWN valuation ("Rice is undervalued", "I think he breaks \
out"), that is a thesis, not an error to correct. Never argue the market price back at \
them, and never adopt their number either - you have no projections. Do both halves \
honestly: state what the market says (price, and what the price is made of - production \
vs remaining years), then reason CONDITIONALLY on their thesis: if their read is right, \
does the acquisition get easier or harder, is the owner a seller, does the timeline fit \
their window? "The market prices him at X on mostly-realized production; if you're right \
about the upside, that's exactly the profile where paying market wins" is a complete, \
honest answer. Their model of the player can beat the market's; this tool's job is the \
league context around that bet, not the bet itself.
15. Only say what the data can support. Two inventions to never make: (a) contract \
terms exist ONLY in get_roster_detail's `contract` field (years_remaining, guaranteed, \
apy) - cite them from there or not at all; "years to decline" and "runway" are age-curve \
estimates, never a contract, so never dress runway as an expiring deal or call years \
"guaranteed" unless that field says so; (b) NO CALENDAR exists in any tool result - do \
not attach specific week numbers or dates to advice. The only timing anchor you may \
use is the trade deadline (principle G); "by Week 5" is your invention.
"""

# The other side of the table. A trade has two sides and the advisor speaks for one;
# this persona is the OTHER manager, judging the same proposal from their own path with
# the same tools - two agents with opposing stances over one deterministic referee (the
# framer). Not a planner, not a critic: the coordination is a Python function.
COUNTERPARTY_PROMPT = """You are the manager of the team "{owner}" in this Sleeper dynasty \
league - not an advisor, the other side of the table. A trade has just been proposed to \
you. Decide it the way {owner} would.

Do this, in order: call check_league_format for the league; call get_team_state for \
{owner} (your team) - your path and posture are how you think; call get_roster_needs; \
call evaluate_trade for exactly the proposal as stated (you are one of the two sides). \
Then answer as {owner}, in this shape:

1. One word first: ACCEPT, COUNTER, or NO.
2. The two or three facts that decide it, from the tools: your path and what it says you \
should be doing, what the trade does to your starting lineup or your dynasty value \
(evaluate_trade's line for YOUR side), any hole it opens on your roster. evaluate_trade's \
lineup line is the fact of record: the piece you RECEIVE is the one that may start, the \
piece you SEND is gone - name who starts and who drops to your bench, and never argue \
your lineup against that line.
3. If COUNTER: exactly what you would need added, removed or swapped, and why that fixes \
it for you - name pieces, don't wave at "more value".

Judge by YOUR path's lens, the way evaluate_trade does. If your path is sell or build, \
you are a rebuild: what matters is dynasty value and timeline - young pieces and picks - \
and a hole this season at a position you are already selling is not a reason (you are \
not trying to field a lineup; "I can't absorb an RB hole" is a contender's sentence, \
not yours). If your path is contend or press, your starting lineup this season is the \
test and future value is the currency you spend. If you are in the middle, weigh both - \
and a proposal that wins on BOTH (more dynasty value in than out AND a better starting \
lineup) is a yes: it is the commit itself, not a detour from one, and the holes it \
leaves untouched are the next trade's problem, not this one's reason. \
Judge only from your own side. The other manager's reasons are their problem. Be a real \
manager, which means: you like your own players and price them a little above the market \
- an "even" trade on paper is a small loss to you, so it needs a reason, and you say what \
the reason would have to be; you do not give up the best player in the deal without a \
clear premium, and you say what premium; if saying yes means committing - pressing this \
season, or pivoting to a rebuild - you name that commitment and the other moves it would \
force (the hole it opens, the next piece you would have to buy or sell), because a trade \
that half-commits you is the one you regret. Brief, self-interested, no hedging, no \
advice for the other side, and never total values into a package price - say which single \
piece matters and why. Speak in the first person as {owner}. Under 200 words."""

class ToolsUnavailable(Exception):
    """The session had no tools: the model wrote tool-call XML as TEXT and confabulated
    from memory (Cloud Run's first deploy did this when the MCP server never registered;
    staging did it again on a cold container, 2026-08-17). Deterministic to detect - real
    tool calls never appear in the answer text - and the caller recycles the session."""


FAKE_TOOL_CALL = re.compile(r"<function_calls>|<invoke name=", re.I)

# Hard guardrails enforced by the SDK itself, not just requested in the prompt.
MAX_TURNS = 8
MAX_BUDGET_USD = 0.50  # per question - a single answer should never cost more than this

# Rule 6 (trade-chip grounding) is a prompt instruction, and prompt instructions are
# probabilistic - eval testing found the model still names a non-offerable player
# occasionally even with the rule spelled out. Fixed the same way every other bug in
# this project got fixed: push the reliability into a deterministic Python check
# instead of trusting the model to follow the rule. One retry, not a loop - if the
# model still gets it wrong after being told exactly what it did wrong, further
# retries are unlikely to help and just burn budget.
MAX_GROUNDING_RETRIES = 1


def _options(persona: str | None = None) -> ClaudeAgentOptions:
    """`persona` = an owner name: the agent speaks as that manager (COUNTERPARTY_PROMPT)
    instead of as the advisor. Same tools, same caps."""
    return ClaudeAgentOptions(
        model=MODEL,
        system_prompt=COUNTERPARTY_PROMPT.format(owner=persona) if persona else SYSTEM_PROMPT,
        mcp_servers={
            # sys.executable + an absolute script path, not "python -m agent.mcp_server":
            # the subprocess would otherwise need `python` on PATH *and* the repo root as
            # its working directory, neither of which is guaranteed (McpStdioServerConfig
            # has no `cwd` option). Both assumptions held locally and broke on Cloud Run.
            SERVER_KEY: {"type": "stdio", "command": sys.executable, "args": [str(MCP_SERVER_PATH)]},
        },
        tools=FULL_TOOL_NAMES,
        allowed_tools=FULL_TOOL_NAMES,
        permission_mode="bypassPermissions",  # safe: `tools` already restricts to our 6 read-only tools
        max_turns=MAX_TURNS,
        max_budget_usd=MAX_BUDGET_USD,
        # Without this, the SDK auto-loads this repo's CLAUDE.md as project memory on
        # every call - that file guides *coding* on this repo, it has nothing to do
        # with how the fantasy agent should answer a user's question. Measured a 38%
        # input-token reduction (3332 -> 2051 for the same trivial call) from this
        # one line - real, paid-for waste, not a rounding error.
        setting_sources=[],
        # Text deltas as they are generated, so the UI can show the answer arriving
        # instead of a spinner for the ~30s the model spends writing it.
        include_partial_messages=True,
    )


# What each tool is DOING, for the person waiting on a 60-90 second answer. The tool
# names are internal jargon; a reader wants to know the machine is working and roughly
# where it is. Missing names fall back to the raw name rather than going silent.
TOOL_PROGRESS = {
    "check_league_format": "checking the league format",
    "get_team_state": "reading every team's window",
    "get_roster_needs": "working out who is short where",
    "get_trade_targets": "matching trade targets across the league",
    "get_player_outlook": "working out both sides of that call",
    "get_waiver_upgrades": "scanning the waiver wire",
    "get_optimal_lineup": "solving the best legal lineup",
    "get_roster_detail": "pulling the full roster detail",
}


def _progress_label(tool_name: str) -> str:
    return TOOL_PROGRESS.get(str(tool_name).split("__")[-1], str(tool_name))


async def _run_turn(client: ClaudeSDKClient, message: str, verbose: bool,
                    on_progress=None, on_text=None) -> dict:
    """Sends one message on an already-open client session and collects the reply,
    including tool *results* (not just calls) - needed to know whether a tool
    errored (e.g. a nonexistent league_id) and what check_league_format actually
    returned, for the observability log below. Split out from run_query so the
    grounding-retry loop can send a second message on the same session without
    repeating this collection logic."""
    text_parts, tool_calls, tool_results, result = [], [], [], None
    tool_name_by_id = {}
    streamed = ""
    await client.query(message)
    async for msg in client.receive_response():
        if isinstance(msg, StreamEvent):
            if not on_text:
                continue
            delta = msg.event.get("delta") or {}
            if delta.get("type") == "text_delta":
                streamed += delta["text"]
                on_text(streamed)
            elif msg.event.get("type") == "content_block_stop" and streamed:
                streamed += "\n"  # mirrors the "\n".join over finished text blocks below
        elif isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                    if verbose:
                        print(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_calls.append({"name": block.name, "input": block.input})
                    tool_name_by_id[block.id] = block.name
                    if on_progress:
                        on_progress(_progress_label(block.name))
                    if verbose:
                        print(f"[tool call: {block.name}({block.input})]")
        elif isinstance(msg, UserMessage):
            content = msg.content if isinstance(msg.content, list) else []
            for block in content:
                if isinstance(block, ToolResultBlock):
                    tool_results.append({
                        "name": tool_name_by_id.get(block.tool_use_id),
                        "is_error": bool(block.is_error),
                        "content": block.content,
                    })
                    if verbose and block.is_error:
                        print(f"[tool error: {tool_name_by_id.get(block.tool_use_id)} -> {block.content}]")
        elif isinstance(msg, ResultMessage):
            result = msg
            if verbose:
                print(f"\n[{result.num_turns} turn(s), ${result.total_cost_usd:.4f}, "
                      f"stop_reason={result.stop_reason}]")
    return {"text": "\n".join(text_parts), "tool_calls": tool_calls, "tool_results": tool_results, "result": result}


def _offerable_from_call(call: dict) -> set[str] | None:
    """The real offerable set for one grounding-relevant tool call, or None if this
    call isn't one of the trade tools rule 6 governs."""
    league_id, owner_name = call["input"].get("league_id"), call["input"].get("owner_name")
    if not league_id or not owner_name:
        return None
    if call["name"] == f"mcp__{SERVER_KEY}__get_trade_targets":
        return trade_targets.offerable_names(trade_targets.find_targets(league_id, owner_name))
    return None


def _banned_trade_names(tool_calls: list[dict]) -> set[str]:
    """Ground truth for rule 6: for every roster a trade tool was called on this
    turn, every name on that roster that ISN'T offerable by ANY of the trade tools
    called for it. Computed straight from the same Python those tools call - free,
    deterministic, and can't be talked out of being right the way the model's own
    rule-following can. Offerable sets from multiple tool calls for the same
    league/owner are unioned BEFORE subtracting from the roster - unioning two
    already-subtracted "banned" sets instead would wrongly flag a player offerable
    via one tool but not the other."""
    offerable_by_roster: dict[tuple[str, str], set[str]] = {}
    for call in tool_calls:
        try:
            offerable = _offerable_from_call(call)
        except Exception:
            continue  # can't validate what we can't recompute - don't block on it
        if offerable is None:
            continue
        league_id, owner_name = call["input"]["league_id"], call["input"]["owner_name"]
        offerable_by_roster.setdefault((league_id, owner_name), set()).update(offerable)

    banned = set()
    for (league_id, owner_name), offerable in offerable_by_roster.items():
        try:
            roster = roster_detail.get_roster_rows(league_id, owner_name)
        except Exception:
            continue
        banned |= {row["name"] for row in roster["rows"]} - offerable
    return banned


FLAGGABLE_POSITIONS = ("QB", "RB", "WR", "TE")

# Words that make a clause a NEED CLAIM about a team. Deliberately narrow: "MSpoto has a
# great QB room" carries no need word and never fires.
NEED_CLAIM_WORDS = ("need", "short at", "short a ", "desperate", "hole at", "thin at",
                    "starving for", "hurting at")


def _flagged_needs(tool_calls: list[dict]) -> tuple[set, set]:
    """Ground truth for need claims: every (owner name, position) the needs data actually
    flags, plus every owner name in the leagues touched this turn - recomputed from the
    same Python the tools call, exactly like `_banned_trade_names`. Exists because a live
    answer invented a QB need for a Contend team holding Josh Allen, Dart AND Kyler -
    a claim present nowhere in any payload - and there was no tripwire for it."""
    from analysis import roster_needs
    from analysis.league import context

    league_ids = {c["input"].get("league_id") for c in tool_calls
                  if isinstance(c.get("input"), dict)} - {None}
    flagged, owner_names = set(), set()
    for league_id in league_ids:
        try:
            ctx = context(league_id)
            needs = roster_needs.league_needs(league_id)
        except Exception:
            continue  # can't validate what we can't recompute - don't block on it
        for owner_id in ctx.owner_names:
            for alias in ctx.aliases_for(owner_id):
                # Team names are free text; a 2-3 char alias would substring-match all
                # over an answer and turn the guard into noise.
                if len(alias) < 4:
                    continue
                owner_names.add(alias)
                flagged |= {(alias, pos) for pos in needs.get(owner_id, {})}
    return flagged, owner_names


def _need_claim_violations(text: str, flagged: set, owner_names: set) -> list[str]:
    """Need claims about named teams that the needs data does not support. Clause-scoped
    and high-precision like `packaged_pieces` in evals: a claim is a team name and a
    position and a need word in ONE clause with no negation. "SeanCenter is short at QB"
    fires only if (SeanCenter, QB) is unflagged; "MSpoto doesn't need a QB" never fires."""
    import re
    violations = []
    for clause in re.split(r"[.;!?\n]", text):
        low = clause.lower()
        if not any(w in low for w in NEED_CLAIM_WORDS):
            continue
        # The shared tuple plus contractions: "doesn't need a QB" is the model getting
        # it RIGHT, and lacks the "not " the shared list catches.
        if any(neg in low for neg in NEGATION_WORDS + ("doesn't", "does not", "isn't", "no ")):
            continue
        for owner in owner_names:
            if not owner or owner not in clause:
                continue
            for pos in FLAGGABLE_POSITIONS:
                if re.search(rf"\b{pos}s?\b", clause) and (owner, pos) not in flagged:
                    violations.append(
                        f'claimed {owner} needs {pos}, but the needs data flags no {pos} '
                        f'need for them: "{clause.strip()[:140]}"')
    return violations


# A banned name only counts as a real violation if the line naming it also reads
# like a trade suggestion, not just team-status description. Found live: the
# original whole-text substring check flagged normal roster-summary lines ("Your
# cornerstones: Lamar Jackson...") on almost every real question, since describing
# a team's current roster necessarily mentions plenty of non-offerable players -
# costing a retry call nearly every time without ever catching a real violation
# there. Real violations, observed live, always co-occur with one of these words on
# the same line ("you can offer...", "send X for Y", "sell candidates: X").
TRADE_ACTION_WORDS = ("send", "offer", "trade", "sell", "give up", "package", "dangle", "swap")

# ...and only if the line isn't telling the reader NOT to. "Don't trade the QBs you can
# actually start (Herbert, Hurts) or Tyler Warren" contains a trade word and a non-offerable
# name, and the check fired on it - spending a retry to tell the model off for advice it had
# given correctly. It then argued back, accurately, and the exchange cost a third of the
# answer's budget. Advising someone to KEEP a cornerstone is the behaviour this rule wants.
#
# Skipping the whole line can hide a real violation in "don't trade X, but do trade Y". That
# is the right way to be wrong: this is a safety net with one retry, and a miss costs an
# ungrounded name while a false positive costs money and contradicts a correct answer.
NEGATION_WORDS = ("don't", "do not", "never", "not ", "keep", "hold", "avoid", "untouchable")


def _trade_violations(text: str, banned: set[str]) -> list[str]:
    """Every banned name mentioned on a line that also contains trade-action
    language - the deliberately narrower check rule 6's retry actually fires on,
    instead of any mention anywhere in the response."""
    violations = set()
    for line in text.splitlines():
        lower = line.lower()
        if any(word in lower for word in NEGATION_WORDS):
            continue
        if any(word in lower for word in TRADE_ACTION_WORDS):
            violations.update(n for n in banned if n in line)
    return sorted(violations)


BALLPARK_WORDS = ("ballpark", "benchmark", "typically lands", "usually lands", "usually fetches")


def _ballpark_violation(text: str, tool_calls: list[dict], tool_results: list[dict]) -> bool:
    """The framer's ballpark is SILENT for some trades (a pick as the best piece, a plain
    mid-tier swap) and the model wrote one anyway - "typically lands around a 2027 2nd or
    2028 1st early" (a slot 2028 picks don't have; owner: "the model is hallucinating").
    A benchmark sentence with no BALLPARK line in any tool result is invented. Only when a
    trade was actually evaluated this run: the first version fired on the word "benchmark"
    in a format answer and the retry replaced a correct answer with "I haven't evaluated a
    trade" - live, 2026-08-18."""
    if not any(c["name"].endswith("evaluate_trade") for c in tool_calls):
        return False
    if not any(w in text.lower() for w in BALLPARK_WORDS):
        return False
    return not any("BALLPARK" in _content_text(r.get("content")) for r in tool_results)


def _content_text(content) -> str:
    """Tool result content comes back as a plain string, a list of {"type",
    "text", ...} blocks, or None depending on the tool/transport - normalize to
    plain text for logging and tier-parsing rather than handling each shape
    separately at every call site."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
    return ""


def _token_fields(result) -> dict:
    """Token and cache counts, not just the dollar total. Measured (see LOGIC.md's
    prompt-caching section) that ~4.7k tokens of cacheable prefix get re-created on
    every question because each run opens a fresh session - that's a real, recurring
    cost that a cost_usd figure alone makes invisible. Logging the breakdown is what
    makes a future cost regression (or improvement) actually detectable."""
    usage = getattr(result, "usage", None) or {}
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_creation_tokens": usage.get("cache_creation_input_tokens"),
        "cache_read_tokens": usage.get("cache_read_input_tokens"),
    }


def _observability_fields(tool_calls: list[dict], tool_results: list[dict]) -> dict:
    """The handful of things worth logging out of a run's tool activity: which
    league(s) got touched, what format tier check_league_format found (the
    original Phase 3 plan explicitly wanted this, not just raw tool names), and
    whether any tool call errored - e.g. a nonexistent league_id, which errors at
    the Sleeper API level (confirmed live) rather than failing gracefully in our
    own code, so this is the only way to see it happened after the fact."""
    league_ids = sorted({
        call["input"]["league_id"] for call in tool_calls if call["input"].get("league_id")
    })
    format_tier = None
    for r in tool_results:
        if r["name"] == f"mcp__{SERVER_KEY}__check_league_format" and not r["is_error"]:
            try:
                format_tier = json.loads(_content_text(r["content"])).get("tier")
            except json.JSONDecodeError:
                pass
            if format_tier:
                break
    tool_errors = [{"tool": r["name"], "message": _content_text(r["content"])} for r in tool_results if r["is_error"]]
    return {"league_ids": league_ids, "format_tier": format_tier, "tool_errors": tool_errors}


async def run_query(question: str, verbose: bool = True, client: ClaudeSDKClient | None = None,
                    on_progress=None, on_text=None, persona: str | None = None) -> dict:
    """Runs one question through the agent, then deterministically checks the answer
    against ground truth before returning it: if it named a player its own trade-tool
    calls say isn't offerable, send one corrective follow-up on the same session
    rather than trusting the prompt rule to have been followed. Prints
    live (for interactive CLI use) and always returns the collected text/tool-calls/
    cost so the eval harness can assert on a real run instead of duplicating this
    query logic. Also logs a structured observability record for every call - success
    or failure - to agent/observability.py, since a console print vanishes the moment
    the process exits and there was previously no durable record of what got asked,
    what it cost, or whether anything errored."""
    start = time.monotonic()
    all_tool_calls: list[dict] = []
    all_tool_results: list[dict] = []
    total_turns, retries, result = 0, 0, None
    outcome, error_message, answer_text = "ok", None, None
    try:
        # A caller may hand in a live client (agent/sessions.py does, to keep a
        # conversation - and both the prompt cache and the MCP data cache - warm across
        # turns). Only a client we created here gets closed here; a session's client
        # outlives the request, so the exit stack must not tear it down.
        async with AsyncExitStack() as stack:
            if client is None:
                client = await stack.enter_async_context(ClaudeSDKClient(options=_options(persona)))
            turn = await _run_turn(client, question, verbose, on_progress, on_text)
            all_tool_calls = list(turn["tool_calls"])
            all_tool_results = list(turn["tool_results"])
            # num_turns resets per client.query() call (verified live: 4, then 1 on
            # a retry) but total_cost_usd is a running session total (verified live:
            # kept climbing across the retry) - the two fields don't share the same
            # semantics, so num_turns needs manual summing and cost_usd doesn't.
            total_turns = turn["result"].num_turns if turn["result"] else 0
            result = turn["result"]
            # Accumulated across turns, not recomputed per-turn: a retry turn
            # typically doesn't re-call get_trade_targets (the model already has the
            # result in context), so a fresh per-turn computation would go empty and
            # silently miss the same violation repeating in the corrected answer.
            banned = _banned_trade_names(turn["tool_calls"])
            # Recompute needs ground truth only when the answer makes a need claim at
            # all - most answers don't, and the recompute is a real league fetch.
            flagged, league_owner_names = (
                _flagged_needs(turn["tool_calls"])
                if any(w in turn["text"].lower() for w in NEED_CLAIM_WORDS)
                else (set(), set()))
            violations = _trade_violations(turn["text"], banned)
            need_violations = _need_claim_violations(turn["text"], flagged, league_owner_names)
            ballpark_violation = _ballpark_violation(turn["text"], all_tool_calls, all_tool_results)
            while (violations or need_violations or ballpark_violation) and retries < MAX_GROUNDING_RETRIES:
                if verbose:
                    print(f"[grounding check failed: {violations + need_violations + (['invented ballpark'] if ballpark_violation else [])} - retrying]")
                # List every violation found, not just one - an earlier version only
                # named a single offender (via next() on a set), so when an answer
                # named two non-offerable players at once, the one retry fixed one
                # and left the other. Found live via the eval harness re-failing
                # after the fix looked solved manually.
                problems = []
                if violations:
                    names = ", ".join(f'"{n}"' for n in violations)
                    problems.append(
                        f"You named {names} as trade-away candidates, but none of them are in "
                        "this team's real offer list from get_trade_targets - use only players "
                        "actually in that tool's offer or sell-candidate lists.")
                if need_violations:
                    claims = " ".join(need_violations)
                    problems.append(
                        f"You asserted positional needs the roster-needs data does not "
                        f"contain: {claims} Only claim a team needs a position when the tools "
                        f"flag that need - if you have not called get_roster_needs for this "
                        f"league, call it rather than inferring needs.")
                if ballpark_violation:
                    problems.append(
                        "You quoted a ballpark / benchmark for this trade, but no tool result "
                        "contains a BALLPARK line for it - the measured benchmark exists only "
                        "for player-centred returns. There is no benchmark here: say so if it "
                        "matters, and never invent one.")
                # The reader never sees this exchange, and the model must not either: a live
                # retry opened its answer "You're absolutely right - I apologize", claimed
                # the tool output had been "too large to display" (confabulated - the run's
                # own log shows every payload fit), and asked the USER to paste tool results.
                # The correction is stagecraft; only the corrected answer goes on stage.
                correction = (
                    " ".join(problems)
                    + " Redo your answer. Write it as a fresh, complete reply to the user's "
                    "original question: no apology, no mention of this correction, no claims "
                    "about tool output size or mechanics, and never ask the user to supply "
                    "data - everything you need is in the tool results you have."
                )
                turn = await _run_turn(client, correction, verbose, on_progress, on_text)
                all_tool_calls += turn["tool_calls"]
                all_tool_results += turn["tool_results"]
                total_turns += turn["result"].num_turns if turn["result"] else 0
                result = turn["result"]
                banned |= _banned_trade_names(turn["tool_calls"])
                new_flagged, new_names = _flagged_needs(turn["tool_calls"])
                flagged |= new_flagged
                league_owner_names |= new_names
                retries += 1
                violations = _trade_violations(turn["text"], banned)
                need_violations = _need_claim_violations(turn["text"], flagged,
                                                         league_owner_names)
                ballpark_violation = _ballpark_violation(turn["text"], all_tool_calls, all_tool_results)

        answer_text = turn["text"]
        if FAKE_TOOL_CALL.search(answer_text or "") and not all_tool_calls:
            outcome = "no_tools"
            raise ToolsUnavailable("answer contains tool-call XML and no tool was called")
        return {
            "text": answer_text,
            "tool_calls": all_tool_calls,
            "num_turns": total_turns,
            "cost_usd": result.total_cost_usd if result else None,
            "grounding_retries": retries,
        }
    except Exception as e:
        outcome, error_message = "error", str(e)
        raise
    finally:
        observability.log_run({
            "question": question[:300],
            # Enough of the answer to audit a bad claim after the fact - the Spoto
            # incident was undiagnosable because only the question survived.
            "answer": answer_text[:600] if answer_text else None,
            "outcome": outcome,
            "error": error_message,
            "latency_seconds": round(time.monotonic() - start, 2),
            "num_turns": total_turns,
            "cost_usd": result.total_cost_usd if result else None,
            "grounding_retries": retries,
            **_token_fields(result),
            **_observability_fields(all_tool_calls, all_tool_results),
        })


if __name__ == "__main__":
    question = " ".join(sys.argv[1:])
    if not question:
        print('Usage: python -m agent.agent "<question>"')
        sys.exit(1)
    asyncio.run(run_query(question))
