"""Source-scan and liveness checks for the idle-contract gate.

Each check reads one live module off the working tree (or drives one sibling
gate in-process) and reds when a production binding the gate protects has
gone idle.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType

from idle_contract_common import (
    _REPO_ROOT,
    GateFailure,
    GateResult,
)

from eawf.platform.lint import eawf024_test_tier_contract as eawf024
from eawf.platform.lint import eawf025_test_placement as eawf025
from eawf.platform.lint.eawf023_artifact_placement import check_artifact_path
from eawf.workflow.audit_dsl.registry import registered_audit_dsl_kinds
from eawf.workflow.verify.readiness import (
    wired_audit_dsl_kinds,
)

# =========================================================================== #
# resolve_routing wiring: the live dispatch path must call resolve_routing.
# =========================================================================== #

#: The live dispatch module that must call ``resolve_routing`` directly so the
#: per-role tier table selects the spawn model instead of a hardcoded default.
#: The pre-commit gate reads this file off the working tree; a test injects the
#: text to exercise both the wired and idle outcomes.
_LIVE_DISPATCH_MODULE = "src/eawf/runtime/daemon/methods/agent.py"

#: A direct ``resolve_routing(...)`` call (not a docstring / import mention).
#: The trailing ``(`` is what distinguishes a live call from a bare reference,
#: so a comment or ``:func:`` cross-link in prose does not satisfy the gate.
_RESOLVE_ROUTING_CALL_RE = re.compile(r"\bresolve_routing\s*\(")


def check_resolve_routing_wired(
    *,
    module_text: str | None = None,
    module_path: str = _LIVE_DISPATCH_MODULE,
) -> GateResult:
    """Assert the live dispatch path calls :func:`resolve_routing` directly.

    The ``(agent_role, effort_bucket) -> model`` routing table is built but was
    only reachable through the per-vendor wrapper; this contract pins that the
    live wave-spawn dispatch (:func:`eawf.runtime.daemon.methods.agent._resolve_spawn_model`)
    calls :func:`eawf.workflow.dispatch.routing.resolve_routing` itself, so role
    + effort selects the model tier rather than a hardcoded default. If a later
    refactor drops the direct call (reverting to a single hardcoded model), the
    source no longer matches :data:`_RESOLVE_ROUTING_CALL_RE` and this gate
    fails :attr:`GateFailure.RESOLVE_ROUTING_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the regressed (idle) outcomes; when it is
    ``None`` the module is read off the working tree via :data:`_REPO_ROOT`
    (mirroring :func:`check_runtime_gate_is_not_idle`, so this gate is immune to
    the meta-gate's injected reader stub).

    Args:
        module_text: The live dispatch module source. ``None`` reads
            *module_path* off the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the live dispatch module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the live
        dispatch module carries a direct ``resolve_routing(`` call; otherwise
        ``failure`` is :attr:`GateFailure.RESOLVE_ROUTING_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    if _RESOLVE_ROUTING_CALL_RE.search(text) is not None:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (live dispatch calls resolve_routing -- "
                "role/effort selects the model tier, not a hardcoded default)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.RESOLVE_ROUTING_IDLE,
        message=(
            "resolve_routing is idle in live dispatch: "
            f"{module_path} carries no direct resolve_routing(...) call, so the "
            "per-role tier table never selects the spawn model (the path "
            "regressed to a hardcoded default)"
        ),
    )


# =========================================================================== #
# I09 jury-validation bindings: each of the four must stay wired, not idle.
# =========================================================================== #

#: The daemon close path that binds the per-item spec-jury ballot fn
#: and consults the earned block authority. Read off the working tree.
_SPEC_JURY_GATE_MODULE = "src/eawf/runtime/daemon/methods/state.py"

#: The live convener that threads the reputation reliability map into the jury
#: reducer. Read off the working tree.
_CROSS_VENDOR_JURY_MODULE = "src/eawf/observability/eval/cross_vendor_jury.py"

#: The ordered-oracle module whose jury branch consults *block_authority* before
#: raising. Read off the working tree.
_ORACLE_MODULE = "src/eawf/workflow/verify/oracle.py"

#: The CLI command that gives ``validate_jury`` a live caller. Read off
#: the working tree.
_METRICS_CLI_MODULE = "src/eawf/surfaces/cli/commands/metrics.py"

#: ``_spec_jury_ballot_fn`` returns a LIVE ballot fn by binding
#: ``live_per_item_ballot_fn(...)``; a regression that reverts the body to a bare
#: ``return None`` (re-idling the producer) drops this call.
_LIVE_BALLOT_FN_CALL_RE = re.compile(r"\blive_per_item_ballot_fn\s*\(")

#: The gate consults ``_spec_jury_ballot_fn(...)`` and degrades when it is None.
#: A regression that stops calling the builder leaves the producer unreachable.
_BALLOT_FN_CONSULT_RE = re.compile(r"\bballot_fn\s*=\s*_spec_jury_ballot_fn\s*\(")

#: The convener threads ``reliability=`` into the jury reducer; a regression that
#: drops the kwarg re-idles the reputation-weighting seam.
_RELIABILITY_THREAD_RE = re.compile(r"\breliability\s*=\s*reliability\b")

#: The reducer forwards the map into ``aggregate_jury(..., reliability=...)``;
#: this is the production sink that consumes the threaded map.
_AGGREGATE_RELIABILITY_RE = re.compile(r"\baggregate_jury\s*\([^)]*\breliability\s*=")

#: The oracle jury branch tests blocking authority BEFORE it raises; the gate
#: keyword and the raise must both be present so the veto is not unconditional.
_BLOCK_AUTHORITY_GATE_RE = re.compile(r"\bif\s+block_authority\s+is\s+BlockAuthority\.BLOCKING\b")

#: The blocking branch raises a LifecycleError so a calibrated jury's veto blocks
#: the close.
_JURY_VETO_RAISE_RE = re.compile(r"\braise\s+LifecycleError\b")

#: The CLI command binds ``validate_jury(...)`` directly, giving it a live caller
#: (the moment a labelled cohort lands the CLI surfaces a scored report).
_VALIDATE_JURY_CALL_RE = re.compile(r"\bvalidate_jury\s*\(")


