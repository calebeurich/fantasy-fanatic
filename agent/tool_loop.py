"""One conversation with the agent as a plain Messages API tool loop - no `claude` CLI and no
MCP subprocess. The tools are agent/mcp_server.py's own functions, called in this process, so
every conversation shares one data cache, and a conversation is just its message list:
any process can pick it up from storage. Returns what agent.agent._run_turn returns, so the
grounding checks run unchanged. ROADMAP "NEXT BUILD (agreed 2026-10-29)"."""

import asyncio
import ssl
import sys
from types import SimpleNamespace

import anthropic

from . import mcp_server

# Haiku 5.5 per million tokens, prompts up to 100K (beyond: input $0.50, output $2.50).
# Cache writes are 1.25x input (5-minute TTL), reads 0.1x.
PRICE_IN, PRICE_OUT = 0.10, 0.50
PRICE_IN_LONG, PRICE_OUT_LONG = 0.50, 2.50
LONG_PROMPT = 100_000
MAX_TOKENS = 32_000

_client = None
_tools = None


def _api() -> anthropic.AsyncAnthropic:
    global _client
    # Windows' own trust store: Norton's TLS inspection root is only there, not in certifi.
    http = anthropic.DefaultAsyncHttpxClient(verify=ssl.create_default_context()) if sys.platform == "win32" else None
    _client = _client or anthropic.AsyncAnthropic(http_client=http)
    return _client


async def _load_tools() -> list[dict]:
    """The MCP server's own schemas, in its own order - a stable prefix for the prompt cache."""
    global _tools
    if _tools is None:
        _tools = [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema}
                  for t in await mcp_server.mcp.list_tools()]
    return _tools


def _cost(usage) -> float:
    prompt = usage.input_tokens + (usage.cache_creation_input_tokens or 0) + (usage.cache_read_input_tokens or 0)
    p_in, p_out = (PRICE_IN_LONG, PRICE_OUT_LONG) if prompt > LONG_PROMPT else (PRICE_IN, PRICE_OUT)
    return (usage.input_tokens * p_in + (usage.cache_creation_input_tokens or 0) * p_in * 1.25
            + (usage.cache_read_input_tokens or 0) * p_in * 0.1 + usage.output_tokens * p_out) / 1e6


def _run_tool(name: str, args: dict) -> tuple[str, bool]:
    """Through FastMCP's call_tool, so arguments are validated and results serialized exactly
    as the MCP server did. Its own event loop, because this runs in a worker thread."""
    try:
        blocks = asyncio.run(mcp_server.mcp.call_tool(name, args))
        return "\n".join(b.text for b in blocks if getattr(b, "type", "") == "text"), False
    except Exception as e:  # a bad league id or owner name: the model reads it and recovers
        return f"Error executing tool {name}: {e}", True


def _service_error(e: anthropic.APIError) -> str:
    if isinstance(e, anthropic.AuthenticationError):
        return "authentication_failed"
    if isinstance(e, anthropic.RateLimitError):
        return "rate_limit"
    if "credit balance" in str(e).lower():
        return "billing_error"
    return "server_error"


class Conversation:
    def __init__(self, model: str, system: str, messages: list | None = None, max_turns: int = 8,
                 max_cost_usd: float = 0.50, tool_prefix: str = ""):
        self.model, self.system = model, system
        self.messages = messages or []
        self.max_turns, self.max_cost_usd, self.tool_prefix = max_turns, max_cost_usd, tool_prefix
        self.cost = 0.0   # this object's lifetime - one request, since it is rebuilt from storage

    async def turn(self, message: str, verbose: bool = False, on_progress=None, on_text=None) -> dict:
        from .agent import AIServiceDown, _progress_label   # agent imports this module
        tools = await _load_tools()
        self.messages.append({"role": "user", "content": message})
        text_parts, tool_calls, tool_results, streamed = [], [], [], ""
        usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        calls, stop = 0, None
        while calls < self.max_turns and self.cost < self.max_cost_usd:
            calls += 1
            try:
                async with _api().messages.stream(
                        model=self.model, max_tokens=MAX_TOKENS, tools=tools,
                        system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
                        cache_control={"type": "ephemeral"}, messages=self.messages) as stream:
                    async for delta in stream.text_stream:
                        streamed += delta
                        if on_text:
                            on_text(streamed)
                    reply = await stream.get_final_message()
            except anthropic.APIError as e:
                raise AIServiceDown(_service_error(e)) from e
            if streamed and not streamed.endswith("\n"):
                streamed += "\n"
            self.cost += _cost(reply.usage)
            for k in usage:
                usage[k] += getattr(reply.usage, k) or 0
            self.messages.append({"role": "assistant", "content": [b.to_dict() for b in reply.content]})
            stop = reply.stop_reason
            uses = [b for b in reply.content if b.type == "tool_use"]
            text_parts += [b.text for b in reply.content if b.type == "text"]
            if stop != "tool_use" or not uses:
                break
            for b in uses:
                tool_calls.append({"name": self.tool_prefix + b.name, "input": b.input})
                if on_progress:
                    on_progress(_progress_label(b.name))
                if verbose:
                    print(f"[tool call: {b.name}({b.input})]")
            outputs = await asyncio.gather(*(asyncio.to_thread(_run_tool, b.name, b.input) for b in uses))
            for b, (out, is_error) in zip(uses, outputs):
                tool_results.append({"name": self.tool_prefix + b.name, "is_error": is_error, "content": out})
            self.messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": out, "is_error": is_error}
                for b, (out, is_error) in zip(uses, outputs)]})
        text = "\n".join(text_parts)
        if verbose:
            print(text)
            print(f"\n[{calls} call(s), ${self.cost:.4f}, stop_reason={stop}]")
        result = SimpleNamespace(num_turns=calls, total_cost_usd=self.cost, usage=usage, stop_reason=stop)
        return {"text": text, "tool_calls": tool_calls, "tool_results": tool_results, "result": result}
