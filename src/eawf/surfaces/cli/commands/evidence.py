"""Evidence-area Typer apps: shared helpers + app registry (W02 deliverable).

This module owns nine evidence Typer apps:

* eight legacy noun-apps (``goal_app`` / ``outcome_app`` /
  ``hypothesis_app`` / ``audit_app`` / ``incident_app`` /
  ``decision_app`` / ``artifact_app`` / ``backlog_app``) — these mount
  at the root via :mod:`eawf.surfaces.cli.registry` and own legacy
  state-evidence verbs (define / add / verdict / list / promote).

* one new top-level ``evidence_app`` (P28-I01-W04) that mounts the
  v0.4 verify-spine attestation surface — ``eawf evidence attest``
  proxies a typed :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord`
  through the daemon ``evidence.append`` RPC and (under the
  ``EAWF_EVIDENCE_DIRECT_WRITE=1`` recovery gate) falls back to a
  direct ``evidence.jsonl`` append. The verb is the operator-facing
  attestation entry point; deterministic + jury appends will land
  through library entry points in later waves (W06 / W08 / W11).

The concrete command bodies for the eight legacy apps live in four
sibling modules:

- :mod:`eawf.surfaces.cli.commands.evidence_hypothesis` — goal / outcome /
  hypothesis.
- :mod:`eawf.surfaces.cli.commands.evidence_backlog` — audit / backlog.
- :mod:`eawf.surfaces.cli.commands.evidence_incident` — incident / decision.
- :mod:`eawf.surfaces.cli.commands.evidence_artifact` — artifact (add / update /
  show / validate / verify).

Each sibling imports the apps and shared helpers from this module and
attaches its handlers via ``@<app>.command(...)``. Importing this module
imports the siblings (at the bottom, after every shared symbol is
defined), so the decorators run and the apps carry their full verb set.
Existing import sites (``app.py``) keep resolving
``from eawf.surfaces.cli.commands.evidence import hypothesis_app`` and the seven
other apps unchanged.

Every state-mutating handler runs inside
:func:`eawf.surfaces.cli._mutation.state_transaction`, which holds
``portalock(state.json)`` across the load + mutate + validate + write
cycle. Library mutators (``define_*`` / ``add_*`` / ``set_*`` /
``verdict_*`` / ``close_*``) take the typed :class:`State` and mutate
it in place, returning the JSONL envelope(s) for the handler to append
after the transaction body completes.

The new ``evidence attest`` verb is non-state: it writes only to
``<state_dir>/store/evidence.jsonl`` (daemon-owned), so it bypasses
the state transaction entirely.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.draft import install_promote_command
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

if TYPE_CHECKING:
    # Heavy imports kept out of the CLI tree-build path per the import-
    # budget gate (tests/perf/cli/test_import_budget.py). Annotation-only
    # references resolve via ``from __future__ import annotations`` (PEP
    # 563) without executing the import at module load. Runtime call
    # sites import lazily inside the command/helper bodies below.
    from eawf.kernel.store.kinds.evidence import EvidenceRecord

logger = logging.getLogger(__name__)


#: Environment-variable gate for the direct-JSONL fallback path. When
#: unset (the default) ``eawf evidence attest`` proxies through the
#: daemon ``evidence.append`` RPC, satisfying AGENTS rule 4
#: (daemon is the canonical writer). Setting ``EAWF_EVIDENCE_DIRECT_WRITE=1``
#: opts into the per-file portalock append used by CI / recovery shell
#: when the daemon is intentionally unavailable.
EVIDENCE_DIRECT_WRITE_ENV: str = "EAWF_EVIDENCE_DIRECT_WRITE"


# ---- Shared helpers --------------------------------------------------------


def _flags(ctx: typer.Context) -> GlobalFlags:
    """Return the resolved :class:`GlobalFlags` from the Typer context."""
    flags = ctx.obj
    if not isinstance(flags, GlobalFlags):
        flags = GlobalFlags()
    return flags


def _state_path(flags: GlobalFlags) -> Path:
    """Resolve the state path or raise :class:`UserError` (``kind="NotFound"``)."""
    try:
        return resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        raise cli_errors.UserError(str(exc), kind="NotFound") from exc


def _emit(payload: dict[str, Any], text: str, flags: GlobalFlags) -> None:
    emit_json_or_text(payload, text, flags=flags)


def _run_read(
    flags: GlobalFlags,
    fn: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run a read-only *fn* and translate :class:`CliError` into an envelope."""
    try:
        return fn(*args, **kwargs)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)