def check_spec_jury_ballot_fn_wired(
    *,
    module_text: str | None = None,
    module_path: str = _SPEC_JURY_GATE_MODULE,
) -> GateResult:
    """Assert the spec-jury close gate binds a live per-item ballot fn.

    The TRUST-5 binding: :func:`_spec_jury_ballot_fn` returns a non-``None`` live
    ballot fn by binding
    :func:`eawf.workflow.dispatch.spec_jury.live_per_item_ballot_fn` (when a
    cross-vendor quorum resolves), and :func:`_enforce_spec_jury_gate` consults
    that builder before routing the close through the producer. If a later
    refactor reverts the builder body to a bare ``return None`` (re-idling the
    producer) or stops consulting it, the source no longer matches and this gate
    fails :attr:`GateFailure.SPEC_JURY_BALLOT_FN_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the re-idled (``return None``) outcomes.

    Args:
        module_text: The spec-jury gate module source. ``None`` reads
            *module_path* off the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the spec-jury gate module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the module
        both binds ``live_per_item_ballot_fn(`` and consults
        ``ballot_fn = _spec_jury_ballot_fn(``; otherwise ``failure`` is
        :attr:`GateFailure.SPEC_JURY_BALLOT_FN_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    binds_live = _LIVE_BALLOT_FN_CALL_RE.search(text) is not None
    consults = _BALLOT_FN_CONSULT_RE.search(text) is not None
    if binds_live and consults:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (spec-jury gate binds the live per-item "
                "ballot fn -- the producer is reachable, not idle)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.SPEC_JURY_BALLOT_FN_IDLE,
        message=(
            "spec-jury per-item ballot fn is idle: "
            f"{module_path} binds_live={binds_live} consults={consults} (expected "
            "both True); _spec_jury_ballot_fn must bind live_per_item_ballot_fn(...) "
            "and the gate must consult it -- the producer regressed to return None"
        ),
    )


def check_jury_reliability_map_wired(
    *,
    module_text: str | None = None,
    module_path: str = _CROSS_VENDOR_JURY_MODULE,
) -> GateResult:
    """Assert the live convener threads a reliability map into the reducer.

    The TRUST-6 binding: the live convener
    (:func:`eawf.observability.eval.cross_vendor_jury.convene_cross_vendor_jury`)
    threads its ``reliability`` map into
    :func:`eawf.observability.eval.cross_vendor_jury._reduce_jury`, which forwards
    it into :func:`eawf.observability.eval.jury.aggregate_jury` as
    ``reliability=...`` -- the first production sink of the built-but-idle
    reputation-weighting seam. If a later refactor drops the ``reliability=``
    forward (re-idling the seam so every juror weights neutrally regardless of
    the reputation map), the source no longer matches and this gate fails
    :attr:`GateFailure.JURY_RELIABILITY_MAP_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the re-idled outcomes.

    Args:
        module_text: The cross-vendor jury module source. ``None`` reads
            *module_path* off the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the convener module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the module
        both threads ``reliability=reliability`` and forwards it into
        ``aggregate_jury(..., reliability=...)``; otherwise ``failure`` is
        :attr:`GateFailure.JURY_RELIABILITY_MAP_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    threads = _RELIABILITY_THREAD_RE.search(text) is not None
    aggregates = _AGGREGATE_RELIABILITY_RE.search(text) is not None
    if threads and aggregates:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (live convener threads the reliability map "
                "into aggregate_jury -- the reputation-weighting seam is not idle)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.JURY_RELIABILITY_MAP_IDLE,
        message=(
            "jury reliability map is idle: "
            f"{module_path} threads={threads} aggregates={aggregates} (expected "
            "both True); the convener must forward reliability=reliability into "
            "aggregate_jury(..., reliability=...) -- the reputation seam went None"
        ),
    )


def check_jury_block_authority_wired(
    *,
    module_text: str | None = None,
    module_path: str = _ORACLE_MODULE,
) -> GateResult:
    """Assert the oracle jury branch consults block authority before raising.

    The TRUST-4 binding: the ordered-oracle jury tier
    (:func:`eawf.workflow.verify.oracle.run_oracle`) tests
    ``block_authority is BlockAuthority.BLOCKING`` BEFORE it raises
    :class:`~eawf.workflow.lifecycle.transitions.LifecycleError`, so a
    non-pass jury outcome blocks the close only once the jury has EARNED blocking
    authority -- an uncalibrated jury's veto stays advisory. If a later refactor
    drops the authority gate (making the veto raise unconditionally, or never),
    the source no longer matches both the gate keyword and the raise, and this
    gate fails :attr:`GateFailure.JURY_BLOCK_AUTHORITY_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the bypassed (unconditional-raise) outcomes.

    Args:
        module_text: The oracle module source. ``None`` reads *module_path* off
            the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the oracle module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the module
        carries both the ``if block_authority is BlockAuthority.BLOCKING`` gate
        and a ``raise LifecycleError``; otherwise ``failure`` is
        :attr:`GateFailure.JURY_BLOCK_AUTHORITY_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    gated = _BLOCK_AUTHORITY_GATE_RE.search(text) is not None
    raises = _JURY_VETO_RAISE_RE.search(text) is not None
    if gated and raises:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (oracle jury branch consults "
                "block_authority before raising -- the veto is earned, not idle)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.JURY_BLOCK_AUTHORITY_IDLE,
        message=(
            "jury block authority is idle: "
            f"{module_path} gated={gated} raises={raises} (expected both True); the "
            "jury branch must test 'block_authority is BlockAuthority.BLOCKING' "
            "before it raises LifecycleError -- the earned-authority gate was bypassed"
        ),
    )


def check_validate_jury_cli_wired(
    *,
    module_text: str | None = None,
    module_path: str = _METRICS_CLI_MODULE,
) -> GateResult:
    """Assert ``validate_jury`` has a live CLI caller.

    The TRUST-7 binding: the ``eawf metrics jury-validation`` command
    (in :data:`_METRICS_CLI_MODULE`) binds
    :func:`eawf.observability.eval.jury_validation.validate_jury` directly, so the
    jury-validation reducer is reachable from the operator surface (it renders the
    honest insufficient-signal banner today and a scored report the moment a
    labelled cohort lands). If a later refactor drops the CLI caller (orphaning
    the reducer back to a never-invoked function), the source no longer matches
    and this gate fails :attr:`GateFailure.VALIDATE_JURY_CLI_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the orphaned outcomes.

    Args:
        module_text: The metrics CLI module source. ``None`` reads *module_path*
            off the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the metrics CLI module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the module
        carries a direct ``validate_jury(`` call; otherwise ``failure`` is
        :attr:`GateFailure.VALIDATE_JURY_CLI_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    if _VALIDATE_JURY_CALL_RE.search(text) is not None:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (validate_jury has a live CLI caller -- "
                "the jury-validation reducer is reachable from the operator surface)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.VALIDATE_JURY_CLI_IDLE,
        message=(
            "validate_jury is idle: "
            f"{module_path} carries no direct validate_jury(...) call, so the "
            "jury-validation reducer has no CLI caller (the metrics command "
            "regressed and orphaned the reducer)"
        ),
    )


