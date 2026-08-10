"""Per-session conversation state: keeps a `ClaudeSDKClient` alive across turns so a
user can actually have a conversation instead of restating everything each message.

Fixes three documented problems at once, which is why it's worth the complexity:
1. **No memory.** Previously every question opened a fresh client, so the agent asked
   "what's your league ID?" and then had no recollection of asking.
2. **Cold Anthropic prompt cache.** Measured earlier: a fresh session re-pays ~4,700
   tokens of cache *creation* per question (billed at 1.25x), while a second turn on
   the same session read ~9,800 tokens from cache. Reuse turns that cost into a hit.
3. **Cold data cache.** `sources/cache.py` lives in the MCP server subprocess, which
   is spawned per client - so a fresh client meant re-downloading FantasyCalc and
   nflverse data every question. A persistent session keeps it warm.

**Sessions are never shared between callers.** Each is keyed by a client-supplied id
and used only for that id. This is the context-leak trap: sharing one client across
users would leak one person's conversation into another's, which is why the naive
"just reuse the client" version was rejected when the caching investigation first
surfaced it.

In-memory, no database - same reasoning as `budget.py`: with `max-instances=1` a
process-local map is exactly correct. Sessions are lost on instance recycle, which
degrades to today's behavior (the user starts a new conversation), not to an error.
"""

import asyncio
import os
import time

from claude_agent_sdk import ClaudeSDKClient

# Each live session holds *two* subprocesses (the `claude` CLI on Node, and the Python
# MCP server with polars/pandas loaded), on top of the uvicorn parent which already has
# them. At the container's 2 GiB that ceiling comes fast, so this is deliberately tiny.
# Raise only alongside the memory limit.
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "2"))

# Close a session after this long unused. Long enough to think between questions,
# short enough that an abandoned tab frees its subprocesses reasonably soon.
IDLE_TTL_SECONDS = float(os.environ.get("SESSION_IDLE_TTL", "900"))


class Session:
    def __init__(self, session_id: str, client: ClaudeSDKClient):
        self.id = session_id
        self.client = client
        self.last_used = time.monotonic()
        # Serializes turns within one session: two concurrent requests sharing a client
        # would interleave on the same conversation and corrupt it.
        self.lock = asyncio.Lock()


class SessionManager:
    def __init__(self, options_factory):
        self._options_factory = options_factory
        self._sessions: dict[str, Session] = {}
        self._guard = asyncio.Lock()  # protects the map itself, not individual turns

    async def acquire(self, session_id: str) -> Session:
        async with self._guard:
            await self._evict_idle()
            session = self._sessions.get(session_id)
            if session is not None:
                session.last_used = time.monotonic()
                return session

            # Evict the least-recently-used session to stay under the cap, rather than
            # refusing the request - a demo visitor shouldn't hit "too many sessions".
            while len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions.values(), key=lambda s: s.last_used)
                await self._close(oldest)

            client = ClaudeSDKClient(options=self._options_factory())
            await client.connect()
            session = Session(session_id, client)
            self._sessions[session_id] = session
            return session

    async def _evict_idle(self) -> None:
        now = time.monotonic()
        for session in [s for s in self._sessions.values() if now - s.last_used > IDLE_TTL_SECONDS]:
            await self._close(session)

    async def _close(self, session: Session) -> None:
        self._sessions.pop(session.id, None)
        try:
            await session.client.disconnect()
        except Exception:
            pass  # a session being torn down shouldn't be able to fail a live request

    async def close_all(self) -> None:
        async with self._guard:
            for session in list(self._sessions.values()):
                await self._close(session)

    def status(self) -> dict:
        now = time.monotonic()
        return {
            "active_sessions": len(self._sessions),
            "max_sessions": MAX_SESSIONS,
            "idle_ttl_seconds": IDLE_TTL_SECONDS,
            "sessions": [
                {"id": s.id[:8], "idle_seconds": round(now - s.last_used, 1)}
                for s in self._sessions.values()
            ],
        }
