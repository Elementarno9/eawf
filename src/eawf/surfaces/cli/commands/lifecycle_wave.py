"""Wave mutator command handlers (plan / claim / close / show / fail / update).

Split out of :mod:`eawf.surfaces.cli.commands.lifecycle` (P27-W06). The ``wave_app``
Typer app, the shared transaction helpers, and the wave git/commit-ref
helpers (``_resolve_commit_sha``, ``_resolve_repo_root_for_drift``,
``_wave_close_via_daemon``) live in the parent module; this module attaches
the wave mutator command bodies via ``@wave_app.command(...)``. The wave
read / dispatch / budget verbs live in
:mod:`eawf.surfaces.cli.commands.lifecycle_wave_read`.
"""

# noqa: EAWF010 cohesive wave-mutator CLI; split with shared close-policy plumbing

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from eawf.kernel.config.schema import VerifyWaiverMode
from eawf.kernel.state.enums import (
    AgentSessionRole,
    EffortBucket,
    WaveStatus,
)
from eawf.kernel.state.ids import (
    is_iter_id,
    is_wave_id,
)
from eawf.kernel.state.mutations import MutationKind
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.lifecycle import (
    _load_state_readonly,
    _resolve_commit_sha,
    _resolve_repo_root_for_drift,
    _run_mutation,
    _wave_close_via_daemon,
    wave_app,
)
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.scope import resolve_state_path
from eawf.workflow.lifecycle._capacity import resolve_max_parallel_waves

if TYPE_CHECKING:
    from eawf.kernel.state.models import State
    from eawf.platform.profiles.models import VerifyBlock
    from eawf.workflow.verify.models import CloseReadiness

logger = logging.getLogger(__name__)

NO_RUNTIME_WAIVER_REF = "runtime-zero"
NO_RUNTIME_WAIVER_REASON = "runtime capture unavailable; operator supplied --no-runtime"


def _config_root_for_state_path(state_path: Path) -> Path:
    """Return the root that owns ``.ea/config.yaml`` for *state_path*."""
    return state_path.parent.parent if state_path.parent.name == ".ea" else state_path.parent


def _effective_waiver_mode(
    state: State,
    *,
    wave_id: str,
    flags: GlobalFlags,
) -> VerifyWaiverMode:
    """Resolve strict layered waiver policy for one CLI mutation boundary."""
    from eawf.surfaces.cli.scope import resolve_state_path
    from eawf.workflow.verify.readiness import load_active_waiver_mode

    state_path = resolve_state_path(flags.workspace)
    config_root = _config_root_for_state_path(state_path)
    repo_root = _resolve_repo_root_for_drift(flags.workspace) or config_root
    try:
        return load_active_waiver_mode(
            wave_id,
            state,
            repo_root=repo_root,
            config_root=config_root,
        )
    except (OSError, ValueError, KeyError) as exc:
        raise cli_errors.ValidationError(f"verify config invalid: {exc}") from exc


def _reject_disabled_close_waivers(
    *,
    ctx: typer.Context,
    flags: GlobalFlags,
    wave_id: str,
    waive_inputs: list[Any],
) -> None:
    """Reject disabled close policy violations before any evidence append."""
    from eawf.workflow.lifecycle._errors import (
        WAIVER_MODE_DISABLED,
        LifecycleGuardError,
        check_disabled_waiver_policy,
    )

    loaded = _load_state_readonly(ctx)
    if loaded is None:
        raise cli_errors.UserError(
            "state not loadable; waiver policy cannot be resolved", kind="NotFound"
        )
    state, _ = loaded
    waiver_mode = _effective_waiver_mode(state, wave_id=wave_id, flags=flags)
    if waiver_mode != "disabled":
        return
    if waive_inputs:
        raise cli_errors.ValidationError(
            f"{WAIVER_MODE_DISABLED}: gate waiver creation is disabled (wave={wave_id!r})"
        )
    wave = state.waves.get(wave_id)
    if wave is not None:
        try:
            check_disabled_waiver_policy(
                waiver_mode=waiver_mode,
                scope_id=wave_id,
                criteria=list(wave.success_criteria),
                criteria_floor_waiver=wave.criteria_floor_waiver,
            )
        except LifecycleGuardError as exc:
            raise cli_errors.ValidationError(str(exc)) from exc


def _resolve_close_verify_block(
    wave_id: str,
    state: State,
    *,
    repo_root: Path,
    config_root: Path,
) -> VerifyBlock | None:
    """Load + band-narrow the active verify block for a closing wave.

    Wraps :func:`~eawf.workflow.verify.readiness.load_active_verify_block`
    with the band-conditional resolver
    (:func:`~eawf.workflow.verify.readiness.resolve_wave_verify_block`) so the
    CLI direct-write fallback matches the daemon close gate: a band-scoped
    profile gates only the wave's UI/UX band, and a non-band wave stays
    advisory. A wave id absent from *state* leaves the merged block
    un-narrowed (the readiness compute then surfaces the missing wave).

    Args:
        wave_id: The closing wave id.
        state: Loaded state -- read for the wave's band membership.
        repo_root: Anchor for SHA derivation + profile discovery.
        config_root: Anchor that owns ``.ea/config.yaml``.

    Returns:
        The band-conditional :class:`VerifyBlock`, or ``None`` when no active
        profile contributes one.
    """
    from eawf.workflow.verify.readiness import (
        load_active_verify_block,
        resolve_wave_verify_block,
    )

    verify_block = load_active_verify_block(
        wave_id,
        state,
        repo_root=repo_root,
        config_root=config_root,
    )
    wave = state.waves.get(wave_id)
    if wave is None:
        return verify_block
    return resolve_wave_verify_block(verify_block, wave)


def _wrap_no_return(_value: object) -> None:
    """Adapter so transition helpers can be passed directly to ``mutate=``."""
    return None


