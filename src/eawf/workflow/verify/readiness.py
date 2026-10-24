"""Verify-spine helpers: the deterministic gate runner and the verify block.

What this module still serves:

* :func:`_run_deterministic_gate` compiles one typed
  :class:`~eawf.kernel.spec.common.GateSpec` via
  :func:`eawf.workflow.verify.compile.compile_gate` and runs it through
  :func:`eawf.workflow.verify.sandboxed_checks.run_checks_out_of_process`,
  in a child bound to a throwaway sandbox rather than in this process.
* :func:`load_active_verify_block` resolves the ``verify`` block the enabled
  profiles and the repository's ``verify.*`` leaves declare.
* :func:`resolve_wave_verify_block` bands one wave's verify block, and
  :func:`legacy_criterion_count` counts its grandfathered criteria; the
  idle-contract and measurable-criterion tools call both.
* :func:`wired_audit_dsl_kinds` names every audit-DSL kind a gate can run.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from eawf.kernel.config.schema import VerifyConfig
from eawf.kernel.migration.v1_state import State
from eawf.kernel.spec.common import (
    GRANDFATHERED_KIND,
    CriterionSpec,
    GateSpec,
)
from eawf.kernel.spec.heuristics import is_ui_scope
from eawf.kernel.state.enums import AgentSessionRole, EffortBucket
from eawf.kernel.state.models import Wave
from eawf.platform.profiles.models import VerifyBlock
from eawf.workflow.audit_dsl.models import CheckSpec, GateFreshnessInput
from eawf.workflow.verify.compile import compile_gate
from eawf.workflow.verify.sandboxed_checks import run_checks_out_of_process

if TYPE_CHECKING:
    from eawf.runtime.daemon.gate_execution import GateExecutionContext

logger = logging.getLogger(__name__)


def _run_gate_via_shared_runner(
    compiled: CheckSpec,
    *,
    gate: GateSpec,
    criterion: CriterionSpec,
    runner_cwd: Path,
    gate_context: GateExecutionContext,
) -> str:
    """Execute *compiled* through the daemon's out-of-process gate runner.

    The daemonless close lane reuses the SAME runner the daemon oracle drives
    (:func:`~eawf.runtime.daemon.gate_execution.run_gate_out_of_process`), so a
    gate-bearing close outside the daemon claims the same freshness key and
    resolves to the same receipt id instead of executing on a private
    in-process path. The child owns the claim, so a gate kind that hard-exits
    cannot take the calling process down with it.

    Args:
        compiled: Already-compiled check spec for *gate*.
        gate: Typed gate spec (its id names the claim).
        criterion: Parent criterion (its id names the claim).
        runner_cwd: Working directory for the child process.
        gate_context: Durable identity the child claims the gate under.

    Returns:
        One of ``"pass"`` / ``"fail"`` / ``"blocked"``.
    """
    from eawf.runtime.daemon.gate_execution import (
        GateChildCrashError,
        gate_receipt_id,
        run_gate_out_of_process,
    )

    try:
        result = run_gate_out_of_process(
            compiled,
            cwd=runner_cwd,
            context=gate_context,
            criterion_id=criterion.id,
            gate_id=gate.id,
        )
    except (GateChildCrashError, ValueError) as exc:
        # A child that crashed or reported a typed fault proved NOTHING about
        # the wave, so it must never project as a pass. "blocked" is the
        # non-pass status the enforcing close already refuses on.
        logger.warning(
            f"_run_deterministic_gate gate_id={gate.id!r} status='blocked' "
            f"runner='out-of-process' detail={exc!s}"
        )
        return "blocked"
    if result.freshness_key is not None:
        logger.info(
            f"_run_deterministic_gate gate_id={gate.id!r} runner='out-of-process' "
            f"receipt={gate_receipt_id(result.freshness_key)!r}"
        )
    return result.status or ("pass" if result.passed else "fail")


def _run_deterministic_gate(
    gate: GateSpec,
    criterion: CriterionSpec,
    *,
    runner_cwd: Path,
    gate_context: GateExecutionContext | None = None,
    live_state_path: Path | None = None,
    freshness: GateFreshnessInput | None = None,
) -> str:
    """Compile + execute *gate* via the W15-hardened audit-DSL runner.

    Delegates the shape transform to
    :func:`eawf.workflow.verify.compile.compile_gate` and the live
    subprocess + diff-base + scope-resolution machinery to
    :func:`eawf.workflow.audit_dsl.runner.run_checks`. Returns the
    rolled-up :data:`~eawf.workflow.verify.models.GateStatus` literal
    so the caller can stamp it onto a :class:`GateResult` row
    untouched.

    The runner is never entered in this process. A gate is routinely a
    whole test suite, and a suite that drives eawf's own RPCs would drive
    the caller's live runtime directory and live ledger. Both lanes
    therefore execute in a child bound to a throwaway sandbox: the durable
    lane through the shared gate runner when *gate_context* is set, the
    advisory lane through
    :func:`~eawf.workflow.verify.sandboxed_checks.run_checks_out_of_process`
    otherwise.

    The function defends against the case where
    :func:`compile_gate` legitimately returns ``None`` for a
    deterministic criterion (e.g. ``command_exit_zero`` gate with no
    usable ``argv``) by mapping that to ``"blocked"``, so the caller
    records the gap as a non-pass rather than raising.

    Args:
        gate: Typed gate spec attached to *criterion*.
        criterion: Parent criterion (read for ``id`` + ``evidence_kind``
            only; the deterministic-floor caller has already verified
            ``criterion.evidence_kind == "deterministic"``).
        runner_cwd: Working directory for the subprocess + git
            diff-base + scope resolution: the repository root.
        gate_context: When set, the gate runs through the shared
            durable gate runner under this identity, claiming a
            freshness key and resolving a receipt. The daemonless close
            lane passes it so its gates claim + resolve exactly like the
            daemon's; every advisory caller leaves it ``None`` and gets
            the receipt-free sandboxed runner instead.
        live_state_path: The live ``state.json`` the advisory lane's
            sandbox ledger is snapshotted from, so the gate reads a
            faithful copy. ``None`` seeds an empty ledger rather than
            guessing one from the process environment.
        freshness: The close attempt's frozen facts for *gate*, compiled
            into the spec so the gate resolves the freshness key the close
            oracle already claimed. ``None`` compiles the unfrozen spec.

    Returns:
        One of ``"pass"`` / ``"fail"`` / ``"blocked"``.
    """
    if freshness is None:
        compiled = compile_gate(gate, criterion=criterion)
    else:
        compiled = compile_gate(gate, criterion=criterion, freshness=freshness)
    if compiled is None:
        logger.debug(
            f"_run_deterministic_gate gate_id={gate.id!r} status=blocked reason=compile-none"
        )
        return "blocked"
    if gate_context is not None:
        return _run_gate_via_shared_runner(
            compiled,
            gate=gate,
            criterion=criterion,
            runner_cwd=runner_cwd,
            gate_context=gate_context,
        )
    results = run_checks_out_of_process(
        [compiled],
        cwd=runner_cwd,
        live_state_path=live_state_path,
    )
    result = results[0]
    status = result.status or ("pass" if result.passed else "fail")
    logger.debug(
        f"_run_deterministic_gate gate_id={gate.id!r} status={status!r} "
        f"runner='out-of-process' evidence_kind={criterion.evidence_kind!r}"
    )
    return status


def _config_root_for_readiness(config_root: Path | None, repo_root: Path) -> Path:
    """Return the config anchor used to resolve active profile bodies."""
    return config_root if config_root is not None else repo_root


def _merge_verify_blocks(blocks: list[VerifyBlock]) -> VerifyBlock | None:
    """Merge active profile verify blocks into one effective block.

    Union-merges the list-valued fields (``floor_checks`` concatenated;
    ``argv_allowlist`` / ``uiux_bands`` / ``jury_vendors`` deduplicated,
    first-occurrence order preserved), OR-folds the boolean gating bits
    (``enforce`` / ``cross_vendor_jury`` / ``odr_blocking``), and takes the
    last contributor's scalar dials (``timeout_class_seconds``,
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
    state: State | None,
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


