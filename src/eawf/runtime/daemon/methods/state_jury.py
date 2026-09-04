"""Auditor and juror plumbing the enforcing wave-close gate spawns through.

The close gate's last-resort tiers convene a fresh auditor or a
cross-vendor jury. This module owns the spawn factory those tiers bind,
the lane-availability pre-check that degrades a sub-quorum host, the
auditor-session write-back, the on-disk spec load, and the durable
close-attempt evidence context an auditor is bound to.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    AgentSessionRole,
    StoreKind,
)
from eawf.kernel.state.models import (
    State,
    Wave,
)
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.kernel.validate.strict import validate_state
from eawf.runtime.daemon.methods import (
    DaemonValidationError,
)
from eawf.workflow.lifecycle.transitions import (
    LifecycleError,
)

if TYPE_CHECKING:
    from eawf.observability.eval.jury import JurorBallot
    from eawf.observability.eval.jury_validation import BlockAuthority
    from eawf.platform.profiles.models import VerifyBlock
    from eawf.workflow.dispatch.verdict import DurableAuditContext
from eawf.runtime.daemon.methods.state_context import read_state

logger = logging.getLogger(__name__)


def persist_auditor_session_snapshot(
    snapshot: State,
    *,
    state_path: Path,
    wave_id: str,
) -> None:
    """Merge one close auditor's session row into canonical state.

    Close verification runs without the long-lived state lock. The verdict
    producer still needs to expose its live and terminal auditor session to
    Watch, so its callbacks pass the in-memory verification snapshot here.
    Only the qualified ``<wave>::audit`` session rows are copied; unrelated
    state from the pre-flight snapshot is never written back over concurrent
    mutations.

    Args:
        snapshot: Verification snapshot carrying the auditor session update.
        state_path: Canonical repository state path.
        wave_id: Wave whose qualified auditor session may be copied.

    Raises:
        DaemonValidationError: When the merged state fails strict validation.
    """
    from eawf.runtime.lock import portalock

    auditor_scope = f"{wave_id}::audit"
    changed = {
        session_id: session
        for session_id, session in snapshot.agent_sessions.items()
        if session.role is AgentSessionRole.AUDITOR and session.scope_id == auditor_scope
    }
    if not changed:
        return
    with portalock.acquire(state_path, timeout=5.0):
        current, _payload = read_state(state_path)
        current.agent_sessions.update(changed)
        current.updated_at = datetime.now(UTC)
        payload = current.model_dump(mode="json")
        post = validate_state(payload, strict_optional=False)
        if post.state is None or post.violations:
            details = list(post.schema_errors[:3])
            details.extend(violation.code for violation in post.violations[:3])
            raise DaemonValidationError(
                "validation_failed: auditor session snapshot invalid: " + "; ".join(details)
            )
        atomic_write_json_locked(state_path, payload)
    logger.info(f"persist_auditor_session_snapshot wave={wave_id!r} sessions={len(changed)}")


#: The three disjoint juror runtime families the cross-vendor jury convenes
#: one auditor from each of (plugin-manifest spelling). Mirrored here so the
#: lane-availability pre-check + the per-runtime spawn factory read the same
#: source as :data:`eawf.observability.eval.cross_vendor_jury.JURY_RUNTIME_FAMILIES`.
_JURY_RUNTIME_TRIPLE: dict[str, str] = {
    "claude-code": "claude",
    "codex": "codex",
    "opencode": "opencode",
}


def cross_vendor_lanes_ready(*, quorum: int) -> bool:
    """Return whether enough juror CLI binaries resolve on PATH to convene.

    A real cross-vendor jury needs at least *quorum* of the three disjoint
    vendor CLIs installed on the host; a box with only the claude CLI cannot
    cast independent cross-vendor ballots, so the close path degrades to the
    single-auditor gate rather than forcing every enforcing close to the
    operator. Reads only binary presence (:func:`shutil.which`) -- never a
    credential -- so it does not weaken the env-scrub / jail floor.

    Args:
        quorum: Minimum number of juror lanes whose CLI must resolve.

    Returns:
        ``True`` when at least *quorum* of the juror CLI binaries resolve.
    """
    import shutil

    from eawf.runtime.runtimes.selector import select_adapter

    available = 0
    for runtime in _JURY_RUNTIME_TRIPLE:
        try:
            binary = select_adapter(runtime).cli_binary
        except ValueError:
            continue
        if shutil.which(binary) is not None:
            available += 1
    ready = available >= quorum
    logger.info(f"cross_vendor_lanes_ready available={available} quorum={quorum} ready={ready}")
    return ready


def jury_spawn_factory(
    state: State,
    wave: Wave,
    *,
    repo_root: Path,
    timeout_seconds: float = 600.0,
    events_path: Path | None = None,
) -> Any:
    """Return the production per-runtime spawn factory for the jury convener.

    Binds, per juror runtime, that vendor's
    :meth:`~eawf.runtime.runtimes.adapter.RuntimeAdapter.spawn_session` with the
    runtime's OWN per-tier model (resolved via
    :func:`eawf.workflow.dispatch.routing.model_for_runtime`) + the wave's
    sandbox deny-list, so each juror spawns its own vendor's CLI behind the
    safety floor. Tests monkeypatch this factory builder to return recording
    stubs so no real subprocess runs.

    When *events_path* is supplied, each juror spawn also streams its stdout
    LIVE to the auditor's Watch roster row: the spawn binds an ``on_chunk``
    callback that batches output off the count / wall-clock budget
    (:func:`~eawf.runtime.daemon.dispatch_runner._chunk_should_flush`, W19) and
    persists each batch bus-less to the auditor session scope
    (:func:`~eawf.workflow.dispatch.verdict._auditor_scope_id`) via
    :func:`~eawf.runtime.daemon.dispatch_runner.persist_agent_output_chunk`. The
    close gate severs the :class:`MethodContext` (only paths cross into
    ``run_oracle``), so the store-poll tail -- not the bus -- surfaces the chunk;
    a call site that threads no *events_path* (the spec-jury builder) spawns
    unchanged, with no live tail.

    Args:
        state: Validated state -- read for the wave's sandbox deny-list + role.
        wave: The wave under audit (supplies role + effort for model routing).
        repo_root: Repository root the juror spawns run in.
        timeout_seconds: Per-juror spawn wall-clock ceiling.
        events_path: Optional ``event.jsonl`` path -- when set, juror stdout
            streams live to the auditor's Watch row; when ``None``, no live tail.

    Returns:
        A :data:`~eawf.observability.eval.cross_vendor_jury.SpawnFactory` -- a
        ``runtime -> SpawnFn`` callable.
    """
    from eawf.kernel.config.layered import resolve_runtime_tier_models
    from eawf.kernel.state.enums import AgentSessionRole as _Role
    from eawf.kernel.state.enums import EffortBucket as _Effort
    from eawf.runtime.daemon.dispatch_runner import (
        _chunk_should_flush,
        persist_agent_output_chunk,
    )
    from eawf.runtime.runtimes.adapter import SpawnResult
    from eawf.runtime.runtimes.selector import select_adapter
    from eawf.runtime.sandbox.policy import resolve_denied_tools
    from eawf.workflow.dispatch.llm_assist import SpawnFn
    from eawf.workflow.dispatch.routing import model_for_runtime
    from eawf.workflow.dispatch.verdict import _auditor_scope_id

    role = wave.agent_role if wave.agent_role is not None else _Role.AUDITOR
    effort = wave.effort_bucket if wave.effort_bucket is not None else _Effort.M
    denied = sorted(resolve_denied_tools(state.sandbox_policies, wave_id=wave.id))
    cwd = str(repo_root)
    runtime_models = resolve_runtime_tier_models(repo_root)
    chunk_scope = _auditor_scope_id(wave.id)

    def _factory(runtime: str) -> SpawnFn:
        triple = _JURY_RUNTIME_TRIPLE.get(runtime, "claude")
        model = model_for_runtime(role, effort, triple, runtime_models=runtime_models)
        adapter = select_adapter(runtime)

        async def _spawn(prompt: str) -> SpawnResult:
            if events_path is None:
                return await adapter.spawn_session(
                    prompt,
                    model=model,
                    cwd=cwd,
                    denied_tools=denied,
                    timeout=timeout_seconds,
                )
            # Live juror-stdout tail: batch chunks off the W19 count /
            # time budget and persist each batch bus-less to the auditor session
            # scope so the Watch store-poll tail renders the juror's own words.
            chunk_buffer: list[str] = []
            chunk_seq = [0]
            last_chunk_flush = [time.monotonic()]

            def _flush_chunk_buffer() -> None:
                if not chunk_buffer:
                    return
                persist_agent_output_chunk(
                    events_path,
                    scope_id=chunk_scope,
                    session_id=None,
                    seq=chunk_seq[0],
                    text="".join(chunk_buffer),
                )
                chunk_seq[0] += 1
                chunk_buffer.clear()
                last_chunk_flush[0] = time.monotonic()

            async def _on_chunk(line: str) -> None:
                chunk_buffer.append(line)
                if _chunk_should_flush(
                    buffered=len(chunk_buffer),
                    elapsed_s=time.monotonic() - last_chunk_flush[0],
                ):
                    _flush_chunk_buffer()

            try:
                return await adapter.spawn_session(
                    prompt,
                    model=model,
                    cwd=cwd,
                    denied_tools=denied,
                    timeout=timeout_seconds,
                    on_chunk=_on_chunk,
                )
            finally:
                _flush_chunk_buffer()

        return _spawn

    return _factory


def load_wave_spec(wave_id: str, *, repo_root: Path) -> Any:
    """Return the on-disk :class:`WaveSpec` for *wave_id*, or ``None``.

    Resolves ``.ea/specs/<phase>/<iter>/<wave>.md``
    (:func:`eawf.kernel.spec.writer.spec_file_path`) and validates its YAML
    frontmatter through :class:`~eawf.kernel.spec.wave.WaveSpec`. Returns
    ``None`` on any miss -- file absent, frontmatter unparseable, or schema
    invalid -- so a banded close with an authoring gap degrades to a
    safe-skip rather than raising out of the close path. The spec-jury
    producer treats a ``None`` spec as nothing to score.

    Args:
        wave_id: The canonical ``P##-I##-W##`` wave id.
        repo_root: Repository root the ``.ea/specs`` tree lives under.

    Returns:
        The validated :class:`~eawf.kernel.spec.wave.WaveSpec`, or ``None``.
    """
    from eawf.kernel.spec.wave import WaveSpec
    from eawf.kernel.spec.writer import spec_file_path
    from eawf.workflow.audit_dsl.kinds.verify_implements import _parse_frontmatter

    spec_path = spec_file_path(wave_id, repo_root=repo_root)
    if not spec_path.exists():
        logger.debug(f"load_wave_spec wave={wave_id} status=skip reason=no-spec-file")
        return None
    try:
        frontmatter = _parse_frontmatter(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug(f"load_wave_spec wave={wave_id} status=skip err={exc!s}")
        return None
    if frontmatter is None or frontmatter.get("kind") != "WaveSpec":
        return None
    try:
        return WaveSpec.model_validate(frontmatter)
    except ValidationError as exc:
        logger.warning(f"load_wave_spec wave={wave_id} status=invalid errors={exc.error_count()}")
        return None


def build_durable_audit_context(  # noqa: C901
    *,
    state_path: Path,
    close_attempt_id: str,
    wave: Wave,
) -> DurableAuditContext:
    """Bind a durable auditor to exact close inputs and persisted receipts.

    This helper is intentionally called after the deterministic oracle loop.
    It re-reads canonical state plus the append-only GateReceipt store, so an
    in-memory callback result that was never durably written cannot enter the
    auditor prompt as proof.

    Raises:
        LifecycleError: When the close attempt is absent, a bound receipt is
            invalid for the frozen attempt, or a required deterministic
            criterion lacks a passing persisted GateReceipt.
    """
    from eawf.kernel.state.enums import GateReceiptResult
    from eawf.kernel.state.urn import build as build_urn
    from eawf.kernel.store.kinds.gate_receipt import GateReceipt, canonical_gate_digest
    from eawf.workflow.dispatch.verdict import DurableAuditContext, DurableAuditCriterion

    canonical_state, _ = read_state(state_path)
    attempt = canonical_state.close_attempts.get(close_attempt_id)
    if attempt is None:
        raise LifecycleError(f"unknown close attempt: {close_attempt_id!r}")

    criteria_by_id = {
        criterion.id: criterion for criterion in wave.success_criteria if criterion.required
    }
    wanted = set(attempt.gate_receipt_ids)
    receipts_by_criterion: dict[str, list[str]] = {}
    receipt_path = store_path(state_path, StoreKind.GATE_RECEIPT)
    if wanted and receipt_path.is_file():
        for line in receipt_path.read_bytes().splitlines():
            if not line:
                continue
            try:
                envelope = Envelope.model_validate(orjson.loads(line))
                if envelope.id not in wanted:
                    continue
                receipt = GateReceipt.model_validate(envelope.payload)
            except orjson.JSONDecodeError, ValidationError:
                continue
            if (
                receipt.result is not GateReceiptResult.PASS
                or receipt.scope_id != attempt.wave_id
                or receipt.integration_id != attempt.integration_id
                or receipt.integrated_sha != attempt.integrated_sha
                or receipt.tree_sha != attempt.tree_sha
                or receipt.contract_digest != canonical_gate_digest(attempt.spec_digest)
                or receipt.criteria_digest != canonical_gate_digest(attempt.criteria_digest)
                or receipt.gate_manifest_digest
                != canonical_gate_digest(attempt.gate_manifest_digest)
                or receipt.policy_digest != canonical_gate_digest(attempt.policy_digest)
                or receipt.dependency_binding_digest
                != canonical_gate_digest(attempt.dependency_binding_digest)
                or receipt.runner_environment_digest
                != canonical_gate_digest(attempt.runner_environment_digest)
            ):
                raise LifecycleError(
                    f"gate receipt does not match frozen close attempt: {envelope.id!r}"
                )
            if receipt.criterion_id is None:
                raise LifecycleError(f"gate receipt has no criterion binding: {envelope.id!r}")
            criterion = criteria_by_id.get(receipt.criterion_id)
            if (
                envelope.kind is not StoreKind.GATE_RECEIPT
                or criterion is None
                or receipt.gate_id not in criterion.gate_ids
                or receipt.gate_id not in attempt.required_gate_ids
            ):
                raise LifecycleError(
                    f"gate receipt has invalid criterion/gate binding: {envelope.id!r}"
                )
            urn = build_urn(
                "store",
                owner=receipt.scope_id,
                id=f"{StoreKind.GATE_RECEIPT.value}/{receipt.id}",
            )
            receipts_by_criterion.setdefault(receipt.criterion_id, []).append(urn)

    criteria: list[DurableAuditCriterion] = []
    for criterion in wave.success_criteria:
        if not criterion.required:
            continue
        deterministic = criterion.evidence_kind == "deterministic"
        receipt_urns = tuple(dict.fromkeys(receipts_by_criterion.get(criterion.id, [])))
        if deterministic and not receipt_urns:
            raise LifecycleError(
                f"required deterministic criterion has no persisted GateReceipt: {criterion.id!r}"
            )
        criteria.append(
            DurableAuditCriterion(
                criterion_id=criterion.id,
                text=criterion.text,
                deterministic=deterministic,
                gate_receipt_urns=receipt_urns,
            )
        )
    return DurableAuditContext(
        wave_id=wave.id,
        close_attempt_id=attempt.id,
        integration_id=attempt.integration_id,
        integrated_sha=attempt.integrated_sha,
        tree_sha=attempt.tree_sha,
        spec_digest=attempt.spec_digest,
        criteria_digest=attempt.criteria_digest,
        gate_manifest_digest=attempt.gate_manifest_digest,
        policy_digest=attempt.policy_digest,
        runner_digest=attempt.runner_environment_digest,
        dependency_binding_digest=attempt.dependency_binding_digest,
        criteria=tuple(criteria),
    )


def resolve_jury_block_authority(
    state: State,
    *,
    state_path: Path,
    verify_block: VerifyBlock | None,
) -> BlockAuthority:
    """Compute the jury's earned block authority for the close gate -- pure read.

    The TRUST-4 staged gate: a cross-vendor jury earns the right to BLOCK a
    close (rather than merely log an advisory veto) only once it has cleared its
    trust floors on eawf's own distribution. This helper scores the jury against
    the ground-truth validation substrate and returns the resulting
    :class:`~eawf.observability.eval.jury_validation.BlockAuthority`:

    - it builds the validation cohort
      (:func:`~eawf.observability.eval.jury_validation.build_jury_validation_cohort`)
      and the verbosity-bias probe over the persisted substrate;
    - it scores the validation report
      (:func:`~eawf.observability.eval.jury_validation.validate_jury`) and the
      verbosity report
      (:func:`~eawf.observability.eval.jury_validation.measure_verbosity_bias`);
    - it maps the profile's ``verify.jury_authority`` leaf onto the eval-module
      :class:`~eawf.observability.eval.jury_validation.JuryAuthorityConfig` and
      runs the earned-authority gate
      (:func:`~eawf.observability.eval.jury_validation.jury_block_authority`).

    Default-advisory by construction: the validation substrate is empty today
    (no labelled cohort, no recorded ballots), so the cohort is honest-empty,
    the validation report is :attr:`JuryValidationStatus.INSUFFICIENT`, and the
    gate returns
    :attr:`~eawf.observability.eval.jury_validation.BlockAuthority.ADVISORY` --
    an enforcing close never blocks on an uncalibrated jury.

    Args:
        state: Loaded, validated state supplying the wave tree the cohort is
            anchored against. Read-only here.
        state_path: Path to ``state.json``; the verdict + gold-label stores
            resolve under its sibling ``store/`` directory.
        verify_block: The resolved verify block (a
            :class:`~eawf.platform.profiles.models.VerifyBlock`) whose
            ``jury_authority`` leaf supplies the trust floors. ``None`` (or a
            block with the default leaf) uses the safe advisory-leaning floors.

    Returns:
        The :class:`~eawf.observability.eval.jury_validation.BlockAuthority`
        the jury has earned -- ``BLOCKING`` only when every trust floor clears,
        else ``ADVISORY``.
    """
    from eawf.observability.eval.jury_validation import (
        BlockAuthority,
        build_jury_validation_cohort,
        jury_block_authority,
        measure_verbosity_bias,
        validate_jury,
    )
    from eawf.observability.eval.jury_validation import (
        JuryAuthorityConfig as EvalJuryAuthorityConfig,
    )

    if verify_block is None:
        return BlockAuthority.ADVISORY
    leaf = verify_block.jury_authority
    authority_config = EvalJuryAuthorityConfig(
        min_labeled_waves=leaf.min_labeled_waves,
        known_bad_catch_lb_floor=leaf.known_bad_catch_lb_floor,
        unanimous_pass_ceiling=leaf.unanimous_pass_ceiling,
    )
    cohort = build_jury_validation_cohort(state, state_path)
    # An empty cohort short-circuits to advisory rather than scoring
    # (validate_jury would otherwise need the ballot substrate a later wave
    # builds); this read stays honest-empty rather than fabricating a
    # calibrated jury.
    if not cohort.silver and not cohort.gold:
        return BlockAuthority.ADVISORY
    ballots_by_wave = _load_recorded_ballots(state_path)
    # A labelled cohort with NO recorded ballots means the jury has never
    # actually run on those waves -- uncalibrated, so advisory. Scoring it
    # instead would trip validate_jury's phantom-jury hard error and crash
    # every enforcing close the moment the first auditor verdict settles into
    # the silver cohort; that hard error stays reserved for the calibration
    # path, where ballots are expected on record.
    if not ballots_by_wave:
        return BlockAuthority.ADVISORY
    report = validate_jury(cohort, ballots_by_wave=ballots_by_wave)
    verbosity = measure_verbosity_bias([])
    return jury_block_authority(report, verbosity, authority_config)


def _load_recorded_ballots(state_path: Path) -> dict[str, tuple[JurorBallot, ...]]:
    """Return the persisted per-wave juror ballots from the ballot store.

    Un-idled by P30-I23-W17: the convener now appends one ballot row per
    juror to ``jury_ballot.jsonl``, so the calibration substrate accrues
    from every convened jury. The close-path caller still resolves
    advisory authority on an empty map, so a repo with no convened jury
    keeps the honest-empty behaviour.
    """
    from eawf.observability.eval.jury_validation import read_recorded_ballots

    return read_recorded_ballots(state_path)