def _parse_waiver_flags(
    *,
    waive: list[str] | None,
    waive_reason: list[str] | None,
    waive_decision: list[str] | None,
    waive_audit: list[str] | None,
) -> list[Any]:
    """Parse the repeatable W11 waiver flags into a list of WaiverInput.

    All four lists pair by position with ``waive``. ``waive_reason``,
    ``waive_decision``, and ``waive_audit`` MUST be either empty (no
    flags supplied) or have the same length as ``waive`` so a slot can
    be left blank with an empty string. Bare ``--waive`` with no
    ``--reason`` slot in modes B and C is caught later inside
    :func:`eawf.workflow.lifecycle.waivers.apply_waiver`; this helper
    catches only structural mismatches (length, parse-time empties).

    Args:
        waive: Repeatable ``--waive GATE_ID`` values.
        waive_reason: Repeatable ``--reason TEXT`` values aligned by
            index with *waive*.
        waive_decision: Repeatable ``--decision URN`` values aligned by
            index with *waive*. Empty string means "no decision ref for
            this slot".
        waive_audit: Repeatable ``--audit URN`` values aligned by
            index with *waive*. Empty string means "no audit ref for
            this slot".

    Returns:
        List of ``WaiverInput`` rows, one per ``--waive`` value.

    Raises:
        cli_errors.UserError: When a parallel-list length disagreement
            is detected.
    """
    from eawf.workflow.lifecycle.waivers import WaiverInput

    if not waive:
        return []

    def _check_len(name: str, values: list[str] | None) -> list[str]:
        if values is None:
            return [""] * len(waive)
        if len(values) != len(waive):
            raise cli_errors.UserError(
                f"--{name} list length ({len(values)}) must equal "
                f"--waive list length ({len(waive)}); "
                f"use an empty string to skip a slot",
                kind="InvalidInput",
            )
        return values

    reasons = _check_len("reason", waive_reason)
    decisions = _check_len("decision", waive_decision)
    audits = _check_len("audit", waive_audit)

    inputs: list[Any] = []
    for index, gate_id in enumerate(waive):
        if not gate_id:
            raise cli_errors.UserError(
                f"--waive value at position {index + 1} is empty",
                kind="InvalidInput",
            )
        inputs.append(
            WaiverInput(
                gate_id=gate_id,
                reason=reasons[index] or None,
                decision_ref=decisions[index] or None,
                audit_ref=audits[index] or None,
            )
        )
    return inputs


def _persist_waivers(
    *,
    ctx: typer.Context,
    flags: GlobalFlags,
    wave_id: str,
    waive_inputs: list[Any],
) -> None:
    """Persist parsed waiver inputs via the daemon RPC or direct fallback.

    Loads the state read-only to resolve the operator session +
    :class:`~eawf.workflow.lifecycle.waivers.WaiverMode`, then calls
    :func:`~eawf.workflow.lifecycle.waivers.apply_waiver` once per
    waiver. Each returned :class:`EvidenceRecord` is either already
    persisted (when ``EAWF_EVIDENCE_DIRECT_WRITE=1``) or POSTed
    through the daemon ``evidence.append`` RPC by this helper.

    Args:
        ctx: Typer context — owns the read-only state loader.
        flags: Resolved CLI flags (workspace, json mode).
        wave_id: Wave id the waivers are scoped to.
        waive_inputs: Parsed :class:`WaiverInput` rows from the CLI.

    Raises:
        cli_errors.ValidationError: When the operator-only contract or
            the linkage policy reject any waiver.
        cli_errors.UserError: When state resolution fails.
        cli_errors.DaemonUnreachable / cli_errors.InternalError: When
            the daemon RPC path fails (mapped from the JSON-RPC error
            code).
    """
    from eawf.surfaces.cli.scope import resolve_state_path
    from eawf.workflow.lifecycle.waivers import apply_waiver

    loaded = _load_state_readonly(ctx)
    if loaded is None:
        # _load_state_readonly already emitted; treat as "exit raised"
        # by raising here so the close path stops.
        raise cli_errors.UserError(
            "state not loadable; waivers cannot be persisted", kind="NotFound"
        )
    state, _ = loaded

    state_path = resolve_state_path(flags.workspace)
    mode = _effective_waiver_mode(state, wave_id=wave_id, flags=flags)

    operator_identity = (
        state.current.active_session_ids[0] if state.current.active_session_ids else None
    )

    repo_root = _resolve_repo_root_for_drift(flags.workspace)

    direct_write = os.environ.get("EAWF_EVIDENCE_DIRECT_WRITE") == "1"
    for waiver in waive_inputs:
        record = apply_waiver(
            state,
            wave_id=wave_id,
            waiver=waiver,
            operator_identity=operator_identity,
            mode=mode,
            state_path=state_path,
            repo_root=repo_root,
        )
        logger.info(
            f"_persist_waivers wave={wave_id!r} gate_id={waiver.gate_id!r} "
            f"produced_by={record.produced_by!r} mode={mode!r} "
            f"evidence_id={record.id!r}"
        )
        if direct_write:
            # apply_waiver already wrote the row via _append_direct.
            continue
        from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

        try:
            with DaemonClient() as client:
                client.call(
                    "evidence.append",
                    {"record": record.model_dump(mode="json")},
                )
        except DaemonRpcError as exc:
            raise cli_errors.UserError(
                f"daemon rejected evidence.append for waiver: code={exc.code} {exc.message}",
                kind="DaemonError",
            ) from exc
        except (OSError, RuntimeError) as exc:
            raise cli_errors.UserError(
                (
                    f"daemon unavailable for evidence.append: {exc}; "
                    "set EAWF_EVIDENCE_DIRECT_WRITE=1 to fall back to a direct "
                    "evidence.jsonl append (CI / recovery shell only)"
                ),
                kind="DaemonError",
            ) from exc