#: Gate kinds whose ground truth needs a jury: a cross-vendor jury vote, or a
#: UI / visual surface whose final oracle is the visual jury. A wave carrying
#: one keeps enforcement outside a UI band.
_JURY_GATE_KINDS: frozenset[str] = frozenset(
    {
        "jury_verdict",
        "cross_vendor_jury",
        "tui_flow",
        "svg_well_formed",
        "svg_pixel_diff",
        "mockup_golden_diff",
        "affordance_parity",
        "transition_coverage",
    }
)


#: Effort buckets large enough that a fresh auditor verdict is mandatory.
_HIGH_RISK_EFFORT: frozenset[EffortBucket] = frozenset({EffortBucket.L, EffortBucket.XL})


#: Roles whose output is a judgment artifact a fresh auditor must confirm,
#: not a mechanical edit.
_JUDGMENT_ROLES: frozenset[AgentSessionRole] = frozenset(
    {
        AgentSessionRole.AUDITOR,
        AgentSessionRole.REVIEWER,
        AgentSessionRole.PLANNER,
        AgentSessionRole.RESEARCHER,
        AgentSessionRole.DOMAIN_SPECIALIST,
        AgentSessionRole.OPERATOR,
    }
)


#: Keywords that make a wave security-scoped when its title or a success
#: criterion names one as a whole word.
_SECURITY_KEYWORDS: tuple[str, ...] = (
    "security",
    "sandbox",
    "auth",
    "egress",
    "secret",
    "jail",
    "scrub",
)


