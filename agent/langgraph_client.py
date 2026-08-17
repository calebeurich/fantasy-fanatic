"""The same MCP tools, driven by a different orchestrator and an open-weight model.

A portability proof, not a second product path: `agent/agent.py` is the Claude Agent
SDK on Haiku; this is a LangGraph ReAct loop over the identical `agent/mcp_server.py`
tools, with any OpenAI-compatible open-weight endpoint as the model (HuggingFace's
router by default). The point it makes is the architectural one - the deterministic
tool layer is the asset; the orchestrator and the model are swappable - and it lets
the same eval questions be asked of an open model.

Not wired into the API, not in the container: extra deps live in
requirements-langgraph.txt. Run:

    pip install -r requirements-langgraph.txt
    HF_TOKEN=... python -m agent.langgraph_client "For Sleeper league <id>, what should <owner> do?"

Env: HF_TOKEN (or LG_API_KEY) for the endpoint; LG_BASE_URL (default HF's router);
LG_MODEL (default Qwen/Qwen2.5-72B-Instruct - it tool-calls reliably). Same tool
allowlist, same system prompt, same turn cap as the SDK agent.
"""

import asyncio
import os
import sys

import httpx

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from .agent import MCP_SERVER_PATH, MAX_TURNS, SYSTEM_PROMPT, TOOL_NAMES

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

BASE_URL = os.environ.get("LG_BASE_URL", "https://router.huggingface.co/v1")
MODEL = os.environ.get("LG_MODEL", "Qwen/Qwen2.5-72B-Instruct")
API_KEY = os.environ.get("LG_API_KEY") or os.environ.get("HF_TOKEN")


async def run(question: str, verbose: bool = True) -> dict:
    """One question through the LangGraph loop. Returns text + the tool calls made, the
    same shape agent.run_query gives, so an eval can compare the two orchestrators."""
    if not API_KEY:
        raise SystemExit("set HF_TOKEN (or LG_API_KEY) - an OpenAI-compatible endpoint key")
    client = MultiServerMCPClient({
        "fantasy_fanatic": {"transport": "stdio", "command": sys.executable, "args": [str(MCP_SERVER_PATH)]},
    })
    tools = [t for t in await client.get_tools() if t.name in TOOL_NAMES]
    # An explicit httpx client: openai 3.x's default async transport hits a RecursionError
    # on this machine (httpx 0.28 + httpx2 side by side); a plain AsyncClient is fine.
    model = ChatOpenAI(model=MODEL, base_url=BASE_URL, api_key=API_KEY, temperature=0,
                       http_async_client=httpx.AsyncClient(timeout=120))
    graph = create_react_agent(model, tools, prompt=SYSTEM_PROMPT)
    result = await graph.ainvoke({"messages": [("user", question)]},
                                 config={"recursion_limit": 2 * MAX_TURNS + 1})
    calls, text = [], ""
    for m in result["messages"]:
        for tc in getattr(m, "tool_calls", None) or []:
            calls.append({"name": tc["name"], "input": tc["args"]})
            if verbose:
                print(f"[tool call: {tc['name']}({tc['args']})]")
        if m.type == "ai" and m.content and not getattr(m, "tool_calls", None):
            text = m.content if isinstance(m.content, str) else "".join(
                c.get("text", "") for c in m.content if isinstance(c, dict))
    if verbose:
        print(text)
        print(f"\n[{len(calls)} tool call(s), model={MODEL}]")
    return {"text": text, "tool_calls": calls, "model": MODEL}


if __name__ == "__main__":
    asyncio.run(run(" ".join(sys.argv[1:]) or "For Sleeper league 1315386978904084480, what should kbmckenna do and why?"))
