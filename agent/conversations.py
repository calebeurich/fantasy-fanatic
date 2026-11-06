"""Conversations for the Messages API backend (CHAT_BACKEND=messages): each is only its message
list, so a turn loads it, runs, and saves it - no process is kept per conversation and there is
no slot to wait for. The same methods as sessions.SessionManager, so agent/api.py serves either.
Kept in memory here; the Supabase store replaces `_saved` (ROADMAP "NEXT BUILD")."""

import asyncio
import time

from .agent import new_conversation

KEEP_SECONDS = 24 * 3600   # long enough to come back to a chat later in the day


class Conversation:
    def __init__(self, session_id: str, messages: list | None):
        self.id = session_id
        self.client = new_conversation(messages=messages)
        self.lock = _locks.setdefault(session_id, asyncio.Lock())

    def cost_delta(self, cumulative: float | None) -> float | None:
        return cumulative   # a client is rebuilt every turn, so its total is this turn's


_saved: dict[str, tuple[float, list]] = {}
_locks: dict[str, asyncio.Lock] = {}


class ConversationStore:
    async def acquire(self, session_id: str, tier: str = "friend", on_wait=None) -> tuple[Conversation, bool]:
        saved = _saved.get(session_id)
        return Conversation(session_id, list(saved[1]) if saved else None), saved is None

    async def save(self, conversation: Conversation) -> None:
        now = time.time()
        for sid in [s for s, (t, _) in _saved.items() if now - t > KEEP_SECONDS]:
            _saved.pop(sid, None)
            _locks.pop(sid, None)
        _saved[conversation.id] = (now, conversation.client.messages)

    async def prewarm(self, session_id: str, tier: str = "friend") -> bool:
        return False   # nothing to start: the tools already live in this process

    async def drop(self, session_id: str) -> None:
        _saved.pop(session_id, None)

    async def close_all(self) -> None:
        pass

    def status(self) -> dict:
        return {"backend": "messages", "saved_conversations": len(_saved)}
