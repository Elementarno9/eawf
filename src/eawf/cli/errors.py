"""Exit-code-mapped error helpers + ErrorEnvelope for CLI handlers.

The v0.3 CLI surface offers exactly five ``CliError`` buckets —
``UserError``, ``ValidationError``, ``StateConflict``,
``DaemonUnreachable``, ``InternalError`` — that map 1:1 onto the 0..5
exit-code surface. Per-cause specificity is preserved via
``ErrorEnvelope.data.kind`` per the disambiguation rule: callers raise
the bucket with a fine-grained cause tag in the ``kind=`` constructor
kwarg, e.g. ``UserError(msg, kind="NotFound")`` or
``StateConflict(msg, kind="LockConflict")``.

The eight deprecated subclass aliases (``NotFound``, ``InvalidInput``,
``ValidationFailed``, ``LockConflict``, ``InstrumentMissing``,
``UserDeclined``, ``IntegrityViolation``, ``HookBlocked``) were removed
in P27-I03-W18 once every callsite migrated to the bucket + ``kind=``
form. Domain-specific subclasses that remain (e.g.
:class:`eawf.install.wizard.WizardCancelled`,
:class:`eawf.profiles.compose.ProfileConflict`) still fold their concrete
class name into ``data.kind`` via :func:`build_envelope`.

Envelope shape (JSON branch) per :class:`ErrorEnvelope`:

.. code-block:: json

    {
      "schema_version": "1.0",
      "error": "<class name>",
      "message": "<str(err)>",
      "exit_code": 3,
      "exit_name": "STATE_CONFLICT",
      "suggested_next_step": "another writer / hook / ...",
      "data": {"kind": "LockConflict"},
      "correlation_id": null,
      "protocol_version": null,
      "timestamp": "2026-05-19T..."
    }
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final, Literal, NoReturn

import typer
from pydantic import BaseModel, ConfigDict, Field

from eawf.cli import exit_codes
from eawf.cli.error_codes import ErrorCode
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text

# --- Five-class CliError taxonomy ------------------------------------------


class CliError(Exception):
    """Base class for CLI-mapped errors.

    The default :attr:`exit_code` is :data:`exit_codes.INTERNAL_ERROR` —
    an uncaught raised path is an internal error. Each subclass
    overrides it with the canonical 0..5 code for its bucket.

    :attr:`kind` carries an optional fine-grained cause tag that is
    folded into ``ErrorEnvelope.data.kind`` when the caller does not
    supply one explicitly. The five canonical buckets coincide with the
    exit-code surface, so a tag like ``"LockConflict"`` or
    ``"RuntimeUnavailable"`` is the only way per-cause specificity
    survives a :class:`StateConflict` raise that is not a legacy
    subclass (e.g. an error built from a daemon JSON-RPC code).
    """

    exit_code: int = exit_codes.INTERNAL_ERROR

    def __init__(self, *args: object, kind: str | None = None) -> None:
        super().__init__(*args)
        self.kind = kind


class UserError(CliError):
    """Operator-fixable input / environment problem.

    Covers bad CLI args, schema mismatch on input, missing scope ids,
    missing external tools, user-declined gates, and protocol-version
    mismatches. Per-cause specificity lives in ``ErrorEnvelope.data.kind``.
    """

    exit_code = exit_codes.USER_ERROR


class ValidationError(CliError):
    """Schema or invariant rejection.

    The candidate state failed strict invariant validation. Operators
    inspect the violation list via ``ErrorEnvelope.data.violations``.
    """

    exit_code = exit_codes.VALIDATION_ERROR


class StateConflict(CliError):  # noqa: N818 — canonical bucket name
    """State-side conflict — lock, integrity, hook gate, or runtime ladder.

    Sibling writer holds a lock, hash mismatch detected, hook fail-closed,
    or the configured runtime ladder exhausted. Operators run
    ``eawf doctor`` to triage.
    """

    exit_code = exit_codes.STATE_CONFLICT


class DaemonUnreachable(CliError):  # noqa: N818 — canonical bucket name
    """Daemon process down, unresponsive, or shutting down.

    Connection refused, stale PID, or ``-32009 daemon shutting down``
    surfaced through the JSON-RPC layer. Operators run
    ``eawf daemon start`` then retry.
    """

    exit_code = exit_codes.DAEMON_UNREACHABLE


class InternalError(CliError):
    """Uncaught raised path — file an issue.

    Covers ``-32700 parse error``, ``-32603 internal error``,
    ``-32000 unauthorized`` (daemon-side bug — OS UDS auth is
    OS-enforced), and any other unexpected raise.
    """

    exit_code = exit_codes.INTERNAL_ERROR


class DaemonMutationIndeterminate(DaemonUnreachable):
    """Daemon connection lost mid-mutation; the write may or may not have applied.

    Raised by the daemon-proxy entry when the request was already sent to
    the daemon and then the connection dropped or the read timed out
    before a reply landed (the daemon may have applied the mutation, then
    died, before answering). Unlike a connect-phase failure — where the
    mutation provably never reached the daemon and the in-process
    fallback is safe — this state is **indeterminate**, so the proxy must
    NOT silently re-run the mutation: a non-idempotent kind (e.g.
    ``EVENT_APPEND``) would double-apply.

    Subclasses :class:`DaemonUnreachable` (exit code 4) because the cause
    is a lost daemon connection; the distinct class name + the
    ``DaemonMutationIndeterminate`` ``data.kind`` let operators and CI tell
    a clean "daemon down" (safe to retry) apart from "daemon dropped
    mid-write" (re-check state before retrying).
    """


# --- ErrorEnvelope ---------------------------------------------------------


def _utcnow() -> datetime:
    """Return current UTC datetime — module-level default factory.

    Lifted out of the :class:`ErrorEnvelope` field definition so the
    factory itself stays importable + mockable in tests.
    """
    return datetime.now(tz=UTC)


class ErrorEnvelope(BaseModel):
    """JSON-branch error envelope shape.

    Wired through :func:`emit_error` when ``flags.json_output`` is true.
    The plain-text branch renders the same fields as ``error: <message>``
    followed by hint / kind / data / correlation_id lines.

    The five canonical ``error`` subclass names are ``UserError``,
    ``ValidationError``, ``StateConflict``, ``DaemonUnreachable``, and
    ``InternalError``. Fine-grained cause tags (``NotFound``,
    ``LockConflict``, etc.) surface through ``data.kind`` for CI scripts
    that need fine-grained pivots without growing the exit-code surface.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"

    error: str
    """CliError subclass ``__name__`` — one of ``UserError``,
    ``ValidationError``, ``StateConflict``, ``DaemonUnreachable``,
    ``InternalError``."""

    message: str
    """User-facing ``str(err)`` body."""

    exit_code: int
    """:mod:`eawf.cli.exit_codes` value (0..5)."""

    exit_name: str
    """Canonical name from :func:`exit_codes.name_for`."""

    suggested_next_step: str | None = None
    """Operator-facing actionable hint — e.g.
    ``"run \\`eawf daemon start\\` then retry"``."""

    error_code: str | None = None
    """Cause-level :class:`~eawf.cli.error_codes.ErrorCode` value layered
    over the five exit buckets — e.g. ``"WAVE_DEPS_NOT_SATISFIED"``.

    Optional (default ``None``) so existing error sites that do not yet
    pass one keep rendering unchanged. When set, the text branch appends a
    ``See <code>`` line pointing at the matching anchor in
    ``docs/reference/error-codes.md`` and CI consumers pivot on the precise
    cause without the exit-code surface having to grow."""

    data: dict[str, Any] = Field(default_factory=dict)
    """Verb-specific structured context. Always JSON-safe. The ``kind``
    key carries the fine-grained cause tag
    (``"NotFound"`` / ``"InstrumentMissing"`` / ``"HookBlocked"`` / ...)
    so CI scripts can pivot on specific failure modes without growing
    the exit-code surface."""

    correlation_id: str | None = None
    """JSON-RPC request id when the error is daemon-mediated. Absent for
    daemonless errors. Lets operators join CLI errors to daemon log
    entries by id."""

    protocol_version: str | None = None
    """Daemon protocol version surfaced when
    ``data.kind == "ProtocolMismatch"``. Absent otherwise."""

    timestamp: datetime = Field(default_factory=_utcnow)
    """Envelope construction time (UTC)."""


