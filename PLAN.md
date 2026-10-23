# Open Source Migration Plan: Supabase / PostgreSQL -> Local SQLite3

## Goal
Completely decouple the project from cloud Supabase and external PostgreSQL databases, enabling a 100% self-contained, zero-configuration local SQLite3 persistence architecture.

## Step-by-Step Execution Plan

### Step 1: Database Engine Core (`app/db/`)
- [x] Create `app/db/sqlite_engine.py` (Local SQLite connection manager, thread safety, auto table schema creation for `ttt_template_store`, `ttt_order_events`, `ttt_trades`, `ttt_intercept_log`, `user_watchlist`, `watchlist`, `signals`).
- [x] Refactor `app/db/__init__.py` to use the local SQLite3 engine by default.
- [x] Provide SQL query compatibility wrapper (translating `%s` to `?` and registering custom SQL functions for `least`, `greatest`, `now`, `coalesce`, and `btrim`).

### Step 2: Supabase REST Client Decoupling (`app/core/sb_client.py`)
- [x] Refactor `app/core/sb_client.py` to route all queries (e.g. `client.table("user_watchlist").select(...).execute()`) directly to the local SQLite3 database.
- [x] Eliminate `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` requirements from runtime startup.

### Step 3: Template & Trade Recorder Adaptation
- [x] Update `app/templates.py` to ensure template saving and loading functions cleanly against SQLite3.
- [x] Seed a default configuration template into `ttt_template_store` automatically upon first launch so the system works out-of-the-box.
- [x] Seed default watchlist tickers (`SPY`, `QQQ`, `IWM`) so watchlist initialization never fails.
- [x] Verify `app/trade_recorder.py` and `OrderJournal` for SQLite3 compatibility.

### Step 4: Dependencies and Configuration Cleanup
- [x] Clean up `.env.example`: Remove `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `DATABASE_URL`; add optional `SQLITE_DB_PATH=data/trading.db`.
- [x] Clean up `requirements.txt`: Remove `supabase` and `psycopg2-binary`.
- [x] Clean up `requirements-dev.txt`: Remove `psycopg2-binary`.
- [x] Update `README.md` architecture diagram (change `PostgreSQL / Supabase` to `Local SQLite3`) and setup guides.

### Step 5: Test Suite Verification & Quality Assurance
- [x] Run core test suites to verify zero regressions (All passed).
- [x] Update `CHANGELOG.md`.
