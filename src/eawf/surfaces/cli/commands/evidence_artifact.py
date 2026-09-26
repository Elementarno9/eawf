"""Artifact command handlers.

Split out of :mod:`eawf.surfaces.cli.commands.evidence`. The
``artifact_app`` Typer app and the shared helpers live in the parent
module; this module attaches the command bodies (add / update / show /
validate / verify) via ``@<app>.command(...)``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final

import typer

from eawf.kernel.state.enums import StoreKind
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.evidence import (
    _emit,
    _flags,
    _run_read,
    _state_path,
    artifact_app,
)

if TYPE_CHECKING:
    from eawf.kernel.spec.measured_contract import MeasuredContract, ScaleBand
    from eawf.surfaces.cli.flags import GlobalFlags

logger = logging.getLogger(__name__)


# ---- artifact --------------------------------------------------------------


@artifact_app.command("add")
def artifact_add(
    ctx: typer.Context,
    artifact_id: Annotated[str, typer.Argument(help="Artifact id")],
    kind: Annotated[str, typer.Option("--kind", help="Artifact kind, e.g. audit_report")],
    uri: Annotated[str, typer.Option("--uri", help="Artifact URI (repo:... or remote URI)")],
    sha256: Annotated[str | None, typer.Option("--sha256", help="Optional SHA-256 hash")] = None,
    size: Annotated[int | None, typer.Option("--size", help="Optional size in bytes")] = None,
    scope_id: Annotated[
        str | None,
        typer.Option(
            "--scope-id",
            help="Owning scope (defaults to project code).",
        ),
    ] = None,
) -> None:
    """Register a durable artifact."""
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import artifact as artifact_evi
    from eawf.workflow.evidence._io import append_jsonl, store_paths

    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        with state_transaction(state_path) as state:
            resolved_scope = scope_id
            if resolved_scope is None:
                if state.project is None:
                    raise cli_errors.UserError(
                        "scope_id required when state.project is unset", kind="InvalidInput"
                    )
                resolved_scope = state.project.code
            event = artifact_evi.add_artifact(
                state,
                artifact_id=artifact_id,
                kind=kind,
                uri=uri,
                scope_id=resolved_scope,
                sha256=sha256,
                size_bytes=size,
            )
            append_jsonl(store_paths(state_path)[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "artifact_id": artifact_id,
            "kind": kind,
            "uri": uri,
            "sha256": sha256,
            "scope_id": resolved_scope,
        },
        f"artifact {artifact_id} added kind={kind}",
        flags,
    )


@artifact_app.command("update")
def artifact_update(
    ctx: typer.Context,
    artifact_id: Annotated[str, typer.Argument(help="Artifact id to update")],
    sha256: Annotated[
        str | None,
        typer.Option("--sha256", help="New SHA-256 hash for the artifact body."),
    ] = None,
    size: Annotated[
        int | None,
        typer.Option("--size", help="New body size in bytes."),
    ] = None,
    uri: Annotated[
        str | None,
        typer.Option("--uri", help="New URI (repo:... or remote URI)."),
    ] = None,
) -> None:
    """Update mutable fields on a registered artifact.

    Re-pins ``sha256`` / ``size_bytes`` / ``uri`` when the underlying
    body content changed (typically after a pre-commit auto-fix). At
    least one of ``--sha256`` / ``--size`` / ``--uri`` must be supplied.
    Identity fields (``id``, ``kind``, ``urn``, ``scope_id``,
    ``created_at``) stay fixed.
    """
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import artifact as artifact_evi
    from eawf.workflow.evidence._io import append_jsonl, store_paths

    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        with state_transaction(state_path) as state:
            event = artifact_evi.update_artifact(
                state,
                artifact_id=artifact_id,
                sha256=sha256,
                size_bytes=size,
                uri=uri,
            )
            append_jsonl(store_paths(state_path)[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "artifact_id": artifact_id,
            "sha256": sha256,
            "size_bytes": size,
            "uri": uri,
        },
        f"artifact {artifact_id} updated",
        flags,
    )


def _refresh_contract_metadata(
    *,
    contract_id: str,
    contract: MeasuredContract,
    required_band: ScaleBand,
    scope_id: str | None,
    state_path: Path,
    flags: GlobalFlags,
) -> None:
    """Run the ``--refresh-metadata`` arm of ``artifact promote-contract``.

    One state transaction rewrites the registered row and one artifact
    event records it; no evidence row is minted, because the measurement
    the promotion already attested to has not changed.

    Args:
        contract_id: Measured-contract id the operator named.
        contract: The in-code contract the row is rewritten from.
        required_band: Band the implementing checkpoint asserts over.
        scope_id: The operator's ``--scope-id``, refused when supplied.
        state_path: Resolved ``state.json`` location.
        flags: Resolved global flags for output and error emission.
    """
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import measured_contract as contract_evi
    from eawf.workflow.evidence._io import append_jsonl, store_paths

    if scope_id is not None:
        cli_errors.emit_error(
            cli_errors.UserError(
                "--scope-id is not accepted with --refresh-metadata: the registered row's "
                "urn already pins the owning scope",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return

    try:
        with state_transaction(state_path) as state:
            refresh = contract_evi.refresh_contract_metadata(
                state,
                contract=contract,
                required_band=required_band,
            )
            append_jsonl(store_paths(state_path)[StoreKind.EVENT], refresh.artifact_event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "contract_id": contract_id,
            "urn": refresh.urn,
            "changed_keys": list(refresh.changed_keys),
            "scale_band": refresh.contract.environment.scale_band.value,
            "required_band": required_band.value,
        },
        f"contract {contract_id} metadata refreshed urn={refresh.urn} "
        f"changed={','.join(refresh.changed_keys) or 'none'}",
        flags,
    )


@artifact_app.command("promote-contract")
def artifact_promote_contract(
    ctx: typer.Context,
    contract_id: Annotated[
        str,
        typer.Argument(help="Measured-contract id, e.g. MCT-26081301."),
    ],
    scope_id: Annotated[
        str | None,
        typer.Option("--scope-id", help="Owning scope (defaults to project code)."),
    ] = None,
    refresh_metadata: Annotated[
        bool,
        typer.Option(
            "--refresh-metadata",
            help=(
                "Rewrite an already-registered contract row's metadata from the "
                "in-code contract instead of registering a new row."
            ),
        ),
    ] = False,
) -> None:
    """Promote a measured contract onto the evidence path.

    Registers the contract as an artifact so it resolves by
    ``urn:eawf:v1:artifact:<scope>/<contract-id>``, and appends one
    evidence (EVD) row recording the promotion. Refuses a contract whose
    measured scale band is below the band its implementing checkpoint
    asserts over.

    ``--refresh-metadata`` is the migration mode for a row that was
    promoted before the typed contract was corrected: it rewrites that
    row's ``metadata`` in one state transaction and appends one artifact
    event, minting no second evidence row. The registered row's URN
    already pins the owning scope, so ``--scope-id`` is refused with it.
    """
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import measured_contract as contract_evi
    from eawf.workflow.evidence._io import append_jsonl, store_paths

    flags = _flags(ctx)
    state_path = _state_path(flags)

    contract = contract_evi.PREFLIGHT_CONTRACTS.get(contract_id)
    if contract is None:
        known = ", ".join(sorted(contract_evi.PREFLIGHT_CONTRACTS))
        cli_errors.emit_error(
            cli_errors.UserError(
                f"unknown measured contract {contract_id!r} (known: {known})",
                kind="NotFound",
            ),
            flags=flags,
        )
        return
    required_band = contract_evi.PREFLIGHT_CHECKPOINT_BANDS[contract_id]

    if refresh_metadata:
        _refresh_contract_metadata(
            contract_id=contract_id,
            contract=contract,
            required_band=required_band,
            scope_id=scope_id,
            state_path=state_path,
            flags=flags,
        )
        return

    try:
        with state_transaction(state_path) as state:
            resolved_scope = scope_id
            if resolved_scope is None:
                if state.project is None:
                    raise cli_errors.UserError(
                        "scope_id required when state.project is unset", kind="InvalidInput"
                    )
                resolved_scope = state.project.code
            promotion = contract_evi.promote_measured_contract(
                state,
                contract=contract,
                scope_id=resolved_scope,
                required_band=required_band,
            )
            paths = store_paths(state_path)
            append_jsonl(paths[StoreKind.EVENT], promotion.artifact_event)
            append_jsonl(paths[StoreKind.EVIDENCE], promotion.evidence_envelope)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "contract_id": promotion.artifact_id,
            "urn": promotion.urn,
            "evidence_id": promotion.evidence.id,
            "scale_band": promotion.contract.environment.scale_band.value,
            "required_band": required_band.value,
            "boundary": promotion.contract.boundary,
            "scope_id": resolved_scope,
        },
        f"contract {promotion.artifact_id} promoted urn={promotion.urn} "
        f"evidence={promotion.evidence.id}",
        flags,
    )


@artifact_app.command("submit-evidence")
def artifact_submit_evidence(
    ctx: typer.Context,
    report_path: Annotated[
        Path,
        typer.Argument(help="Path to a verified SpikeReport JSON document."),
    ],
    scope_id: Annotated[
        str | None,
        typer.Option("--scope-id", help="Owning scope (defaults to project code)."),
    ] = None,
) -> None:
    """Submit a verified SpikeReport's measured contracts onto the evidence path.

    This is the general form of ``promote-contract``: rather than naming
    one of the contracts already hard-coded into this module, it reads a
    :class:`~eawf.kernel.spec.measured_contract.SpikeReport` from
    *report_path* and promotes every contract it carries. Refuses the
    whole report unverified, and refuses any one contract whose
    environment names a repository other than the target scope, before
    anything is written.
    """
    from eawf.kernel.spec.measured_contract import SpikeReport
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import measured_contract as contract_evi
    from eawf.workflow.evidence._io import append_jsonl, store_paths

    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        report = SpikeReport.model_validate(json.loads(report_path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"cannot read spike report {report_path}: {exc}", kind="InvalidInput"
            ),
            flags=flags,
        )
        return

    try:
        with state_transaction(state_path) as state:
            resolved_scope = scope_id
            if resolved_scope is None:
                if state.project is None:
                    raise cli_errors.UserError(
                        "scope_id required when state.project is unset", kind="InvalidInput"
                    )
                resolved_scope = state.project.code
            promotions = contract_evi.submit_evidence(state, report=report, scope_id=resolved_scope)
            paths = store_paths(state_path)
            for promotion in promotions:
                append_jsonl(paths[StoreKind.EVENT], promotion.artifact_event)
                append_jsonl(paths[StoreKind.EVIDENCE], promotion.evidence_envelope)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "report_id": report.report_id,
            "scope_id": resolved_scope,
            "promoted": [
                {
                    "contract_id": promotion.artifact_id,
                    "urn": promotion.urn,
                    "content_digest": promotion.content_digest,
                }
                for promotion in promotions
            ],
        },
        f"spike report {report.report_id} submitted: {len(promotions)} contract(s) promoted",
        flags,
    )


#: The dotted JSON-RPC name ``artifact file-spike-report`` forwards to.
#: Spelled here rather than imported so the Typer tree builds without the
#: daemon method registry on the path -- the same reason
#: :mod:`eawf.surfaces.cli.commands.domain` spells its own verb table.
#: ``tests/integration/test_cli_evidence_artifact.py`` asserts this
#: against the daemon's own
#: :data:`eawf.runtime.daemon.semantic_handlers.EVIDENCE_SPIKE_REPORT_FILE_METHOD`,
#: so a renamed verb reds rather than drifts.
EVIDENCE_SPIKE_REPORT_FILE: Final = "runtime.evidence.spike_report.file"


@artifact_app.command("file-spike-report")
def artifact_file_spike_report(
    ctx: typer.Context,
    report_path: Annotated[
        Path,
        typer.Argument(help="Path to a SpikeReport JSON document."),
    ],
    run: Annotated[str, typer.Option("--run", help="URN of the Run filing its own report.")],
    artifact_ref: Annotated[
        str,
        typer.Option(
            "--artifact-ref",
            help="Reference a later `submit_evidence` call names this report by.",
        ),
    ],
    actor: Annotated[str, typer.Option("--actor", help="Who asked.")],
) -> None:
    """File a SpikeReport as the artifact a ``submit_evidence`` call resolves.

    Dispatch only: sends ``runtime.evidence.spike_report.file`` to the
    daemon, which records the report once and replays the standing
    artifact on a retry that repeats the same content -- refilling the
    gap where only a test filer could stand in for it. Prints the filed
    ``artifact_ref`` and ``content_digest``, the pair a later
    ``submit_evidence`` semantic call names to promote the report's
    contracts.
    """
    from eawf.surfaces.cli.commands.domain import _call_native_rpc

    flags = _flags(ctx)
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"cannot read spike report {report_path}: {exc}", kind="InvalidInput"
            ),
            flags=flags,
        )
        return

    try:
        answer = _call_native_rpc(
            EVIDENCE_SPIKE_REPORT_FILE,
            {"urn": run, "actor": actor, "artifact_ref": artifact_ref, "report": report},
            flags=flags,
            verb_text="artifact file-spike-report",
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        answer,
        f"spike report filed artifact_ref={answer['artifact_ref']} "
        f"content_digest={answer['content_digest']}",
        flags,
    )


@artifact_app.command("show")
def artifact_show(
    ctx: typer.Context,
    artifact_id: Annotated[
        str,
        typer.Argument(help="Artifact id or urn:eawf:v1:artifact:<scope>/<id>."),
    ],
) -> None:
    """Show artifact metadata.

    Accepts a bare artifact id or a full artifact URN. A citation that
    points into the gitignored spike tree is refused with the promotion
    command rather than a bare not-found.
    """
    from eawf.workflow.evidence import measured_contract as contract_evi
    from eawf.workflow.evidence._io import load_state

    flags = _flags(ctx)
    state_path = _state_path(flags)
    state = _run_read(flags, load_state, state_path)
    artifact = _run_read(flags, contract_evi.resolve_contract_citation, state, artifact_id)
    payload = json.loads(artifact.model_dump_json())
    _emit(
        payload,
        f"artifact {artifact.id} kind={artifact.kind} uri={artifact.uri}",
        flags,
    )


@artifact_app.command("validate")
def artifact_validate(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="Markdown artifact path.")],
) -> None:
    """Validate one markdown artifact body."""
    from eawf.platform.artifacts.validation import validate_markdown_artifact

    flags = _flags(ctx)
    text = path.read_text(encoding="utf-8")
    report = validate_markdown_artifact(text)
    payload = {"ok": report.ok, "errors": report.errors}
    if not report.ok:
        _emit(payload, "\n".join(report.errors), flags)
        raise typer.Exit(code=4)
    _emit(payload, "artifact validate: ok", flags)


# ---- artifact verify -------------------------------------------------------


def _verify_one_artifact(
    artifact: Any,
    repo_root: Path,
    *,
    refresh: bool,
) -> dict[str, Any]:
    """Verify one artifact body against its registered sha256.

    Returns a result row of the shape used by :func:`artifact_verify`. The
    ``status`` field is one of:

    - ``"ok"`` — sha256 recomputed and matched the registered value.
    - ``"no_hash"`` — no registered sha256 to compare against.
    - ``"missing_file"`` — repo-relative uri did not resolve to a file.
    - ``"skipped_remote"`` — non-``repo:`` uri with ``--refresh`` not set.
    - ``"mismatch"`` — recomputed sha256 did not match the registered value.
    """
    from eawf.platform.artifacts.validation import sha256_file

    uri = artifact.uri
    registered = artifact.sha256
    row: dict[str, Any] = {
        "artifact_id": artifact.id,
        "uri": uri,
        "registered_sha256": registered,
        "computed_sha256": None,
        "status": "ok",
    }
    if not uri.startswith("repo:"):
        if not refresh:
            row["status"] = "skipped_remote"
            return row
        # --refresh is not implemented for remote URIs (no network in v0.3).
        row["status"] = "skipped_remote"
        row["message"] = "refresh not supported for remote uris yet"
        return row
    relpath = uri[len("repo:") :]
    if not relpath:
        row["status"] = "missing_file"
        row["message"] = "empty repo-relative path in uri"
        return row
    body_path = repo_root / relpath
    if not body_path.is_file():
        row["status"] = "missing_file"
        row["message"] = f"artifact body not found: {relpath}"
        return row
    computed = sha256_file(body_path)
    row["computed_sha256"] = computed
    if registered is None:
        row["status"] = "no_hash"
        return row
    if registered.lower() != computed.lower():
        row["status"] = "mismatch"
        row["message"] = f"sha256 mismatch: registered={registered!r} computed={computed!r}"
    return row


@artifact_app.command("verify")
def artifact_verify(
    ctx: typer.Context,
    artifact_id: Annotated[
        str | None,
        typer.Argument(help="Artifact id to verify; omit with --all."),
    ] = None,
    verify_all: Annotated[
        bool,
        typer.Option("--all", help="Verify every registered artifact."),
    ] = False,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help="Re-fetch remote URIs (no-op in v0.3; remote always skipped).",
        ),
    ] = False,
) -> None:
    """Recompute artifact sha256 and compare to the registered hash.

    Exit codes:

    - ``0`` — every checked artifact matched (or had no registered hash).
    - ``2`` (``NOT_FOUND``) — single-id mode with unknown artifact id.
    - ``8`` (``INTEGRITY_VIOLATION``) — at least one artifact mismatched.
    """
    from eawf.workflow.evidence._io import load_state

    flags = _flags(ctx)
    if (artifact_id is None) == (not verify_all):
        cli_errors.emit_error(
            cli_errors.UserError(
                "exactly one of <artifact-id> or --all must be provided", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    state_path = _state_path(flags)
    state = _run_read(flags, load_state, state_path)
    repo_root = state_path.parent.parent

    if verify_all:
        artifacts_to_check = sorted(state.artifacts.values(), key=lambda a: a.id)
    else:
        assert artifact_id is not None
        if artifact_id not in state.artifacts:
            cli_errors.emit_error(
                cli_errors.UserError(f"artifact not found: {artifact_id!r}", kind="NotFound"),
                flags=flags,
            )
            return
        artifacts_to_check = [state.artifacts[artifact_id]]

    results: list[dict[str, Any]] = [
        _verify_one_artifact(art, repo_root, refresh=refresh) for art in artifacts_to_check
    ]
    mismatches = [r for r in results if r["status"] == "mismatch"]
    missing = [r for r in results if r["status"] == "missing_file"]
    payload: dict[str, Any] = {
        "checked": len(results),
        "ok": len(results) - len(mismatches) - len(missing),
        "mismatches": len(mismatches),
        "missing": len(missing),
        "results": results,
    }
    if mismatches:
        text = (
            f"artifact verify: mismatch ({len(mismatches)}/{len(results)} "
            f"artifacts failed sha256 check)"
        )
        _emit(payload, text, flags)
        raise typer.Exit(code=cli_errors.StateConflict.exit_code)
    if missing:
        text = (
            f"artifact verify: missing files ({len(missing)}/{len(results)} "
            f"artifacts had no resolvable body)"
        )
        _emit(payload, text, flags)
        raise typer.Exit(code=cli_errors.StateConflict.exit_code)
    text = f"artifact verify: ok ({len(results)} checked)"
    _emit(payload, text, flags)
