"""Local CLI agent for dynasty fantasy football analysis - Phase 2 of the agent
build-out plan. Wraps the Phase 1 MCP server (agent/mcp_server.py) with the Claude
Agent SDK, defaulting to Haiku (cheapest capable model, given a small starting API
budget) with a tightly scoped, read-only tool surface: nothing beyond the 6
fantasy-fanatic MCP tools is ever exposed, so there's no path to a file/shell/network
action outside this project's own already-validated analysis code.

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

load_dotenv()

MODEL = "claude-haiku-4-5-20251001"  # cheapest capable model - see LOGIC.md's cost notes

SERVER_KEY = "fantasy_fanatic"
TOOL_NAMES = [
    "check_league_format",
    "get_team_state",
    "get_roster_needs",
    "get_trade_targets",
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
are ones literally present in that team's "my_offers" (or "sell_candidates") list \
from get_trade_targets - no other player, ever, even a declining starter who seems \
replaceable to you. That list already accounts for the team's own needs and starter \
status; if a player isn't on it, there is a specific reason, and second-guessing it \
produces suggestions that quietly contradict the team's own roster needs. Before \
naming any player as something to trade away, check that their exact name appears in \
that list - if it doesn't, don't suggest them.
7. You are scoped to dynasty fantasy football analysis using the tools you have, \
nothing else. If asked for anything unrelated (general chat, other topics, writing, \
coding, math, etc.), briefly decline and redirect to what you can actually help \
with - don't answer the off-topic request just because you technically know how.
"""

# Hard guardrails enforced by the SDK itself, not just requested in the prompt.
MAX_TURNS = 8
MAX_BUDGET_USD = 0.50  # per question - a single answer should never cost more than this


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


async def run_query(question: str, verbose: bool = True) -> dict:
    """Runs one question through the agent. Prints live (for interactive CLI use)
    and always returns the collected text/tool-calls/cost so the eval harness can
    assert on a real run instead of duplicating this query logic."""
    text_parts, tool_calls, result = [], [], None
    async with ClaudeSDKClient(options=_options()) as client:
        await client.query(question)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
                        if verbose:
                            print(block.text)
                    elif isinstance(block, ToolUseBlock):
                        tool_calls.append({"name": block.name, "input": block.input})
                        if verbose:
                            print(f"[tool call: {block.name}({block.input})]")
            elif isinstance(message, ResultMessage):
                result = message
                if verbose:
                    print(f"\n[{result.num_turns} turn(s), ${result.total_cost_usd:.4f}, "
                          f"stop_reason={result.stop_reason}]")

    return {
        "text": "\n".join(text_parts),
        "tool_calls": tool_calls,
        "num_turns": result.num_turns if result else None,
        "cost_usd": result.total_cost_usd if result else None,
    }


if __name__ == "__main__":
    question = " ".join(sys.argv[1:])
    if not question:
        print('Usage: python -m agent.agent "<question>"')
        sys.exit(1)
    asyncio.run(run_query(question))
