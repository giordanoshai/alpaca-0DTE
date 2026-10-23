"""Local SQLite-backed Database Client for alpaca-0DTE.

Provides a drop-in replacement for Supabase Client, enabling completely local,
offline execution without any external Supabase account or credentials.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app import db
from app.core.dailylogger import get_logger

logger = get_logger("tt.sb_client")


@dataclass
class LocalResponse:
    data: list[dict[str, Any]]
    count: int | None = None


class LocalTableQuery:
    """Fluent query builder executing against local SQLite database."""

    def __init__(self, table_name: str):
        self.table_name = table_name
        self.op = "select"
        self.select_cols = "*"
        self.filters: list[tuple[str, str, Any]] = []
        self.payload: Any = None
        self.on_conflict: str | None = None
        self.order_by: str | None = None
        self.order_desc: bool = False
        self.limit_val: int | None = None
        self.not_flag: bool = False

    @property
    def not_(self) -> LocalTableQuery:
        self.not_flag = True
        return self

    def is_(self, col: str, val: Any) -> LocalTableQuery:
        op = "IS NOT" if self.not_flag else "IS"
        self.not_flag = False
        if val is None or str(val).lower() == "null":
            self.filters.append((col, f"{op} NULL", None))
        else:
            self.filters.append((col, f"{op} ?", val))
        return self

    def select(self, cols: str = "*") -> LocalTableQuery:
        self.op = "select"
        self.select_cols = cols
        return self

    def eq(self, col: str, val: Any) -> LocalTableQuery:
        if self.not_flag:
            self.not_flag = False
            self.filters.append((col, "!= ?", val))
        else:
            self.filters.append((col, "= ?", val))
        return self

    def neq(self, col: str, val: Any) -> LocalTableQuery:
        self.filters.append((col, "!= ?", val))
        return self

    def order(self, col: str, desc: bool = False) -> LocalTableQuery:
        self.order_by = col
        self.order_desc = desc
        return self

    def limit(self, n: int) -> LocalTableQuery:
        self.limit_val = int(n)
        return self

    def insert(self, data: dict | list[dict]) -> LocalTableQuery:
        self.op = "insert"
        self.payload = data
        return self

    def upsert(self, data: dict | list[dict], on_conflict: str | None = None) -> LocalTableQuery:
        self.op = "upsert"
        self.payload = data
        self.on_conflict = on_conflict
        return self

    def update(self, data: dict) -> LocalTableQuery:
        self.op = "update"
        self.payload = data
        return self

    def delete(self) -> LocalTableQuery:
        self.op = "delete"
        return self

    def _build_where(self) -> tuple[str, list[Any]]:
        if not self.filters:
            return "", []
        clauses = []
        params = []
        for col, cond, val in self.filters:
            clauses.append(f"{col} {cond}")
            if val is not None:
                params.append(val)
        return " WHERE " + " AND ".join(clauses), params

    def execute(self) -> LocalResponse:
        with db.connect() as conn:
            cur = conn.cursor()

            if self.op == "select":
                where_clause, params = self._build_where()
                sql = f"SELECT {self.select_cols} FROM {self.table_name}{where_clause}"
                if self.order_by:
                    dir_str = "DESC" if self.order_desc else "ASC"
                    sql += f" ORDER BY {self.order_by} {dir_str}"
                if self.limit_val is not None:
                    sql += f" LIMIT {self.limit_val}"

                cur.execute(sql, tuple(params) if params else None)
                rows = cur.fetchall()
                cols = [desc[0] for desc in cur.description] if cur.description else []
                data = [dict(zip(cols, row)) for row in rows]
                return LocalResponse(data=data, count=len(data))

            elif self.op in ("insert", "upsert"):
                items = self.payload if isinstance(self.payload, list) else [self.payload]
                for item in items:
                    if not isinstance(item, dict) or not item:
                        continue
                    cols = list(item.keys())
                    vals = []
                    for v in item.values():
                        if isinstance(v, (dict, list)):
                            vals.append(json.dumps(v, ensure_ascii=False))
                        else:
                            vals.append(v)

                    placeholders = ", ".join(["?"] * len(cols))
                    col_names = ", ".join(cols)

                    if self.op == "upsert" and self.on_conflict:
                        update_clause = ", ".join([f"{c}=excluded.{c}" for c in cols if c != self.on_conflict])
                        sql = f"""
                        INSERT INTO {self.table_name} ({col_names})
                        VALUES ({placeholders})
                        ON CONFLICT({self.on_conflict}) DO UPDATE SET {update_clause}
                        """
                    else:
                        sql = f"INSERT INTO {self.table_name} ({col_names}) VALUES ({placeholders})"

                    cur.execute(sql, tuple(vals))
                conn.commit()
                return LocalResponse(data=items if isinstance(self.payload, list) else [self.payload])

            elif self.op == "update":
                if not isinstance(self.payload, dict) or not self.payload:
                    return LocalResponse(data=[])
                set_clauses = []
                params = []
                for k, v in self.payload.items():
                    set_clauses.append(f"{k} = ?")
                    if isinstance(v, (dict, list)):
                        params.append(json.dumps(v, ensure_ascii=False))
                    else:
                        params.append(v)

                where_clause, where_params = self._build_where()
                sql = f"UPDATE {self.table_name} SET {', '.join(set_clauses)}{where_clause}"
                cur.execute(sql, tuple(params + where_params))
                conn.commit()
                return LocalResponse(data=[])

            elif self.op == "delete":
                where_clause, params = self._build_where()
                sql = f"DELETE FROM {self.table_name}{where_clause}"
                cur.execute(sql, tuple(params) if params else None)
                conn.commit()
                return LocalResponse(data=[])

            return LocalResponse(data=[])


class LocalDatabaseClient:
    """Local SQLite-driven database client matching Supabase Client API."""

    def table(self, table_name: str) -> LocalTableQuery:
        return LocalTableQuery(table_name)


_local_client: LocalDatabaseClient | None = None


def get_supabase_client() -> LocalDatabaseClient:
    """Return local SQLite database client. Zero external credentials needed."""
    global _local_client
    if _local_client is None:
        _local_client = LocalDatabaseClient()
    return _local_client