# =========================================================================== #
# I11 Track binding: the silent phase-tag stamp.
# =========================================================================== #

#: The lifecycle module whose ``open_phase`` silently stamps every phase with the
#: current Track id. Read off the working tree; if the stamp stops
#: firing, phases stop tagging their owning Track and the binding is idle.
_PHASE_TRACK_TAG_MODULE = "src/eawf/workflow/lifecycle/phase.py"

#: The silent phase-tag stamp: ``open_phase`` constructs each ``Phase`` with
#: ``track_id=state.current.track_id``. A regression that drops the keyword
#: (constructing the phase with no Track tag) leaves phases untagged and reds
#: this row.
_PHASE_TRACK_TAG_RE = re.compile(r"\btrack_id\s*=\s*state\.current\.track_id\b")


def check_phase_track_tag_wired(
    *,
    module_text: str | None = None,
    module_path: str = _PHASE_TRACK_TAG_MODULE,
) -> GateResult:
    """Assert ``open_phase`` silently stamps each phase with the Track id.

    The TRACK-3 binding: :func:`eawf.workflow.lifecycle.phase.open_phase`
    constructs every :class:`~eawf.kernel.state.models.Phase` with
    ``track_id=state.current.track_id``, so a phase opened while a Track is in
    focus is silently tagged with its owning Track. If a later refactor drops the
    stamp (constructing the phase with no Track tag), phases stop tagging their
    Track and the silent phase-tag binding regresses to idle -- the source no
    longer matches and this gate fails :attr:`GateFailure.PHASE_TRACK_TAG_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the re-idled (stamp-dropped) outcomes.

    Args:
        module_text: The lifecycle phase module source. ``None`` reads
            *module_path* off the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the phase module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the module
        carries a ``track_id=state.current.track_id`` phase-construction stamp;
        otherwise ``failure`` is :attr:`GateFailure.PHASE_TRACK_TAG_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    if _PHASE_TRACK_TAG_RE.search(text) is not None:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (open_phase stamps track_id=state.current."
                "track_id -- phases silently tag their owning Track, not idle)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.PHASE_TRACK_TAG_IDLE,
        message=(
            "phase track-tag stamp is idle: "
            f"{module_path} carries no 'track_id=state.current.track_id' phase "
            "construction stamp, so open_phase no longer tags phases with their "
            "owning Track (the silent phase-tag binding regressed)"
        ),
    )


# =========================================================================== #
# P30-I17 live-autopilot bindings: the drive ladders + the live stdout fan.
# =========================================================================== #

#: The daemon fleet module whose ``start_background_drive`` arms the LIVE drive
#: (I17-W11). Read off the working tree; the two production ``arm_drive`` calls
#: must each pass ``classify`` + ``repair`` so the bounded spawn ladder (DL-11)
#: and the grounded repair ladder (DL-7) fire on a real autopilot run -- without
#: the kwargs ``classify is None`` / ``repair is None`` and both ladders stay
#: dormant (they only fire when a test injects the hooks).
_LIVE_DRIVE_MODULE = "src/eawf/runtime/daemon/methods/fleet.py"

#: The live wave-spawn dispatch module whose ``_spawn_and_dispatch`` threads the
#: captured agent answer into the W08 stdout producer. Read off the
#: working tree; without ``output_text=spawn_result.text`` the producer is wired
#: into ``run_dispatch`` but no live caller supplies it, so the agent-watch live
#: tail stays empty on a real spawn.
_LIVE_OUTPUT_MODULE = "src/eawf/runtime/daemon/methods/agent.py"

#: The live drive arms ``arm_drive`` with the production error classifier so the
#: bounded spawn ladder fires; a regression that drops the kwarg re-dormants it.
_DRIVE_CLASSIFY_RE = re.compile(r"\bclassify\s*=\s*_live_lane_error_classifier\b")

#: The live drive arms ``arm_drive`` with the live grounded-repair hook so the
#: bounded repair ladder fires; a regression that drops the kwarg re-dormants it.
_DRIVE_REPAIR_RE = re.compile(r"\brepair\s*=\s*repair_hook\b")

#: The live spawn threads its captured answer text into the stdout producer.
_LIVE_OUTPUT_TEXT_RE = re.compile(r"\boutput_text\s*=\s*spawn_result\.text\b")

# =========================================================================== #
# P30-I18 campaign-run bindings: the state.claims fold + the L1 carryover prune.
# =========================================================================== #

#: The daemon research-methods module whose ``run_campaign`` folds each round's
#: reconciled claims into the canonical ``state.claims`` and runs the
#: L1 carryover prune between rounds. Read off the working tree; if either
#: binding regresses to the throwaway-shadow / no-prune path the source no longer
#: matches and the corresponding row reds.
_CAMPAIGN_RUN_MODULE = "src/eawf/runtime/daemon/methods/research.py"

#: ``run_campaign`` folds the reconciled claims into REAL state through the
#: daemon-owned canonical writer (``_commit_worktree_state``), the same path
#: ``add_question`` uses for ``state.open_questions``. A regression that reverts
#: to the throwaway ``State.model_construct(claims={}, ...)`` shadow as the ONLY
#: reconcile path drops this call and re-idles the fold.
_CAMPAIGN_CLAIM_FOLD_RE = re.compile(r"\b_commit_worktree_state\s*\(")

#: ``run_campaign`` runs ``reconcile_round_claims`` through that canonical writer
#: by binding it inside the ``apply_func`` the writer calls -- the fold mutates
#: the real ``state.claims``, not a shadow.
_CAMPAIGN_RECONCILE_FOLD_RE = re.compile(r"\breconcile_round_claims\s*\(\s*state\b")

#: ``run_campaign`` calls the L1 between-rounds carryover reducer so the next
#: round + the synthesis work over only the live claims. A regression that drops
#: the call leaves the reducer with zero production callers (its prior state).
_CAMPAIGN_CARRYOVER_CALL_RE = re.compile(r"\bprune_round_carryover\s*\(")

# ===========================================================================
# P30-I20 honest-close binding checks: new gate/helpers must be live.
# ===========================================================================

#: Synthetic canonical artifact path used to prove the EAWF023 single-path
#: helper runs under this always-on gate. The file need not exist; the lint is
#: path-shape only.
_EAWF023_GOOD_PATH = ".ea/artifacts/audits/2026-06-12-idle-contract.md"

#: Synthetic bad artifact path used as the negative control for
#: :func:`check_artifact_path`.
_EAWF023_BAD_PATH = ".ea/artifacts/notakind/2026-06-12-idle-contract.md"

#: The daemon research module that will carry the live campaign producer class
#: when P30-I20-W06 lands. W05's guard is class-scoped so it can ship before W06:
#: no class means there is no class seam to judge yet; once a class lands, a
#: ``NotImplementedError`` / ``pass`` / ellipsis body reds immediately.
_CAMPAIGN_PRODUCER_MODULE = "src/eawf/runtime/daemon/methods/research.py"

#: Campaign producer classes are named with both ``Campaign`` and ``Producer``
#: in the class symbol, e.g. ``LiveCampaignAgentEndProducer``.
_CAMPAIGN_PRODUCER_CLASS_RE = re.compile(
    r"^class\s+(?P<name>[A-Za-z0-9_]*Campaign[A-Za-z0-9_]*Producer[A-Za-z0-9_]*)\b"
    r"(?P<body>.*?)(?=^class\s|^def\s|\Z)",
    flags=re.MULTILINE | re.DOTALL,
)

#: Stub markers forbidden inside a campaign producer class seam.
_PRODUCER_STUB_RE = re.compile(
    r"\bNotImplementedError\b|^\s*(?:pass|\.\.\.)\s*(?:#.*)?$",
    flags=re.MULTILINE,
)


def check_eawf023_artifact_placement_wired(
    *,
    check_path_fn: Callable[[str], object | None] = check_artifact_path,
) -> GateResult:
    """Assert the EAWF023 single-path helper is live under the idle gate.

    The EAWF023 hook calls the batch helper, but the meta-gate tracks the newly
    added single-path contract too. This row calls :func:`check_artifact_path`
    directly against a conforming path and a bad-kind path, proving the helper
    has a live non-test call-site and a negative control.

    Args:
        check_path_fn: Single-path placement checker. Defaults to the live
            :func:`check_artifact_path`.

    Returns:
        A :class:`GateResult` that passes only when the canonical path is clean
        and the bad-kind path yields a violation.
    """
    good = check_path_fn(_EAWF023_GOOD_PATH)
    bad = check_path_fn(_EAWF023_BAD_PATH)
    if good is None and bad is not None:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (EAWF023 check_artifact_path has a live "
                "gate call-site and flags a bad artifact kind)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.EAWF023_ARTIFACT_PLACEMENT_IDLE,
        message=(
            "EAWF023 artifact-placement helper is idle or toothless: "
            f"good={good!r} bad={bad!r}; check_artifact_path must be called by "
            "the always-on idle gate and must reject a non-canonical kind"
        ),
    )


#: A conforming unit-test snippet (imports nothing banned) used as the
#: positive control for the EAWF024 test-tier check.
_EAWF024_GOOD_SOURCE = "from __future__ import annotations\n\nimport json\n\nx = json.dumps({})\n"

#: A violating unit-test snippet (imports subprocess + textual + CliRunner)
#: used as the negative control for the EAWF024 test-tier check.
_EAWF024_BAD_SOURCE = "import subprocess\nimport textual\nfrom typer.testing import CliRunner\n"


def check_eawf024_test_tier_wired(
    *,
    check_source_fn: Callable[[str], list[object]] = eawf024.check_source,
) -> GateResult:
    """Assert the EAWF024 test-tier check flags a non-unit import.

    The EAWF024 hook scopes its scan to the git-tracked ``tests/unit/``
    tree, but the meta-gate tracks the pure content check too. This row
    calls :func:`check_source` directly against a clean snippet and a
    snippet importing ``subprocess`` / ``textual`` / ``CliRunner``,
    proving the check has a live non-test call-site and rejects a
    mislabeled unit test.

    Args:
        check_source_fn: Content-only test-tier checker. Defaults to the
            live :func:`eawf.platform.lint.eawf024_test_tier_contract.check_source`.

    Returns:
        A :class:`GateResult` that passes only when the clean snippet
        yields no finding and the banned-import snippet yields at least
        one finding.
    """
    good = check_source_fn(_EAWF024_GOOD_SOURCE)
    bad = check_source_fn(_EAWF024_BAD_SOURCE)
    if not good and bad:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (EAWF024 check_source has a live gate "
                "call-site and flags a non-unit import)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.EAWF024_TEST_TIER_IDLE,
        message=(
            "EAWF024 test-tier check is idle or toothless: "
            f"good={good!r} bad={bad!r}; check_source must be called by the "
            "always-on idle gate and must reject a subprocess/textual/CliRunner import"
        ),
    )


#: A conforming test path: ``tests/unit/`` names a declared kind and
#: ``kernel/state`` mirrors a real package under ``src/eawf/``. Used as the
#: positive control for the EAWF025 test-placement check.
_EAWF025_GOOD_PATH = "tests/unit/kernel/state/test_epoch2_transition_parity.py"

#: A violating test path: ``tools`` is a repo directory, not a package under
#: ``src/eawf/``, so the mirror chain resolves to nothing. Used as the negative
#: control.
_EAWF025_BAD_PATH = "tests/unit/tools/test_idle_surface_report.py"


def check_eawf025_test_placement_wired(
    *,
    check_paths_fn: Callable[..., Sequence[object]] = eawf025.check_test_paths,
) -> GateResult:
    """Assert the EAWF025 test-placement check flags an unmirrored path.

    The EAWF025 hook is diff-scoped: it sees only the paths a commit adds,
    so on a commit that adds no test the rule never executes and a
    regression in it would surface much later, on someone else's commit.
    This row calls :func:`check_test_paths` directly against a conforming
    path and a path whose mirror chain names no source package, giving the
    checker a live non-test call-site and a negative control.

    Args:
        check_paths_fn: Placement checker. Defaults to the live
            :func:`eawf.platform.lint.eawf025_test_placement.check_test_paths`.

    Returns:
        A :class:`GateResult` that passes only when the conforming path
        yields no violation and the unmirrored path yields exactly one.
    """
    source_packages = eawf025.source_package_paths(_REPO_ROOT)
    good = check_paths_fn([_EAWF025_GOOD_PATH], source_packages=source_packages)
    bad = check_paths_fn([_EAWF025_BAD_PATH], source_packages=source_packages)
    if not good and len(bad) == 1:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (EAWF025 check_test_paths has a live gate "
                "call-site and flags an unmirrored test path)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.EAWF025_TEST_PLACEMENT_IDLE,
        message=(
            "EAWF025 test-placement check is idle or toothless: "
            f"good={good!r} bad={bad!r}; check_test_paths must be called by the "
            "always-on idle gate and must reject a path mirroring no source package"
        ),
    )


def _coverage_gate_module() -> ModuleType:
    """Import the sibling ``tools/coverage_gate.py`` module CI executes."""
    # ``coverage_gate`` is importable by name only when ``tools/`` is on
    # ``sys.path``; a caller that loads this gate by path (an out-of-tree unit
    # test) would otherwise hit ``ModuleNotFoundError``.
    tools_dir = str(Path(__file__).resolve().parent)
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import coverage_gate

    return coverage_gate


#: A gate config whose one package clears a 50% line floor and whose TUI floors
#: are zero, so on the freshness probe tree only the stale-report refusal can red.
_COVERAGE_PROBE_PYPROJECT = """
[tool.eawf.coverage.gates.probe]
path = "src/probe/"
line = 50
branch = 0

