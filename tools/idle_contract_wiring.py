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
# I09 jury-validation binding: the CLI caller must stay wired, not idle.
# =========================================================================== #


#: The CLI command that gives ``validate_jury`` a live caller. Read off
#: the working tree.
_METRICS_CLI_MODULE = "src/eawf/surfaces/cli/commands/metrics.py"

#: The CLI command binds ``validate_jury(...)`` directly, giving it a live caller
#: (the moment a labelled cohort lands the CLI surfaces a scored report).
_VALIDATE_JURY_CALL_RE = re.compile(r"\bvalidate_jury\s*\(")


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
golden_glob = "tests/fixtures/console/golden/sequences/*frames*.json"
min_goldens = 0
flow_glob = "tests/fixtures/console/golden/sequences/journeys.json"
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
