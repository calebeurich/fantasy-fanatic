"""Validates mcp_server.py's tools return the same data as calling the underlying
Python functions directly - confirms the MCP wrapping itself works correctly (schemas,
argument passing, serialization), since the business logic underneath is already
validated elsewhere in this project.

Run: python test_mcp_server.py
"""

import asyncio
import json

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import format_support
import team_state

DYNASTY_LEAGUE = "1315386978904084480"  # XFL 2
REDRAFT_LEAGUE = "1323741311471194112"  # Tangy Football


def _content(result):
    return json.loads(result.content[0].text)


async def main():
    params = StdioServerParameters(command="python", args=["mcp_server.py"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"{len(names)} tools registered: {names}")

            via_mcp = _content(await session.call_tool("check_league_format", {"league_id": DYNASTY_LEAGUE}))
            direct = format_support.assess_format(DYNASTY_LEAGUE)
            assert via_mcp == direct, f"mismatch: {via_mcp} vs {direct}"
            print("check_league_format (dynasty): matches direct call ->", via_mcp["tier"])

            via_mcp = _content(await session.call_tool("check_league_format", {"league_id": REDRAFT_LEAGUE}))
            direct = format_support.assess_format(REDRAFT_LEAGUE)
            assert via_mcp == direct, f"mismatch: {via_mcp} vs {direct}"
            print("check_league_format (redraft): matches direct call ->", via_mcp["tier"])

            via_mcp = _content(await session.call_tool("get_team_state", {"league_id": DYNASTY_LEAGUE}))
            direct = team_state.classify_league(DYNASTY_LEAGUE)
            assert len(via_mcp["teams"]) == len(direct) == 12
            print(f"get_team_state: {len(via_mcp['teams'])} teams, matches direct call length")

            via_mcp = _content(await session.call_tool(
                "get_trade_targets", {"league_id": DYNASTY_LEAGUE, "owner_name": "dezdroppedit27"}))
            assert via_mcp["mode"] == "buy"
            print("get_trade_targets: mode =", via_mcp["mode"])

            via_mcp = _content(await session.call_tool(
                "get_waiver_upgrades", {"league_id": DYNASTY_LEAGUE, "owner_name": "dezdroppedit27"}))
            print("get_waiver_upgrades: available_count =", via_mcp["available_count"])

            via_mcp = _content(await session.call_tool(
                "get_roster_detail", {"league_id": DYNASTY_LEAGUE, "owner_name": "dezdroppedit27"}))
            print("get_roster_detail:", len(via_mcp["rows"]), "players, owner =", via_mcp["owner"])

            via_mcp = _content(await session.call_tool("get_roster_needs", {"league_id": DYNASTY_LEAGUE}))
            assert len(via_mcp) == 12
            print("get_roster_needs:", len(via_mcp), "teams")

    print("\nAll MCP tool calls succeeded and matched direct calls where compared.")


if __name__ == "__main__":
    asyncio.run(main())