[tool.eawf.coverage.tui_behavioural]
golden_glob = "tests/snapshots/tui/golden/*.txt"
min_goldens = 0
flow_glob = "tests/snapshots/tui/test_tui_flow.py"
min_flows = 0
""".lstrip()

#: HEAD commit time the freshness probe pins, earlier than every probe mtime, so
#: the source-mtime leg alone decides whether the probe report is stale.
_COVERAGE_PROBE_HEAD_AT = 0


def _seed_coverage_probe_tree(root: Path, *, source_mtime: float, report_mtime: float) -> Path:
    """Write a one-package tree the probe config passes, with the given mtimes.

    Args:
        root: Empty directory to seed.
        source_mtime: mtime of the one measured source file.
        report_mtime: mtime of the written ``coverage.xml``.

    Returns:
        The path of the written ``coverage.xml``.
    """
    (root / "pyproject.toml").write_text(_COVERAGE_PROBE_PYPROJECT, encoding="utf-8")
    source = root / "src" / "probe" / "a.py"
    source.parent.mkdir(parents=True)
    source.write_text("x = 1\n", encoding="utf-8")
    os.utime(source, (source_mtime, source_mtime))
    report = ET.Element("coverage")
    classes = ET.SubElement(ET.SubElement(ET.SubElement(report, "packages"), "package"), "classes")
    cls = ET.SubElement(classes, "class", {"filename": "src/probe/a.py"})
    ET.SubElement(ET.SubElement(cls, "lines"), "line", {"number": "1", "hits": "1"})
    coverage_xml = root / "coverage.xml"
    ET.ElementTree(report).write(coverage_xml, encoding="utf-8", xml_declaration=True)
    os.utime(coverage_xml, (report_mtime, report_mtime))
    return coverage_xml


def _coverage_main_exit(gate: ModuleType, *, source_mtime: float, report_mtime: float) -> int:
    """Run the coverage gate's ``main`` over a throwaway probe tree.

    Args:
        gate: The coverage-gate module whose ``main`` runs.
        source_mtime: mtime of the probe tree's measured source file.
        report_mtime: mtime of the probe tree's ``coverage.xml``.

    Returns:
        The exit code ``main`` returned; its console output is discarded.
    """
    with (
        tempfile.TemporaryDirectory() as tmp,
        contextlib.redirect_stdout(io.StringIO()),
        contextlib.redirect_stderr(io.StringIO()),
    ):
        root = Path(tmp)
        coverage_xml = _seed_coverage_probe_tree(
            root, source_mtime=source_mtime, report_mtime=report_mtime
        )
        return int(gate.main(["--coverage-xml", str(coverage_xml), "--repo-root", str(root)]))


def _probe_coverage_freshness(gate: ModuleType) -> tuple[bool, bool]:
    """Run ``main`` on a stale and on a fresh report and return both verdicts.

    ``head_commit_time`` is pinned for the two runs because the probe tree is not
    a git work tree, and the coverage gate refuses a tree whose HEAD it cannot
    read; the pin is restored before returning.

    Args:
        gate: The coverage-gate module to probe.

    Returns:
        ``(refuses_stale, passes_fresh)``: whether ``main`` exits non-zero on a
        report older than its source, and zero on one newer than it.
    """
    missing = object()
    original = getattr(gate, "head_commit_time", missing)
    gate.head_commit_time = lambda _root: _COVERAGE_PROBE_HEAD_AT
    try:
        refuses_stale = _coverage_main_exit(gate, source_mtime=2_000.0, report_mtime=1_000.0) != 0
        passes_fresh = _coverage_main_exit(gate, source_mtime=1_000.0, report_mtime=2_000.0) == 0
    finally:
        if original is missing:
            del gate.head_commit_time
        else:
            gate.head_commit_time = original
    return refuses_stale, passes_fresh


def check_coverage_gate_helpers_wired(*, gate_module: ModuleType | None = None) -> GateResult:
    """Assert the coverage gate can red on a regression and on a stale report.

    Two probes, both in-process. The ratchet probe feeds
    ``evaluate_package_gates`` a synthetic half-covered runtime class against a
    90% line floor and requires a ``line`` failure naming the probe package, so
    a matcher that selects nothing or an evaluator that never fails reds here.
    The freshness probe runs ``main`` over two throwaway trees and requires it to
    refuse a ``coverage.xml`` older than the source it measures and to pass one
    newer than it: a ``main`` that computes the staleness reason but ignores it,
    or that refuses every report, reds here.

    Args:
        gate_module: The coverage-gate module to probe; defaults to the live
            ``tools/coverage_gate.py``.

    Returns:
        A :class:`GateResult` that passes only when both probes hold.
    """
    gate = gate_module if gate_module is not None else _coverage_gate_module()
    cls = ET.Element("class", {"filename": "src/eawf/runtime/daemon/methods/agent.py"})
    lines = ET.SubElement(cls, "lines")
    ET.SubElement(lines, "line", {"hits": "1"})
    ET.SubElement(lines, "line", {"hits": "0"})
    _report, failures = gate.evaluate_package_gates(
        {"probe": {"path": "src/eawf/runtime/", "line": 90, "branch": 0}}, [cls]
    )
    ratchet_fires = any(entry.startswith("probe: line") for entry in failures)
    refuses_stale, passes_fresh = _probe_coverage_freshness(gate)
    if ratchet_fires and refuses_stale and passes_fresh:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (coverage gate reds a below-floor package, "
                "refuses a stale coverage.xml, and passes a fresh one)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.COVERAGE_GATE_IDLE,
        message=(
            "coverage gate cannot fail: "
            f"ratchet_fires={ratchet_fires} refuses_stale={refuses_stale} "
            f"passes_fresh={passes_fresh}; evaluate_package_gates must red a 50% "
            "package under a 90% floor and main must refuse a coverage.xml older "
            "than its sources while passing a fresh one"
        ),
    )


def check_campaign_producer_class_non_stub(
    *,
    module_text: str | None = None,
    module_path: str = _CAMPAIGN_PRODUCER_MODULE,
) -> GateResult:
    """Assert campaign producer classes do not ship stubbed.

    P30-I20-W06 owns wiring the live campaign producer. This W05 guard is scoped
    to the producer *class* seam: if the class is absent (current pre-W06 tree),
    there is nothing to judge; once a ``*Campaign*Producer*`` class lands, any
    ``NotImplementedError`` / ``pass`` / ellipsis marker inside its class body
    fails this always-on gate before the stub can close a phase.

    Args:
        module_text: Optional injected research module source.
        module_path: Repo-relative research module path to scan.

    Returns:
        A :class:`GateResult` that fails only when a campaign producer class
        contains an obvious stub marker.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    stubbed: list[str] = []
    for match in _CAMPAIGN_PRODUCER_CLASS_RE.finditer(text):
        if _PRODUCER_STUB_RE.search(match.group("body")) is not None:
            stubbed.append(match.group("name"))
    if stubbed:
        return GateResult(
            passed=False,
            failure=GateFailure.CAMPAIGN_PRODUCER_STUB_IDLE,
            message=(
                "campaign producer class seam is stubbed: "
                f"{', '.join(stubbed)} contains pass/ellipsis/NotImplementedError"
            ),
        )
    return GateResult(
        passed=True,
        failure=None,
        message=("idle-contract gate: ok (campaign producer class seams carry no stub markers)"),
    )


