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
import sys

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
    ResultMessage,
)

from analysis import trade_targets, roster_detail

load_dotenv()

MODEL = "claude-haiku-4-5-20251001"  # cheapest capable model - see LOGIC.md's cost notes

SERVER_KEY = "fantasy_fanatic"
TOOL_NAMES = [
    "check_league_format",
    "get_team_state",
    "get_roster_needs",
    "get_trade_targets",
    "get_mutual_swaps",
    "get_waiver_upgrades",
    "get_roster_detail",
]
# Fully-qualified MCP tool names - the only tools this agent can ever call. Setting
# `tools` to this explicit list (not just `allowed_tools`) is what actually excludes
# every built-in Claude Code tool (Bash, Read, Write, WebFetch, ...) rather than just
# gating them behind a permission prompt.
FULL_TOOL_NAMES = [f"mcp__{SERVER_KEY}__{name}" for name in TOOL_NAMES]

SYSTEM_PROMPT = """You are a dynasty fantasy football assistant with tools for \
analyzing a Sleeper dynasty league: team windows (Win-Now/Middling/Rebuilding), \
positional needs, trade targets, waiver upgrades, and roster detail.

Rules:
1. For any league_id you haven't checked yet this conversation, call \
check_league_format first, before any other tool for that league.
2. If the result is "unsupported", explain why in plain language (it isn't a \
dynasty league, so this analysis doesn't apply there) and do not call any other \
tool for that league.
3. If the result is "degraded", proceed normally but mention the caveat (a shallow \
league - percentile-based numbers are rougher estimates than usual).
4. Everything you say is advisory only. Never claim you executed a trade, waiver \
claim, or any other change - you can only analyze and suggest.
5. Ground every claim in what the tools actually returned. Never invent a player \
name, value, or team name that didn't come from a tool result.
6. When suggesting what a team could offer in a trade, the ONLY players you may name \
are ones literally present in that team's "my_offers"/"sell_candidates"/"situational" \
list from get_trade_targets, or "you_send" from get_mutual_swaps - no other player, \
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
8. get_trade_targets finds one-way fits against Rebuilding teams' sell candidates. \
get_mutual_swaps finds two-way trades between this team and one other Win-Now/ \
Middling team where each side's positional surplus is the other's need - use it when \
asked about trading with a specific other team, or how to fix a need without giving \
up a core piece, not as a replacement for get_trade_targets.
9. If a team's data includes "no_trade_history": true, mention that this league \
hasn't had any trades yet, so the Win-Now/Middling/Rebuilding labels are less \
reliable this early - that kind of team identity normally comes from trade activity, \
which hasn't happened here yet.
"""

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


def _options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=MODEL,
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={
            SERVER_KEY: {"type": "stdio", "command": "python", "args": ["-m", "agent.mcp_server"]},
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
    )


