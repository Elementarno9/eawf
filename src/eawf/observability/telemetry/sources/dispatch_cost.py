"""dispatch_cost source adapter — EU-forward capture from the event store.

The ``DispatchCostSessionSource`` reads the canonical ``event.jsonl`` store
under a project root and projects every ``dispatch_cost`` event into a
:class:`~eawf.observability.telemetry.models.TelemetryDispatchCost` row. Those
rows are the *disconnect-EU* capture: the per-dispatch token + cost facts
recorded forward at dispatch time, which a later metering writer folds into
the unified ``actual_eu`` accessor independent of the per-runtime session
join (C09 §5.9.4).

Filtering is on the closed ``event_type`` discriminator
(``"dispatch_cost"``): the event store interleaves many event types on one
``event.jsonl``, so a line that parses to an :class:`Envelope` but is not a
``dispatch_cost`` event is silently skipped (not a corruption — just a row
this adapter does not own). A line that fails JSON parsing or
:class:`Envelope` validation is skipped with a logged ``WARNING`` carrying
the file + 1-based line number, and the scan continues (C09 §6 F3). A wholly
missing store file is not an error.

Keying caveat (the W02 spike finding): the
:class:`~eawf.kernel.store.kinds.events.dispatch_cost.DispatchCostPayload`
carries **no** ``session_id`` field — its correlation keys are ``wave_id``
plus a per-dispatch ``attempt_id`` UUID that the daemon never writes back
into :attr:`eawf.kernel.state.models.SessionAttempt.session_id`. So the rows
this adapter yields do **not** map 1:1 onto ``Wave.sessions[*].session_id``;
the row keys on the source envelope id instead. See
:class:`~eawf.observability.telemetry.models.TelemetryDispatchCost` for the full
finding.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.observability.telemetry.models import RuntimeName, TelemetryDispatchCost

logger = logging.getLogger(__name__)

_DISPATCH_COST_EVENT_TYPE = "dispatch_cost"
_RUNTIME_NAMES: frozenset[str] = frozenset({"claude", "codex", "opencode"})


class DispatchCostSessionSource:
    """Reader projecting ``dispatch_cost`` events into EU-forward rows.

    Implements the :class:`~eawf.observability.telemetry.sources.base.SessionSource`
    protocol over :class:`~eawf.observability.telemetry.models.TelemetryDispatchCost`
    rows. Like :class:`~eawf.observability.telemetry.sources.event_jsonl.EventJsonlSource`
    it is line-independent: every ``event.jsonl`` line projects to a
    complete, self-contained row.
    """

    source_name = "dispatch_cost"

    def discover(self, root: Path) -> Iterator[Path]:
        """Yield the canonical ``event.jsonl`` store under *root* if present.

        *root* is a project state path (the ``.ea/state.json`` file);
        :func:`~eawf.kernel.store.paths.store_path` derives the
        ``<state_dir>/store/event.jsonl`` location. Only an existing file is
        yielded — a never-emitted store is silently skipped (C09 §6 F2).
        """
        path = store_path(root, StoreKind.EVENT)
        if path.is_file():
            yield path

    def iter_rows(self, path: Path) -> Iterator[TelemetryDispatchCost]:
        """Yield a :class:`TelemetryDispatchCost` per ``dispatch_cost`` line.

        Lines whose ``event_type`` is not ``"dispatch_cost"`` are skipped
        silently (the event store interleaves many event types). Lines that
        fail JSON / :class:`Envelope` validation, or that carry a
        ``dispatch_cost`` payload missing a required field, are skipped with
        a logged ``WARNING`` (file + 1-based line number) and the scan
        continues (C09 §6 F3). A missing *path* yields nothing.
        """
        if not path.is_file():
            return
        with path.open(encoding="utf-8") as handle:
            for line_no, raw in enumerate(handle, start=1):
                line = raw.strip()
                if not line:
                    continue
                row = self._row_from_line(path, line_no, line)
                if row is not None:
                    yield row

    def _row_from_line(self, path: Path, line_no: int, line: str) -> TelemetryDispatchCost | None:
        """Parse one JSONL line into a row, or ``None`` to skip it."""
        try:
            envelope = Envelope.model_validate_json(line)
        except ValidationError as exc:
            logger.warning(
                f"iter_rows source={self.source_name} path={str(path)!r} "
                f"line={line_no} errors={exc.error_count()} skipped corrupt envelope"
            )
            return None
        if envelope.kind != StoreKind.EVENT:
            return None
        if envelope.payload.get("event_type") != _DISPATCH_COST_EVENT_TYPE:
            return None
        return self._row_from_envelope(path, line_no, envelope)

    def _row_from_envelope(
        self, path: Path, line_no: int, envelope: Envelope
    ) -> TelemetryDispatchCost | None:
        """Build a row from a ``dispatch_cost`` envelope, or ``None`` if malformed."""
        payload = envelope.payload
        runtime = _runtime_or_none(payload.get("runtime"))
        model = payload.get("model")
        pricing_version = payload.get("pricing_version")
        if runtime is None or not isinstance(model, str) or not isinstance(pricing_version, str):
            logger.warning(
                f"_row_from_envelope source={self.source_name} path={str(path)!r} "
                f"line={line_no} envelope={envelope.id!r} runtime={payload.get('runtime')!r} "
                f"malformed dispatch_cost; row skipped"
            )
            return None
        return TelemetryDispatchCost(
            envelope_id=envelope.id,
            wave_id=_str_or_none(payload.get("wave_id")),
            attempt_id=_str_or_none(payload.get("attempt_id")),
            runtime=runtime,
            model=model,
            input_tokens=_int_or_zero(payload.get("input_tokens")),
            output_tokens=_int_or_zero(payload.get("output_tokens")),
            cache_creation_input_tokens=_int_or_zero(payload.get("cache_creation_input_tokens")),
            cache_read_input_tokens=_int_or_zero(payload.get("cache_read_input_tokens")),
            cost_usd=_decimal_or_zero(payload.get("cost_usd")),
            pricing_version=pricing_version,
            ts=envelope.created_at,
        )


def _runtime_or_none(raw: Any) -> RuntimeName | None:
    """Return *raw* coerced to a closed runtime name, else ``None``."""
    if isinstance(raw, str) and raw in _RUNTIME_NAMES:
        # The membership check above narrows the value to the closed
        # RuntimeName literal set; the cast keeps mypy aligned with that.
        return raw  # type: ignore[return-value]
    return None


def _str_or_none(raw: Any) -> str | None:
    """Return *raw* when it is a non-empty string, else ``None``."""
    return raw if isinstance(raw, str) and raw else None


def _int_or_zero(raw: Any) -> int:
    """Return *raw* as an ``int`` when it is a non-bool integer, else ``0``."""
    if isinstance(raw, bool):
        return 0
    if isinstance(raw, int):
        return raw
    return 0


def _decimal_or_zero(raw: Any) -> Decimal:
    """Return *raw* coerced to a :class:`Decimal`, else ``Decimal("0")``.

    ``int`` / ``str`` values are coerced exactly; ``float`` is routed through
    ``str`` so binary-floating-point error never enters the Decimal cost
    field. An uncoercible value falls back to zero.
    """
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, bool):
        return Decimal("0")
    if isinstance(raw, (int, str)):
        try:
            return Decimal(raw)
        except InvalidOperation, ValueError:
            return Decimal("0")
    if isinstance(raw, float):
        return Decimal(str(raw))
    return Decimal("0")


__all__ = ["DispatchCostSessionSource"]
