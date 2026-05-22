"""Metrics-store contract + DDL generated from the Pydantic row models.

The store backends (SQLite default, DuckDB opt-in) share one contract and
one DDL generator so the SQL schema cannot drift from the typed row shape:

* :class:`TableSpec` pins, per table, the row model + primary key + the
  columns that need ``NOT NULL`` or a foreign-key reference. Everything
  else (column names, SQL types) is derived from the model's fields.
* :data:`TABLES` is the ordered registry of every telemetry table. Order
  matters: parent tables precede the tables that reference them so the FK
  references resolve at ``CREATE TABLE`` time.
* :func:`column_sql_type` maps a Pydantic field annotation onto a SQL
  type. The mapping is intentionally small (KISS): the store is a cache,
  not a constraint engine.
* :func:`render_ddl` walks :data:`TABLES` and emits ``CREATE TABLE IF NOT
  EXISTS`` statements — so :meth:`AbstractMetricsStore.init_schema` is
  idempotent (a second call no-ops).
* :class:`AbstractMetricsStore` is the shared base both backends subclass;
  it owns the model-driven row read/write plumbing and leaves only the
  connection + SQL-dialect details to the concrete backends.
* :func:`open_store` is the factory; it auto-falls-back to SQLite when
  ``duckdb`` is requested but not importable.
"""

from __future__ import annotations

import logging
import types
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Final, Literal, get_args, get_origin

from pydantic import BaseModel

from eawf.telemetry.models import (
    TelemetryCompaction,
    TelemetryFileMeta,
    TelemetryIncident,
    TelemetryProject,
    TelemetryRuntimeSwitch,
    TelemetrySchemaMeta,
    TelemetrySession,
    TelemetryToolCall,
    TelemetryTurn,
)

logger = logging.getLogger(__name__)

#: Pinned projection schema version (C09 §5.9.3). Stamped into
#: ``telemetry_schema_meta`` by :meth:`AbstractMetricsStore.init_schema`.
SCHEMA_VERSION: Final[str] = "1"

StoreBackend = Literal["sqlite", "duckdb"]
"""Closed set of metrics-store backend identifiers."""


@dataclass(frozen=True, slots=True)
class TableSpec:
    """Per-table DDL inputs that are not derivable from the row model.

    Attributes:
        name: SQL table name.
        model: Pydantic v2 row model whose fields define the columns.
        primary_key: Column names forming the primary key (1 = inline
            ``PRIMARY KEY``; >1 = a trailing composite ``PRIMARY KEY``
            clause).
        not_null: Column names that take a ``NOT NULL`` constraint in
            addition to the type. Primary-key columns are implicitly
            ``NOT NULL`` and need not be listed.
    """

    name: str
    model: type[BaseModel]
    primary_key: tuple[str, ...]
    not_null: frozenset[str] = frozenset()


#: Ordered registry of every telemetry table. Parents precede children so
#: ``CREATE TABLE`` foreign-key references resolve in order. The column set
#: and SQL types are derived from each ``model``'s Pydantic fields; only the
#: keys / not-null constraints live here.
TABLES: Final[tuple[TableSpec, ...]] = (
    TableSpec(
        name="telemetry_projects",
        model=TelemetryProject,
        primary_key=("project_id",),
        not_null=frozenset({"cwd"}),
    ),
    TableSpec(
        name="telemetry_sessions",
        model=TelemetrySession,
        primary_key=("session_id",),
        not_null=frozenset({"runtime", "session_log_path"}),
    ),
    TableSpec(
        name="telemetry_turns",
        model=TelemetryTurn,
        primary_key=("session_id", "turn_idx"),
    ),
    TableSpec(
        name="telemetry_tool_calls",
        model=TelemetryToolCall,
        primary_key=("tool_use_id",),
    ),
    TableSpec(
        name="telemetry_compactions",
        model=TelemetryCompaction,
        primary_key=("session_id", "ts"),
    ),
    TableSpec(
        name="telemetry_runtime_switches",
        model=TelemetryRuntimeSwitch,
        primary_key=("wave_id", "attempt_id_from", "attempt_id_to"),
    ),
    TableSpec(
        name="telemetry_incidents",
        model=TelemetryIncident,
        primary_key=("incident_id",),
    ),
    TableSpec(
        name="telemetry_file_meta",
        model=TelemetryFileMeta,
        primary_key=("jsonl_path",),
        not_null=frozenset({"mtime", "size", "last_offset", "last_scan_ts"}),
    ),
    TableSpec(
        name="telemetry_schema_meta",
        model=TelemetrySchemaMeta,
        primary_key=("key",),
    ),
)