async def _run_turn(client: ClaudeSDKClient, message: str, verbose: bool) -> dict:
    """Sends one message on an already-open client session and collects the reply.
    Split out from run_query so the grounding-retry loop below can send a second
    message on the same session without repeating this collection logic."""
    text_parts, tool_calls, result = [], [], None
    await client.query(message)
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
                    if verbose:
                        print(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_calls.append({"name": block.name, "input": block.input})
                    if verbose:
                        print(f"[tool call: {block.name}({block.input})]")
        elif isinstance(msg, ResultMessage):
            result = msg
            if verbose:
                print(f"\n[{result.num_turns} turn(s), ${result.total_cost_usd:.4f}, "
                      f"stop_reason={result.stop_reason}]")
    return {"text": "\n".join(text_parts), "tool_calls": tool_calls, "result": result}


def _offerable_from_call(call: dict) -> set[str] | None:
    """The real offerable set for one grounding-relevant tool call, or None if this
    call isn't one of the trade tools rule 6 governs."""
    league_id, owner_name = call["input"].get("league_id"), call["input"].get("owner_name")
    if not league_id or not owner_name:
        return None
    if call["name"] == f"mcp__{SERVER_KEY}__get_trade_targets":
        return trade_targets.offerable_names(trade_targets.find_targets(league_id, owner_name))
    if call["name"] == f"mcp__{SERVER_KEY}__get_mutual_swaps":
        swaps = trade_targets.find_mutual_swaps(league_id, owner_name)["swaps"]
        return {e["name"] for swap in swaps for e in swap["you_send"]}
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


# A banned name only counts as a real violation if the line naming it also reads
# like a trade suggestion, not just team-status description. Found live: the
# original whole-text substring check flagged normal roster-summary lines ("Your
# cornerstones: Lamar Jackson...") on almost every real question, since describing
# a team's current roster necessarily mentions plenty of non-offerable players -
# costing a retry call nearly every time without ever catching a real violation
# there. Real violations, observed live, always co-occur with one of these words on
# the same line ("you can offer...", "send X for Y", "sell candidates: X").
TRADE_ACTION_WORDS = ("send", "offer", "trade", "sell", "give up", "package", "dangle", "swap")


def _trade_violations(text: str, banned: set[str]) -> list[str]:
    """Every banned name mentioned on a line that also contains trade-action
    language - the deliberately narrower check rule 6's retry actually fires on,
    instead of any mention anywhere in the response."""
    violations = set()
    for line in text.splitlines():
        lower = line.lower()
        if any(word in lower for word in TRADE_ACTION_WORDS):
            violations.update(n for n in banned if n in line)
    return sorted(violations)


async def run_query(question: str, verbose: bool = True) -> dict:
    """Runs one question through the agent, then deterministically checks the answer
    against ground truth before returning it: if it named a player its own trade-tool
    calls say isn't offerable, send one corrective follow-up on the same session
    rather than trusting the prompt rule to have been followed. Prints
    live (for interactive CLI use) and always returns the collected text/tool-calls/
    cost so the eval harness can assert on a real run instead of duplicating this
    query logic."""
    async with ClaudeSDKClient(options=_options()) as client:
        turn = await _run_turn(client, question, verbose)
        all_tool_calls = list(turn["tool_calls"])
        # num_turns resets per client.query() call (verified live: 4, then 1 on a
        # retry) but total_cost_usd is a running session total (verified live: kept
        # climbing across the retry) - the two fields don't share the same semantics,
        # so num_turns needs manual summing and cost_usd doesn't.
        total_turns = turn["result"].num_turns if turn["result"] else 0
        # Accumulated across turns, not recomputed per-turn: a retry turn typically
        # doesn't re-call get_trade_targets (the model already has the result in
        # context), so a fresh per-turn computation would go empty and silently miss
        # the same violation repeating in the corrected answer.
        banned = _banned_trade_names(turn["tool_calls"])
        retries = 0
        violations = _trade_violations(turn["text"], banned)
        while violations and retries < MAX_GROUNDING_RETRIES:
            if verbose:
                print(f"[grounding check failed: {violations} aren't offerable - retrying]")
            # List every violation found, not just one - an earlier version only
            # named a single offender (via next() on a set), so when an answer named
            # two non-offerable players at once, the one retry fixed one and left the
            # other. Found live via the eval harness re-failing after the fix looked
            # solved manually.
            names = ", ".join(f'"{n}"' for n in violations)
            correction = (
                f"You named {names} as trade-away candidates, but none of them are in this "
                "team's real offer list from get_trade_targets or get_mutual_swaps. Redo your "
                "answer using only players that actually appear in one of those tools' offer/"
                "sell-candidate/you_send lists - check every name against that list first."
            )
            turn = await _run_turn(client, correction, verbose)
            all_tool_calls += turn["tool_calls"]
            total_turns += turn["result"].num_turns if turn["result"] else 0
            banned |= _banned_trade_names(turn["tool_calls"])
            retries += 1
            violations = _trade_violations(turn["text"], banned)

    result = turn["result"]
    return {
        "text": turn["text"],
        "tool_calls": all_tool_calls,
        "num_turns": total_turns,
        "cost_usd": result.total_cost_usd if result else None,
        "grounding_retries": retries,
    }


if __name__ == "__main__":
    question = " ".join(sys.argv[1:])
    if not question:
        print('Usage: python -m agent.agent "<question>"')
        sys.exit(1)
    asyncio.run(run_query(question))