def _wave_in_uiux_band(wave: Wave, bands: list[str]) -> bool:
    """Return whether *wave* is in a UI/UX band.

    A wave is banded when its ``file_scopes`` are UI surface, or when a token
    in *bands* matches a whole word of the wave id or title, case-insensitively.
    The whole-word match keeps ``"ui"`` from arming on ``"build"`` or ``"quiz"``.

    Args:
        wave: The wave being closed.
        bands: The band tokens; an empty token never matches.

    Returns:
        ``True`` when either arm matches.
    """
    if is_ui_scope(wave.file_scopes):
        return True
    corpus = f"{wave.id}\n{wave.title}".lower()
    return any(token and re.search(rf"\b{re.escape(token.lower())}\b", corpus) for token in bands)


def _verdict_always_required(wave: Wave) -> bool:
    """Return whether *wave* needs a fresh auditor verdict unconditionally.

    A large effort bucket, a judgment-heavy role, or a security keyword in the
    title or success criteria makes the verdict mandatory. A hyphen counts as
    part of a word, so a wave code such as ``"AUTH-3"`` does not arm ``"auth"``.

    Args:
        wave: The wave being closed.

    Returns:
        ``True`` when any high-risk signal is present.
    """
    if wave.effort_bucket in _HIGH_RISK_EFFORT or wave.agent_role in _JUDGMENT_ROLES:
        return True
    corpus = "\n".join([wave.title, *(c.text for c in wave.success_criteria)]).lower()
    return any(
        re.search(rf"(?<![\w-]){re.escape(keyword)}(?![\w-])", corpus)
        for keyword in _SECURITY_KEYWORDS
    )


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
      match, per :func:`_wave_in_uiux_band`)
      keeps the merged block's enforcement bits as authored -- so a
      band-scoped profile resolves to ``enforce=True`` +
      ``cross_vendor_jury=True`` and the close routes through the gate;
    * a **fleet high-risk** wave (it carries a jury / cross-vendor /
      visual-band gate, per :data:`_JURY_GATE_KINDS`) ALSO keeps enforcement
      on even when it is NOT in a UI band: a jury-gated wave's ground truth
      needs the jury, so narrowing its ``enforce`` to advisory-only would let
      a high-risk close slip the gate. The band narrowing is for low-risk
      non-band waves, never for a wave whose own gate set demands a jury;
    * a **verdict-always** wave (per :func:`_verdict_always_required`:
      a large effort bucket, a judgment-heavy ``agent_role``, or a
      security-scoped surface) ALSO keeps enforcement on even when it is
      neither in a UI band nor gate-classified high-risk: a fresh auditor
      verdict is mandatory for such a wave, so narrowing its ``enforce`` to
      advisory-only would de-scope the very gate that produces that verdict.
      This is the third preservation arm -- the band narrowing must never
      down-grade a wave whose verdict is required unconditionally;
    * a **non-band, non-high-risk, non-verdict-always** wave resolves to
      ``enforce=False`` and no jury. The close gate still scores such a wave's
      decisive deterministic gates and holds it to the receipt floor, because
      the merged block enforces; the narrowing withdraws only the auditor and
      the jury.

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
    if verify_block is None or not verify_block.enforce:
        return verify_block
    if not verify_block.uiux_bands:
        # Not band-scoped -- a whole-fleet enforce profile gates every wave.
        return verify_block
    if _wave_in_uiux_band(wave, verify_block.uiux_bands):
        return verify_block
    # Band check failed -- but a wave whose OWN gate set needs a jury must
    # keep enforcement even outside a UI band: narrowing it to advisory would
    # let a high-risk close slip the gate.
    if any(gate.kind in _JURY_GATE_KINDS for gate in wave.gates):
        logger.debug(
            f"resolve_wave_verify_block wave={wave.id!r} band=False jury_gate=True enforce=True"
        )
        return verify_block
    # A wave that needs a fresh auditor verdict unconditionally keeps
    # enforcement too: narrowing it would de-scope the gate that produces
    # that verdict. Only mechanical non-band waves fall through to advisory.
    if _verdict_always_required(wave):
        logger.debug(
            f"resolve_wave_verify_block wave={wave.id!r} band=False "
            f"verdict_always=True enforce=True"
        )
        return verify_block
    narrowed = verify_block.model_copy(update={"enforce": False, "cross_vendor_jury": False})
    logger.debug(
        f"resolve_wave_verify_block wave={wave.id!r} band=False "
        f"enforce={narrowed.enforce} cross_vendor_jury={narrowed.cross_vendor_jury}"
    )
    return narrowed


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


__all__ = [
    "legacy_criterion_count",
    "load_active_verify_block",
    "resolve_wave_verify_block",
    "wired_audit_dsl_kinds",
]