# --- Subclass-to-hint mapping ----------------------------------------------

_DEFAULT_HINTS: dict[str, str] = {
    "UserError": "run `eawf <verb> --help` for option shapes; check ids and env",
    "ValidationError": "run `eawf validate` to inspect schema errors",
    "StateConflict": "another writer / hook / runtime conflict; run `eawf doctor` for diagnosis",
    "DaemonUnreachable": (
        "run `eawf daemon start` then retry; pass --daemonless for read-only verbs"
    ),
    "InternalError": (
        "file an issue with the error envelope; include `eawf daemon logs --lines 200`"
    ),
}

# Per-``data.kind`` refinement preserves per-cause specificity inside the
# five buckets.
_KIND_HINTS: dict[str, str] = {
    "NotFound": "check the scope id or run `eawf state resolve` to see resolved paths",
    "InvalidInput": "run `eawf <verb> --help` to see option shapes",
    "InstrumentMissing": "install the missing tool then retry; run `eawf doctor` for inventory",
    "UserDeclined": "re-run without --no-input to interact, or pass --yes when supported",
    "ProtocolMismatch": "upgrade with `uv tool upgrade eawf` then retry",
    "LockConflict": "another writer holds the lock; retry in a moment or run `eawf doctor`",
    "IntegrityViolation": "run `eawf doctor --repair` to inspect the integrity violation",
    "HookBlocked": "the hook printed its reason above; fix the underlying issue and retry",
    "RuntimeUnavailable": (
        "check runtime preference: `eawf runtime list` and `eawf config get runtime.preference`"
    ),
    "DaemonMutationIndeterminate": (
        "the mutation may or may not have applied; re-check state "
        "(`eawf state show`) before retrying to avoid a double-apply"
    ),
}


