"""DuckDB metrics-store backend — opt-in analytics backend.

``duckdb`` is an **optional** dependency: it is not declared as a hard
runtime requirement and is never imported at module top level. Every method
that needs the driver imports it lazily inside the function body, so:

* importing this module on a host without ``duckdb`` installed never fails;
* :func:`~eawf.observability.telemetry.store.base.open_store` can ``try`` the lazy
  ``from ... import DuckDbMetricsStore`` and fall back to SQLite when the
  driver is absent (the ``ImportError`` surfaces from ``_connect``, but the
  factory pre-checks importability before constructing).

The schema DDL and most row read/write plumbing are inherited from
:class:`~eawf.observability.telemetry.store.base.AbstractMetricsStore`; this module
supplies the DuckDB connection + execution primitives and one
SQL-dialect override. DuckDB accepts the ``?`` parameter placeholder but
not the SQLite-only ``INSERT OR REPLACE`` upsert the base emits, so
:meth:`DuckDbMetricsStore.upsert` re-emits the row write as a portable
``INSERT ... ON CONFLICT DO UPDATE`` statement.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from eawf.observability.telemetry.store.base import (
    TABLES,
    AbstractMetricsStore,
    column_names,
    encode_value,
)

logger = logging.getLogger(__name__)

# Per-table primary-key columns, sourced from the shared table registry so
# the DuckDB upsert's conflict target stays in lockstep with the DDL. The
# key columns are excluded from the ``DO UPDATE SET`` clause — they are the
# matched-on conflict target, not a value to overwrite.
_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {spec.name: spec.primary_key for spec in TABLES}


class DuckDbMetricsStore(AbstractMetricsStore):
    """Metrics store backed by the optional ``duckdb`` driver.

    The driver is imported lazily inside :meth:`_connect`; constructing the
    store without ``duckdb`` installed raises :class:`ImportError`, which
    :func:`~eawf.observability.telemetry.store.base.open_store` guards against by
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

    def upsert(self, table: str, row: BaseModel) -> None:
        """Insert-or-replace ``row`` into ``table`` via DuckDB upsert.

        Overrides :meth:`AbstractMetricsStore.upsert` because DuckDB does
        not accept the SQLite-only ``INSERT OR REPLACE`` form the base
        emits. The portable equivalent is ``INSERT ... ON CONFLICT DO
        UPDATE SET col = excluded.col`` over every non-primary-key column,
        with the table's primary key as the inferred conflict target.

        Args:
            table: Target table name (must appear in :data:`TABLES`).
            row: A populated row model whose type matches the table.
        """
        cols = column_names(type(row))
        placeholder = self._placeholder()
        col_list = ", ".join(cols)
        value_list = ", ".join(placeholder for _ in cols)
        values = [encode_value(getattr(row, col)) for col in cols]
        pk = _PRIMARY_KEYS[table]
        update_cols = [col for col in cols if col not in pk]
        if update_cols:
            set_clause = ", ".join(f"{col} = excluded.{col}" for col in update_cols)
            conflict_action = f"DO UPDATE SET {set_clause}"
        else:
            # Every column is part of the primary key, so there is nothing to
            # update on conflict — an empty ``DO UPDATE SET`` is invalid SQL.
            conflict_action = "DO NOTHING"
        sql = (
            f"INSERT INTO {table} ({col_list}) VALUES ({value_list}) "
            f"ON CONFLICT ({', '.join(pk)}) {conflict_action}"
        )
        self._execute(sql, values)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
