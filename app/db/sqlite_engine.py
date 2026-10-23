"""Local SQLite3 Database Engine & Schema Manager for alpaca-0DTE.

Provides a zero-configuration, self-contained relational database layer
replacing remote Supabase and PostgreSQL for open-source deployment.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from app.core.dailylogger import get_logger

logger = get_logger("tt.db")

_BASE = pathlib.Path(__file__).resolve().parent.parent.parent
_DEFAULT_DB_PATH = _BASE / "data" / "trading.db"
_DB_LOCK = threading.RLock()


def get_sqlite_path() -> pathlib.Path:
    """Return the absolute path to the local SQLite database file."""
    custom = os.environ.get("SQLITE_DB_PATH")
    if custom:
        p = pathlib.Path(custom)
        if not p.is_absolute():
            p = _BASE / p
        return p
    return _DEFAULT_DB_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fn_least(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _fn_greatest(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _fn_coalesce(*args):
    for arg in args:
        if arg is not None:
            return arg
    return None


class SQLiteCursorWrapper:
    """Cursor wrapper that transparently adapts PostgreSQL %s placeholders to SQLite ?."""

    def __init__(self, raw_cursor: sqlite3.Cursor):
        self._cur = raw_cursor

    def execute(self, sql: str, params: tuple | list | None = None) -> SQLiteCursorWrapper:
        adapted_sql = sql
        # Adapt PostgreSQL %s to SQLite ?
        if params is not None and "%s" in adapted_sql:
            adapted_sql = adapted_sql.replace("%s", "?")

        # Strip table qualifiers like public.tablename -> tablename
        if "public." in adapted_sql:
            adapted_sql = adapted_sql.replace("public.", "")

        if params is None:
            self._cur.execute(adapted_sql)
        else:
            self._cur.execute(adapted_sql, params)
        return self

    def executemany(self, sql: str, seq_of_params) -> SQLiteCursorWrapper:
        adapted_sql = sql
        if "%s" in adapted_sql:
            adapted_sql = adapted_sql.replace("%s", "?")
        if "public." in adapted_sql:
            adapted_sql = adapted_sql.replace("public.", "")
        self._cur.executemany(adapted_sql, seq_of_params)
        return self

    def fetchone(self) -> Any:
        return self._cur.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cur.fetchall()

    def fetchmany(self, size: int = 1) -> list[Any]:
        return self._cur.fetchmany(size)

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self) -> int | None:
        return self._cur.lastrowid

    @property
    def description(self):
        return self._cur.description

    def close(self):
        self._cur.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class SQLiteConnectionWrapper:
    """Connection wrapper ensuring full context manager and cursor compatibility."""

    def __init__(self, raw_conn: sqlite3.Connection):
        self._conn = raw_conn
        # Register user-defined functions to match PostgreSQL built-ins
        self._conn.create_function("now", 0, _now_iso)
        self._conn.create_function("least", 2, _fn_least)
        self._conn.create_function("greatest", 2, _fn_greatest)
        self._conn.create_function("coalesce", -1, _fn_coalesce)
        self._conn.create_function("btrim", 1, lambda s: s.strip() if isinstance(s, str) else s)

    def cursor(self) -> SQLiteCursorWrapper:
        return SQLiteCursorWrapper(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()
        self.close()


def connect() -> SQLiteConnectionWrapper:
    """Establish and return an active connection to the local SQLite database."""
    db_path = get_sqlite_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(db_path), timeout=15.0, check_same_thread=False)
    # Enable Write-Ahead Logging (WAL) for concurrency
    raw.execute("PRAGMA journal_mode=WAL;")
    raw.execute("PRAGMA synchronous=NORMAL;")
    return SQLiteConnectionWrapper(raw)


def init_database():
    """Idempotently initialize all core database tables and default seed data."""
    with _DB_LOCK:
        with connect() as conn:
            cur = conn.cursor()

            # 1. ttt_template_store
            cur.execute("""
            CREATE TABLE IF NOT EXISTS ttt_template_store (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP)
            );
            """)

            # 2. ttt_order_events
            cur.execute("""
            CREATE TABLE IF NOT EXISTS ttt_order_events (
                order_id TEXT PRIMARY KEY,
                parent_order_id TEXT,
                client_order_id TEXT,
                ticker TEXT NOT NULL,
                option_code TEXT NOT NULL,
                side TEXT NOT NULL,
                qty INTEGER NOT NULL,
                status TEXT NOT NULL,
                origin TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT,
                cancel_origin TEXT,
                fail_reason TEXT,
                broker_status TEXT,
                price REAL,
                price_type TEXT,
                limit_price REAL,
                submitted_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP),
                triggered_at TEXT,
                trigger_bid REAL,
                trigger_ask REAL,
                trigger_value REAL,
                trigger_loss REAL,
                stop_mode TEXT,
                stop_threshold REAL,
                entry_value REAL
            );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_order_events_submitted ON ttt_order_events (submitted_at DESC);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_order_events_parent ON ttt_order_events (parent_order_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_order_events_status ON ttt_order_events (status, updated_at DESC);")

            # 3. ttt_trades
            cur.execute("""
            CREATE TABLE IF NOT EXISTS ttt_trades (
                order_id TEXT PRIMARY KEY,
                ticker TEXT,
                direction TEXT,
                option_code TEXT,
                qty INTEGER,
                entry_mid REAL,
                entry_time TEXT,
                exit_mid REAL,
                exit_time TEXT,
                realized_pnl REAL,
                total_pnl REAL,
                status TEXT DEFAULT 'pending',
                source TEXT,
                template_snapshot TEXT,
                close_reason TEXT,
                created_at TEXT DEFAULT (CURRENT_TIMESTAMP),
                updated_at TEXT DEFAULT (CURRENT_TIMESTAMP)
            );
            """)

            # 4. ttt_intercept_log
            cur.execute("""
            CREATE TABLE IF NOT EXISTS ttt_intercept_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT,
                direction TEXT,
                qty_requested INTEGER,
                rule TEXT,
                reason TEXT,
                source TEXT,
                market_snapshot TEXT,
                template_snapshot TEXT,
                created_at TEXT DEFAULT (CURRENT_TIMESTAMP)
            );
            """)

            # 5. user_watchlist & watchlist
            cur.execute("""
            CREATE TABLE IF NOT EXISTS user_watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT DEFAULT 'default',
                ticker TEXT NOT NULL UNIQUE,
                gear TEXT DEFAULT 'instant_0.15',
                created_at TEXT DEFAULT (CURRENT_TIMESTAMP)
            );
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL UNIQUE,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (CURRENT_TIMESTAMP)
            );
            """)

            # 6. signals
            cur.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT,
                signal_type TEXT,
                timeframe TEXT DEFAULT '1m',
                price REAL DEFAULT 0.0,
                score REAL,
                indicators TEXT,
                regime TEXT,
                validator_result TEXT,
                direction TEXT,
                confidence REAL,
                entry_price REAL,
                stop_loss REAL,
                take_profit REAL,
                payload TEXT,
                timestamp TEXT,
                created_at TEXT DEFAULT (CURRENT_TIMESTAMP)
            );
            """)


            # 7. trading_rules
            cur.execute("""
            CREATE TABLE IF NOT EXISTS trading_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_key TEXT UNIQUE,
                value TEXT,
                description TEXT,
                updated_at TEXT DEFAULT (CURRENT_TIMESTAMP)
            );
            """)

            # 8. option_liquidity_probe
            cur.execute("""
            CREATE TABLE IF NOT EXISTS option_liquidity_probe (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT,
                option_symbol TEXT,
                bid REAL,
                ask REAL,
                spread REAL,
                created_at TEXT DEFAULT (CURRENT_TIMESTAMP)
            );
            """)

            # Seed default template if empty
            cur.execute("SELECT count(*) FROM ttt_template_store;")
            row = cur.fetchone()
            if row and row[0] == 0:
                default_payload = {
                    "params": {
                        "max_daily_loss": "150",
                        "max_weekly_loss": "300",
                        "max_position_size": "500",
                        "opening_blackout_min": "10",
                        "min_delta": "0.40",
                        "max_spread_pct": "0.03",
                        "stop_pct": "0.20",
                        "max_loss_per_trade": "80",
                        "max_hold_mins": "15",
                        "cooldown_seconds": 600,
                        "position_tolerance": "0.20",
                        "max_trades_per_day": 50,
                        "daily_profit_target": "5000",
                        "wall_room_atr_threshold": "3.0",
                        "entry_cutoff_mins": "150",
                        "order_timeout_s": "5",
                    },
                    "reason": "Default open-source risk configuration",
                    "status": "active",
                    "created_at": _now_iso(),
                }
                cur.execute(
                    "INSERT INTO ttt_template_store (id, name, payload, updated_at) VALUES (?, ?, ?, ?)",
                    (1, "Default", json.dumps(default_payload, ensure_ascii=False), _now_iso()),
                )
                logger.info("[DB] Seeded initial risk template into ttt_template_store")

            # Seed default user_watchlist if empty
            cur.execute("SELECT count(*) FROM user_watchlist;")
            row = cur.fetchone()
            if row and row[0] == 0:
                for t in ("SPY", "QQQ", "IWM"):
                    cur.execute(
                        "INSERT OR IGNORE INTO user_watchlist (user_id, ticker, gear, created_at) VALUES ('default', ?, 'instant_0.15', ?)",
                        (t, _now_iso()),
                    )
                logger.info("[DB] Seeded default user_watchlist: SPY, QQQ, IWM")

            # Seed default watchlist if empty
            cur.execute("SELECT count(*) FROM watchlist;")
            row = cur.fetchone()
            if row and row[0] == 0:
                for t in ("SPY", "QQQ", "IWM"):
                    cur.execute(
                        "INSERT OR IGNORE INTO watchlist (ticker, is_active, created_at) VALUES (?, 1, ?)",
                        (t, _now_iso()),
                    )
                logger.info("[DB] Seeded default watchlist: SPY, QQQ, IWM")