def _canonical_error_name(err: CliError) -> str:
    """Return the canonical (five-bucket) class name for *err*.

    Walks the MRO until it hits one of the five bucket classes. For a
    domain subclass like :class:`eawf.profiles.compose.ProfileConflict`,
    this returns ``"ValidationError"``. For a direct ``UserError`` raise,
    this returns ``"UserError"``. The concrete class name is preserved
    separately via ``data.kind``.
    """
    for cls in type(err).__mro__:
        name = cls.__name__
        if name in _DEFAULT_HINTS:
            return name
    return "CliError"


def _concrete_kind(err: CliError) -> str | None:
    """Return the concrete class name when *err* is a domain subclass.

    Returns ``None`` when *err* is one of the five bucket classes itself
    (so the bucket's threaded ``kind=`` tag wins instead). For a domain
    subclass such as :class:`eawf.install.wizard.WizardCancelled`, this
    returns its concrete name, which is what gets folded into ``data.kind``
    so CI scripts retain their fine-grained pivot.
    """
    cls_name = type(err).__name__
    if cls_name in _DEFAULT_HINTS:
        return None
    return cls_name


def _resolve_hint(canonical: str, kind: str | None) -> str:
    """Pick the most specific hint for *canonical* + *kind*.

    Args:
        canonical: One of the five bucket class names.
        kind: Fine-grained cause tag (concrete subclass name or
            operator-supplied ``kind=``) when present.

    Returns:
        The kind-specific hint when one exists; otherwise the bucket
        default hint.
    """
    if kind is not None and kind in _KIND_HINTS:
        return _KIND_HINTS[kind]
    return _DEFAULT_HINTS.get(canonical, _DEFAULT_HINTS["InternalError"])


def build_envelope(
    err: CliError,
    *,
    error_code: ErrorCode | None = None,
    correlation_id: str | None = None,
    protocol_version: str | None = None,
    data: dict[str, Any] | None = None,
) -> ErrorEnvelope:
    """Construct a typed :class:`ErrorEnvelope` for *err*.

    The envelope ``error`` field carries the canonical bucket name
    (``UserError`` / ``ValidationError`` / ``StateConflict`` /
    ``DaemonUnreachable`` / ``InternalError``). When *err* is a domain
    subclass, its concrete class name is preserved in ``data.kind`` per
    the disambiguation rule. An optional cause-level *error_code* layers
    the precise :class:`~eawf.cli.error_codes.ErrorCode` over the bucket
    without changing the exit code.

    Args:
        err: The :class:`CliError` (or domain subclass) instance.
        error_code: Optional cause-level
            :class:`~eawf.cli.error_codes.ErrorCode`. Stored as its string
            value so the text branch renders a ``See <code>`` line.
        correlation_id: JSON-RPC request id when the error is
            daemon-mediated.
        protocol_version: Daemon protocol version (only set on
            ``ProtocolMismatch``).
        data: Verb-specific structured context. When ``data`` lacks
            ``kind``, the concrete subclass name (if *err* is a domain
            subclass) or the error's threaded ``kind`` tag (if set, e.g.
            from :func:`cli_error_for_rpc`) is injected as ``data.kind``.

    Returns:
        A populated :class:`ErrorEnvelope`.
    """
    canonical = _canonical_error_name(err)
    concrete = _concrete_kind(err)
    merged_data: dict[str, Any] = dict(data) if data else {}
    threaded = concrete if concrete is not None else err.kind
    if threaded is not None and "kind" not in merged_data:
        merged_data["kind"] = threaded
    kind = merged_data.get("kind")
    return ErrorEnvelope(
        error=canonical,
        message=str(err),
        exit_code=err.exit_code,
        exit_name=exit_codes.name_for(err.exit_code),
        suggested_next_step=_resolve_hint(canonical, kind),
        error_code=error_code.value if error_code is not None else None,
        data=merged_data,
        correlation_id=correlation_id,
        protocol_version=protocol_version,
    )


