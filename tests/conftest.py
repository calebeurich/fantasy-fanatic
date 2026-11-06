import pytest


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    """Tests never touch the real Supabase database, even when a local .env names it."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
