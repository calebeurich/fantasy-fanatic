"""Supabase Postgres, when DATABASE_URL is set: conversations (conversations.py) and the daily
spend counters (budget.py), so every instance shares them. Without it both keep their state in
this process, which is exact only while one instance serves everything."""

import os

import psycopg

SCHEMA = """
create table if not exists conversations (
    id text primary key, messages jsonb not null, updated_at timestamptz not null default now());
create table if not exists daily_spend (
    day date, tier text, usd double precision not null default 0, requests integer not null default 0,
    primary key (day, tier));
create table if not exists demo_visitors (
    day date, visitor text, asks integer not null default 0, primary key (day, visitor));
"""
_schema_ready = False


def enabled() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


def connect() -> psycopg.Connection:
    """The tables are created on first use, so a fresh database needs no setup step."""
    global _schema_ready
    # prepare_threshold=None: Supabase's transaction pooler can't hold prepared statements.
    db = psycopg.connect(os.environ["DATABASE_URL"], prepare_threshold=None, autocommit=True, connect_timeout=10)
    if not _schema_ready:
        db.execute(SCHEMA)
        _schema_ready = True
    return db
