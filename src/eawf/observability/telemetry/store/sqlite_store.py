"""SQLite metrics-store backend — the always-available default.

Built on the stdlib :mod:`sqlite3` module so it carries zero extra
dependency weight. This is the backend :func:`~eawf.observability.telemetry.store.base.
open_store` returns when ``duckdb`` is unavailable, and the default for
operators who do not opt into the heavier DuckDB analytics backend.

The schema DDL and the row read/write plumbing are inherited unchanged
from :class:`~eawf.observability.telemetry.store.base.AbstractMetricsStore`; this module
supplies only the ``sqlite3`` connection and execution primitives.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from typing import Any

from eawf.observability.telemetry.store.base import AbstractMetricsStore

logger = logging.getLogger(__name__)


class SqliteMetricsStore(AbstractMetricsStore):
    """Metrics store backed by stdlib ``sqlite3``."""

    backend = "sqlite"
    _conn: sqlite3.Connection

    def _connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.execute("PRAGMA foreign_keys = ON")
        logger.debug(f"_connect backend='sqlite' path={str(self.path)!r}")

    def _placeholder(self) -> str:
        return "?"

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self._conn.execute(sql, tuple(params))

    def _query(self, sql: str) -> list[tuple[Any, ...]]:
        cursor = self._conn.execute(sql)
        return list(cursor.fetchall())

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
