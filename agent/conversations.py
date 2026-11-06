"""Conversations for the Messages API backend (CHAT_BACKEND=messages): each is only its message
list, so a turn loads it, runs, and saves it - no process is kept per conversation and there is
no slot to wait for. The same methods as sessions.SessionManager, so agent/api.py serves either.
Stored in Supabase Postgres when DATABASE_URL is set (any instance can then serve any chat, and a
restart loses nothing), in this process's memory otherwise."""

import asyncio
import time

from psycopg.types.json import Jsonb

from . import db
from .agent import new_conversation

KEEP_SECONDS = 24 * 3600   # long enough to come back to a chat later in the day

_memory: dict[str, tuple[float, list]] = {}
_locks: dict[str, asyncio.Lock] = {}


def _load(session_id: str) -> list | None:
    if not db.enabled():
        saved = _memory.get(session_id)
        return list(saved[1]) if saved else None
    with db.connect() as conn:
        row = conn.execute("select messages from conversations where id = %s", (session_id,)).fetchone()
    return row[0] if row else None


def _save(session_id: str, messages: list) -> None:
    if not db.enabled():
        now = time.time()
        for sid in [s for s, (t, _) in _memory.items() if now - t > KEEP_SECONDS]:
            _memory.pop(sid, None)
        _memory[session_id] = (now, messages)
        return
    with db.connect() as conn:
        conn.execute("insert into conversations (id, messages) values (%s, %s) on conflict (id) do update "
                   "set messages = excluded.messages, updated_at = now()", (session_id, Jsonb(messages)))
        conn.execute("delete from conversations where updated_at < now() - make_interval(secs => %s)", (KEEP_SECONDS,))


def _drop(session_id: str) -> None:
    _memory.pop(session_id, None)
    if db.enabled():
        with db.connect() as conn:
            conn.execute("delete from conversations where id = %s", (session_id,))


class Conversation:
    def __init__(self, session_id: str, messages: list | None):
        self.id = session_id
        self.client = new_conversation(messages=messages)
        self.lock = _locks.setdefault(session_id, asyncio.Lock())

    def cost_delta(self, cumulative: float | None) -> float | None:
        return cumulative   # a client is rebuilt every turn, so its total is this turn's


class ConversationStore:
    async def acquire(self, session_id: str, tier: str = "friend", on_wait=None) -> tuple[Conversation, bool]:
        messages = await asyncio.to_thread(_load, session_id)
        return Conversation(session_id, messages), messages is None

    async def save(self, conversation: Conversation) -> None:
        await asyncio.to_thread(_save, conversation.id, conversation.client.messages)

    async def prewarm(self, session_id: str, tier: str = "friend") -> bool:
        return False   # nothing to start: the tools already live in this process

    async def drop(self, session_id: str) -> None:
        _locks.pop(session_id, None)
        await asyncio.to_thread(_drop, session_id)

    async def close_all(self) -> None:
        pass

    def status(self) -> dict:
        return {"backend": "messages", "store": "postgres" if db.enabled() else "memory"}