def check_campaign_claim_fold_wired(
    *,
    module_text: str | None = None,
    module_path: str = _CAMPAIGN_RUN_MODULE,
) -> GateResult:
    """Assert ``run_campaign`` folds reconciled claims into canonical state.

    The P30-I18 binding: :func:`eawf.runtime.daemon.methods.research.run_campaign`
    folds each round's reconciled claims into the REAL ``state.claims`` through
    the daemon-owned canonical writer
    (:func:`eawf.runtime.daemon.methods.state._commit_worktree_state`, the same
    path ``add_question`` uses for ``state.open_questions``), by binding
    :func:`reconcile_round_claims` against the live ``state`` inside the writer's
    ``apply_func``. Before the binding the reconcile ran only against a throwaway
    ``State.model_construct(claims={}, ...)`` shadow, so a live round never wrote
    a Claim row to ``state.claims`` -- only ``claim_ids`` landed on the round
    record. If a later refactor reverts to the shadow-only path the source no
    longer matches both anchors and this gate fails
    :attr:`GateFailure.CAMPAIGN_CLAIM_FOLD_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the re-idled (shadow-only) outcomes.

    Args:
        module_text: The campaign-run module source. ``None`` reads
            *module_path* off the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the campaign-run module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the module
        both calls ``_commit_worktree_state(`` and reconciles against the live
        ``state`` (``reconcile_round_claims(state``); otherwise ``failure`` is
        :attr:`GateFailure.CAMPAIGN_CLAIM_FOLD_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    commits = _CAMPAIGN_CLAIM_FOLD_RE.search(text) is not None
    reconciles_state = _CAMPAIGN_RECONCILE_FOLD_RE.search(text) is not None
    if commits and reconciles_state:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (run_campaign folds reconciled claims into "
                "state.claims via _commit_worktree_state -- a live round populates "
                "the canonical claim ledger, not a throwaway shadow)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.CAMPAIGN_CLAIM_FOLD_IDLE,
        message=(
            "campaign claim fold is idle: "
            f"{module_path} commits={commits} reconciles_state={reconciles_state} "
            "(expected both True); run_campaign must reconcile each round into the "
            "live state via _commit_worktree_state -- the fold regressed to the "
            "throwaway State.model_construct shadow, so state.claims stays empty"
        ),
    )


def check_campaign_carryover_prune_wired(
    *,
    module_text: str | None = None,
    module_path: str = _CAMPAIGN_RUN_MODULE,
) -> GateResult:
    """Assert ``run_campaign`` calls the L1 carryover prune between rounds.

    The P30-I18 binding: :func:`eawf.runtime.daemon.methods.research.run_campaign`
    calls :func:`eawf.kernel.spec.pruning.prune_round_carryover` over the
    accumulated claim ledger between rounds, so the next round + the synthesis
    work over only the live claims (the provably-dead rows drop). Before the
    binding the L1 reducer had ZERO production callers -- a built-but-idle
    contract the P30 thesis forbids. If a later refactor drops the call the
    source no longer matches and this gate fails
    :attr:`GateFailure.CAMPAIGN_CARRYOVER_PRUNE_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the re-idled (no-caller) outcomes.

    Args:
        module_text: The campaign-run module source. ``None`` reads
            *module_path* off the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the campaign-run module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the module
        carries a ``prune_round_carryover(`` call; otherwise ``failure`` is
        :attr:`GateFailure.CAMPAIGN_CARRYOVER_PRUNE_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    if _CAMPAIGN_CARRYOVER_CALL_RE.search(text) is not None:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (run_campaign calls prune_round_carryover "
                "between rounds -- the L1 reducer has a production caller, not idle)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.CAMPAIGN_CARRYOVER_PRUNE_IDLE,
        message=(
            "campaign carryover prune is idle: "
            f"{module_path} carries no prune_round_carryover(...) call, so the L1 "
            "between-rounds reducer has zero production callers (the run never prunes "
            "the carried claim ledger -- the contract ships built-but-idle)"
        ),
    )


