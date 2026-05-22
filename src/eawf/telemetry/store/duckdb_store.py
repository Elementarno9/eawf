"""DuckDB metrics-store backend — opt-in analytics backend.

``duckdb`` is an **optional** dependency: it is not declared as a hard
runtime requirement and is never imported at module top level. Every method
that needs the driver imports it lazily inside the function body, so:

* importing this module on a host without ``duckdb`` installed never fails;
* :func:`~eawf.telemetry.store.base.open_store` can ``try`` the lazy
  ``from ... import DuckDbMetricsStore`` and fall back to SQLite when the
  driver is absent (the ``ImportError`` surfaces from ``_connect``, but the
  factory pre-checks importability before constructing).

The schema DDL and row read/write plumbing are inherited unchanged from
:class:`~eawf.telemetry.store.base.AbstractMetricsStore`; this module
supplies only the DuckDB connection and execution primitives. DuckDB
accepts the ``?`` parameter placeholder and the ``INSERT OR REPLACE``
upsert form the base emits, so no SQL-dialect overrides are needed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from eawf.telemetry.store.base import AbstractMetricsStore

logger = logging.getLogger(__name__)


class DuckDbMetricsStore(AbstractMetricsStore):
    """Metrics store backed by the optional ``duckdb`` driver.

    The driver is imported lazily inside :meth:`_connect`; constructing the
    store without ``duckdb`` installed raises :class:`ImportError`, which
    :func:`~eawf.telemetry.store.base.open_store` guards against by
    pre-checking importability before it constructs this class.
    """

    backend = "duckdb"
    _conn: Any

    def _connect(self) -> None:
        import duckdb  # type: ignore[import-not-found]

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(str(self.path))
        logger.debug(f"_connect backend='duckdb' path={str(self.path)!r}")

    def _placeholder(self) -> str:
        return "?"

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self._conn.execute(sql, list(params))

    def _query(self, sql: str) -> list[tuple[Any, ...]]:
        return list(self._conn.execute(sql).fetchall())

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
