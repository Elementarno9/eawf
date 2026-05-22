"""opencode_session source adapter — reader for the OpenCode SQLite store.

OpenCode persists session data in a single SQLite database
(``<data-home>/opencode/opencode.db``) managed by the drizzle ORM in WAL mode.
Unlike the JSONL adapters this one reads a relational store: per-session token /
turn data is reconstructed by joining ``session`` against ``part`` and decoding
each ``part.data`` JSON blob (``type == "step-finish"`` rows carry the per-step
``tokens`` object). This adapter folds each session into one
:class:`~eawf.telemetry.models.TelemetrySession` row (C09 §5.9.4).

Because the schema rides an external project's drizzle migrations it is not
stable — tables have already grown 13 → 15 since the spec was written
(C09 §6 F21). The adapter therefore checks the ``__drizzle_migrations``
fingerprint before projecting: on a **known** fingerprint it projects normally;
on an **unknown** fingerprint it records a
:class:`~eawf.telemetry.models.TelemetryIncident` with cause
:attr:`~eawf.state.enums.IncidentCause.EXTERNAL_API_FAILURE` and skips
projection (yields nothing for that database) rather than crashing on a column
that has moved. The projector drains :attr:`OpenCodeSessionSource.drift_incidents`
and upserts the recorded incidents.

The database is opened read-only (``file:<path>?mode=ro``) so eawf never takes a
write lock or risks corrupting OpenCode's WAL. The adapter implements the
:class:`~eawf.telemetry.sources.base.SessionSource` protocol over
:class:`TelemetrySession` rows.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eawf.state.enums import IncidentCause, IncidentSeverity
from eawf.telemetry.models import TelemetryIncident, TelemetrySession

logger = logging.getLogger(__name__)

_STEP_FINISH = "step-finish"

# Known-good drizzle schema fingerprints — the latest applied migration name in
# ``__drizzle_migrations``. drizzle migrations are append-only, so the highest-id
# row uniquely identifies the schema state the parser was written against. A db
# whose latest migration is absent from this set is treated as drifted.
_KNOWN_DRIZZLE_FINGERPRINTS: frozenset[str] = frozenset(
    {
        "20260428004200_add_session_path",
    }
)


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open *db_path* read-only with a :class:`sqlite3.Row` factory."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _epoch_ms_to_dt(raw: object) -> datetime | None:
    """Return a UTC datetime from an epoch-milliseconds value, or ``None``."""
    if not isinstance(raw, int) or isinstance(raw, bool):
        return None
    return datetime.fromtimestamp(raw / 1000, tz=UTC)


def _coerce_int(raw: object) -> int:
    """Return *raw* as an int, defaulting to ``0`` for absent / non-numeric values."""
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0


def _drizzle_fingerprint(conn: sqlite3.Connection) -> str | None:
    """Return the latest applied drizzle migration name, or ``None``.

    ``None`` is returned when the ``__drizzle_migrations`` table is missing or
    empty — both treated by the caller as drift (an OpenCode db must carry the
    drizzle metadata table).
    """
    try:
        row = conn.execute(
            "SELECT name FROM __drizzle_migrations ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    name = row["name"]
    return name if isinstance(name, str) and name else None


class OpenCodeSessionSource:
    """Reader for the OpenCode SQLite store.

    Implements the :class:`~eawf.telemetry.sources.base.SessionSource` protocol
    over :class:`~eawf.telemetry.models.TelemetrySession` rows. Schema-drift
    incidents recorded during projection accumulate in
    :attr:`drift_incidents` for the projector to drain.
    """

    source_name = "opencode"

    def __init__(self) -> None:
        self.drift_incidents: list[TelemetryIncident] = []

    def discover(self, root: Path) -> Iterator[Path]:
        """Yield the OpenCode database (``opencode.db``) under *root*.

        A missing root or absent database yields nothing (C09 §6 F2); the
        sibling ``-wal`` / ``-shm`` files are not yielded (they are not opened
        directly).
        """
        if not root.is_dir():
            return
        db_path = root / "opencode.db"
        if db_path.is_file():
            yield db_path

    def iter_rows(self, path: Path) -> Iterator[TelemetrySession]:
        """Project each OpenCode session at *path* into a :class:`TelemetrySession`.

        On an unknown drizzle-migration fingerprint the adapter records a
        :class:`TelemetryIncident` (cause
        :attr:`~eawf.state.enums.IncidentCause.EXTERNAL_API_FAILURE`) on
        :attr:`drift_incidents` and yields nothing (C09 §6 F21). A missing path
        also yields nothing. A :class:`sqlite3.OperationalError` from the
        relational ``session`` / ``part`` queries (a column or table that moved
        under a fingerprint the adapter still considers known) is likewise
        caught: a query-failure incident is recorded and projection stops
        rather than crashing the projector.
        """
        if not path.is_file():
            return
        conn = _open_readonly(path)
        try:
            fingerprint = _drizzle_fingerprint(conn)
            if fingerprint not in _KNOWN_DRIZZLE_FINGERPRINTS:
                self._record_drift(path, fingerprint)
                return
            try:
                yield from self._iter_sessions(conn, jsonl_path=path)
            except sqlite3.OperationalError as exc:
                self._record_query_failure(path, exc)
        finally:
            conn.close()

    def _record_drift(self, path: Path, fingerprint: str | None) -> None:
        """Append a schema-drift incident and log the skip."""
        observed = fingerprint if fingerprint is not None else "absent"
        incident = TelemetryIncident(
            incident_id=f"opencode-schema-drift:{observed}",
            severity=IncidentSeverity.MEDIUM,
            cause=IncidentCause.EXTERNAL_API_FAILURE,
            ts=datetime.now(tz=UTC),
            summary=f"opencode schema drift; latest_migration={observed!r}; projection skipped",
        )
        self.drift_incidents.append(incident)
        logger.warning(
            f"iter_rows source={self.source_name} path={str(path)!r} "
            f"fingerprint={observed!r} skipped projection on unknown drizzle fingerprint"
        )

    def _record_query_failure(self, path: Path, exc: sqlite3.OperationalError) -> None:
        """Append a query-failure incident and log the skip.

        Reached when the drizzle fingerprint is known but a relational query
        against ``session`` / ``part`` still fails — a column or table the
        parser expects has moved within a fingerprint the adapter has not yet
        retired. Treated as schema drift so the projector skips gracefully.
        """
        incident = TelemetryIncident(
            incident_id="opencode-query-failure",
            severity=IncidentSeverity.MEDIUM,
            cause=IncidentCause.EXTERNAL_API_FAILURE,
            ts=datetime.now(tz=UTC),
            summary=f"opencode relational query failed; projection skipped; error={exc}",
        )
        self.drift_incidents.append(incident)
        logger.warning(
            f"iter_rows source={self.source_name} path={str(path)!r} "
            f"error={exc!r} skipped projection on relational query failure"
        )

    def _iter_sessions(
        self, conn: sqlite3.Connection, *, jsonl_path: Path
    ) -> Iterator[TelemetrySession]:
        """Yield one :class:`TelemetrySession` per row in the ``session`` table."""
        sessions = conn.execute(
            "SELECT id, time_created, time_updated FROM session ORDER BY id"
        ).fetchall()
        for session in sessions:
            session_id = session["id"]
            if not isinstance(session_id, str) or not session_id:
                continue
            yield self._session_row(conn, session, jsonl_path=jsonl_path)

    def _session_row(
        self, conn: sqlite3.Connection, session: sqlite3.Row, *, jsonl_path: Path
    ) -> TelemetrySession:
        """Aggregate one session's ``part`` rows into a :class:`TelemetrySession`."""
        session_id = session["id"]
        started_at = _epoch_ms_to_dt(session["time_created"])
        ended_at = _epoch_ms_to_dt(session["time_updated"])
        duration_ms: int | None = None
        if started_at is not None and ended_at is not None:
            duration_ms = int((ended_at - started_at).total_seconds() * 1000)

        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cache_write = 0
        turn_count = 0
        parts = conn.execute(
            "SELECT data FROM part WHERE session_id = ? ORDER BY time_created, id",
            (session_id,),
        )
        for part in parts:
            data = _decode_part(part["data"])
            if data is None or data.get("type") != _STEP_FINISH:
                continue
            tokens = data.get("tokens")
            if not isinstance(tokens, dict):
                continue
            turn_count += 1
            total_input += _coerce_int(tokens.get("input"))
            total_output += _coerce_int(tokens.get("output"))
            cache = tokens.get("cache")
            if isinstance(cache, dict):
                total_cache_read += _coerce_int(cache.get("read"))
                total_cache_write += _coerce_int(cache.get("write"))

        return TelemetrySession(
            session_id=session_id,
            project_id="",
            runtime="opencode",
            wave_id=None,
            attempt_id=None,
            session_log_path=str(jsonl_path),
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            model_primary="opencode",
            total_input_tokens=total_input,
            total_output_tokens=total_output,
            total_cache_read=total_cache_read,
            total_cache_write=total_cache_write,
            turn_count=turn_count,
            end_marker="other",
        )


def _decode_part(raw: object) -> dict[str, Any] | None:
    """Decode a ``part.data`` JSON blob into a dict, or ``None`` when malformed."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


__all__ = ["OpenCodeSessionSource"]
