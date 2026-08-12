"""Derived close-readiness view for v0.4 verify spine.

# noqa: EAWF010 close-readiness projection accreted the W13 floor-scoping seam
# (per-tool argv narrowing + policy-to-required mapping); the cohesive
# floor-scope helpers split into a sibling ``floor_scope`` module in a later
# polish pass, once the P30-I23 close-gate work settles.

The :func:`compute` function returns a :class:`CloseReadiness` projection
of three inputs:

* typed :class:`~eawf.kernel.spec.common.CriterionSpec` /
  :class:`~eawf.kernel.spec.common.GateSpec` definitions attached to
  the scope, loaded via :func:`_load_criterion_specs` /
  :func:`_load_gate_specs`. Both loaders read the typed wave row
  directly — criteria from
  :attr:`~eawf.kernel.state.models.Wave.success_criteria` (retyped at
  ``1.6 -> 1.7``) and gates from
  :attr:`~eawf.kernel.state.models.Wave.gates` (added at
  ``1.7 -> 1.8``) — so the wave row IS the on-disk source of truth for
  typed specs; tests may still monkeypatch the loaders to inject
  synthetic specs without constructing a full wave row;
* the legacy :attr:`~eawf.kernel.state.models.Wave.success_criteria`
  string list (still load-bearing in v0.4.0);
* :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord` rows in
  ``<store_dir>/evidence.jsonl`` whose ``scope_id`` matches the wave's
  derived SHA (see :func:`eawf.workflow.lifecycle.wave_sha.derive_wave_sha`).

W08 layers the **deterministic floor** on top of W06: criteria whose
``evidence_kind == "deterministic"`` have their gates compiled via
:func:`eawf.workflow.verify.compile.compile_gate` and executed live via
:func:`eawf.workflow.audit_dsl.runner.run_checks`. Jury / attested
criteria stay on the W06 evidence-row path until v0.4.1 lands the
jury + attestation subsystems. Waivers pre-empt the live run
across both flavours so operator overrides survive the floor.

``compute`` is read-only at the state-mutation boundary: it does not
mutate *state* nor write any file. The W08 deterministic floor DOES
invoke subprocesses for live gate execution; those subprocesses are
scored as gate results, not persisted. The function is idempotent on
its inputs (calling it twice on the same tuple returns equal results
for the evidence path; the deterministic floor's idempotency depends
on the gate's own idempotency, e.g. ``git status``). Wave-close seams
use :func:`compute` as advisory by default; profiles that set
``verify.enforce=true`` promote a non-ready result to a lifecycle
rejection.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import orjson

from eawf.kernel.config.schema import VerifyConfig, VerifyWaiverMode
from eawf.kernel.spec.common import GRANDFATHERED_KIND, CriterionSpec, GateSpec
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State, Wave
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.platform.profiles.models import FloorCheck, VerifyBlock
from eawf.workflow.audit_dsl.kinds.backlog_resolution import (
    BACKLOG_RESOLUTION_KIND,
    check_backlog_resolution,
)
from eawf.workflow.audit_dsl.models import CheckSpec
from eawf.workflow.audit_dsl.runner import run_checks
from eawf.workflow.lifecycle._errors import LifecycleError, check_disabled_waiver_policy
from eawf.workflow.lifecycle.wave_sha import derive_wave_sha
from eawf.workflow.verify.compile import compile_floor_pack, compile_gate
from eawf.workflow.verify.models import (
    CloseReadiness,
    CriterionView,
    GateResult,
)

logger = logging.getLogger(__name__)


def _load_criterion_specs(scope_id: str, state: State) -> list[CriterionSpec]:
    """Return typed CriterionSpec rows attached to *scope_id*.

    Reads the typed :attr:`~eawf.kernel.state.models.Wave.success_criteria`
    field directly: the ``1.6 -> 1.7`` migration retyped it from a free-form
    string list into ``list[CriterionSpec]``, so the wave row IS the on-disk
    source of truth for criterion specs (no separate spec-cache round-trip
    is needed). A wave with no criteria yields ``[]``; a non-wave scope (iter
    / phase) likewise yields ``[]`` until those scopes carry their own
    criteria.

    The helper stays a separate function (rather than inlined into
    :func:`compute`) so tests can still monkeypatch it to feed synthetic
    specs into the deterministic-floor integration and the
    waiver-aware rollup without constructing a full wave row.

    Args:
        scope_id: Wave / iter / phase URN. Only wave scopes carry
            criteria today; other scopes resolve to ``[]``.
        state: Validated state model the wave row is read from.

    Returns:
        The wave's typed criterion rows, or ``[]`` when the scope is not
        a known wave or carries no criteria.
    """
    wave = state.waves.get(scope_id)
    if wave is None:
        return []
    return list(wave.success_criteria)


def _load_gate_specs(scope_id: str, state: State) -> list[GateSpec]:
    """Return typed GateSpec rows attached to *scope_id*.

    Symmetric to :func:`_load_criterion_specs` — reads the typed
    :attr:`~eawf.kernel.state.models.Wave.gates` field directly: the
    ``1.7 -> 1.8`` migration added it as ``list[GateSpec]`` (default
    ``[]``), so the wave row IS the on-disk source of truth for gate
    specs (no separate spec-cache round-trip is needed). A wave with no
    gates yields ``[]``; a non-wave scope (iter / phase) likewise yields
    ``[]`` until those scopes carry their own gates.

    The helper stays a separate function (rather than inlined into
    :func:`compute` and the daemon close gate) so tests can still
    monkeypatch it to feed synthetic specs into the deterministic-floor
    integration and the criterion-gate-ref validation without
    constructing a full wave row.

    Args:
        scope_id: Wave / iter / phase URN. Only wave scopes carry gates
            today; other scopes resolve to ``[]``.
        state: Validated state model the wave row is read from.

    Returns:
        The wave's typed gate rows, or ``[]`` when the scope is not a
        known wave or carries no gates.
    """
    wave = state.waves.get(scope_id)
    if wave is None:
        return []
    return list(wave.gates)


def _read_evidence_rows(store_dir: Path) -> list[EvidenceRecord]:
    """Decode + validate every row in ``<store_dir>/evidence.jsonl``.

    Returns an empty list when the file does not exist (the v0.4
    evidence store is created lazily on first ``evidence.append``).
    Malformed rows are skipped with a debug log — a malformed
    evidence row is an advisory failure, not a close blocker (W06's
    whole point).

    Args:
        store_dir: ``<state_dir>/store/`` (resolved by callers via
            :func:`eawf.kernel.store.paths.store_dir`).

    Returns:
        List of validated :class:`EvidenceRecord` rows in file order.
    """
    evidence_path = store_dir / f"{StoreKind.EVIDENCE.value}.jsonl"
    if not evidence_path.exists():
        return []
    rows: list[EvidenceRecord] = []
    try:
        raw = evidence_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug(f"_read_evidence_rows status=os-error path={evidence_path!s} err={exc!s}")
        return []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            envelope = Envelope.model_validate_json(line)
            if envelope.kind != StoreKind.EVIDENCE:
                continue
            record = EvidenceRecord.model_validate(envelope.payload)
        except (orjson.JSONDecodeError, ValueError) as exc:
            logger.debug(f"_read_evidence_rows status=row-skip err={exc!s}")
            continue
        rows.append(record)
    return rows


def _filter_evidence_for_scope(
    rows: list[EvidenceRecord], *, scope_id: str
) -> list[EvidenceRecord]:
    """Return the evidence rows whose ``scope_id`` matches *scope_id*.

    SHA-bound freshness is the W08 contract (compile-gate); W06 keeps
    the filter scope-id-equal so the readiness view is deterministic
    on the rows currently visible.

    Args:
        rows: All evidence rows read off disk.
        scope_id: Wave id we are computing readiness for.

    Returns:
        Filtered rows in original order.
    """
    return [row for row in rows if row.scope_id == scope_id]


def _is_stale_waiver(row: EvidenceRecord, *, current_sha: str | None) -> bool:
    """Return ``True`` when *row* is a waiver against an outdated wave SHA.

    Waivers are SHA-bound: each waiver row carries
    ``metrics["wave_sha"]`` at attest-time. Once the wave SHA advances
    (e.g. an executor adds a new commit), prior waivers are stale and
    MUST NOT count toward the readiness rollup — the gate needs a
    fresh attestation against the post-advance code.

    The check fires only for ``status="waived"`` rows; non-waiver
    rows are scope-id-bound (W06 contract) and freshness is the W08
    compile-gate's responsibility.

    Args:
        row: One scope-filtered :class:`EvidenceRecord`.
        current_sha: Current wave SHA derived via
            :func:`eawf.workflow.lifecycle.wave_sha.derive_wave_sha`.
            ``None`` (git unavailable, no commit yet) makes every waiver
            stale: absence is not proof of freshness.

    Returns:
        ``True`` iff *row* is a waiver whose stamped ``wave_sha``
        disagrees with *current_sha*.
    """
    if row.status != "waived":
        return False
    if current_sha is None:
        return True
    if row.metrics is None:
        return True
    stamped = row.metrics.get("wave_sha")
    if stamped is None:
        return True
    return stamped != current_sha


def _gate_status_from_evidence(gate: GateSpec, evidence: list[EvidenceRecord]) -> tuple[str, bool]:
    """Return ``(status, was_waived)`` for *gate* given its evidence rows.

    Rolls up evidence per gate by looking at the most recent row whose
    ``refs`` list mentions the gate id. The most recent row wins so
    repeat runs converge. Returns ``("blocked", False)`` when no
    evidence row references the gate.

    The caller is responsible for filtering out SHA-stale waiver rows
    via :func:`_is_stale_waiver` BEFORE calling this helper; that
    keeps the per-gate roll-up free of branching on the freshness
    contract.

    Args:
        gate: Gate spec to score.
        evidence: Scope-filtered evidence rows with stale waivers
            already removed.

    Returns:
        Tuple of (rolled-up gate status, whether the gate was waived).
    """
    relevant = [row for row in evidence if gate.id in row.refs]
    if not relevant:
        return ("blocked", False)
    latest = relevant[-1]
    if latest.status == "waived":
        return ("pass", True)
    if latest.status == "pass":
        return ("pass", False)
    if latest.status == "fail":
        return ("fail", False)
    # "blocked" — pass-through.
    return ("blocked", False)


def _criterion_status_from_gates(
    criterion: CriterionSpec,
    gate_results: list[GateResult],
    *,
    waived_gate_ids: list[str] | None = None,
) -> str:
    """Roll up *criterion* status from its per-gate :class:`GateResult` list.

    Algorithm:

    * If the criterion has an explicit ``waiver_reason``, the rolled-up
      status is ``waived`` regardless of gates.
    * If *waived_gate_ids* covers every gate on *criterion*, the rolled-
      up status is ``waived`` (W11: a fully-waived criterion surfaces
      its waiver explicitly so renderers can flag operator overrides).
    * Any required ``fail`` => ``fail``.
    * Any required ``blocked`` (and no fails) => ``blocked``.
    * Empty required-gate set with ``required=True`` criterion =>
      ``pending`` (no evidence to score yet).
    * Otherwise => ``pass``.

    Args:
        criterion: The criterion under evaluation.
        gate_results: Per-gate results scored against the criterion.
        waived_gate_ids: Gate ids the W11 waiver path cleared (i.e.
            rows whose status was ``waived`` rather than ``pass``).
            ``None`` is treated as an empty list.

    Returns:
        One of ``pass`` / ``fail`` / ``blocked`` / ``pending`` /
        ``waived`` — see :data:`~eawf.workflow.verify.models.CriterionStatus`.
    """
    if criterion.waiver_reason:
        return "waived"
    if not gate_results:
        return "pending" if criterion.required else "pass"
    waived_set = set(waived_gate_ids or [])
    if waived_set and all(r.gate_id in waived_set for r in gate_results):
        return "waived"
    required_results = [r for r in gate_results if r.status != "pass"]
    if any(r.status == "fail" for r in required_results):
        return "fail"
    if any(r.status == "blocked" for r in required_results):
        return "blocked"
    return "pass"


def _latest_waiver_for_gate(
    gate: GateSpec, evidence: list[EvidenceRecord]
) -> EvidenceRecord | None:
    """Return the most-recent waiver row that references *gate*, or ``None``.

    The caller is responsible for filtering out SHA-stale waiver rows
    via :func:`_is_stale_waiver` BEFORE invoking this helper; only
    fresh waivers reach the lookup so the per-gate roll-up never
    surfaces an outdated operator override.

    Args:
        gate: Gate spec whose id we match against ``EvidenceRecord.refs``.
        evidence: Scope-filtered evidence rows with stale waivers
            already removed.

    Returns:
        The latest :class:`EvidenceRecord` whose ``status == "waived"``
        AND whose ``refs`` list mentions ``gate.id``. ``None`` when
        no such row exists.
    """
    waivers = [r for r in evidence if gate.id in r.refs and r.status == "waived"]
    return waivers[-1] if waivers else None


def _run_deterministic_gate(
    gate: GateSpec,
    criterion: CriterionSpec,
    *,
    runner_cwd: Path,
) -> str:
    """Compile + execute *gate* via the W15-hardened audit-DSL runner.

    Delegates the shape transform to
    :func:`eawf.workflow.verify.compile.compile_gate` and the live
    subprocess + diff-base + scope-resolution machinery to
    :func:`eawf.workflow.audit_dsl.runner.run_checks`. Returns the
    rolled-up :data:`~eawf.workflow.verify.models.GateStatus` literal
    so the caller can stamp it onto a :class:`GateResult` row
    untouched.

    The function defends against the case where
    :func:`compile_gate` legitimately returns ``None`` for a
    deterministic criterion (e.g. ``command_exit_zero`` gate with no
    usable ``argv``) by mapping that to ``"blocked"`` — the readiness
    view surfaces the gap as a non-pass criterion without raising out
    of the read-only :func:`compute` boundary.

    Args:
        gate: Typed gate spec attached to *criterion*.
        criterion: Parent criterion (read for ``id`` + ``evidence_kind``
            only; the deterministic-floor caller has already verified
            ``criterion.evidence_kind == "deterministic"``).
        runner_cwd: Working directory for the subprocess + git
            diff-base + scope resolution. Threaded through from
            :func:`compute`'s ``repo_root``.

    Returns:
        One of ``"pass"`` / ``"fail"`` / ``"blocked"``.
    """
    compiled = compile_gate(gate, criterion=criterion)
    if compiled is None:
        logger.debug(
            f"_run_deterministic_gate gate_id={gate.id!r} status=blocked reason=compile-none"
        )
        return "blocked"
    results = run_checks([compiled], cwd=runner_cwd)
    result = results[0]
    status = result.status or ("pass" if result.passed else "fail")
    logger.debug(
        f"_run_deterministic_gate gate_id={gate.id!r} status={status!r} "
        f"evidence_kind={criterion.evidence_kind!r}"
    )
    return status


def _build_spec_views(
    criterion_specs: list[CriterionSpec],
    gate_specs: list[GateSpec],
    evidence: list[EvidenceRecord],
    *,
    runner_cwd: Path,
    prevalidated_gate_ids: Collection[str] = (),
) -> tuple[list[CriterionView], list[str]]:
    """Convert typed CriterionSpec / GateSpec into :class:`CriterionView` rows.

    Returns the per-criterion views plus the list of waived gate ids
    (collected across all criteria so :class:`CloseReadiness` can
    surface them at the top level).

    Per the W08 deterministic-floor contract, scoring branches on
    ``criterion.evidence_kind``:

    * ``"deterministic"`` — the gate is executed live via
      :func:`_run_deterministic_gate` (compile-gate +
      W15-hardened runner). A fresh waiver row on the gate
      pre-empts the live run and yields ``("pass", was_waived=True)``
      so W11's waiver semantics still apply on top of the live
      floor.
    * ``"jury"`` / ``"attested"`` — defer to
      :func:`_gate_status_from_evidence`; jury votes + operator
      attestations land in v0.4.1+, so v0.4.0 reads them as
      evidence rows recorded by external means.

    Args:
        criterion_specs: Typed criterion rows attached to the scope.
        gate_specs: Typed gate rows attached to the scope.
        evidence: Already scope-filtered evidence rows. The caller is
            responsible for filtering out SHA-stale waiver rows BEFORE
            invoking this helper (see :func:`_is_stale_waiver`).
        runner_cwd: Working directory for the deterministic-floor
            subprocess execution. Threaded from
            :func:`compute`'s ``repo_root`` so wave-anchored diff-base
            + scope-resolution land in the right git tree.
        prevalidated_gate_ids: Deterministic gates already executed and
            passed by the enforcing close oracle for the same frozen
            inputs. These gates project as pass without a second
            subprocess execution.

    Returns:
        ``(views, waived_gate_ids)``.
    """
    gates_by_criterion: dict[str, list[GateSpec]] = {}
    for gate in gate_specs:
        gates_by_criterion.setdefault(gate.criterion_id, []).append(gate)

    views: list[CriterionView] = []
    waived: list[str] = []
    for criterion in criterion_specs:
        # Grandfathered criteria (legacy strings the 1.6->1.7 migration wrapped
        # into typed rows) carry no gates and no authored evidence kind, so they
        # render through the advisory legacy path (:func:`_build_legacy_views`)
        # rather than the gated spec path -- scoring them here would surface
        # every un-gated legacy criterion as a blocking ``pending``.
        if criterion.kind == GRANDFATHERED_KIND:
            continue
        gates = gates_by_criterion.get(criterion.id, [])
        gate_results: list[GateResult] = []
        per_criterion_waived: list[str] = []
        for gate in gates:
            if criterion.evidence_kind == "deterministic":
                if gate.id in prevalidated_gate_ids:
                    status = "pass"
                    was_waived = False
                else:
                    # Waiver pre-empts the live run so W11's operator
                    # override semantics survive the deterministic floor.
                    waiver = _latest_waiver_for_gate(gate, evidence)
                    if waiver is not None:
                        status = "pass"
                        was_waived = True
                    else:
                        status = _run_deterministic_gate(
                            gate,
                            criterion,
                            runner_cwd=runner_cwd,
                        )
                        was_waived = False
            else:
                # jury / attested -> the evidence-row path remains
                # authoritative until v0.4.1's jury + attestation
                # subsystems land.
                status, was_waived = _gate_status_from_evidence(gate, evidence)
            gate_results.append(
                GateResult(
                    gate_id=gate.id,
                    # status literal is closed; cast not needed because
                    # both branches return one of the four GateStatus
                    # values by construction.
                    status=status,  # type: ignore[arg-type]
                )
            )
            if was_waived:
                waived.append(gate.id)
                per_criterion_waived.append(gate.id)
        criterion_status = _criterion_status_from_gates(
            criterion,
            gate_results,
            waived_gate_ids=per_criterion_waived,
        )
        views.append(
            CriterionView(
                id=criterion.id,
                source="spec",
                # Same rationale as the gate status cast above.
                status=criterion_status,  # type: ignore[arg-type]
                gate_results=gate_results,
                required=criterion.required,
            )
        )
    return views, waived


def _config_root_for_readiness(config_root: Path | None, repo_root: Path) -> Path:
    """Return the config anchor used to resolve active profile bodies."""
    return config_root if config_root is not None else repo_root


def _merge_verify_blocks(blocks: list[VerifyBlock]) -> VerifyBlock | None:
    """Merge active profile verify blocks into one effective block.

    Union-merges the list-valued fields (``floor_checks`` concatenated;
    ``argv_allowlist`` / ``uiux_bands`` / ``jury_vendors`` deduplicated,
    first-occurrence order preserved), OR-folds the boolean gating bits
    (``enforce`` / ``cross_vendor_jury`` / ``odr_blocking`` /
    ``require_iter_audit_accepted``), and takes the last contributor's scalar
    dials (``timeout_class_seconds``,
    ``juror_wall_clock_seconds``, ``odr_floor``, ``jury_authority``,
    ``checkpoint``). ``waiver_mode`` is last-contributor-wins unless any
    contributor disables waivers, which is absorbing. Because the other
    scalar dials are last-contributor-wins, a
    downstream profile that sets ``checkpoint.checkpoint_mode: barrier`` overrides
    an upstream ``optimistic`` block. The merged ``enforce`` is the *fleet*
    opt-in; band-conditional resolution (:func:`resolve_wave_verify_block`)
    narrows it per wave at the close seam so a single enforcing profile does
    not gate every wave.

    The merge rebuilds the block field by field, so EVERY field the model carries
    has to be listed here: one left out is silently reset to its default, and a
    profile that set it has no way to tell. That is what happened to
    ``juror_wall_clock_seconds``, ``odr_floor``, ``odr_blocking`` and
    ``jury_authority`` -- declared as config, dropped on merge, latent only
    because no shipped profile happened to set them. Add a field to
    :class:`~eawf.platform.profiles.models.VerifyBlock` and it must be added
    here in the same change.
    """
    if not blocks:
        return None
    floor_checks = []
    argv_allowlist: list[str] = []
    seen_argv: set[str] = set()
    uiux_bands: list[str] = []
    seen_bands: set[str] = set()
    jury_vendors: list[str] = []
    seen_vendors: set[str] = set()
    timeout_class_seconds: dict[Literal["quick", "standard", "slow", "very_slow"], int] = {}
    has_timeout_overrides = False
    waiver_mode: Literal["A", "B", "C", "disabled"] = (
        "disabled"
        if any(block.waiver_mode == "disabled" for block in blocks)
        else blocks[-1].waiver_mode
    )
    checkpoint = blocks[-1].checkpoint
    juror_wall_clock_seconds = blocks[-1].juror_wall_clock_seconds
    odr_floor = blocks[-1].odr_floor
    jury_authority = blocks[-1].jury_authority
    enforce = False
    cross_vendor_jury = False
    odr_blocking = False
    require_iter_audit_accepted = False
    for block in blocks:
        floor_checks.extend(block.floor_checks)
        for argv_head in block.argv_allowlist:
            if argv_head in seen_argv:
                continue
            argv_allowlist.append(argv_head)
            seen_argv.add(argv_head)
        for band in block.uiux_bands:
            if band in seen_bands:
                continue
            uiux_bands.append(band)
            seen_bands.add(band)
        for vendor in block.jury_vendors:
            if vendor in seen_vendors:
                continue
            jury_vendors.append(vendor)
            seen_vendors.add(vendor)
        if block.timeout_class_seconds is not None:
            has_timeout_overrides = True
            timeout_class_seconds.update(block.timeout_class_seconds)
        enforce = enforce or block.enforce
        cross_vendor_jury = cross_vendor_jury or block.cross_vendor_jury
        odr_blocking = odr_blocking or block.odr_blocking
        require_iter_audit_accepted = (
            require_iter_audit_accepted or block.require_iter_audit_accepted
        )
    return VerifyBlock(
        floor_checks=floor_checks,
        argv_allowlist=argv_allowlist,
        timeout_class_seconds=timeout_class_seconds if has_timeout_overrides else None,
        waiver_mode=waiver_mode,
        enforce=enforce,
        cross_vendor_jury=cross_vendor_jury,
        juror_wall_clock_seconds=juror_wall_clock_seconds,
        uiux_bands=uiux_bands,
        jury_vendors=jury_vendors,
        odr_floor=odr_floor,
        odr_blocking=odr_blocking,
        require_iter_audit_accepted=require_iter_audit_accepted,
        jury_authority=jury_authority,
        checkpoint=checkpoint,
    )


def _enabled_profile_ids(
    merged: dict[str, Any],
    *,
    strict_config: bool,
) -> list[str] | None:
    """Validate and return the enabled profile ids from merged config."""
    profiles = merged.get("profiles")
    if not isinstance(profiles, dict):
        if strict_config:
            raise ValueError(f"profiles config must be a mapping, got {type(profiles).__name__}")
        return None
    enabled_raw = profiles.get("enabled", [])
    if not isinstance(enabled_raw, list):
        if strict_config:
            raise ValueError(f"profiles.enabled must be a list, got {type(enabled_raw).__name__}")
        logger.warning(
            f"_load_active_verify_block status=skip reason=bad-enabled "
            f"type={type(enabled_raw).__name__!r}"
        )
        return None
    profile_ids: list[str] = []
    for profile_id_raw in enabled_raw:
        if isinstance(profile_id_raw, str):
            profile_ids.append(profile_id_raw)
            continue
        if strict_config:
            raise ValueError(
                f"profiles.enabled entries must be strings, got {type(profile_id_raw).__name__}"
            )
        logger.warning(
            f"_load_active_verify_block status=skip-profile reason=bad-id "
            f"type={type(profile_id_raw).__name__!r}"
        )
    return profile_ids


def _load_active_verify_block(
    scope_id: str,
    state: State | None,
    *,
    repo_root: Path,
    config_root: Path | None = None,
    strict_config: bool = False,
) -> VerifyBlock | None:
    """Return the active profile's :class:`VerifyBlock`, or ``None``.

    The helper resolves ``profiles.enabled`` from layered config,
    loads each profile body through the profile discovery layer, and
    folds the present ``verify`` leaves into one effective block.
    Tests can still monkeypatch this function to feed a synthetic
    :class:`VerifyBlock` into the floor-pack integration.

    Args:
        scope_id: Wave / iter / phase URN. Reserved for future per-scope
            profile attachments.
        state: Validated state model. Reserved for future per-scope
            profile attachments.
        repo_root: Repository root used when no separate config root is
            supplied.
        config_root: Optional config/profile-discovery anchor. Close
            paths pass the directory that owns ``.ea/config.yaml`` so
            ``EA_STATE`` recovery shells and daemon cross-repo calls do
            not accidentally read the daemon process cwd.

    Returns:
        Merged :class:`VerifyBlock`, or ``None`` when no enabled
        profile contributes one.
    """
    del scope_id, state  # reserved for scoped profile attachments
    from eawf.kernel.config.layered import merge_config
    from eawf.platform.profiles.loader import load_profile

    anchor = _config_root_for_readiness(config_root, repo_root)
    try:
        merged, sources = merge_config(workspace=anchor, repo=anchor)
    except (OSError, ValueError, KeyError) as exc:
        if strict_config:
            raise
        logger.warning(f"_load_active_verify_block status=skip err={exc!s}")
        return None
    profile_ids = _enabled_profile_ids(merged, strict_config=strict_config)
    if profile_ids is None:
        return None
    blocks: list[VerifyBlock] = []
    for profile_id_raw in profile_ids:
        try:
            body = load_profile(profile_id_raw, workspace=anchor)
        except (OSError, ValueError, KeyError) as exc:
            if strict_config:
                raise
            logger.warning(
                f"_load_active_verify_block status=skip-profile "
                f"profile={profile_id_raw!r} err={exc!s}"
            )
            continue
        if body.verify is not None:
            blocks.append(body.verify)
    merged_block = _merge_verify_blocks(blocks)
    return _overlay_repo_verify_leaves(merged_block, merged, source_map=sources)


def _verify_leaf_explicitly_supplied(
    key: str,
    *,
    verify_section: dict[str, Any],
    source_map: dict[str, str] | None,
) -> bool:
    """Return whether a verify leaf came from outside built-in defaults."""
    if source_map is None:
        return key in verify_section
    return source_map.get(f"verify.{key}") not in {None, "built-in"}


def _overlay_repo_verify_leaves(
    block: VerifyBlock | None,
    merged_config: dict[str, Any],
    *,
    source_map: dict[str, str] | None = None,
) -> VerifyBlock | None:
    """Fold the repo-layer ``verify:`` leaves onto *block*.

    Profiles ship the verify spine's defaults; ``.ea/config.yaml`` may opt THIS
    repo into ODR blocking (``verify: {odr_blocking: true}``) once its new-iter
    ODR is honest. A *gate* layer can only tighten: the overlay ORs onto the
    profile value, so a repo cannot silently loosen a blocking profile.

    ``juror_wall_clock_seconds`` is not a gate but a resource bound -- how long a
    close-time auditor may run before it is killed -- so the repo value is taken
    in either direction. It is carried here because it previously was NOT: the
    overlay dropped every leaf except ``odr_blocking``, so a repo that configured
    a longer audit ceiling got the 600s default anyway, and the config line read
    as behaviour that did not exist. A killed auditor writes no verdict, and the
    close gate reads "no verdict" as a refusal, so the wave could never close.
    """
    verify_section = merged_config.get("verify")
    if not isinstance(verify_section, dict):
        return block
    wall_clock = verify_section.get("juror_wall_clock_seconds")
    schema_section = {
        key: value for key, value in verify_section.items() if key != "juror_wall_clock_seconds"
    }
    repo_verify = VerifyConfig.model_validate(schema_section)

    if block is None and not any(
        _verify_leaf_explicitly_supplied(
            key,
            verify_section=verify_section,
            source_map=source_map,
        )
        for key in verify_section
    ):
        return None
    if block is None:
        block = VerifyBlock()

    updates: dict[str, Any] = {}
    if repo_verify.odr_blocking and not block.odr_blocking:
        updates["odr_blocking"] = True
    if repo_verify.require_iter_audit_accepted and not block.require_iter_audit_accepted:
        updates["require_iter_audit_accepted"] = True
    if (
        _verify_leaf_explicitly_supplied(
            "waiver_mode",
            verify_section=verify_section,
            source_map=source_map,
        )
        and block.waiver_mode != "disabled"
    ):
        updates["waiver_mode"] = repo_verify.waiver_mode
    if (
        _verify_leaf_explicitly_supplied(
            "juror_wall_clock_seconds",
            verify_section=verify_section,
            source_map=source_map,
        )
        and isinstance(wall_clock, int | float)
        and not isinstance(wall_clock, bool)
        and wall_clock > 0
    ):
        updates["juror_wall_clock_seconds"] = float(wall_clock)
    if not updates:
        return block
    return block.model_copy(update=updates)


def load_active_verify_block(
    scope_id: str,
    state: State,
    *,
    repo_root: Path,
    config_root: Path | None = None,
) -> VerifyBlock | None:
    """Return the active merged verify block through strict config loading."""
    return _load_active_verify_block(
        scope_id,
        state,
        repo_root=repo_root,
        config_root=config_root,
        strict_config=True,
    )


def load_active_waiver_mode(
    scope_id: str,
    state: State | None,
    *,
    repo_root: Path,
    config_root: Path | None = None,
) -> VerifyWaiverMode:
    """Resolve the effective waiver policy through strict layered config.

    Unlike the advisory verify loader, this security boundary never converts
    malformed config or a broken enabled profile into permissive mode B.

    Args:
        scope_id: Wave whose policy is being resolved.
        state: Optional validated state snapshot. Current profile resolution
            is scope-independent, so non-state stores may pass ``None``.
        repo_root: Repository root for profile discovery.
        config_root: Optional layered-config anchor.

    Returns:
        Effective waiver mode; ``B`` when no verify block contributes one.
    """
    block = _load_active_verify_block(
        scope_id,
        state,
        repo_root=repo_root,
        config_root=config_root,
        strict_config=True,
    )
    return "B" if block is None else block.waiver_mode


def resolve_wave_verify_block(
    verify_block: VerifyBlock | None,
    wave: Wave,
) -> VerifyBlock | None:
    """Resolve the merged verify block to its band-conditional form for *wave*.

    A profile opts into **band-scoped** enforcement by declaring a non-empty
    :attr:`~eawf.platform.profiles.models.VerifyBlock.uiux_bands`. For such a
    block, verify enforcement is conditional on the wave's UI/UX band rather
    than a fleet-wide flip: the merged block records the *intent*
    (``enforce: true`` + ``cross_vendor_jury: true``), and this resolver
    turns it on or off per wave:

    * a **band** wave (UI-surface ``file_scopes`` OR a ``uiux_bands`` token
      match, per :func:`eawf.workflow.dispatch.spec_jury.wave_in_uiux_band`)
      keeps the merged block's enforcement bits as authored -- so a
      band-scoped profile resolves to ``enforce=True`` +
      ``cross_vendor_jury=True`` and the close routes through the gate;
    * a **fleet high-risk** wave (its own gate set classifies
      :attr:`~eawf.kernel.state.enums.RiskTier.HIGH` or
      :attr:`~eawf.kernel.state.enums.RiskTier.UI` per
      :func:`eawf.workflow.verify.oracle.classify_risk_tier` -- it carries a
      jury / cross-vendor / visual-band gate) ALSO keeps enforcement on even
      when it is NOT in a UI band: a jury-gated wave's ground truth needs the
      jury, so narrowing its ``enforce`` to advisory-only would let a
      high-risk close slip the gate. The band narrowing is for low-risk
      non-band waves, never for a wave whose own gate set demands a jury;
    * a **verdict-always** wave (its risk-weighted requirement per
      :func:`eawf.workflow.dispatch.verdict.verdict_requirement` is
      ``"always"`` -- a large effort bucket, a judgment-heavy ``agent_role``,
      or a security-scoped surface) ALSO keeps enforcement on even when it is
      neither in a UI band nor gate-classified high-risk: a fresh auditor
      verdict is mandatory for such a wave, so narrowing its ``enforce`` to
      advisory-only would de-scope the very gate that produces that verdict.
      This is the third preservation arm -- the band narrowing must never
      down-grade a wave whose verdict is required unconditionally;
    * a **non-band, non-high-risk, non-verdict-always** wave resolves to
      ``enforce=False`` (and, consequently, no jury) -- it closes exactly as
      it does today, advisory-only.

    A block that does NOT declare ``uiux_bands`` is a whole-fleet enforce
    profile (the pre-W06 shape) and is returned untouched: its operator
    deliberately gates every wave, so there is no band to narrow to. A
    ``None`` input (no active profile contributes a verify block) and a
    fleet-advisory block (``enforce=False``) likewise pass through unchanged.

    The list-valued config (``uiux_bands`` / ``jury_vendors`` /
    ``floor_checks`` / ``argv_allowlist``) and the ``waiver_mode`` /
    ``timeout_class_seconds`` overrides are carried through untouched; only
    the two gating booleans are wave-conditional.

    Args:
        verify_block: The fleet-merged active verify block, or ``None``.
        wave: The wave being closed -- read for ``file_scopes`` / ``id`` /
            ``title`` band membership only.

    Returns:
        The band-conditional :class:`VerifyBlock` for *wave*, or ``None``
        when the input was ``None``.
    """
    from eawf.kernel.state.enums import RiskTier
    from eawf.workflow.dispatch.spec_jury import wave_in_uiux_band
    from eawf.workflow.dispatch.verdict import verdict_requirement
    from eawf.workflow.verify.oracle import classify_risk_tier

    if verify_block is None or not verify_block.enforce:
        return verify_block
    if not verify_block.uiux_bands:
        # Not band-scoped -- a whole-fleet enforce profile gates every wave.
        return verify_block
    if wave_in_uiux_band(wave, bands=verify_block.uiux_bands):
        return verify_block
    # Band check failed -- but a wave whose OWN gate set is high-risk
    # (a jury / cross-vendor / visual-band gate -> RiskTier.HIGH | UI) must
    # keep enforcement even outside a UI band: its ground truth needs the
    # jury, so narrowing it to advisory would let a high-risk close slip the
    # gate. Only low-risk non-band waves (MECH / MED) narrow to advisory.
    risk_tier = classify_risk_tier(list(wave.gates))
    if risk_tier in {RiskTier.HIGH, RiskTier.UI}:
        logger.debug(
            f"resolve_wave_verify_block wave={wave.id!r} band=False "
            f"risk_tier={risk_tier.value} enforce=True high_risk_keeps_enforce=True"
        )
        return verify_block
    # Band + gate-risk checks both missed -- but a wave whose risk-weighted
    # verdict requirement is "always" (large effort, judgment-heavy role, or a
    # security-scoped surface) needs a mandatory fresh auditor verdict, so
    # narrowing it to advisory would de-scope the gate that produces it. This
    # third preservation arm holds such a wave un-narrowed; only genuinely
    # mechanical (sampled / skip) non-band waves fall through to advisory.
    if verdict_requirement(wave) == "always":
        logger.debug(
            f"resolve_wave_verify_block wave={wave.id!r} band=False "
            f"risk_tier={risk_tier.value} verdict_requirement=always "
            f"enforce=True verdict_always_keeps_enforce=True"
        )
        return verify_block
    narrowed = verify_block.model_copy(update={"enforce": False, "cross_vendor_jury": False})
    logger.debug(
        f"resolve_wave_verify_block wave={wave.id!r} band=False "
        f"risk_tier={risk_tier.value} "
        f"enforce={narrowed.enforce} cross_vendor_jury={narrowed.cross_vendor_jury}"
    )
    return narrowed


def _floor_check_waived(name: str, fresh_evidence: list[EvidenceRecord]) -> bool:
    """Return True when a fresh operator waiver covers floor check *name*.

    Mirrors the spec-gate waiver match in :func:`_gate_status_from_evidence`:
    a floor check is waived when the most recent fresh evidence row that
    references its name (floor-check names address waivers the same way a
    gate id does) carries ``status="waived"``. SHA-stale waivers are dropped
    by the caller via :func:`_is_stale_waiver` before this runs, so a waiver
    suppresses the (often long) floor run only while it stays fresh for the
    wave's current SHA.

    Args:
        name: Floor-check name (doubles as the waiver gate id).
        fresh_evidence: Scope-filtered evidence rows with SHA-stale waivers
            already removed.

    Returns:
        True when the latest referencing row is a fresh ``waived`` row.
    """
    relevant = [row for row in fresh_evidence if name in row.refs]
    return bool(relevant) and relevant[-1].status == "waived"


def _is_under(path: str, top: str) -> bool:
    """Return True when *path*'s first path component equals *top*.

    Args:
        path: A repo-relative POSIX path (a file or directory scope).
        top: Top-level directory to test membership against (e.g.
            ``"src"`` or ``"tests"``).

    Returns:
        True iff *path* lives under *top*.
    """
    parts = PurePosixPath(path.rstrip("/")).parts
    return bool(parts) and parts[0] == top


def _mirror_src_to_test_dir(path: str) -> str | None:
    """Map a ``src/<pkg>/<sub>/<file>`` scope to its ``tests/<sub>/`` dir.

    The mirror stays at *directory* granularity so it never has to guess a
    ``test_<name>.py`` filename: a ``src`` scope resolves to the test package
    that mirrors its containing directory. Returns ``None`` when *path* is not
    a ``src/<pkg>/...`` path carrying at least one component below the package,
    so the caller skips an unmappable scope rather than fabricating a target.

    Args:
        path: A repo-relative POSIX ``src`` scope (file or directory).

    Returns:
        The mirrored ``tests/...`` directory (trailing ``/``), or ``None``
        when *path* cannot be mirrored.
    """
    parts = PurePosixPath(path.rstrip("/")).parts
    if len(parts) < 3 or parts[0] != "src":
        return None
    sub = parts[2:]
    # Drop a trailing file component (a name carrying a suffix) so the mirror
    # lands on the containing test package, not a src filename.
    dir_parts = sub[:-1] if PurePosixPath(sub[-1]).suffix else sub
    if not dir_parts:
        return None
    return str(PurePosixPath("tests", *dir_parts)) + "/"


def _pytest_scope_targets(file_scopes: list[str], *, runner_cwd: Path) -> list[str]:
    """Resolve *file_scopes* to the pytest target set for a scoped floor.

    Prefers the test paths already declared in *file_scopes* (changed-test
    selection); when none are present, mirrors each ``src`` scope to its test
    package (:func:`_mirror_src_to_test_dir`) and keeps only the mirrors that
    exist under *runner_cwd* so a scope with no mirror test package does not
    turn into a false pytest failure.

    Args:
        file_scopes: The wave's declared ``file_scopes``.
        runner_cwd: Repo root the floor subprocess runs in; mirror existence
            is checked against it.

    Returns:
        Sorted unique pytest targets, or ``[]`` when nothing maps (the caller
        then keeps the whole-tree argv).
    """
    declared = sorted({p.rstrip("/") for p in file_scopes if _is_under(p, "tests")})
    if declared:
        return declared
    mirrored: set[str] = set()
    for scope in file_scopes:
        mirror = _mirror_src_to_test_dir(scope)
        if mirror is not None and (runner_cwd / mirror).exists():
            mirrored.add(mirror)
    return sorted(mirrored)


def _scope_floor_argv(
    argv: list[str],
    *,
    scope: str,
    file_scopes: list[str],
    runner_cwd: Path,
) -> list[str]:
    """Rewrite a whole-tree floor *argv* to carry only *file_scopes* targets.

    Honors the floor row's declared ``scope`` (``python.yaml`` ships the
    dev-loop floors as ``scope: touched``): a non-``all`` scope narrows the
    invocation to the wave's ``file_scopes`` per the tool it names, so a
    module-scoped close never runs the whole tree and no longer rejects on
    pre-existing whole-tree noise. ``scope: all`` (schema / migration floors)
    and an empty *file_scopes* fall through unchanged so the full-tree
    gauntlet -- which belongs at iter close -- stays intact.

    Per-tool narrowing:

    * ``pre-commit`` -- drop ``--all-files`` and pass ``--files`` over the
      scope set.
    * ``pytest`` -- append the scoped test targets
      (:func:`_pytest_scope_targets`); an unmappable scope keeps the
      whole-tree argv.
    * ``mypy`` -- replace the whole-tree positional with the ``src`` scopes; a
      scope set that crosses out of ``src`` keeps the whole-tree argv (the
      documented escape).
    * any other file-consuming tool -- append the scope set as trailing
      positional targets.

    Args:
        argv: The compiled floor argv in its whole-tree form.
        scope: The floor row's declared scope literal.
        file_scopes: The wave's declared ``file_scopes``.
        runner_cwd: Repo root the floor subprocess runs in.

    Returns:
        The scoped argv, or *argv* unchanged when the scope is ``all``,
        *file_scopes* is empty, or the named tool has nothing mappable.
    """
    if scope == "all" or not file_scopes:
        return list(argv)
    if "pre-commit" in argv:
        stripped = [token for token in argv if token != "--all-files"]
        return [*stripped, "--files", *sorted(set(file_scopes))]
    if "pytest" in argv:
        targets = _pytest_scope_targets(file_scopes, runner_cwd=runner_cwd)
        return [*argv, *targets] if targets else list(argv)
    if "mypy" in argv:
        src_targets = sorted({p.rstrip("/") for p in file_scopes if _is_under(p, "src")})
        if not src_targets:
            return list(argv)
        head_end = argv.index("mypy") + 1
        flags = [token for token in argv[head_end:] if token.startswith("-")]
        return [*argv[:head_end], *flags, *src_targets]
    return [*argv, *sorted(set(file_scopes))]


def _floor_required(check: FloorCheck | None) -> bool:
    """Return whether a floor row gates the close.

    Mirrors the oracle's deterministic-tier blocking test
    (:mod:`eawf.workflow.verify.oracle`): a floor row blocks only when it is
    both ``required`` and ``policy == "block"``. A ``warn`` / ``advisory`` row
    -- what ``python.yaml`` ships the dev-loop floors as -- is surfaced but
    never flips ``ready``, so pre-existing whole-tree noise no longer forces a
    reflexive waiver. An unmatched row (no floor check by that name) defaults
    to blocking so a compiled spec never silently down-grades.

    Args:
        check: The floor check the compiled spec was built from, or ``None``
            when no row matched by name.

    Returns:
        True when the row gates the close.
    """
    if check is None:
        return True
    return check.required and check.policy == "block"


def _build_floor_views(
    verify_block: VerifyBlock | None,
    *,
    wave: Wave,
    runner_cwd: Path,
    fresh_evidence: list[EvidenceRecord],
) -> list[CriterionView]:
    """Convert the profile-fed floor pack into :class:`CriterionView` rows.

    When the active profile carries a non-empty
    :attr:`VerifyBlock.floor_checks`, each floor check is compiled
    into a :class:`~eawf.workflow.audit_dsl.models.CheckSpec` via
    :func:`compile_floor_pack` and run through
    :func:`~eawf.workflow.audit_dsl.runner.run_checks`. One floor
    check yields one ``CriterionView(source="floor")`` whose single
    gate result mirrors the live run.

    Each floor row's declared ``scope`` is honored before the run: a
    non-``all`` scope narrows the compiled argv to *wave*'s ``file_scopes``
    via :func:`_scope_floor_argv`, so a module-scoped close carries only the
    scoped target set instead of the whole tree. A ``scope: all`` row (the
    schema / migration escape) passes through unchanged. The wave's ``id`` +
    ``file_scopes`` are also threaded onto the scoped spec so the runner's
    diff-base + ``touched`` file-union resolution is wave-anchored.

    Each row's ``policy`` sets whether the resulting view is ``required``
    (:func:`_floor_required`): only a ``policy: block`` row gates the close,
    so a ``policy: warn`` row is surfaced but never flips ``ready`` -- the
    end of the reflexive triple-waive on pre-existing whole-tree noise.

    An absent ``verify`` block or an empty ``floor_checks`` list
    returns ``[]`` so the caller can continue with the legacy /
    spec-only path unchanged.

    A floor check carrying a fresh operator waiver (see
    :func:`_floor_check_waived`) is surfaced as a ``waived``
    ``CriterionView`` WITHOUT running its subprocess — the waiver is the
    operator's explicit attestation, so re-running the (often
    minutes-long) floor command at close time would only burn wall-clock
    and, on the daemon close path, block the event loop while the state
    lock is held. This mirrors how the typed spec-gate path honours the
    same fresh-waiver evidence in :func:`_build_spec_views`.

    Args:
        verify_block: Active profile's :class:`VerifyBlock`, or
            ``None`` when no profile is attached.
        wave: The closing wave — read for ``id`` + ``file_scopes`` so the
            floor invocation can be narrowed to the wave's declared scope.
        runner_cwd: Working directory for the deterministic-floor
            subprocess execution. Threaded from
            :func:`compute`'s ``repo_root``.
        fresh_evidence: Scope-filtered evidence rows (SHA-stale waivers
            already removed) used to suppress waived floor checks.

    Returns:
        Per-floor-check :class:`CriterionView` rows. Empty list when
        the active profile contributes no floor checks.
    """
    if verify_block is None or not verify_block.floor_checks:
        return []
    checks_by_name: dict[str, FloorCheck] = {c.name: c for c in verify_block.floor_checks}
    compiled = compile_floor_pack(
        verify_block.floor_checks,
        allowlist=list(verify_block.argv_allowlist),
    )
    scoped = _scope_compiled_floor_pack(compiled, checks_by_name, wave=wave, runner_cwd=runner_cwd)
    to_run = [c for c in scoped if not _floor_check_waived(c.name, fresh_evidence)]
    ran_views: dict[str, CriterionView] = {}
    for check_spec, result in zip(to_run, run_checks(to_run, cwd=runner_cwd), strict=True):
        # status literal closed by CheckResult validator: pass / fail / blocked.
        status = result.status or ("pass" if result.passed else "fail")
        ran_views[check_spec.name] = CriterionView(
            id=check_spec.name,
            source="floor",
            status=status,
            gate_results=[GateResult(gate_id=check_spec.name, status=status)],
            required=_floor_required(checks_by_name.get(check_spec.name)),
        )
    views: list[CriterionView] = []
    for check_spec in scoped:
        ran = ran_views.get(check_spec.name)
        if ran is not None:
            views.append(ran)
            continue
        # Waived: subprocess skipped. The criterion surfaces ``waived`` so
        # _is_ready clears it; the gate-level status stays ``pass`` because
        # GateStatus has no ``waived`` member.
        views.append(
            CriterionView(
                id=check_spec.name,
                source="floor",
                status="waived",
                gate_results=[GateResult(gate_id=check_spec.name, status="pass")],
                required=_floor_required(checks_by_name.get(check_spec.name)),
            )
        )
    return views


def _scope_compiled_floor_pack(
    compiled: list[CheckSpec],
    checks_by_name: dict[str, FloorCheck],
    *,
    wave: Wave,
    runner_cwd: Path,
) -> list[CheckSpec]:
    """Narrow each declared-scope floor spec to *wave*'s ``file_scopes``.

    A ``scope: all`` floor (or a spec with no matching floor row) passes
    through unchanged so the schema / migration whole-tree gauntlet stays
    intact. A ``scope: touched`` / ``changed`` floor has its argv rewritten by
    :func:`_scope_floor_argv` and the wave's ``id`` + ``file_scopes`` threaded
    onto the spec args so the runner's diff-base + ``touched`` union is
    wave-anchored.

    Args:
        compiled: The floor specs from :func:`compile_floor_pack`.
        checks_by_name: The originating floor rows keyed by name (source of
            the declared ``scope``).
        wave: The closing wave read for ``id`` + ``file_scopes``.
        runner_cwd: Repo root the floor subprocess runs in.

    Returns:
        One :class:`CheckSpec` per input spec, scoped where the declared
        scope is not ``all``.
    """
    file_scopes = list(wave.file_scopes)
    scoped: list[CheckSpec] = []
    for spec in compiled:
        check = checks_by_name.get(spec.name)
        if check is None or check.scope == "all":
            scoped.append(spec)
            continue
        argv = [str(token) for token in spec.args.get("argv", [])]
        new_argv = _scope_floor_argv(
            argv,
            scope=check.scope,
            file_scopes=file_scopes,
            runner_cwd=runner_cwd,
        )
        new_args = {
            **spec.args,
            "argv": new_argv,
            "wave_id": wave.id,
            "wave_file_scopes": file_scopes,
        }
        scoped.append(CheckSpec(kind=spec.kind, name=spec.name, args=new_args))
    return scoped


def _build_backlog_resolution_view(
    scope_id: str,
    state: State,
) -> CriterionView | None:
    """Score the wave-linked backlog items into a close-gate :class:`CriterionView`.

    Runs the :func:`~eawf.workflow.audit_dsl.kinds.backlog_resolution.check_backlog_resolution`
    close-gate kind over the wave's linked backlog items. The view is
    surfaced under ``source="floor"`` (the close-gate baseline family)
    with a single gate result whose id is
    :data:`~eawf.workflow.audit_dsl.kinds.backlog_resolution.BACKLOG_RESOLUTION_KIND`,
    so a wave that leaves a linked backlog item dangling flips the
    criterion to ``fail`` and -- when ``verify.enforce`` is active --
    blocks the close (see :func:`_enforce_readiness`).

    The check is the production caller that un-idles the
    ``backlog_resolution`` registered kind: it fires on every wave whose
    close-readiness is computed, scoring the wave's own dogfood backlog
    links.

    Args:
        scope_id: The closing wave id the backlog items link against.
        state: Validated state model the backlog items are read from.

    Returns:
        A :class:`CriterionView` when the wave links at least one
        backlog item; ``None`` when the wave links none (no view is
        surfaced so an unlinked wave's readiness is byte-unchanged).
    """
    result = check_backlog_resolution(state, wave_id=scope_id)
    if not result.linked_ids:
        return None
    status: Literal["pass", "fail"] = "pass" if result.passed else "fail"
    logger.debug(
        f"_build_backlog_resolution_view wave={scope_id!r} status={status!r} "
        f"linked={len(result.linked_ids)} dangling={len(result.dangling_ids)}"
    )
    return CriterionView(
        id=BACKLOG_RESOLUTION_KIND,
        source="floor",
        status=status,
        gate_results=[GateResult(gate_id=BACKLOG_RESOLUTION_KIND, status=status)],
    )


def _build_legacy_views(wave: Wave) -> tuple[list[CriterionView], list[str]]:
    """Project grandfathered criteria into advisory legacy :class:`CriterionView`.

    Each grandfathered criterion (the ``kind == GRANDFATHERED_KIND`` rows the
    ``1.6 -> 1.7`` migration wrapped from legacy strings) becomes one view with
    ``source="legacy"``, ``status="pass"``, and ``gate_results=None``. Returns
    the per-criterion views plus a list of advisory warning strings — one per
    grandfathered criterion so the readiness rollup can tally the
    legacy-not-gated count without re-walking the views. Authored typed
    criteria are skipped here; they render through the gated spec path
    (:func:`_build_spec_views`).

    Args:
        wave: The closing wave whose grandfathered criteria we project.

    Returns:
        ``(views, warnings)``.
    """
    views: list[CriterionView] = []
    warnings: list[str] = []
    for criterion in wave.success_criteria:
        if criterion.kind != GRANDFATHERED_KIND:
            continue
        views.append(
            CriterionView(
                id=criterion.id,
                source="legacy",
                status="pass",
                gate_results=None,
            )
        )
        warnings.append(f"legacy criterion {criterion.id!r} not gated")
    return views, warnings


def legacy_criterion_count(criteria: list[CriterionSpec]) -> int:
    """Return how many *criteria* are still grandfathered legacy no-ops.

    The active-criteria legacy count is the population the legacy-to-typed
    converter (:func:`eawf.kernel.spec.common.convert_legacy_criterion`)
    exists to drain: a criterion counts as legacy iff its ``kind`` is the
    :data:`~eawf.kernel.spec.common.GRANDFATHERED_KIND` sentinel -- a row the
    ``1.6 -> 1.7`` migration wrapped from a free-form string with no falsifying
    gate. A converted criterion carries
    :data:`~eawf.kernel.spec.common.CONVERTED_KIND` instead, so it drops out of
    this tally and routes through the gated spec path. The count over a fully
    converted criterion set is therefore ZERO -- the binary proof the backfill
    sample asserts.

    Args:
        criteria: The typed criterion rows to tally (a wave's
            ``success_criteria``, or a converter's output sample).

    Returns:
        The number of rows whose ``kind == GRANDFATHERED_KIND``.
    """
    return sum(1 for criterion in criteria if criterion.kind == GRANDFATHERED_KIND)


#: Supplemental tier seam for registered checkout-scoring kinds that the
#: production close oracle runs (via :func:`compile_gate` + :func:`run_checks`)
#: but that the kernel-level ``_GATE_KIND_TIER`` map does not name. This stays
#: as a forward seam: when a new checkout gate kind is registered ahead of its
#: kernel tier assignment, name it here so the close path -- and the BIND-1
#: wired-on sweep that reads :func:`wired_audit_dsl_kinds` -- treats it as
#: production-bound rather than registered-but-idle. ``tui_flow`` and
#: ``journal_chain`` were folded into the canonical ``_GATE_KIND_TIER`` (both
#: T2 structural) so :func:`assign_oracle_tier` is total over them; the seam is
#: now empty because every registered kind has a canonical tier.
_SUPPLEMENTAL_GATE_KIND_TIERS: dict[str, str] = {}


def wired_audit_dsl_kinds() -> frozenset[str]:
    """Return every registered audit-DSL kind that has a production binding.

    A registered kind (from
    :func:`eawf.workflow.audit_dsl.registry.registered_audit_dsl_kinds`)
    is *wired* when the production close path can reach it as a real
    falsifier, proven by one of three bindings:

    * a :data:`eawf.kernel.spec.common._GATE_KIND_TIER` entry -- the
      oracle escalation tier the close gate sorts and scores by;
    * a :data:`_SUPPLEMENTAL_GATE_KIND_TIERS` entry -- a checkout-scoring
      kind the close path runs that the kernel tier map does not yet
      name (``tui_flow``);
    * membership in
      :data:`eawf.workflow.audit_dsl.registry.CLOSE_GATE_KINDS` -- a
      state-scoring close gate this module drives directly
      (``backlog_resolution``, run by :func:`_build_backlog_resolution_view`).

    The BIND-1 idle-contract meta-gate compares this wired set against
    the full registered set: any registered kind absent here ships
    registered-but-idle and reds CI.

    Returns:
        The frozenset of registered kind strings with a production
        binding.
    """
    from eawf.kernel.spec.common import _GATE_KIND_TIER
    from eawf.workflow.audit_dsl.registry import CLOSE_GATE_KINDS

    return frozenset(_GATE_KIND_TIER) | frozenset(_SUPPLEMENTAL_GATE_KIND_TIERS) | CLOSE_GATE_KINDS


def _not_ready_criteria(criteria: list[CriterionView]) -> list[str]:
    """Return compact ``criterion_id:status`` strings for blocking non-ready criteria.

    Only ``required`` criteria gate the close, so a non-required (advisory)
    criterion that failed is omitted here -- it is surfaced in the view but
    never reported as a close blocker.
    """
    return [
        f"{view.id}:{view.status}"
        for view in criteria
        if view.required and view.status not in ("pass", "waived")
    ]


def _enforce_readiness(
    *,
    scope_id: str,
    readiness: CloseReadiness,
    verify_block: VerifyBlock | None,
    deferred_criterion_ids: frozenset[str] = frozenset(),
) -> None:
    """Raise when the active profile makes close readiness mandatory.

    Args:
        scope_id: Wave id being closed.
        readiness: Rolled-up readiness view for the scope.
        verify_block: Active profile verify configuration, or ``None``.
        deferred_criterion_ids: Criterion ids whose PENDING status does not
            block here because a later close phase owns their enforcement —
            the D-LOCK-SPLIT pre-flight defers un-gated verdict-kind
            criteria to the under-lock verdict / jury tier, which produces
            the auditor evidence this rollup would otherwise wait for.
            Empty (the default) keeps the single-pass behaviour.

    Raises:
        LifecycleError: When ``verify.enforce`` is true and
            ``readiness.ready`` is false after the deferral filter.
    """
    if verify_block is None or not verify_block.enforce or readiness.ready:
        return
    blocked = [
        entry
        for entry in _not_ready_criteria(readiness.criteria)
        if not (entry.endswith(":pending") and entry.split(":", 1)[0] in deferred_criterion_ids)
    ]
    if not blocked and deferred_criterion_ids:
        return
    details = ", ".join(blocked) if blocked else "ready=false"
    raise LifecycleError(f"readiness enforcement failed for wave {scope_id!r}: {details}")


def compute(
    scope_id: str,
    *,
    state: State,
    store_dir: Path,
    repo_root: Path,
    config_root: Path | None = None,
    load_profile_verify: bool = True,
    deferred_criterion_ids: frozenset[str] = frozenset(),
    prevalidated_gate_ids: Collection[str] = (),
) -> CloseReadiness:
    """Return the close-readiness projection for *scope_id*.

    Read-only at the state-mutation boundary — does not mutate *state*
    nor write any file. The W08 deterministic floor DOES spawn live
    subprocesses for ``evidence_kind="deterministic"`` gates via the
    W15-hardened gate runner; those subprocesses are scored as gate
    results, not persisted. Reads the typed CriterionSpec / GateSpec
    attachments (via :func:`_load_criterion_specs` /
    :func:`_load_gate_specs`), the legacy
    :attr:`~eawf.kernel.state.models.Wave.success_criteria` list, and
    the SHA-bound EvidenceRecord rows under *store_dir*.

    Scoring branches on ``criterion.evidence_kind``:

    * ``"deterministic"`` — gates compile via
      :func:`eawf.workflow.verify.compile.compile_gate` and run via
      :func:`eawf.workflow.audit_dsl.runner.run_checks` with
      *repo_root* as the cwd. Fresh waiver rows pre-empt the live
      run so W11's operator override semantics still apply.
    * ``"jury"`` / ``"attested"`` — evidence-row scoring (W06's
      original path); jury votes + operator attestations land in
      v0.4.1+.

    Floor-pack fallback: when the wave has no typed
    CriterionSpec rows AND the active profile carries a non-empty
    :attr:`~eawf.platform.profiles.models.VerifyBlock.floor_checks`,
    each floor check compiles via
    :func:`~eawf.workflow.verify.compile.compile_floor_pack` and runs
    through :func:`~eawf.workflow.audit_dsl.runner.run_checks`. Each
    floor check yields one ``CriterionView(source="floor")``. The
    floor pack does NOT render when the wave already carries typed
    CriterionSpec rows — typed specs are authoritative when present.
    When the active profile sets ``verify.enforce=true``, a non-ready
    result raises :class:`~eawf.workflow.lifecycle._errors.LifecycleError`
    so close seams reject the mutation. With the default ``False``,
    non-ready results stay advisory and are returned normally.

    Args:
        scope_id: Wave id (today; iter / phase scopes land later).
        state: Validated state model. Read-only.
        store_dir: ``<state_dir>/store/`` — the JSONL store root the
            EvidenceRecord rows live under.
        repo_root: Repository root, forwarded to
            :func:`eawf.workflow.lifecycle.wave_sha.derive_wave_sha` so
            the SHA-bound freshness lookup runs in the right git tree.
            Also forwarded as the deterministic-floor subprocess cwd
            so wave-anchored ``diff_base`` + scope resolution land in
            the right tree.
        config_root: Optional root for layered config and workspace
            profile discovery. Defaults to *repo_root*.
        load_profile_verify: When ``False``, skip active profile
            verify loading. Close paths use this for non-enforcing
            advisory metrics so long-running profile floor checks do
            not run while the state lock is held.
        prevalidated_gate_ids: Deterministic gates already executed and
            passed by the enforcing close oracle against the same frozen
            inputs. Readiness consumes their result instead of executing
            them again.

    Returns:
        A :class:`CloseReadiness` view. Empty waves (no typed specs +
        no legacy criteria) return ``ready=True`` with empty
        ``criteria``; legacy-only waves return ``ready=True`` plus one
        warning per legacy criterion.

    Raises:
        KeyError: When *scope_id* is not a known wave id in *state*.
            (Iter / phase scopes will resolve to their own loader once
            attached in a later wave; today only waves are scored.)
        LifecycleError: When active ``profile.verify.enforce`` is
            true and the computed readiness is not ready.
    """
    wave = state.waves.get(scope_id)
    if wave is None:
        raise KeyError(f"unknown wave: {scope_id!r}")

    # Waiver policy is security-relevant even when callers request advisory
    # readiness. Always load it strictly; ``load_profile_verify=False`` skips
    # floor execution and readiness enforcement, not disabled-mode policy.
    policy_block = _load_active_verify_block(
        scope_id,
        state,
        repo_root=repo_root,
        config_root=config_root,
        strict_config=True,
    )
    waiver_mode: VerifyWaiverMode = "B" if policy_block is None else policy_block.waiver_mode
    check_disabled_waiver_policy(
        waiver_mode=waiver_mode,
        scope_id=scope_id,
        criteria=list(wave.success_criteria),
        criteria_floor_waiver=wave.criteria_floor_waiver,
    )
    verify_block = policy_block if load_profile_verify else None

    # SHA derive feeds both the W06 advisory log + the W11 SHA-bound
    # waiver freshness filter: a waiver row whose stamped
    # ``metrics["wave_sha"]`` no longer matches the current wave SHA
    # is considered stale and dropped before per-gate roll-up so the
    # readiness view reflects only fresh operator attestations. The
    # compile-gate flips the same SHA into a full hard filter
    # for non-waiver evidence.
    sha = derive_wave_sha(scope_id, repo_root=repo_root)
    logger.debug(f"compute wave={scope_id!r} sha={sha!r}")

    criterion_specs = _load_criterion_specs(scope_id, state)
    gate_specs = _load_gate_specs(scope_id, state)

    evidence_rows = _read_evidence_rows(store_dir)
    scope_evidence = _filter_evidence_for_scope(evidence_rows, scope_id=scope_id)
    fresh_evidence = [
        row
        for row in scope_evidence
        if not _is_stale_waiver(row, current_sha=sha)
        and not (waiver_mode == "disabled" and row.status == "waived")
    ]

    spec_views, waived_gate_ids = _build_spec_views(
        criterion_specs,
        gate_specs,
        fresh_evidence,
        runner_cwd=repo_root,
        prevalidated_gate_ids=prevalidated_gate_ids,
    )
    legacy_views, legacy_warnings = _build_legacy_views(wave)
    # Profile-fed floor pack. Floor checks render only
    # when the wave has no typed CriterionSpec rows — the typed-spec
    # layer is authoritative when present; the floor pack is the
    # per-domain baseline that applies when no wave-level criteria
    # have been authored yet.
    floor_views: list[CriterionView] = []
    if not spec_views:
        floor_views = _build_floor_views(
            verify_block, wave=wave, runner_cwd=repo_root, fresh_evidence=fresh_evidence
        )

    # Backlog-resolution close-gate (P30-I10 QUAL-2). Scores the wave's
    # linked backlog items independently of the typed-spec / floor split:
    # a wave that fixes a backlog item but leaves the linked row dangling
    # surfaces a blocking ``fail`` here, and the ``verify.enforce`` path
    # (see :func:`_enforce_readiness`) turns that into a close refusal. A
    # wave linking no backlog items yields ``None`` (no view), so an
    # unlinked wave's readiness is byte-unchanged.
    backlog_view = _build_backlog_resolution_view(scope_id, state)
    backlog_views = [backlog_view] if backlog_view is not None else []

    criteria: list[CriterionView] = [*spec_views, *floor_views, *backlog_views, *legacy_views]
    warnings = list(legacy_warnings)
    if not spec_views and not floor_views and not backlog_views and not legacy_views:
        # Empty waves cannot be meaningfully blocking — flag the
        # advisory so operators notice the gap without raising.
        warnings.append("no criteria attached to wave")

    ready = _is_ready(criteria)
    readiness = CloseReadiness(
        ready=ready,
        criteria=criteria,
        warnings=warnings,
        waived_gate_ids=waived_gate_ids,
    )
    _enforce_readiness(
        scope_id=scope_id,
        readiness=readiness,
        verify_block=verify_block,
        deferred_criterion_ids=deferred_criterion_ids,
    )
    return readiness


def _is_ready(criteria: list[CriterionView]) -> bool:
    """Return ``True`` iff every REQUIRED criterion has status in ``{pass, waived}``.

    Pure helper so :func:`compute` reads top-down. A required criterion with
    ``status="pending"`` or ``"blocked"`` or ``"fail"`` flips the aggregate
    to ``ready=False``. A non-required (advisory) criterion is exempt -- it
    is surfaced in the view but never blocks the close, matching the oracle
    path's ``if not criterion.required: continue`` skip, so an advisory gate
    (e.g. a ``cadence="ship"`` pixel diff marked ``required=false``) cannot
    block an every-wave close.

    Args:
        criteria: All :class:`CriterionView` rows for the scope.

    Returns:
        ``True`` when every required criterion is pass/waived (or no
        required criterion exists -- an empty / advisory-only scope is
        trivially ready by definition).
    """
    return all(view.status in ("pass", "waived") for view in criteria if view.required)


__all__ = [
    "compute",
    "legacy_criterion_count",
    "load_active_verify_block",
    "load_active_waiver_mode",
    "resolve_wave_verify_block",
    "wired_audit_dsl_kinds",
]