def _warn_on_zero_eu_close(ctx: typer.Context, *, wave_id: str, waived: bool) -> None:
    """Tell the operator when a close recorded no runtime EU.

    The daemon's zero-runtime gate is advisory unless the active profile enforces
    it, and an advisory refusal used to reach only the daemon log -- which is how
    EU capture stayed dead for the whole of its life without anyone noticing. Read
    the recorded actual straight back off state and say so on the close surface.

    Args:
        ctx: The Typer context the close ran under (resolves the scope's state).
        wave_id: The wave that just closed.
        waived: Whether the operator passed ``--no-runtime``, which makes the
            zero EU an accepted fact rather than a surprise.
    """
    loaded = _load_state_readonly(ctx)
    if loaded is None:
        return
    state, _ = loaded
    actual = (state.actuals or {}).get(wave_id)
    if actual is not None and actual.elapsed_eu > 0.0:
        return
    detail = (
        "runtime waiver accepted"
        if waived
        else "EU capture is not landing -- check the runtime Stop hook and the "
        "daemon runtime.capture path"
    )
    print(
        f"advisory: wave {wave_id} closed with no captured runtime (elapsed_eu=0.0); {detail}",
        file=sys.stderr,
    )


def _build_no_runtime_waiver_record(
    state: State,
    *,
    wave_id: str,
    operator_identity: str | None,
    state_path: Path,
    repo_root: Path | None,
) -> Any:
    """Build the human waiver evidence row for ``--no-runtime``."""
    from eawf.workflow.lifecycle.waivers import WaiverInput, apply_waiver

    return apply_waiver(
        state,
        wave_id=wave_id,
        waiver=WaiverInput(gate_id=NO_RUNTIME_WAIVER_REF, reason=NO_RUNTIME_WAIVER_REASON),
        operator_identity=operator_identity,
        mode="B",
        state_path=state_path,
        repo_root=repo_root,
    )


def _persist_no_runtime_waiver(
    *,
    ctx: typer.Context,
    flags: GlobalFlags,
    wave_id: str,
) -> None:
    """Persist the operator's ``--no-runtime`` waiver via ``evidence.append``."""
    from eawf.surfaces.cli.scope import resolve_state_path

    loaded = _load_state_readonly(ctx)
    if loaded is None:
        raise cli_errors.UserError(
            "state not loadable; no-runtime waiver cannot be persisted", kind="NotFound"
        )
    state, _ = loaded
    state_path = resolve_state_path(flags.workspace)
    repo_root = _resolve_repo_root_for_drift(flags.workspace)
    operator_identity = (
        state.current.active_session_ids[0] if state.current.active_session_ids else None
    )
    record = _build_no_runtime_waiver_record(
        state,
        wave_id=wave_id,
        operator_identity=operator_identity,
        state_path=state_path,
        repo_root=repo_root,
    )
    logger.info(
        f"_persist_no_runtime_waiver wave={wave_id!r} "
        f"produced_by={record.produced_by!r} evidence_id={record.id!r}"
    )
    if os.environ.get("EAWF_EVIDENCE_DIRECT_WRITE") == "1":
        return

    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

    try:
        with DaemonClient() as client:
            client.call(
                "evidence.append",
                {"record": record.model_dump(mode="json")},
            )
    except DaemonRpcError as exc:
        raise cli_errors.UserError(
            f"daemon rejected evidence.append for no-runtime waiver: code={exc.code} {exc.message}",
            kind="DaemonError",
        ) from exc
    except (OSError, RuntimeError) as exc:
        raise cli_errors.UserError(
            (
                f"daemon unavailable for evidence.append: {exc}; "
                "set EAWF_EVIDENCE_DIRECT_WRITE=1 to fall back to a direct "
                "evidence.jsonl append (CI / recovery shell only)"
            ),
            kind="DaemonError",
        ) from exc


def _persist_close_waiver_inputs(
    *,
    ctx: typer.Context,
    flags: GlobalFlags,
    wave_id: str,
    waive_inputs: list[Any],
    no_runtime: bool,
) -> None:
    """Persist all pre-close waiver evidence rows."""
    if waive_inputs:
        _persist_waivers(
            ctx=ctx,
            flags=flags,
            wave_id=wave_id,
            waive_inputs=waive_inputs,
        )
    if no_runtime:
        _persist_no_runtime_waiver(ctx=ctx, flags=flags, wave_id=wave_id)


def _stamp_close_mechanism(
    state: State,
    *,
    wave_id: str,
    state_path: Path,
    waived: bool,
    transport_fallback: bool,
    holder: list[Any],
) -> None:
    """Run the daemonless bypass door for a closing wave and record its mechanism.

    The W18 -> W25 wiring point for the in-process close path: under
    ``EAWF_DAEMONLESS`` the daemon close gate never runs, so a GATE-BEARING wave
    would slip its falsifiers. :func:`enforce_daemonless_close_waiver` REJECTS
    such a close unless *waived* (the operator's ``--no-runtime`` flag), and on
    the allowed bypass appends an auditable waiver event naming the wave +
    reason. A daemon-mediated or non-gate-bearing close passes through untouched.
    The resolved :class:`~eawf.surfaces.cli._mutation.CloseMechanism` is appended to
    *holder* so the caller can stamp it on the close event's ``extras`` map.

    When *transport_fallback* is set the close reached this in-process path after
    the daemon close RPC failed at the transport layer; the mechanism is
    ``"daemon-fallback"`` (distinct from a gate-passed ``"daemon"`` close) and the
    daemonless bypass door is skipped -- a transport fallback can only arise on a
    daemon-mediated invocation, which is never under the ``EAWF_DAEMONLESS`` hatch.

    Args:
        state: Loaded state -- the closing wave row is read for its gates.
        wave_id: Id of the wave being closed.
        state_path: Path to ``state.json``; anchors the waiver event store.
        waived: Whether the operator passed ``--no-runtime`` this call.
        transport_fallback: Whether the close fell back to the in-process path
            after a daemon close-RPC transport error.
        holder: One-element sink the resolved mechanism is appended to. Left
            empty when the wave id is absent (the close path then defaults the
            stamp to ``"daemon"``).

    Raises:
        cli_errors.UserError: When the close is daemonless + gate-bearing + NOT
            waived (re-raised from :func:`enforce_daemonless_close_waiver`).
    """
    from eawf.surfaces.cli._mutation import enforce_daemonless_close_waiver

    wave = state.waves.get(wave_id)
    if wave is None:
        return
    if transport_fallback:
        holder.append("daemon-fallback")
        return
    holder.append(
        enforce_daemonless_close_waiver(
            wave,
            state_path=state_path,
            waived=waived,
            reason=NO_RUNTIME_WAIVER_REASON if waived else None,
        )
    )


