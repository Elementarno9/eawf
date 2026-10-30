"""Deterministic idle-contract gate.

This repo has a history of building a verifier and then leaving it IDLE
forever -- a dead gate that never runs (tracked as the B091 idle-verifier
regression). This gate makes "wired, not idle" a CHECKED invariant rather
than a hope.

The gates below run from :func:`main`, in precedence order, and any failing
exits non-zero:

- :func:`check_skill_body_binding` -- a *binding-proof* probe that drives a
  deliberately drifted dict body through ``run_skill`` and asserts the emit
  path RAISES ``pydantic.ValidationError``. The drift is an extra-forbid key
  against a registered body model, so a working binding rejects it before the
  envelope is built. If a later refactor lets the body-validation-at-emit
  binding regress to idle, the drifted body would emit silently and this probe
  stops raising -- failing the gate. This is the meta-binding that keeps the
  emit-validation binding from silently going dead.
- :func:`check_i03_contracts` -- an in-process probe for the I03 contract:
  ``mockup_golden_diff`` must be a registered ``CheckKind`` with a mapped
  ``OracleTier``.
- :func:`check_validate_jury_cli_wired` -- a source-scan probe asserting
  ``validate_jury`` has a live CLI caller: the ``eawf metrics
  jury-validation`` command binds it directly. Orphaning the reducer reds this
  row.
- :func:`check_runtime_gate_is_not_idle` -- verifies this always-run
  pre-commit gate stays enabled so the runtime close gate cannot ship idle.
- :func:`detect_idle_contracts` -- a *meta-gate* that reads a git diff and
  flags any newly-defined contract (a ``check_*`` / ``*_gate`` / ``*_lint``
  function, a ``CheckKind`` runner registration, an ``OracleTier`` dispatch
  arm, or an ``eawf0##_*.py`` lint module) that ships idle: no call-site
  outside its own module AND no asserting test references it. The call-site
  probe is AST-based (call / import / name detection), so a comment or docstring
  mention no longer discharges the call-site contract the way the prior
  word-boundary regex did. The meta-gate generalizes the B091 lesson from one
  hardcoded contract to *every* future contract a diff introduces, so a fresh
  dead verifier is caught the same commit it lands.
- :func:`check_contract_exercised` -- a *dynamic* leg (armed under
  ``--phase-close``) that proves a bound contract actually RAN, not just that it
  is called + tested. It reads the stores each bound contract writes and reds the
  phase close on a contract that captured zero runtime output -- the exact blind
  spot behind EU capture passing while writing no rows. A jury-gated contract
  (the ballot) is skipped until the jury has convened at least once.

The body-binding probe is a pure in-process object -- the gate never mutates
state, never writes a file, never runs a mutating ``eawf`` command.
:func:`check_skill_body_binding` takes the ``run_skill`` callable (defaulting
to the live production value) so the failure mode is testable without editing
the engine. Each check returns a typed :class:`GateResult` and the thin
:func:`main` CLI maps them onto an exit code.

This module keeps the dynamic-exercise leg and :func:`main`;
the other checks live in sibling modules it re-exports: ``idle_contract_probes``
(in-process binding probes), ``idle_contract_wiring`` (source-scan and liveness
checks), ``idle_contract_meta`` (the meta-gate), and ``idle_contract_common``
(the shared result types).

Invocation:

    uv run tools/idle_contract_gate.py

Exit codes:
- ``0`` -- the emit-time body validation rejects a drifted body, AND all I03
  probes fire, AND no newly-defined contract in the staged diff ships idle.
- ``1`` -- a contract failed (the failure is named on stderr).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from eawf.kernel.spec.common import _tier_for_gate_kind
from eawf.kernel.state.enums import (
    AgentReportVerdict,
    ReportSource,
)
from eawf.workflow.audit_dsl.registry import CHECK_REGISTRY, registered_audit_dsl_kinds
from eawf.workflow.skills.engine import run_skill as _run_skill
from eawf.workflow.verify.readiness import wired_audit_dsl_kinds

# The sibling modules below live beside this file, not in an installed package;
# a caller that imports this gate as ``tools.idle_contract_gate`` or by file path
# has no ``tools/`` on ``sys.path`` until this runs.
_TOOLS_DIR = str(Path(__file__).resolve().parent)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from idle_contract_common import (  # noqa: E402
    _REPO_ROOT,
    GateResult,
)
from idle_contract_common import GateFailure as GateFailure  # noqa: E402
from idle_contract_meta import (  # noqa: E402
    IdleContractFinding,
    MissingDischarge,
    _default_diff,
    _default_read,
    _default_tree,
    detect_idle_contracts,
)
from idle_contract_meta import _ast_references_symbol as _ast_references_symbol  # noqa: E402
from idle_contract_meta import _has_asserting_test as _has_asserting_test  # noqa: E402
from idle_contract_meta import _has_call_site as _has_call_site  # noqa: E402
from idle_contract_probes import check_i03_contracts, check_skill_body_binding  # noqa: E402
from idle_contract_wiring import (  # noqa: E402
    _EAWF023_BAD_PATH as _EAWF023_BAD_PATH,
)
from idle_contract_wiring import (  # noqa: E402
    _EAWF023_GOOD_PATH as _EAWF023_GOOD_PATH,
)
from idle_contract_wiring import (  # noqa: E402
    _EAWF024_BAD_SOURCE as _EAWF024_BAD_SOURCE,
)
from idle_contract_wiring import (  # noqa: E402
    _EAWF024_GOOD_SOURCE as _EAWF024_GOOD_SOURCE,
)
from idle_contract_wiring import (  # noqa: E402
    _EAWF025_BAD_PATH as _EAWF025_BAD_PATH,
)
from idle_contract_wiring import (  # noqa: E402
    _EAWF025_GOOD_PATH as _EAWF025_GOOD_PATH,
)
from idle_contract_wiring import (  # noqa: E402
    check_audit_dsl_kinds_wired,
    check_console_app_construction_wired,
    check_coverage_gate_helpers_wired,
    check_eawf023_artifact_placement_wired,
    check_eawf024_test_tier_wired,
    check_eawf025_test_placement_wired,
    check_runtime_gate_is_not_idle,
    check_validate_jury_cli_wired,
)

# =========================================================================== #
# Dynamic-exercise leg: a bound contract must produce runtime output per phase.
# =========================================================================== #

#: A store-row reader: returns the parsed records of ``.ea/store/<stem>.jsonl``
#: for a store *stem*. Enveloped stores are unwrapped to their ``payload`` so a
#: predicate reads record fields directly; plain (non-enveloped) stores are
#: returned as parsed. Injected so a test feeds synthetic rows without a real
#: store.
type StoreRowsFn = Callable[[str], list[dict[str, Any]]]

#: A jury-convened predicate: ``True`` once the cross-vendor jury has convened at
#: least once (read from the event store). Injected so a test drives both the
#: gated-off and gated-on ballot outcomes.
type JuryConvenedFn = Callable[[], bool]

#: Event type of the evidence-store row a convened spec jury wrote for its raw
#: per-juror FindingSets. Rows written before the jury was retired still count.
_SPEC_JURY_FINDINGSET_EVENT_TYPE = "spec_jury_findingset"


@dataclass(frozen=True, slots=True)
class _BoundContract:
    """A contract whose runtime exercise is proved by a row in a store.

    Attributes:
        name: The contract name a finding reports. This is a bound-contract
            label, not a source symbol -- the dynamic leg proves runtime output,
            not a static call-site.
        store_stem: The ``.ea/store/<stem>.jsonl`` store file the contract writes.
        counts_row: Predicate deciding whether one store record proves exercise
            (e.g. an actuals row counts only when ``elapsed_eu > 0``).
        gated_by_jury: When ``True`` the contract is SKIPPED until the jury has
            convened at least once, so an un-exercised-by-design contract cannot
            wedge a phase close (the ballot contract, reserved for P31).
    """

    name: str
    store_stem: str
    counts_row: Callable[[Mapping[str, Any]], bool]
    gated_by_jury: bool = False


def _row_has_authored_verdict(record: Mapping[str, Any]) -> bool:
    """Count an auditor report only when its agent authored a typed verdict.

    A daemon-synthesized body is minted after the agent's own output failed
    validation, so it proves the producer ran without producing a verdict.

    Args:
        record: A parsed store record (the unwrapped auditor-report payload).

    Returns:
        ``True`` when ``body.verdict`` is an :class:`AgentReportVerdict` value
        and ``body.report_source`` is not ``synthesized``.
    """
    body = record.get("body")
    if not isinstance(body, dict):
        return False
    verdicts = {verdict.value for verdict in AgentReportVerdict}
    return (
        body.get("verdict") in verdicts
        and body.get("report_source") != ReportSource.SYNTHESIZED.value
    )


def _row_has_ground_truth(record: Mapping[str, Any]) -> bool:
    """Count a gold label only when it pins a boolean ground truth to a wave.

    Args:
        record: A parsed gold-label store record.

    Returns:
        ``True`` when ``wave_id`` is a non-empty string and ``ground_truth`` a bool.
    """
    wave_id = record.get("wave_id")
    return (
        isinstance(wave_id, str) and bool(wave_id) and isinstance(record.get("ground_truth"), bool)
    )


def _row_has_cast_verdict(record: Mapping[str, Any]) -> bool:
    """Count a juror ballot only when the juror cast a verdict, not an abstention.

    Args:
        record: A parsed jury-ballot store record (the unwrapped payload).

    Returns:
        ``True`` when ``verdict`` is a non-empty string.
    """
    verdict = record.get("verdict")
    return isinstance(verdict, str) and bool(verdict)


#: The I22 bound contracts the dynamic leg enforces at phase close: the
#: verdict producer (an ``auditor_report`` row with an authored verdict) and
#: calibration (a ``gold_label`` row pinning a boolean ground truth). The
#: juror-ballot contract is deliberately absent -- see :data:`_BALLOT_CONTRACT`.
_I22_BOUND_CONTRACTS: tuple[_BoundContract, ...] = (
    _BoundContract(
        name="verdict_producer",
        store_stem="auditor_report",
        counts_row=_row_has_authored_verdict,
    ),
    _BoundContract(
        name="calibration",
        store_stem="gold_label",
        counts_row=_row_has_ground_truth,
    ),
)

#: The juror-ballot contract, EXCLUDED from the I22 enforced set. The
#: cross-vendor jury has never convened in repo history, so enforcing a
#: ballot-output contract now would wedge the phase re-close on a row nothing in
#: I22 produces. It is gated behind the "jury convened >= 1" predicate and earns
#: its contract in P31 calibration -- adding it to the enforced set stays inert
#: until a real jury run records its event.
_BALLOT_CONTRACT = _BoundContract(
    name="juror_ballot",
    store_stem="jury_ballot",
    counts_row=_row_has_cast_verdict,
    gated_by_jury=True,
)


def _default_store_rows(store_stem: str) -> list[dict[str, Any]]:
    """Read parsed records from ``.ea/store/<store_stem>.jsonl``, honest-empty when absent.

    Enveloped stores (:class:`eawf.kernel.store.envelope.Envelope`) wrap the
    kind-specific record in a ``payload`` dict; this reader unwraps it so a
    predicate reads record fields directly. Plain stores (e.g. the gold-label
    store, which carries no envelope) are returned as parsed. A missing file, a
    blank line, or a malformed line is skipped rather than raising, so the leg is
    honest-empty on a store no wave has written yet.

    Args:
        store_stem: The store file stem under ``.ea/store/`` (``auditor_report``,
            ``actual``, ``gold_label``, ...).

    Returns:
        The parsed records, oldest-first; ``[]`` when the store file is absent.
    """
    path = _REPO_ROOT / ".ea" / "store" / f"{store_stem}.jsonl"
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        records.append(payload if isinstance(payload, dict) else record)
    return records


def _default_jury_convened(store_rows_fn: StoreRowsFn = _default_store_rows) -> bool:
    """Return whether the cross-vendor jury has convened at least once.

    A convened jury persists its raw per-juror FindingSets as an evidence-store
    row typed :data:`_SPEC_JURY_FINDINGSET_EVENT_TYPE`; only that exact type
    counts. Other jury-named events (an operator's
    ``jury_gold_label``, for one) record no convening, so a substring match on
    "jury" would ungate the ballot contract before any ballot could exist.

    Args:
        store_rows_fn: Reader returning the parsed records for a store stem.

    Returns:
        ``True`` when a FindingSet row exists in the evidence store; ``False``
        otherwise.
    """
    return any(
        record.get("event_type") == _SPEC_JURY_FINDINGSET_EVENT_TYPE
        for record in store_rows_fn("evidence")
    )


def check_contract_exercised(
    *,
    store_rows_fn: StoreRowsFn = _default_store_rows,
    jury_convened_fn: JuryConvenedFn = _default_jury_convened,
    contracts: Sequence[_BoundContract] = _I22_BOUND_CONTRACTS,
) -> list[IdleContractFinding]:
    """Flag every bound contract that produced NO runtime output this phase.

    The static meta-gate (:func:`detect_idle_contracts`) proves a contract is
    *called* and *tested*; this dynamic leg proves it actually *ran*, closing the
    blind spot where a contract is wired + tested yet captures zero data. For each
    bound contract it reads the contract's store and flags a
    :attr:`MissingDischarge.NO_RUNTIME_OUTPUT` finding when no record counts as
    exercise. A jury-gated contract (the ballot) is SKIPPED entirely while the
    jury-convened predicate is ``False`` so an un-exercised-by-design contract
    cannot wedge a phase close.

    The leg reads stores only -- it never mutates state, writes a file, or runs a
    mutating ``eawf`` command. The store reader, jury predicate, and contract set
    are injected (each defaulting to the live production value) so a test drives
    the zero-output, has-output, and jury-gated outcomes without a real store.

    Args:
        store_rows_fn: Reader returning the parsed records for a store stem.
        jury_convened_fn: Predicate gating the ballot contract.
        contracts: The bound contracts to check; defaults to the I22 three.

    Returns:
        One :class:`IdleContractFinding` per bound contract with no exercising
        row, in contract order. Empty when every non-skipped contract has at
        least one exercising row.
    """
    findings: list[IdleContractFinding] = []
    for contract in contracts:
        if contract.gated_by_jury and not jury_convened_fn():
            continue
        rows = store_rows_fn(contract.store_stem)
        if any(contract.counts_row(row) for row in rows):
            continue
        findings.append(
            IdleContractFinding(
                symbol=contract.name,
                module=f".ea/store/{contract.store_stem}.jsonl",
                missing=MissingDischarge.NO_RUNTIME_OUTPUT,
            )
        )
    return findings


def _render_findings(findings: Sequence[IdleContractFinding]) -> str:
    """Render *findings* as a human-readable multi-line failure message.

    Args:
        findings: The orphan contracts to describe.

    Returns:
        One line per finding, each naming the symbol, its module, and the
        missing discharge, with advice specific to the discharge kind.
    """
    lines = [f"idle-contract gate: {len(findings)} contract(s) ship idle:"]
    for finding in findings:
        if finding.missing is MissingDischarge.NO_RUNTIME_OUTPUT:
            advice = (
                "a bound contract must produce at least one row in its store "
                "during the phase -- a wired-but-never-exercised contract captures "
                "nothing"
            )
        else:
            advice = "a new contract needs a call-site outside its module AND an asserting test"
        lines.append(
            f"  - {finding.symbol} (in {finding.module}) is missing "
            f"{finding.missing.value}; {advice}"
        )
    return "\n".join(lines)


def _report_result(result: GateResult) -> bool:
    """Print *result* to the right stream and report whether it failed.

    A passing result prints its message to stdout; a failing one prints to
    stderr. Folding the print + stream choice here keeps :func:`main` a flat
    sequence of ``failed |= _report_result(...)`` calls instead of repeating the
    ``if result.passed: ... else: ...`` block once per gate.

    Args:
        result: The gate outcome to report.

    Returns:
        ``True`` when *result* failed (so the caller can fold it into the
        aggregate exit status), ``False`` on a pass.
    """
    if result.passed:
        print(result.message)
        return False
    print(result.message, file=sys.stderr)
    return True


def _resolve_diff_range(argv: list[str]) -> str:
    """Return the meta-gate diff range from either script-style or direct argv.

    ``main`` historically received ``sys.argv`` (script path at index 0), while
    unit tests and PR-range callers naturally pass argparse-style args (no
    script path). Accept both so the pre-commit hook (``--cached``) and PR range
    invocation (``BASE..HEAD``) drive the same range-aware code path.

    Args:
        argv: Either ``sys.argv`` or an argument list excluding the program.

    Returns:
        ``--cached`` when no explicit range is supplied; otherwise the first
        non-script argument.
    """
    if not argv:
        return "--cached"
    if len(argv) == 1:
        only = argv[0]
        if only.endswith(".py") or only.endswith("idle_contract_gate"):
            return "--cached"
        return only
    return argv[1]


def main(argv: list[str]) -> int:
    """Run all idle-contract gates over the staged diff and current tree.

    The emit-validation binding-proof probe
    (:func:`check_skill_body_binding`) runs first, then the I03 contract probe
    (:func:`check_i03_contracts`), then the I09 jury-validation CLI
    binding probe (:func:`check_validate_jury_cli_wired`), then the runtime-gate binding
    check (:func:`check_runtime_gate_is_not_idle`), then the
    console-app construction-reachability check
    (:func:`check_console_app_construction_wired`), then the
    registry-wide audit-DSL wired-on sweep
    (:func:`check_audit_dsl_kinds_wired`), then the meta-gate
    (:func:`detect_idle_contracts`) over the staged diff. All must pass; the
    exit code is non-zero when any fails.

    Under ``--phase-close`` one further leg runs after the static gates: the
    dynamic-exercise leg (:func:`check_contract_exercised`) reads the stores each
    bound contract writes and reds the close on a contract that captured no
    runtime output this phase. It is flag-gated because runtime output accrues
    across a whole phase, not on any single staged commit, so it must not fire on
    every pre-commit run.

    Args:
        argv: Process argv (with or without the script path). The first
            non-script argument overrides the default ``--cached`` diff range
            fed to the meta-gate (e.g. ``HEAD~1..HEAD`` for a CI range check);
            the ``--phase-close`` flag additionally arms the dynamic-exercise
            leg and is stripped before the diff range is resolved.

    Returns:
        ``0`` when all gates pass; ``1`` when any fails.
    """
    phase_close = "--phase-close" in argv
    diff_range = _resolve_diff_range([arg for arg in argv if arg != "--phase-close"])

    failed = False
    # Pass the module-level run_skill explicitly so a test (or a future caller)
    # can patch it via attribute assignment -- a default-bound parameter would
    # snapshot the unpatched function at def time (see the same pattern below
    # for the meta-gate's diff / tree / read sources).
    failed |= _report_result(check_skill_body_binding(run_skill_fn=_run_skill))

    failed |= _report_result(
        check_i03_contracts(
            registry=CHECK_REGISTRY,
            tier_for_gate_kind_fn=_tier_for_gate_kind,
        )
    )

    # The I09 validate_jury CLI binding reads its live call-site off the
    # working tree (not the monkeypatchable _default_read, so it stays immune
    # to the meta-gate's reader stub). A re-idle fails its row and reds the
    # gate.
    failed |= _report_result(check_validate_jury_cli_wired())

    # P30-I20 honest-close rows: close the meta-gate holes for EAWF023's
    # single-path helper and the coverage-gate private matcher/runner. EAWF025
    # joins them because it is diff-scoped: a commit that adds no test never
    # runs the rule, so only an always-on row keeps the checker honest.
    failed |= _report_result(check_eawf023_artifact_placement_wired())
    failed |= _report_result(check_eawf024_test_tier_wired())
    failed |= _report_result(check_eawf025_test_placement_wired())
    failed |= _report_result(check_coverage_gate_helpers_wired())

    failed |= _report_result(check_runtime_gate_is_not_idle())

    failed |= _report_result(check_console_app_construction_wired())

    # Pass the module-level wired-on sources explicitly so a test (or a future
    # caller) can patch them via attribute assignment.
    failed |= _report_result(
        check_audit_dsl_kinds_wired(
            registered_fn=registered_audit_dsl_kinds,
            wired_fn=wired_audit_dsl_kinds,
        )
    )

    # Pass the module-level default sources explicitly so a test (or a future
    # caller) can patch them via attribute assignment -- a default-bound
    # parameter would snapshot the unpatched function at def time.
    findings = detect_idle_contracts(
        diff_range,
        diff_fn=_default_diff,
        tree_fn=_default_tree,
        read_fn=_default_read,
    )
    if findings:
        print(_render_findings(findings), file=sys.stderr)
        failed = True
    else:
        print("idle-contract meta-gate: ok (no new contract ships idle)")

    # Dynamic-exercise leg (per-phase, not per-commit): a bound contract can be
    # statically called + tested yet still capture nothing at runtime. Under
    # --phase-close this reads the stores each bound contract writes and reds the
    # close on a zero-output contract. It is gated behind the flag because runtime
    # output accrues across a whole phase, not on any single staged commit, so
    # firing it on every pre-commit would red on stores no commit has written yet.
    if phase_close:
        # Pass the module-level sources explicitly (same reason as the checks
        # above): a default-bound parameter would snapshot the unpatched function
        # at def time, so a test could not redirect the store reader.
        runtime_findings = check_contract_exercised(
            store_rows_fn=_default_store_rows,
            jury_convened_fn=_default_jury_convened,
        )
        if runtime_findings:
            print(_render_findings(runtime_findings), file=sys.stderr)
            failed = True
        else:
            print("idle-contract dynamic leg: ok (every bound contract has runtime output)")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