def _unwrap_optional(annotation: Any) -> Any:
    """Strip a ``T | None`` / ``Optional[T]`` wrapper down to ``T``.

    Returns the annotation unchanged when it is not an optional union.
    """

    origin = get_origin(annotation)
    if origin is types.UnionType:
        non_none = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation


def column_sql_type(annotation: Any) -> str:
    """Map a Pydantic field annotation onto a SQL column type.

    The mapping is deliberately small: the store is a typed cache, not a
    constraint engine. ``Decimal`` lands as ``TEXT`` so the exact decimal
    string round-trips without binary-float drift across both backends;
    enums and ``Literal`` string sets land as ``TEXT``.

    Args:
        annotation: The (possibly optional) field annotation to map.

    Returns:
        The SQL type keyword for the column.
    """

    inner = _unwrap_optional(annotation)

    if inner is bool:
        return "INTEGER"
    if inner is int:
        return "INTEGER"
    if inner is float:
        return "REAL"
    if inner is Decimal:
        return "TEXT"
    if inner is datetime:
        return "TEXT"
    if inner is str:
        return "TEXT"
    if isinstance(inner, type) and issubclass(inner, Enum):
        return "TEXT"
    if get_origin(inner) is Literal:
        return "TEXT"
    return "TEXT"


def column_names(model: type[BaseModel]) -> tuple[str, ...]:
    """Return the model's field names in declaration order."""

    return tuple(model.model_fields)


def render_create_table(spec: TableSpec) -> str:
    """Render the ``CREATE TABLE IF NOT EXISTS`` statement for ``spec``.

    Columns and types are derived from ``spec.model``'s Pydantic fields;
    the primary key and not-null constraints come from ``spec``. A
    single-column primary key is declared inline; a composite key is
    declared as a trailing ``PRIMARY KEY (...)`` clause.
    """

    single_pk = spec.primary_key[0] if len(spec.primary_key) == 1 else None
    lines: list[str] = []
    for field_name, field in spec.model.model_fields.items():
        sql_type = column_sql_type(field.annotation)
        parts = [field_name, sql_type]
        if field_name == single_pk:
            parts.append("PRIMARY KEY")
        elif field_name in spec.not_null:
            parts.append("NOT NULL")
        lines.append(" ".join(parts))
    if single_pk is None:
        pk_cols = ", ".join(spec.primary_key)
        lines.append(f"PRIMARY KEY ({pk_cols})")
    body = ",\n    ".join(lines)
    return f"CREATE TABLE IF NOT EXISTS {spec.name} (\n    {body}\n)"


def render_ddl() -> tuple[str, ...]:
    """Render the ordered DDL statements for every telemetry table."""

    return tuple(render_create_table(spec) for spec in TABLES)


def encode_value(value: Any) -> Any:
    """Encode a row field value into a backend-portable scalar.

    ``bool`` becomes ``0``/``1``, ``Decimal`` and ``datetime`` become their
    string forms, and :class:`~enum.Enum` members become their ``.value``.
    Everything else passes through unchanged.
    """

    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