# ---- Typer apps ------------------------------------------------------------

goal_app = typer.Typer(
    name="goal",
    help="Manage project goals (define).",
    no_args_is_help=True,
)

outcome_app = typer.Typer(
    name="outcome",
    help="Manage outcomes (define / set).",
    no_args_is_help=True,
)

hypothesis_app = typer.Typer(
    name="hypothesis",
    help="Manage hypotheses (define / verdict / list).",
    no_args_is_help=True,
)

audit_app = typer.Typer(
    name="audit",
    help="Manage audits (add / run / integrity / show / list).",
    no_args_is_help=True,
)

incident_app = typer.Typer(
    name="incident",
    help="Manage incidents (open / close / view).",
    no_args_is_help=True,
)

decision_app = typer.Typer(
    name="decision",
    help="Manage decisions (add / supersede / list / graph).",
    no_args_is_help=True,
)

artifact_app = typer.Typer(
    name="artifact",
    help="Manage artifacts (add / show / verify).",
    no_args_is_help=True,
)

backlog_app = typer.Typer(
    name="backlog",
    help="Manage backlog items (add / close).",
    no_args_is_help=True,
)

# Top-level ``eawf evidence`` group — owns the v0.4 verify-spine attestation
# verb (``attest``). Kept separate from the eight legacy noun-apps so the
# root-mounted ``goal`` / ``audit`` / ``hypothesis`` / etc. command tree
# stays unchanged.
evidence_app = typer.Typer(
    name="evidence",
    help="Attest verify-spine evidence (attest).",
    no_args_is_help=True,
)


# ---- evidence attest -------------------------------------------------------


def _direct_write_enabled() -> bool:
    """Return ``True`` when the direct-JSONL append fallback is opted in.

    Reads :data:`EVIDENCE_DIRECT_WRITE_ENV` from the environment; any
    value other than ``"1"`` is treated as off so the operator must
    affirmatively opt in.
    """
    return os.environ.get(EVIDENCE_DIRECT_WRITE_ENV) == "1"