def _render_text(env: ErrorEnvelope) -> str:
    """Render *env* as the plain-text branch body.

    The operator-facing lead follows the C10 error-UX order: cause
    (``error: <message>``) -> next_step (``hint: <suggested_next_step>``)
    -> ``See <error_code>`` when an :class:`~eawf.cli.error_codes.ErrorCode`
    is set. The ``See`` line is omitted when ``error_code`` is ``None`` so
    legacy error sites render unchanged.

    Skipped fields when empty: ``error_code`` (when ``None``), ``kind``
    (when absent), ``data`` (when only ``kind`` was present),
    ``correlation_id`` (when ``None``).
    """
    lines = [f"error: {env.message}"]
    if env.suggested_next_step is not None:
        lines.append(f"hint: {env.suggested_next_step}")
    if env.error_code is not None:
        lines.append(f"See {env.error_code}")
    lines.append(f"exit_code: {env.exit_code} ({env.exit_name})")
    kind = env.data.get("kind")
    if kind is not None:
        lines.append(f"kind: {kind}")
    leftover = {k: v for k, v in env.data.items() if k != "kind"}
    if leftover:
        lines.append(f"data: {leftover}")
    if env.correlation_id is not None:
        lines.append(f"correlation_id: {env.correlation_id}")
    return "\n".join(lines)


def emit_error(
    err: CliError,
    *,
    flags: GlobalFlags,
    error_code: ErrorCode | None = None,
    correlation_id: str | None = None,
    protocol_version: str | None = None,
    data: dict[str, Any] | None = None,
) -> NoReturn:
    """Print the canonical envelope for *err* and exit with its code.

    Args:
        err: The :class:`CliError` instance to surface. The class name
            and string body populate the envelope.
        flags: Resolved global flags. ``flags.json_output`` controls the
            text/JSON branch in :func:`eawf.cli.output.emit_json_or_text`.
        error_code: Optional cause-level
            :class:`~eawf.cli.error_codes.ErrorCode` layered over the
            bucket; renders a ``See <code>`` line in the text branch.
        correlation_id: Optional JSON-RPC request id for daemon-mediated
            errors.
        protocol_version: Optional daemon protocol version for
            ``ProtocolMismatch``.
        data: Optional verb-specific structured context merged into
            ``ErrorEnvelope.data``.

    Raises:
        typer.Exit: Always — with ``err.exit_code``.
    """
    env = build_envelope(
        err,
        error_code=error_code,
        correlation_id=correlation_id,
        protocol_version=protocol_version,
        data=data,
    )
    payload = env.model_dump(mode="json", exclude_none=False)
    emit_json_or_text(payload, _render_text(env), flags=flags)
    raise typer.Exit(err.exit_code)


# --- Daemon JSON-RPC error code mapping ------------------------------------
# Each daemon JSON-RPC error code folds onto one of the five CliError
# subclasses. The mapping is consulted by the daemon client when
# surfacing an RPC failure as a CLI exit.

#: Wire code the daemon emits for a validation_failed rejection (mirrors
#: ``eawf.daemon.methods.VALIDATION_FAILED``). Single-sourced here so the
#: client-side comparison sites do not drift from the code table below.
RPC_VALIDATION_FAILED: Final[int] = -32002

_DAEMON_RPC_MAP: dict[int, tuple[type[CliError], str | None]] = {
    -32700: (InternalError, None),
    -32600: (UserError, "InvalidInput"),
    -32601: (UserError, "InvalidInput"),
    -32602: (UserError, "InvalidInput"),
    -32603: (InternalError, None),
    -32000: (InternalError, None),
    -32001: (StateConflict, "LockConflict"),
    RPC_VALIDATION_FAILED: (ValidationError, None),
    -32003: (UserError, "NotFound"),
    -32004: (UserError, "ProtocolMismatch"),
    -32005: (StateConflict, "RuntimeUnavailable"),
    -32006: (StateConflict, "RuntimeUnavailable"),
    -32007: (InternalError, None),
    -32008: (InternalError, None),
    -32009: (DaemonUnreachable, None),
}


def cli_error_for_rpc(rpc_code: int, message: str) -> CliError:
    """Map a JSON-RPC error *rpc_code* onto the matching :class:`CliError`.

    Args:
        rpc_code: The JSON-RPC error code (e.g. ``-32001``).
        message: The RPC error message body — becomes ``str(err)``.

    Returns:
        A :class:`CliError` instance whose class matches the § 5.3 table
        bucket, carrying the table's fine-grained ``kind`` tag so per-
        cause specificity (``LockConflict`` / ``RuntimeUnavailable`` /
        ...) survives into the error envelope. Unknown codes fall back to
        :class:`InternalError`.
    """
    cls, kind = _DAEMON_RPC_MAP.get(rpc_code, (InternalError, None))
    return cls(message, kind=kind)