def check_drive_ladders_wired(
    *,
    module_text: str | None = None,
    module_path: str = _LIVE_DRIVE_MODULE,
) -> GateResult:
    """Assert the LIVE drive enables the spawn + repair ladders.

    The W11 binding: :func:`eawf.runtime.daemon.methods.fleet.start_background_drive`
    arms ``arm_drive`` with the production
    :func:`~eawf.runtime.daemon.methods.fleet._live_lane_error_classifier`
    (``classify=``) and the live
    :func:`~eawf.runtime.daemon.methods.fleet._build_live_lane_repair_hook` hook
    (``repair=``), so the bounded spawn ladder (DL-11, :func:`spawn_lane_or_fork`)
    and the bounded grounded repair ladder (DL-7, :func:`repair_lane_or_fork`)
    FIRE on a real autopilot run. With either kwarg dropped the loop takes the
    direct-spawn / terminal-fork path -- the ladders ship built-but-dormant on
    the live path (they fire only when a test injects the hooks). If a later
    refactor drops either binding the source no longer matches and this gate
    fails :attr:`GateFailure.DRIVE_LADDERS_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the re-dormant outcomes.

    Args:
        module_text: The fleet module source. ``None`` reads *module_path* off
            the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the fleet module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the module
        arms the drive with BOTH ``classify=_live_lane_error_classifier`` and
        ``repair=repair_hook``; otherwise ``failure`` is
        :attr:`GateFailure.DRIVE_LADDERS_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    wires_classify = _DRIVE_CLASSIFY_RE.search(text) is not None
    wires_repair = _DRIVE_REPAIR_RE.search(text) is not None
    if wires_classify and wires_repair:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (live drive arms classify + repair -- the "
                "bounded spawn ladder (DL-11) + grounded repair ladder (DL-7) fire "
                "on a real run, not just under test)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.DRIVE_LADDERS_IDLE,
        message=(
            "live drive ladders are idle: "
            f"{module_path} wires_classify={wires_classify} wires_repair={wires_repair} "
            "(expected both True); start_background_drive must arm arm_drive with "
            "classify=_live_lane_error_classifier AND repair=repair_hook -- a dropped "
            "kwarg re-dormants the spawn / repair ladder on the live autopilot run"
        ),
    )


def check_live_output_text_wired(
    *,
    module_text: str | None = None,
    module_path: str = _LIVE_OUTPUT_MODULE,
) -> GateResult:
    """Assert the live wave spawn supplies ``output_text`` to the stdout producer.

    The W11 binding: the live wave-spawn dispatch
    (:func:`eawf.runtime.daemon.methods.agent._spawn_and_dispatch`) threads the
    spawned agent's OWN captured answer (``spawn_result.text``) into
    :func:`~eawf.runtime.daemon.dispatch_runner.run_dispatch` as
    ``output_text=``, so the W08 stdout producer emits an ``agent.output`` event
    the agent-watch live tail renders. Without the thread the producer is wired
    into ``run_dispatch`` but no live caller supplies it, so ``emit_agent_output``
    never fires on a real spawn and the tail stays empty. If a later refactor
    drops the binding the source no longer matches and this gate fails
    :attr:`GateFailure.LIVE_OUTPUT_TEXT_IDLE`.

    The probe reads source only -- it never mutates state, never writes a file,
    and never runs a mutating ``eawf`` command. *module_text* is injectable so a
    test can drive both the wired and the re-dormant outcomes.

    Args:
        module_text: The live dispatch module source. ``None`` reads
            *module_path* off the working tree under :data:`_REPO_ROOT`.
        module_path: Repo-relative path of the live dispatch module to scan.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the module
        threads ``output_text=spawn_result.text`` into the live dispatch;
        otherwise ``failure`` is :attr:`GateFailure.LIVE_OUTPUT_TEXT_IDLE`.
    """
    text = module_text if module_text is not None else (_REPO_ROOT / module_path).read_text()
    if _LIVE_OUTPUT_TEXT_RE.search(text) is not None:
        return GateResult(
            passed=True,
            failure=None,
            message=(
                "idle-contract gate: ok (live spawn threads output_text=spawn_result.text "
                "-- the stdout producer fires on a real spawn, the live tail is not empty)"
            ),
        )
    return GateResult(
        passed=False,
        failure=GateFailure.LIVE_OUTPUT_TEXT_IDLE,
        message=(
            "live output_text fan is idle: "
            f"{module_path} carries no 'output_text=spawn_result.text' thread into "
            "run_dispatch, so the W08 stdout producer never fires on a real spawn "
            "(the agent-watch live tail stays empty -- the producer is wired but unfed)"
        ),
    )


def check_runtime_gate_is_not_idle(
    *,
    precommit_text: str | None = None,
    precommit_path: Path | None = None,
) -> GateResult:
    """Assert the always-run pre-commit idle-contract gate remains enabled."""
    path = precommit_path or (_REPO_ROOT / ".pre-commit-config.yaml")
    text = precommit_text if precommit_text is not None else path.read_text()
    required = (
        "id: idle-contract-gate",
        "entry: uv run python tools/idle_contract_gate.py --cached",
        "pass_filenames: false",
        "always_run: true",
        "stages: [pre-commit]",
    )
    missing = [snippet for snippet in required if snippet not in text]
    if missing:
        return GateResult(
            passed=False,
            failure=GateFailure.RUNTIME_GATE_IDLE,
            message=f"runtime close gate idle: pre-commit binding missing {missing!r}",
        )
    return GateResult(
        passed=True,
        failure=None,
        message="runtime close gate binding: ok (idle-contract-gate always runs at pre-commit)",
    )


def _idle_contract_console_module() -> ModuleType:
    """Import the sibling ``idle_contract_console.py`` (mirrors coverage_gate's loader)."""
    tools_dir = str(Path(__file__).resolve().parent)
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import idle_contract_console

    return idle_contract_console


def check_console_app_construction_wired(*, module: ModuleType | None = None) -> GateResult:
    """Assert every TUI ``App`` subclass has a reachable production construction site.

    Discovery and the reachability walk (pyproject console-script entry ->
    CLI dispatcher -> the TUI launcher's ``eawf.*`` imports) live in the
    sibling :mod:`idle_contract_console` module -- this file is already over
    its line cap, so the rule itself is defined there and this check only
    wires it into the gate run.

    Args:
        module: The ``idle_contract_console`` module to probe; defaults to
            the live sibling module.

    Returns:
        A :class:`GateResult` that fails naming any App subclass with no
        reachable ``ClassName(`` construction call -- built (W30's
        ``ConsoleApp``, or a future console screen) but never opened by any
        console-script entry point.
    """
    mod = module if module is not None else _idle_contract_console_module()
    unconstructed = mod.find_unconstructed_console_apps()
    if unconstructed:
        return GateResult(
            passed=False,
            failure=GateFailure.CONSOLE_APP_CONSTRUCTION_IDLE,
            message=(
                "console App subclass ships with no reachable production "
                f"construction site: {', '.join(unconstructed)} -- built but never "
                "opened by any console-script entry point"
            ),
        )
    return GateResult(
        passed=True,
        failure=None,
        message="idle-contract gate: ok (every TUI App subclass has a reachable launcher)",
    )


# =========================================================================== #
# Registry-wide sweep: every registered audit-DSL kind must be wired on.
# =========================================================================== #

#: A registered-kinds source with the shape of
#: :func:`eawf.workflow.audit_dsl.registry.registered_audit_dsl_kinds`. Injected
#: so a test can drive the sweep with a synthetic kind set (e.g. one re-idled
#: kind) without editing the real registry.
type RegisteredKindsFn = Callable[[], frozenset[str]]

#: A wired-kinds source with the shape of
#: :func:`eawf.workflow.verify.readiness.wired_audit_dsl_kinds`. Injected so a
#: test can simulate a kind losing its production binding (re-idling) and assert
#: the sweep reds.
type WiredKindsFn = Callable[[], frozenset[str]]


def check_audit_dsl_kinds_wired(
    *,
    registered_fn: RegisteredKindsFn = registered_audit_dsl_kinds,
    wired_fn: WiredKindsFn = wired_audit_dsl_kinds,
) -> GateResult:
    """Assert every registered audit-DSL kind has a production binding.

    The wired-on sweep (P30-I10 QUAL-2) generalizes the B091 idle-verifier
    lesson to the audit-DSL kind registry: a kind that is registered (so it
    advertises itself as a falsifier) but has no production binding -- no
    oracle-tier mapping and no close-gate wiring -- can never be escalated to
    by the live close gate, so it ships registered-but-idle exactly like the
    spec-jury producer did.

    The check compares the full registered set (*registered_fn*, the
    :data:`CHECK_REGISTRY` keys plus the state-scoring close-gate kinds)
    against the wired set (*wired_fn*, the kernel ``_GATE_KIND_TIER`` map plus
    the supplemental checkout-gate tiers plus the close-gate kinds). Any
    registered kind absent from the wired set is idle and reds CI, naming the
    offending kind(s).

    Both sources are injectable so a test can drive the re-idle failure mode
    (a wired set missing a registered kind) without editing the real registry
    or tier map -- mirroring how :func:`check_idle_contract` injects its
    ``profiles`` / ``resolve_fn``. The check reads only -- it never mutates
    state, never writes a file, and never runs a mutating ``eawf`` command.

    Args:
        registered_fn: Source of the full registered kind set. Defaults to
            the live :func:`registered_audit_dsl_kinds`.
        wired_fn: Source of the production-wired kind set. Defaults to the
            live :func:`wired_audit_dsl_kinds`.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when every
        registered kind is wired; otherwise ``failure`` is
        :attr:`GateFailure.AUDIT_DSL_KIND_IDLE` and the message names the
        idle kind(s).
    """
    registered = registered_fn()
    wired = wired_fn()
    idle = sorted(registered - wired)
    if idle:
        return GateResult(
            passed=False,
            failure=GateFailure.AUDIT_DSL_KIND_IDLE,
            message=(
                f"audit-DSL kind(s) ship registered-but-idle: {', '.join(idle)} "
                "-- a registered kind needs an oracle-tier mapping or a close-gate "
                "wiring (no production caller means the close gate can never "
                "escalate to it)"
            ),
        )
    return GateResult(
        passed=True,
        failure=None,
        message=(
            f"idle-contract gate: ok (all {len(registered)} registered audit-DSL "
            "kinds are wired on -- each has an oracle-tier or close-gate binding)"
        ),
    )