def _build_record(
    *,
    scope_id: str,
    produced_by: str,
    evidence_kind: str,
    status: str,
    summary: str,
    refs: list[str],
    metrics_json: str | None,
) -> EvidenceRecord:
    """Validate CLI inputs and return a typed :class:`EvidenceRecord`.

    Args:
        scope_id: URN of the phase / iter / wave / decision the row backs.
        produced_by: Producer literal (``human`` / ``agent`` / ``tool`` /
            ``canary``).
        evidence_kind: Source-family literal (``deterministic`` / ``jury``
            / ``attested``).
        status: Outcome literal (``pass`` / ``fail`` / ``blocked`` /
            ``waived``).
        summary: One-line description (1-500 chars).
        refs: Typed reference ids (decision / audit / artifact).
        metrics_json: Optional JSON-encoded scalar map.

    Returns:
        Validated :class:`EvidenceRecord` with a freshly minted id and
        timezone-aware UTC ``created_at``.

    Raises:
        UserError: When ``metrics_json`` fails to parse, or when a typed
            field rejects its input (``kind="InvalidInput"``).
    """
    # Lazy imports — the heavy state-model graph stays off the CLI
    # tree-build path so the import-budget gate
    # (tests/perf/cli/test_import_budget.py) holds.
    from datetime import UTC, datetime

    import orjson

    from eawf.kernel.store.kinds.evidence import (
        EvidenceRecord as _EvidenceRecord,
    )
    from eawf.kernel.store.kinds.evidence import mint_evidence_id

    metrics: dict[str, int | float | str] | None = None
    if metrics_json is not None:
        try:
            parsed = orjson.loads(metrics_json)
        except orjson.JSONDecodeError as exc:
            raise cli_errors.UserError(
                f"invalid --metrics JSON: {exc}", kind="InvalidInput"
            ) from exc
        if not isinstance(parsed, dict):
            raise cli_errors.UserError("metrics must be a JSON object", kind="InvalidInput")
        metrics = parsed
    try:
        return _EvidenceRecord(
            id=mint_evidence_id(),
            scope_id=scope_id,
            produced_by=produced_by,  # type: ignore[arg-type]
            evidence_kind=evidence_kind,  # type: ignore[arg-type]
            status=status,  # type: ignore[arg-type]
            summary=summary,
            refs=list(refs),
            metrics=metrics,
            created_at=datetime.now(UTC),
        )
    except Exception as exc:  # pydantic.ValidationError + friends
        raise cli_errors.UserError(f"invalid evidence record: {exc}", kind="InvalidInput") from exc


def _append_direct(record: EvidenceRecord, *, state_path: Path) -> str:
    """Append *record* directly to ``<state_dir>/store/evidence.jsonl``.

    Used only when :data:`EVIDENCE_DIRECT_WRITE_ENV` is set; mirrors the
    daemon-side append shape so a parallel reader cannot tell a direct-
    write row from a daemon-written one.

    Args:
        record: Validated evidence record to append.
        state_path: Path to ``state.json`` (anchors the store dir).

    Returns:
        ISO-8601 timestamp of the local append (the JSONL row carries
        ``record.created_at``; this value is the wall-clock of the write).
    """
    from datetime import UTC, datetime

    from eawf.kernel.state.enums import StoreKind
    from eawf.kernel.store.append import append_envelope
    from eawf.kernel.store.envelope import Envelope
    from eawf.kernel.store.paths import store_path

    evidence_path = store_path(state_path, StoreKind.EVIDENCE)
    envelope = Envelope(
        id=record.id,
        kind=StoreKind.EVIDENCE,
        scope_id=record.scope_id,
        created_at=record.created_at,
        summary=record.summary,
        payload=record.model_dump(mode="json"),
    )
    append_envelope(evidence_path, envelope)
    return datetime.now(UTC).isoformat()


