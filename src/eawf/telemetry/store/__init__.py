"""Metrics store subpackage — the typed projection sink (C09 §5.9.3).

The store is the durable cache the telemetry projector writes rows into and
the ``eawf metrics`` surface reads from. Two backends implement the single
:class:`~eawf.telemetry.store.base.AbstractMetricsStore` contract:

- :class:`~eawf.telemetry.store.sqlite_store.SqliteMetricsStore` — the
  always-available default, built on the stdlib ``sqlite3`` module.
- :class:`~eawf.telemetry.store.duckdb_store.DuckDbMetricsStore` — the
  opt-in analytics backend. ``duckdb`` is an optional dependency; the
  module imports it lazily so importing this subpackage never requires it.

:func:`open_store` is the factory: it selects the requested backend and
auto-falls-back to SQLite when ``duckdb`` is requested but not importable.

The DDL emitted by ``init_schema`` is generated from the Pydantic v2 row
models in :mod:`eawf.telemetry.models` (see
:data:`~eawf.telemetry.store.base.TABLES`), so the schema cannot drift from
the row shape.
"""

from __future__ import annotations

from eawf.telemetry.store.base import (
    TABLES,
    AbstractMetricsStore,
    StoreBackend,
    TableSpec,
    metrics_db_path,
    open_store,
)
from eawf.telemetry.store.sqlite_store import SqliteMetricsStore

__all__ = [
    "TABLES",
    "AbstractMetricsStore",
    "SqliteMetricsStore",
    "StoreBackend",
    "TableSpec",
    "metrics_db_path",
    "open_store",
]
