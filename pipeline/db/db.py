"""Thin DB-API adapter: sqlite3 (local dev, default) or psycopg (Railway
Postgres deployment), selected by the DATABASE_URL scheme. Both back ends
speak the same portable schema.sql (see that file's header comment).

Locker immutability: the app connection never issues UPDATE/DELETE against
cases/case_sources/artifacts. Deployment hardens this with a DB-level grant
(migrate.py, Postgres only); local sqlite dev relies on code discipline
plus this module only exposing insert_* / select_* helpers for those tables.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from pipeline.settings import ROOT, settings


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


def _is_postgres() -> bool:
    return settings.database_url.startswith(("postgres://", "postgresql://"))


@contextlib.contextmanager
def get_conn() -> Iterator[Any]:
    if _is_postgres():
        import psycopg

        conn = psycopg.connect(settings.database_url)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        path = settings.database_url.removeprefix("sqlite:///")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def qmark(sql: str) -> str:
    """sqlite3 uses '?' placeholders; psycopg uses '%s'. Write queries with
    '?' and this converts them for postgres at call time."""
    return sql.replace("?", "%s") if _is_postgres() else sql


def init_schema() -> None:
    schema = (ROOT / "pipeline" / "db" / "schema.sql").read_text()
    with get_conn() as conn:
        if _is_postgres():
            with conn.cursor() as cur:
                cur.execute(schema)
        else:
            conn.executescript(schema)


def dict_rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(s: str | None) -> Any:
    return json.loads(s) if s else None