@evidence_app.command("attest")
def evidence_attest(
    ctx: typer.Context,
    scope_id: Annotated[
        str,
        typer.Option(
            "--scope-id",
            help="Scope URN this evidence backs (phase / iter / wave / decision id).",
        ),
    ],
    produced_by: Annotated[
        str,
        typer.Option(
            "--produced-by",
            help="Producer family: human | agent | tool | canary.",
        ),
    ],
    evidence_kind: Annotated[
        str,
        typer.Option(
            "--evidence-kind",
            help="Source family: deterministic | jury | attested.",
        ),
    ],
    status: Annotated[
        str,
        typer.Option(
            "--status",
            help="Outcome: pass | fail | blocked | waived.",
        ),
    ],
    summary: Annotated[
        str,
        typer.Option(
            "--summary",
            help="One-line evidence description (1-500 chars).",
        ),
    ],
    ref: Annotated[
        list[str] | None,
        typer.Option(
            "--ref",
            help="Typed reference id (decision / audit / artifact). Repeatable.",
        ),
    ] = None,
    metrics: Annotated[
        str | None,
        typer.Option(
            "--metrics",
            help="Optional JSON object of scalar metrics (e.g. '{\"coverage\":0.92}').",
        ),
    ] = None,
) -> None:
    """Append a typed verify-spine evidence row.

    Default path proxies through the daemon ``evidence.append`` RPC so
    the daemon-canonical-writer invariant (AGENTS rule 4) holds. Setting
    :data:`EVIDENCE_DIRECT_WRITE_ENV` ``=1`` opts into a direct
    ``evidence.jsonl`` append; without the env var a direct write is
    refused with a clear error pointing at the daemon RPC.
    """
    flags = _flags(ctx)
    state_path = _state_path(flags)
    refs = list(ref) if ref else []

    try:
        record = _build_record(
            scope_id=scope_id,
            produced_by=produced_by,
            evidence_kind=evidence_kind,
            status=status,
            summary=summary,
            refs=refs,
            metrics_json=metrics,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    appended_at: str
    via_direct = False
    if _direct_write_enabled():
        try:
            appended_at = _append_direct(record, state_path=state_path)
        except cli_errors.CliError as err:
            cli_errors.emit_error(err, flags=flags)
            return
        via_direct = True
    else:
        try:
            from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

            with DaemonClient() as client:
                result = client.call(
                    "evidence.append",
                    {"record": record.model_dump(mode="json")},
                )
            appended_at = str(result.get("appended_at", ""))
        except DaemonRpcError as exc:
            cli_errors.emit_error(
                cli_errors.UserError(
                    f"daemon rejected evidence.append: code={exc.code} {exc.message}",
                    kind="DaemonError",
                ),
                flags=flags,
            )
            return
        except (OSError, RuntimeError) as exc:
            # Daemon unreachable: refuse direct write unless the env gate
            # is opted in. The error name-checks the env var so the
            # operator knows the documented recovery hatch.
            cli_errors.emit_error(
                cli_errors.UserError(
                    (
                        f"daemon unavailable for evidence.append: {exc}; "
                        f"set {EVIDENCE_DIRECT_WRITE_ENV}=1 to fall back to a "
                        "direct evidence.jsonl append (CI / recovery shell only)"
                    ),
                    kind="DaemonError",
                ),
                flags=flags,
            )
            return

    logger.info(
        f"evidence_attest id={record.id!r} scope={record.scope_id!r} "
        f"evidence_kind={record.evidence_kind!r} via_direct={via_direct}"
    )
    _emit(
        {
            "id": record.id,
            "scope_id": record.scope_id,
            "evidence_kind": record.evidence_kind,
            "status": record.status,
            "appended_at": appended_at,
            "via_direct_write": via_direct,
        },
        f"evidence {record.id} appended status={record.status}",
        flags,
    )


# ---- Command registration --------------------------------------------------
# Importing the sibling modules runs their ``@<app>.command(...)`` decorators
# so the apps above carry their full verb set. The imports sit at the bottom,
# after every shared symbol is defined, so the siblings can import the apps and
# helpers from this module without a circular-import failure.
from eawf.surfaces.cli.commands import evidence_artifact as _evidence_artifact  # noqa: E402, F401
from eawf.surfaces.cli.commands import evidence_backlog as _evidence_backlog  # noqa: E402, F401
from eawf.surfaces.cli.commands import (  # noqa: E402
    evidence_hypothesis as _evidence_hypothesis,  # noqa: F401
)
from eawf.surfaces.cli.commands import evidence_incident as _evidence_incident  # noqa: E402, F401

install_promote_command(audit_app, "audit")
install_promote_command(hypothesis_app, "hypothesis")
install_promote_command(decision_app, "decision")
install_promote_command(incident_app, "incident")


# ---- Re-exports ------------------------------------------------------------

__all__ = [
    "EVIDENCE_DIRECT_WRITE_ENV",
    "artifact_app",
    "audit_app",
    "backlog_app",
    "decision_app",
    "evidence_app",
    "goal_app",
    "hypothesis_app",
    "incident_app",
    "outcome_app",
]