def _log_advisory_criteria(wave_id: str, readiness: CloseReadiness) -> None:
    """Log one ``close_advisory`` warning per non-passing criterion view."""
    for view in readiness.criteria:
        if view.status != "pass":
            logger.warning(
                f"close_advisory wave={wave_id!r} criterion={view.id!r} status={view.status!r}"
            )


def _run_daemonless_close_preflight(
    state: State,
    *,
    wave_id: str,
    state_path: Path,
    repo_root: Path,
    config_root: Path,
    waived: bool,
) -> CloseReadiness | None:
    """Mirror the daemon close gate on the daemonless / in-process close path.

    Under ``verify.enforce`` this runs the two daemon-side close checks the
    in-process fallback used to only log as advisories: the deterministic
    pre-flight BLOCKS on a failing required gate (:func:`~eawf.workflow.verify.compute`
    raises under enforce), and a verdict-always wave with no fresh auditor verdict
    is REFUSED via the synchronous read gate (the daemonless path cannot spawn the
    auditor). Both honour ``--no-runtime``; sampled / skip waves never block.

    Args:
        state: Loaded state -- read for the closing wave + persisted auditor rows.
        wave_id: Id of the closing wave.
        state_path: Path to ``state.json``; stores resolve under its ``store/``.
        repo_root: Anchor for SHA derive + the deterministic-gate subprocess cwd.
        config_root: Anchor that owns ``.ea/config.yaml``.
        waived: Whether the operator passed ``--no-runtime`` this call.

    Returns:
        The :class:`CloseReadiness` for the close event advisory tally, or
        ``None`` when the wave id is absent or a not-ready refusal was waived.

    Raises:
        LifecycleError: A required gate fails and the close is not waived.
        cli_errors.ValidationError: A verdict-always wave lacks a fresh verdict
            and the close is not waived.
    """
    from eawf.kernel.store.paths import store_dir as _store_dir
    from eawf.workflow.dispatch.verdict import verdict_requirement, verify_wave_verdict_gate
    from eawf.workflow.lifecycle._errors import LifecycleGuardError
    from eawf.workflow.lifecycle.transitions import LifecycleError
    from eawf.workflow.verify import compute as compute_readiness

    readiness: CloseReadiness | None = None
    try:
        readiness = compute_readiness(
            wave_id,
            state=state,
            store_dir=_store_dir(state_path),
            repo_root=repo_root,
            config_root=config_root,
        )
    except KeyError as exc:
        logger.warning(f"close_advisory wave={wave_id!r} status='skip' err={exc!s}")
    except LifecycleGuardError:
        raise
    except LifecycleError:
        # A failing required gate refuses the close unless waived (daemon parity).
        if not waived:
            raise
        logger.warning(f"daemonless_close wave={wave_id!r} status='waived-not-ready'")
    else:
        _log_advisory_criteria(wave_id, readiness)
    wave = state.waves.get(wave_id)
    if wave is None or waived or verdict_requirement(wave) != "always":
        return readiness
    gate = verify_wave_verdict_gate(wave, state_path=state_path)
    if gate.passed:
        return readiness
    reasons = "; ".join(gate.reasons) if gate.reasons else "no fresh auditor verdict"
    logger.warning(
        f"daemonless_close wave={wave_id!r} status='verdict-refused' reasons={reasons!r}"
    )
    raise cli_errors.ValidationError(
        f"daemonless close refused: wave {wave.id!r} requires a fresh auditor "
        f"verdict ({reasons}); the daemonless path cannot spawn the auditor -- "
        "close via the daemon or waive this close with --no-runtime"
    )


