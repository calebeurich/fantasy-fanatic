"""Conversations: each is only its message list, so a turn loads it, runs, and saves it - no
process is kept per conversation and there is no slot to wait for. Stored in Supabase Postgres
when DATABASE_URL is set (any instance can then serve any chat, and a restart loses nothing),
in this process's memory otherwise - and for the moment the database can't be reached (the free
tier pauses after a week idle), so a chat still answers."""

import asyncio
import sys
import time

from psycopg.types.json import Jsonb

from . import db

KEEP_SECONDS = 24 * 3600   # long enough to come back to a chat later in the day

_memory: dict[str, tuple[float, list]] = {}
_locks: dict[str, asyncio.Lock] = {}


def lock(session_id: str) -> asyncio.Lock:
    """Held from load to save, so two asks on one chat in this process take turns instead of
    one overwriting the other's history."""
    return _locks.setdefault(session_id, asyncio.Lock())


def _db_failed(e: Exception) -> None:
    print(f"conversations: database unavailable, using this process's memory: {type(e).__name__}: {e}", file=sys.stderr)


def _load(session_id: str) -> list | None:
    if db.enabled():
        try:
            with db.connect() as conn:
                row = conn.execute("select messages from conversations where id = %s", (session_id,)).fetchone()
            return row[0] if row else None
        except Exception as e:
            _db_failed(e)
    saved = _memory.get(session_id)
    return list(saved[1]) if saved else None


def _save(session_id: str, messages: list) -> None:
    if db.enabled():
        try:
            with db.connect() as conn:
                conn.execute("insert into conversations (id, messages) values (%s, %s) on conflict (id) do update "
                             "set messages = excluded.messages, updated_at = now()", (session_id, Jsonb(messages)))
                conn.execute("delete from conversations where updated_at < now() - make_interval(secs => %s)",
                             (KEEP_SECONDS,))
            return
        except Exception as e:
            _db_failed(e)
    now = time.time()
    for sid in [s for s, (t, _) in _memory.items() if now - t > KEEP_SECONDS]:
        _memory.pop(sid, None)
    _memory[session_id] = (now, messages)


def _drop(session_id: str) -> None:
    _memory.pop(session_id, None)
    if db.enabled():
        try:
            with db.connect() as conn:
                conn.execute("delete from conversations where id = %s", (session_id,))
        except Exception as e:
            _db_failed(e)


async def load(session_id: str) -> list | None:
    """The chat's messages, or None when it is new - or was lost, which the page then says."""
    return await asyncio.to_thread(_load, session_id)


async def save(session_id: str, messages: list) -> None:
    await asyncio.to_thread(_save, session_id, messages)


async def drop(session_id: str) -> None:
    _locks.pop(session_id, None)
    await asyncio.to_thread(_drop, session_id)


def status() -> dict:
    return {"store": "postgres" if db.enabled() else "memory", "locks_in_this_process": len(_locks)}