class AbstractMetricsStore(ABC):
    """Shared metrics-store contract + model-driven row plumbing.

    Both backends subclass this base. The base owns everything that is
    backend-independent — DDL generation (:meth:`init_schema`), the
    model-driven ``upsert`` / ``fetch_all`` plumbing, and schema-meta
    stamping. Concrete backends supply only the connection, the SQL
    parameter placeholder, and the row-execution primitives.

    ``init_schema`` is idempotent: it emits ``CREATE TABLE IF NOT EXISTS``
    DDL generated from the Pydantic row models, so a second call no-ops.
    """

    backend: StoreBackend

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._connect()

    # -- backend primitives --------------------------------------------

    @abstractmethod
    def _connect(self) -> None:
        """Open the backend connection, binding it to ``self.path``."""

    @abstractmethod
    def _placeholder(self) -> str:
        """Return the SQL parameter placeholder (``?`` for both backends)."""

    @abstractmethod
    def _execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Execute a single statement with bound ``params``."""

    @abstractmethod
    def _query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run a read query and return all rows as tuples."""

    @abstractmethod
    def commit(self) -> None:
        """Flush pending writes to durable storage."""

    @abstractmethod
    def close(self) -> None:
        """Close the backend connection."""

    # -- schema --------------------------------------------------------

    def init_schema(self) -> None:
        """Create every telemetry table if absent and stamp the version.

        Idempotent: the DDL uses ``CREATE TABLE IF NOT EXISTS`` (generated
        from the row models via :func:`render_ddl`), and the schema-version
        stamp is an upsert, so a second call mutates nothing.
        """

        for statement in render_ddl():
            self._execute(statement)
        self.upsert(
            "telemetry_schema_meta",
            TelemetrySchemaMeta(key="telemetry_schema_version", value=SCHEMA_VERSION),
        )
        self.commit()

    # -- row plumbing --------------------------------------------------

    def upsert(self, table: str, row: BaseModel) -> None:
        """Insert-or-replace ``row`` into ``table`` keyed on its PK.

        Args:
            table: Target table name (must appear in :data:`TABLES`).
            row: A populated row model whose type matches the table.
        """

        cols = column_names(type(row))
        placeholder = self._placeholder()
        col_list = ", ".join(cols)
        value_list = ", ".join(placeholder for _ in cols)
        values = [encode_value(getattr(row, col)) for col in cols]
        sql = f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({value_list})"
        self._execute(sql, values)

    def bulk_upsert(self, table: str, rows: Iterable[BaseModel]) -> None:
        """Upsert each row in ``rows`` into ``table`` (one statement each)."""

        for row in rows:
            self.upsert(table, row)

    def fetch_all(self, table: str, model: type[BaseModel]) -> list[BaseModel]:
        """Read every row of ``table`` back into ``model`` instances.

        The select uses the model's declared column order so the row tuples
        align positionally with the field names; Pydantic re-validates each
        row (coercing the stored scalar back to the typed field).
        """

        cols = column_names(model)
        col_list = ", ".join(cols)
        sql = f"SELECT {col_list} FROM {table}"
        out: list[BaseModel] = []
        for record in self._query(sql):
            data = dict(zip(cols, record, strict=True))
            out.append(model.model_validate(data))
        return out


def metrics_db_path(state_path: Path) -> Path:
    """Return the telemetry metrics DB path for a project state path.

    The metrics cache lives at ``<state_dir>/telemetry.db`` — a sibling of
    ``state.json`` and the ``store/`` JSONL directory. The file may not
    exist yet (a project that has never run a projection has no cache).

    Args:
        state_path: The project's ``.ea/state.json`` path.

    Returns:
        The metrics-store database path (not guaranteed to exist).
    """
    return Path(state_path).parent / "telemetry.db"


def open_store(
    db_kind: StoreBackend,
    path: Path | str,
) -> AbstractMetricsStore:
    """Open a metrics store of the requested backend, with SQLite fallback.

    When ``db_kind == "duckdb"`` but the optional ``duckdb`` package is not
    importable, a warning is logged and a
    :class:`~eawf.telemetry.store.sqlite_store.SqliteMetricsStore` is
    returned instead — so the absence of the optional dependency never
    fails the caller.

    Args:
        db_kind: ``"sqlite"`` or ``"duckdb"``.
        path: Filesystem path for the backing database file.

    Returns:
        An opened, but not yet schema-initialised, store. Call
        :meth:`AbstractMetricsStore.init_schema` before writing rows.

    Raises:
        ValueError: ``db_kind`` is not a recognised backend.
    """

    from eawf.telemetry.store.sqlite_store import SqliteMetricsStore

    if db_kind == "sqlite":
        return SqliteMetricsStore(path)
    if db_kind == "duckdb":
        try:
            import duckdb  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            logger.warning(
                f"open_store db_kind='duckdb' fallback='sqlite' "
                f"reason='duckdb_not_importable' path={str(path)!r}"
            )
            return SqliteMetricsStore(path)
        from eawf.telemetry.store.duckdb_store import DuckDbMetricsStore

        return DuckDbMetricsStore(path)
    raise ValueError(f"unknown store backend: {db_kind!r}")
