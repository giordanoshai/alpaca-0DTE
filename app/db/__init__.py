"""Database connection and engine coordinator for alpaca-0DTE.

Defaults to local zero-configuration SQLite3 engine (data/trading.db).
If DATABASE_URL is explicitly set with a postgresql:// scheme, falls back to psycopg2.
"""
from __future__ import annotations

import os
import pathlib
import re

from app.db import sqlite_engine

_BASE = pathlib.Path(__file__).resolve().parent.parent.parent


def database_url() -> str:
    """Return configured database URL or default local SQLite URI."""
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    envf = _BASE / ".env"
    if envf.exists():
        for line in envf.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*DATABASE_URL\s*=\s*(.*)\s*$", line)
            if m:
                val = m.group(1).strip().strip('"').strip("'")
                if val:
                    return val
    return f"sqlite:///{sqlite_engine.get_sqlite_path().as_posix()}"


def connect():
    """Establish and return database connection (SQLite by default)."""
    url = ""
    try:
        url = database_url()
    except Exception:
        url = ""

    if url.startswith("postgresql://") or url.startswith("postgres://"):
        try:
            import psycopg2
            return psycopg2.connect(url, connect_timeout=8)
        except ImportError:
            pass

    # Default to local SQLite3 engine
    return sqlite_engine.connect()


def init_db():
    """Initialize SQLite database tables and default seed records."""
    sqlite_engine.init_database()


# Auto-initialize SQLite tables on module import
try:
    init_db()
except Exception as _e:
    pass