@wave_app.command("plan")
def wave_plan_cmd(
    ctx: typer.Context,
    iter_id: Annotated[str, typer.Argument(help="Parent iter ID.")],
    wave_id: Annotated[str, typer.Option("--id", help="Explicit wave ID.")],
    title: Annotated[str, typer.Option("--title", help="Wave title.")],
    files: Annotated[
        str,
        typer.Option(
            "--files",
            help="Comma-separated file globs that the wave covers.",
        ),
    ],
    deps: Annotated[
        str | None,
        typer.Option("--deps", help="Comma-separated dep wave IDs (must already exist)."),
    ] = None,
    success_criteria: Annotated[
        str | None,
        typer.Option("--success", help="Comma-separated success criteria."),
    ] = None,
    agent_role: Annotated[
        AgentSessionRole | None,
        typer.Option("--agent-role", help="Executor role expected for the wave."),
    ] = None,
    effort_bucket: Annotated[
        EffortBucket | None,
        typer.Option("--effort-bucket", help="XS/S/M/L/XL estimate bucket."),
    ] = None,
    description: Annotated[
        str | None,
        typer.Option("--description", help="Optional long-form wave description (≤500 chars)."),
    ] = None,
    criteria_floor_waiver: Annotated[
        str | None,
        typer.Option(
            "--criteria-floor-waiver",
            help=(
                "Waive the typed-criteria floor for legacy --success strings; "
                "pass a >= 20-char reason. The waiver persists on the wave."
            ),
        ),
    ] = None,
) -> None:
    """Plan a new pending wave under an open iter."""
    from datetime import UTC, datetime

    from eawf.kernel.spec.common import grandfather_criterion
    from eawf.kernel.spec.intent import IntentBrief
    from eawf.kernel.state.models import CriteriaFloorWaiver
    from eawf.workflow.lifecycle.transitions import plan_wave

    flags: GlobalFlags = ctx.obj
    if not is_iter_id(iter_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid iter id: {iter_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    if not wave_id.startswith(f"{iter_id}-W"):
        cli_errors.emit_error(
            cli_errors.UserError(
                f"wave id {wave_id!r} does not belong to iter {iter_id!r}", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    file_list = [f.strip() for f in files.split(",") if f.strip()]
    deps_list = [d.strip() for d in (deps or "").split(",") if d.strip()]
    criteria_list = [c.strip() for c in (success_criteria or "").split(",") if c.strip()]
    # An authored wave carries an IntentBrief. This low-level ``wave plan``
    # command takes no ``--intent-*`` flags (the operator authoring surface
    # is ``roadmap revise --add-wave``), so it synthesises a minimal brief
    # from the wave title. The synthesised brief carries a non-blank
    # priority_rationale so the authoring body-completeness guard is
    # satisfied; the rich body comes from the ``roadmap revise`` surface.
    wave_intent = IntentBrief(
        problem=f"plan wave {wave_id}",
        desired_outcome=title,
        priority_rationale=f"staged via wave plan {wave_id}",
    )

    def _plan_with_waiver_policy(state: State) -> None:
        _wrap_no_return(
            plan_wave(
                state,
                wave_id=wave_id,
                iter_id=iter_id,
                title=title,
                file_scopes=file_list,
                deps=deps_list,
                success_criteria=[
                    grandfather_criterion(text, index=idx)
                    for idx, text in enumerate(criteria_list, start=1)
                ],
                agent_role=agent_role,
                effort_bucket=effort_bucket,
                description=description,
                intent=wave_intent,
                criteria_floor_waiver=(
                    CriteriaFloorWaiver(
                        reason=criteria_floor_waiver,
                        waived_at=datetime.now(UTC),
                    )
                    if criteria_floor_waiver is not None
                    else None
                ),
                waiver_mode=_effective_waiver_mode(
                    state,
                    wave_id=wave_id,
                    flags=flags,
                ),
            )
        )

    _run_mutation(
        ctx,
        command="wave plan",
        args={
            "iter_id": iter_id,
            "id": wave_id,
            "title": title,
            "files": file_list,
            "deps": deps_list,
            "success_criteria": criteria_list,
            "agent_role": agent_role.value if agent_role else None,
            "effort_bucket": effort_bucket.value if effort_bucket else None,
            "description": description,
        },
        scope_id=wave_id,
        text=f"wave plan {wave_id} iter={iter_id} title={title!r}",
        envelope=lambda: {
            "wave": wave_id,
            "iter": iter_id,
            "title": title,
            "files": file_list,
            "deps": deps_list,
            "success_criteria": criteria_list,
            "agent_role": agent_role.value if agent_role else None,
            "effort_bucket": effort_bucket.value if effort_bucket else None,
            "description": description,
        },
        mutate=_plan_with_waiver_policy,
        mutation_kind=MutationKind.ROADMAP_REVISE,
        params={
            "op": "add_wave",
            "wave_id": wave_id,
            "iter_id": iter_id,
            "title": title,
            "file_scopes": file_list,
            "deps": deps_list,
            "success_criteria": criteria_list,
            "agent_role": agent_role.value if agent_role else None,
            "effort_bucket": effort_bucket.value if effort_bucket else None,
            "description": description,
            "intent": wave_intent.model_dump(mode="json"),
            "criteria_floor_waiver_reason": criteria_floor_waiver,
        },
    )


@wave_app.command("claim")
def wave_claim_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID to claim.")],
    session: Annotated[str, typer.Option("--session", help="Claiming agent/operator session id.")],
    worktree_policy: Annotated[
        str,
        typer.Option(
            "--worktree-policy",
            help="One of current_branch|fresh_branch|inline.",
        ),
    ] = "current_branch",
    out_of_order: Annotated[
        bool,
        typer.Option(
            "--out-of-order",
            help=(
                "Bypass the W## monotonic gate (P19-W02). Use only for parallel-"
                "worktree dispatch where multiple siblings of the same dep "
                "frontier are claimed at once."
            ),
        ),
    ] = False,
) -> None:
    """Claim a pending wave for *session*. Exactly-once across concurrent calls."""
    from eawf.workflow.lifecycle.transitions import claim_wave

    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    if worktree_policy not in {"current_branch", "fresh_branch", "inline"}:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"--worktree-policy must be current_branch|fresh_branch|inline; "
                f"got {worktree_policy!r}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return

    committed_claim_session_id: list[str] = []

    def _claim_with_budget_gate(state: State) -> None:
        wave = state.waves.get(wave_id)
        if (
            wave is not None
            and wave.token_budget is not None
            and wave.tokens_consumed >= wave.token_budget
        ):
            raise cli_errors.ValidationError(
                f"wave {wave_id!r} is over token budget "
                f"({wave.tokens_consumed}/{wave.token_budget}); raise budget or split work"
            )
        claim_wave(
            state,
            wave_id=wave_id,
            session_id=session,
            out_of_order=out_of_order,
            max_parallel_waves=resolve_max_parallel_waves(
                _config_root_for_state_path(resolve_state_path(flags.workspace))
            ),
            waiver_mode=_effective_waiver_mode(
                state,
                wave_id=wave_id,
                flags=flags,
            ),
        )
        claim_session_id = state.waves[wave_id].claim_session_id
        if claim_session_id is None:  # pragma: no cover - transition guarantees it
            raise RuntimeError(f"claimed wave has no session binding: {wave_id!r}")
        committed_claim_session_id[:] = [claim_session_id]

    _run_mutation(
        ctx,
        command="wave claim",
        args={
            "id": wave_id,
            "session": session,
            "worktree_policy": worktree_policy,
            "out_of_order": out_of_order,
        },
        scope_id=wave_id,
        text=f"wave claim {wave_id} session={session}",
        envelope=lambda: {
            "wave": wave_id,
            "session": session,
            "worktree_policy": worktree_policy,
            "out_of_order": out_of_order,
        },
        mutate=_claim_with_budget_gate,
        extras_factory=lambda: {"claim_session_id": committed_claim_session_id[0]},
        mutation_kind=MutationKind.WAVE_CLAIM,
        params={
            "wave_id": wave_id,
            "session_id": session,
            "out_of_order": out_of_order,
        },
    )


@wave_app.command("close")
def wave_close_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID to close.")],
    outcome: Annotated[
        str | None, typer.Option("--outcome", help="Outcome description (required).")
    ] = None,
    commit_ref: Annotated[
        str | None,
        typer.Option(
            "--commit",
            help=(
                "Optional commit ref to pin on the wave. Accepts full/short "
                "SHA, branch tip, tag, or HEAD-relative ref; normalised via "
                "``git rev-parse <ref>^{commit}`` to a 40-char hex SHA."
            ),
        ),
    ] = None,
    tokens_consumed: Annotated[
        int | None,
        typer.Option(
            "--tokens-consumed",
            help="Final non-negative token tally to persist before closing.",
        ),
    ] = None,
    no_runtime: Annotated[
        bool,
        typer.Option(
            "--no-runtime",
            help=(
                "Operator waiver for a missing runtime capture. Persists a human "
                "waived EvidenceRecord and lets this close bypass the zero-runtime gate."
            ),
        ),
    ] = False,
    waive: Annotated[
        list[str] | None,
        typer.Option(
            "--waive",
            help=(
                "Operator waiver — gate id to mark waived (repeatable). "
                "Pairs by position with --reason / --decision / --audit. "
                "Modes B and C require a matching --reason; mode C also "
                "requires --decision or --audit. Persists one "
                "EvidenceRecord(produced_by='human', status='waived') per "
                "gate via the daemon evidence.append RPC."
            ),
        ),
    ] = None,
    waive_reason: Annotated[
        list[str] | None,
        typer.Option(
            "--reason",
            help=(
                "Reason text for the matching --waive entry (repeatable). "
                "Length MUST equal the --waive list length when supplied; "
                "use an empty string to leave a slot blank in mode A."
            ),
        ),
    ] = None,
    waive_decision: Annotated[
        list[str] | None,
        typer.Option(
            "--decision",
            help=(
                "Decision URN backing the matching --waive entry "
                "(repeatable; pairs by position). Empty string skips the "
                "slot. Mode C requires at least one of --decision or "
                "--audit per waiver."
            ),
        ),
    ] = None,
    waive_audit: Annotated[
        list[str] | None,
        typer.Option(
            "--audit",
            help=(
                "Audit URN backing the matching --waive entry "
                "(repeatable; pairs by position). Empty string skips the "
                "slot. Mode C requires at least one of --decision or "
                "--audit per waiver."
            ),
        ),
    ] = None,
) -> None:
    """Close a claimed/in-progress wave with an outcome string.

    When ``--commit`` is supplied the ref is resolved via
    ``git rev-parse <ref>^{commit}`` to a canonical 40-char hex SHA and
    persisted on the wave. ``eawf wave show --commit <wave-id>``
    prefers this stored value; absent it falls back to
    :func:`~eawf.workflow.lifecycle.wave_sha.derive_wave_sha` walking
    ``git log --grep "[P##-W##]"``.

    P24-W09 canary: when ``daemon.proxy_enabled=true`` the close
    proxies through the daemon's ``state.mutate`` RPC (typed
    :class:`~eawf.kernel.state.mutations.Mutation` payload with
    ``kind=WAVE_CLOSE``); otherwise the legacy in-process path runs.
    Both paths converge on the same ``state.json`` + ``event.jsonl``
    on-disk shape.

    P28-I01-W11: when ``--waive`` flags are present each named gate is
    waived via :func:`eawf.workflow.lifecycle.waivers.apply_waiver`
    BEFORE the close mutation lands; this guarantees the readiness
    compute (W06) sees the waivers when it scores the closed wave.
    Waivers are operator-only — the active session MUST carry the
    OPERATOR role.

    P30-I05-W09: ``--no-runtime`` is a close-scoped operator waiver for
    missing runtime capture. It writes a human ``EvidenceRecord`` and
    threads ``no_runtime_waiver=True`` into the daemon close mutation.
    """
    from eawf.kernel.store.paths import store_dir as _store_dir
    from eawf.surfaces.cli.scope import resolve_state_path
    from eawf.workflow.lifecycle.criterion_drift import check_wave_criteria_drift
    from eawf.workflow.lifecycle.transitions import close_wave
    from eawf.workflow.verify import compute as compute_readiness

    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    if outcome is None or outcome == "":
        cli_errors.emit_error(
            cli_errors.UserError("--outcome is required for wave close", kind="InvalidInput"),
            flags=flags,
        )
        return
    if tokens_consumed is not None and tokens_consumed < 0:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"--tokens-consumed must be non-negative; got {tokens_consumed}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return

    # W11: parse + validate the per-gate waiver flags BEFORE the close
    # mutation lands. The persistence path runs after the input shape
    # is validated so a bad waiver does not leave the state half-
    # written. ``waive_inputs`` is the empty list when --waive is not
    # supplied.
    try:
        waive_inputs = _parse_waiver_flags(
            waive=waive,
            waive_reason=waive_reason,
            waive_decision=waive_decision,
            waive_audit=waive_audit,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    # Resolve the commit ref BEFORE any state mutation so a bad ref
    # fails the precondition without touching state.json.
    resolved_sha: str | None = None
    if commit_ref is not None:
        try:
            resolved_sha = _resolve_commit_sha(commit_ref)
        except cli_errors.CliError as err:
            cli_errors.emit_error(err, flags=flags)
            return

    # Persist waiver evidence BEFORE the close + readiness compute so the
    # readiness view sees the rows.
    try:
        _reject_disabled_close_waivers(
            ctx=ctx,
            flags=flags,
            wave_id=wave_id,
            waive_inputs=waive_inputs,
        )
        _persist_close_waiver_inputs(
            ctx=ctx,
            flags=flags,
            wave_id=wave_id,
            waive_inputs=waive_inputs,
            no_runtime=no_runtime,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    # W09 daemon-proxy canary: route the close through ``state.mutate``
    # when ``daemon.proxy_enabled=true`` in the merged config. Falls
    # back to the in-process ``_run_mutation`` path transparently when
    # the flag is False (W09 default) or the daemon refuses the kind.
    from eawf.surfaces.cli._mutation import _proxy_enabled

    # One-element sink set by ``_wave_close_via_daemon`` when the close RPC hit
    # a transport error and fell back to the in-process path; read below so the
    # fallback close event stamps ``close_mechanism = "daemon-fallback"``.
    transport_fallback = [False]
    if _proxy_enabled(flags.workspace):
        proxied = _wave_close_via_daemon(
            flags=flags,
            wave_id=wave_id,
            outcome=outcome,
            resolved_sha=resolved_sha,
            tokens_consumed=tokens_consumed,
            no_runtime_waiver=no_runtime,
            transport_fallback=transport_fallback,
        )
        if proxied:
            _warn_on_zero_eu_close(ctx, wave_id=wave_id, waived=no_runtime)
            return

    drift_warnings: list[str] = []
    close_succeeded = [False]
    readiness_holder: list[CloseReadiness] = []
    from eawf.surfaces.cli._mutation import CloseMechanism

    close_mechanism_holder: list[CloseMechanism] = []

    def _close_and_pin(state: State) -> None:
        state_path = resolve_state_path(flags.workspace)
        evidence_store_dir = _store_dir(state_path)
        config_root = _config_root_for_state_path(state_path)
        repo_root = _resolve_repo_root_for_drift(flags.workspace)
        anchor_for_sha = repo_root if repo_root is not None else config_root
        # Band-conditional enforcement: the helper loads + band-narrows the
        # active verify block so the direct-write fallback matches the daemon
        # close gate (a non-band wave stays advisory under a band-scoped
        # profile).
        verify_block = _resolve_close_verify_block(
            wave_id,
            state,
            repo_root=anchor_for_sha,
            config_root=config_root,
        )
        # W20 daemonless teeth: mirror the daemon close gate (deterministic
        # pre-flight + verdict read gate) BEFORE the bypass-door mechanism stamp.
        if verify_block is not None and verify_block.enforce:
            readiness = _run_daemonless_close_preflight(
                state,
                wave_id=wave_id,
                state_path=state_path,
                repo_root=anchor_for_sha,
                config_root=config_root,
                waived=no_runtime,
            )
            if readiness is not None:
                readiness_holder.append(readiness)
        # Daemonless bypass door + close-mechanism stamp (W18 -> W25 wiring): a
        # gate-bearing daemonless close needs --no-runtime; the mechanism stamps.
        _stamp_close_mechanism(
            state,
            wave_id=wave_id,
            state_path=state_path,
            waived=no_runtime,
            transport_fallback=transport_fallback[0],
            holder=close_mechanism_holder,
        )
        wave = close_wave(
            state,
            wave_id=wave_id,
            outcome=outcome,
            tokens_consumed=tokens_consumed,
        )
        if resolved_sha is not None:
            wave.commit = resolved_sha
        if verify_block is None or not verify_block.enforce:
            try:
                readiness = compute_readiness(
                    wave_id,
                    state=state,
                    store_dir=evidence_store_dir,
                    repo_root=anchor_for_sha,
                    config_root=config_root,
                    load_profile_verify=False,
                )
            except KeyError as exc:
                logger.warning(f"close_advisory wave={wave_id!r} status='skip' err={exc!s}")
            else:
                readiness_holder.append(readiness)
                _log_advisory_criteria(wave_id, readiness)
        if repo_root is not None:
            drift_warnings.extend(check_wave_criteria_drift(wave, repo_root))
        close_succeeded[0] = True

    _run_mutation(
        ctx,
        command="wave close",
        args={
            "id": wave_id,
            "outcome": outcome,
            "commit": resolved_sha,
            "tokens_consumed": tokens_consumed,
            "no_runtime_waiver": no_runtime,
        },
        scope_id=wave_id,
        text=f"wave close {wave_id} outcome={outcome!r}",
        envelope=lambda: {
            "wave": wave_id,
            "outcome": outcome,
            "commit": resolved_sha,
            "tokens_consumed": tokens_consumed,
            "no_runtime_waiver": no_runtime,
            "readiness_warnings_count": (
                len(readiness_holder[0].warnings) if readiness_holder else 0
            ),
        },
        mutate=_close_and_pin,
        extras_factory=lambda: {
            "readiness_warnings_count": (
                len(readiness_holder[0].warnings) if readiness_holder else 0
            ),
            "close_mechanism": (close_mechanism_holder[0] if close_mechanism_holder else "daemon"),
        },
        closure_kind=True,
    )
    if close_succeeded[0]:
        for glob in drift_warnings:
            print(
                f"warning: wave {wave_id} success_criteria reference path glob "
                f"that resolves to zero files: {glob!r}",
                file=sys.stderr,
            )
        if readiness_holder:
            readiness = readiness_holder[0]
            for warning in readiness.warnings:
                print(f"advisory: wave {wave_id} readiness: {warning}", file=sys.stderr)
        _warn_on_zero_eu_close(ctx, wave_id=wave_id, waived=no_runtime)


@wave_app.command("fail")
def wave_fail_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID to fail.")],
    reason: Annotated[
        str | None, typer.Option("--reason", help="Failure reason (required).")
    ] = None,
) -> None:
    """Mark a claimed/in-progress wave as failed with *reason*."""
    from eawf.workflow.lifecycle.transitions import fail_wave

    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    if reason is None or reason == "":
        cli_errors.emit_error(
            cli_errors.UserError("--reason is required for wave fail", kind="InvalidInput"),
            flags=flags,
        )
        return
    _run_mutation(
        ctx,
        command="wave fail",
        args={"id": wave_id, "reason": reason},
        scope_id=wave_id,
        text=f"wave fail {wave_id} reason={reason!r}",
        envelope=lambda: {"wave": wave_id, "reason": reason},
        mutate=lambda state: _wrap_no_return(fail_wave(state, wave_id=wave_id, reason=reason)),
        mutation_kind=MutationKind.WAVE_FAIL,
        params={"wave_id": wave_id, "reason": reason},
    )


@wave_app.command("release")
def wave_release_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID to release back to pending.")],
    reason: Annotated[
        str | None,
        typer.Option("--reason", help="Optional reason recorded on the lifecycle log line."),
    ] = None,
) -> None:
    """Release a claimed/in-progress wave back to pending (the inverse of claim).

    Clears the claim binding (``claim_session_id`` / ``worktree_id``) and
    drops the wave from ``current.active_wave_ids`` so another runtime can
    re-claim it. The parent iter is left untouched. A CLOSED/FAILED/
    ABANDONED wave is rejected (terminal status cannot be un-claimed); an
    already-PENDING wave is a no-op.
    """
    from eawf.workflow.lifecycle.transitions import release_wave

    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    _run_mutation(
        ctx,
        command="wave release",
        args={"id": wave_id, "reason": reason},
        scope_id=wave_id,
        text=f"wave release {wave_id} reason={reason!r}",
        envelope=lambda: {"wave": wave_id, "reason": reason},
        mutate=lambda state: _wrap_no_return(release_wave(state, wave_id=wave_id, reason=reason)),
        mutation_kind=MutationKind.WAVE_RELEASE,
        params={"wave_id": wave_id, "reason": reason},
    )


_WAVE_UPDATE_FILES_ALLOWED_STATUSES: frozenset[WaveStatus] = frozenset(
    {WaveStatus.PENDING, WaveStatus.CLAIMED}
)


@wave_app.command("update")
def wave_update_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID whose file_scopes are being updated.")],
    files: Annotated[
        str | None,
        typer.Option(
            "--files",
            help="Comma-separated file globs that REPLACE the wave's file_scopes.",
        ),
    ] = None,
    add_file: Annotated[
        str | None,
        typer.Option(
            "--add-file",
            help="Comma-separated file globs to append to file_scopes (dedup, preserve order).",
        ),
    ] = None,
    remove_file: Annotated[
        str | None,
        typer.Option(
            "--remove-file",
            help="Comma-separated file globs to drop from file_scopes (missing entries ignored).",
        ),
    ] = None,
) -> None:
    """Mutate a PENDING/CLAIMED wave's ``file_scopes``.

    Reactive scope shifts ("we found we need to touch X too") flow through
    this verb. Exactly one of ``--files`` / ``--add-file`` / ``--remove-file``
    must be passed. CLOSED waves are rejected with ``VALIDATION_FAILED`` (4)
    so historical scope cannot be rewritten.

    Exit codes:
        0: file_scopes updated.
        2: wave id is unknown (``NOT_FOUND``).
        3: invalid args — bad wave id, no mode selected, multiple modes, or
           empty file list (``INVALID_INPUT``).
        4: wave is not in {PENDING, CLAIMED} — typically CLOSED
           (``VALIDATION_FAILED``).
    """
    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    selected = [opt for opt in (files, add_file, remove_file) if opt is not None]
    if len(selected) != 1:
        cli_errors.emit_error(
            cli_errors.UserError(
                "exactly one of --files / --add-file / --remove-file must be passed",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    if files is not None:
        mode = "set"
        raw = files
    elif add_file is not None:
        mode = "add"
        raw = add_file
    else:
        assert remove_file is not None  # mutually-exclusive guard above
        mode = "remove"
        raw = remove_file
    file_list = [tok.strip() for tok in raw.split(",") if tok.strip()]
    if not file_list:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"--{'files' if mode == 'set' else f'{mode}-file'} requires at least one path",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return

    result: dict[str, Any] = {}

    def _mutator(state: State) -> None:
        wave = state.waves.get(wave_id)
        if wave is None:
            raise cli_errors.UserError(f"unknown wave: {wave_id!r}", kind="NotFound")
        if wave.status not in _WAVE_UPDATE_FILES_ALLOWED_STATUSES:
            raise cli_errors.ValidationError(
                f"wave {wave_id!r} is {wave.status.value!r}; "
                f"update --files only allowed on PENDING or CLAIMED waves"
            )
        before = list(wave.file_scopes)
        if mode == "set":
            after = list(file_list)
            added = [p for p in after if p not in before]
            removed = [p for p in before if p not in after]
        elif mode == "add":
            after = list(before)
            added = []
            for path in file_list:
                if path not in after:
                    after.append(path)
                    added.append(path)
            removed = []
        else:  # remove
            drop = set(file_list)
            after = [p for p in before if p not in drop]
            added = []
            removed = [p for p in before if p in drop]
        wave.file_scopes = after
        result["before"] = before
        result["after"] = after
        result["added"] = added
        result["removed"] = removed
        logger.info(
            f"update_wave_files wave={wave_id} mode={mode!r} added={added} removed={removed}"
        )

    _run_mutation(
        ctx,
        command="wave update",
        args={"id": wave_id, "mode": mode, "files": file_list},
        scope_id=wave_id,
        text_factory=lambda: (
            f"wave update {wave_id} mode={mode} "
            f"before={len(result['before'])} after={len(result['after'])} "
            f"added={len(result['added'])} removed={len(result['removed'])}"
        ),
        envelope_factory=lambda: {
            "wave": wave_id,
            "mode": mode,
            "file_scopes": result["after"],
            "added": result["added"],
            "removed": result["removed"],
        },
        mutate=_mutator,
    )
