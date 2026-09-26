"""The test-kind taxonomy: one declaration of the suite's kind axis.

A test's *kind* is the shape of evidence it produces, and it is carried
in four places at once: the pytest marker the item wears, the directory
the file lives in, the quality lane that runs it, and the proof locus a
criterion cites when it leans on that kind. Those four drift silently --
a marker gets registered with no directory, a directory appears with no
marker and dies deselected, a new kind ships with no declared oracle.

:class:`TestKind` is the single source; the other four surfaces are
projections of it. :func:`taxonomy_drift` validates all four against the
enum as one unit, so a kind present in any single surface but absent
from the enum is a hard failure rather than a slow divergence.

Two registries carry the pre-taxonomy tree so the check passes on the
current suite while every new kind is held to the contract:
:data:`NON_KIND_TEST_DIRS` names the ``tests/`` sub-directories that
partition by subsystem rather than by kind, and
:data:`NON_KIND_MARKERS` names the markers that are modifiers
(``slow``) or opt-in lanes (``eval``) rather than kinds.

``e2e`` and ``perf`` stay first-class kinds rather than folding into
``integration``: both already own a directory, ``e2e`` already owns a
registered marker, and each runs in its own quality lane, so folding
them would erase the lane distinction the taxonomy exists to make
legible.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from eawf.kernel.spec.common import ProofLocus

#: Repo-relative root every test file lives beneath.
TESTS_ROOT = "tests/"


class TestKind(StrEnum):
    """The kind of evidence a test produces.

    The value is simultaneously the pytest marker name, the ``tests/``
    sub-directory name, and the key each per-kind mapping is addressed
    by, so the three never need a translation table.
    """

    UNIT = "unit"
    CONTRACT = "contract"
    INTEGRATION = "integration"
    PROPERTY = "property"
    METAMORPHIC = "metamorphic"
    GOLDEN = "golden"
    TUI = "tui"
    CONFORMANCE = "conformance"
    E2E = "e2e"
    PERF = "perf"


#: Registered pytest markers that are NOT kinds. ``slow`` is a runtime
#: modifier a test of any kind may wear; ``acceptance`` and
#: ``golden_scenarios`` are scenario-scope labels; ``eval`` is an opt-in
#: suite deselected by the default ``addopts`` expression.
NON_KIND_MARKERS: frozenset[str] = frozenset(
    {
        "slow",
        "acceptance",
        "eval",
        "golden_scenarios",
    }
)

#: ``tests/`` sub-directories that partition by subsystem rather than by
#: kind. These pre-date the taxonomy; they are exempt from the placement
#: rule and are never auto-marked. A directory drops off this registry
#: when its files are re-filed under a kind.
NON_KIND_TEST_DIRS: frozenset[str] = frozenset(
    {
        "cli",
        "config",
        "daemon",
        "eval",
        "fixtures",
        "kernel",
        "lint",
        "observability",
        "platform",
        "regression",
        "runtime",
        "runtimes",
        "snapshots",
        "state",
        "workflow",
    }
)

#: The oracle a kind's evidence is read from. A criterion that leans on
#: a kind cites this locus, so adding a kind forces an explicit answer to
#: "where is this kind's proof observed".
KIND_PROOF_LOCI: Mapping[TestKind, ProofLocus] = {
    TestKind.UNIT: ProofLocus.PYTEST,
    TestKind.CONTRACT: ProofLocus.SCHEMA,
    TestKind.INTEGRATION: ProofLocus.PYTEST,
    TestKind.PROPERTY: ProofLocus.HYPOTHESIS,
    TestKind.METAMORPHIC: ProofLocus.HYPOTHESIS,
    TestKind.GOLDEN: ProofLocus.GOLDEN,
    TestKind.TUI: ProofLocus.TUI_SNAPSHOT,
    TestKind.CONFORMANCE: ProofLocus.SCHEMA,
    TestKind.E2E: ProofLocus.CLI_EXIT,
    TestKind.PERF: ProofLocus.PYTEST,
}

#: The lane vocabulary ``[tool.eawf.quality.kind_gates]`` values are drawn
#: from. ``default`` is the ``-n auto`` parallel sweep, ``pooled`` the
#: small fixed worker pool the TUI needs, ``serial`` the ``-n0`` lane for
#: timing-sensitive work, and ``opt-in`` a lane the default marker
#: expression deselects.
QUALITY_LANES: frozenset[str] = frozenset({"default", "pooled", "serial", "opt-in"})

#: Pre-taxonomy files that carry an explicit kind marker contradicting
#: their directory. Grandfathered so the auto-marker contract can be
#: enforced on the current tree; a file drops off when its marker is
#: reconciled with its directory or the file is re-filed.
GRANDFATHERED_MARKER_CONFLICTS: frozenset[str] = frozenset(
    {
        "tests/contract/test_jsonrpc_contract.py",
        "tests/contract/test_runtime_resolution_contract.py",
        "tests/integration/test_cli_mcp_grant.py",
        "tests/integration/test_cli_research_question_status.py",
        "tests/integration/test_mcp_owner_gate.py",
        "tests/integration/test_pricing.py",
        "tests/integration/test_spec_writer.py",
        "tests/perf/test_turn_cost_harness.py",
        "tests/perf/test_turn_cost_producer.py",
        "tests/tui/test_research_round_progress.py",
        "tests/unit/lifecycle/test_transition_tables.py",
    }
)

#: Every kind marker name, for membership tests against an item's own
#: declared markers.
KIND_MARKERS: frozenset[str] = frozenset(kind.value for kind in TestKind)


class GateTier(StrEnum):
    """The gate cadence a test kind is run at, cheapest first.

    ``wave`` is the per-wave targeted run, ``iter`` the batch run at iter
    close, and ``release`` the phase/release-candidate run. Declaration
    order is the ladder order, so :func:`tier_rank` needs no side table.
    """

    WAVE = "wave"
    ITER = "iter"
    RELEASE = "release"


#: Wall-clock ceiling, in seconds, of one tier's run. The wave budget is
#: the targeted-run bound wave criteria are held to; the iter budget is
#: the local batch run at iter close; the release budget is the CI test
#: job's timeout, so a release-tier kind still has to fit one CI job.
TIER_RUNTIME_BUDGET_SECONDS: Mapping[GateTier, int] = {
    GateTier.WAVE: 60,
    GateTier.ITER: 600,
    GateTier.RELEASE: 2700,
}

#: The one tier each kind is gated at. A kind sits at the cheapest tier
#: whose budget and environment it fits: in-process kinds run every
#: wave; kinds needing a mounted terminal, pinned frames, or a certified
#: runtime run at iter close; kinds needing a real host or a timing
#: harness run only at release.
KIND_GATE_TIERS: Mapping[TestKind, GateTier] = {
    TestKind.UNIT: GateTier.WAVE,
    TestKind.CONTRACT: GateTier.WAVE,
    TestKind.INTEGRATION: GateTier.WAVE,
    TestKind.PROPERTY: GateTier.WAVE,
    TestKind.METAMORPHIC: GateTier.WAVE,
    TestKind.GOLDEN: GateTier.ITER,
    TestKind.TUI: GateTier.ITER,
    TestKind.CONFORMANCE: GateTier.ITER,
    TestKind.E2E: GateTier.RELEASE,
    TestKind.PERF: GateTier.RELEASE,
}


def tier_rank(tier: GateTier) -> int:
    """Return ``tier``'s 0-based position on the ladder (``wave`` is 0)."""
    return list(GateTier).index(tier)


def gate_tier_for_kind(kind: TestKind) -> GateTier:
    """Return the one gate tier ``kind`` runs at.

    Raises:
        TypeError: ``kind`` is not a :class:`TestKind`.
        KeyError: ``kind`` has no tier row (taxonomy drift).
    """
    if not isinstance(kind, TestKind):
        raise TypeError(f"expected a TestKind, got {type(kind).__name__}")
    return KIND_GATE_TIERS[kind]


def gate_tier_for_test_path(path: str) -> GateTier | None:
    """Return the tier of the kind directory ``path`` lives under, or ``None``.

    Args:
        path: A candidate test path, absolute or repo-relative.

    Returns:
        The directory kind's :class:`GateTier`, or ``None`` when ``path``
        is outside every kind directory.
    """
    kind = kind_for_test_path(path)
    return None if kind is None else gate_tier_for_kind(kind)


class RunOutcome(StrEnum):
    """The observed outcome of one run of a task's named test."""

    RED = "red"
    GREEN = "green"


@dataclass(frozen=True)
class TaskTestRun:
    """One run of the named test inside a task, in chronological order.

    Attributes:
        test_id: The pytest node id the task's protocol names.
        outcome: Whether that run failed (red) or passed (green).
        revision: The revision the run was taken at.
    """

    test_id: str
    outcome: RunOutcome
    revision: str


@dataclass(frozen=True)
class RedToGreenFinding:
    """A task whose named test lacks a red-then-green pair.

    The finding proves only that the pair is missing; a present pair says
    nothing about whether the test was weakened between the two runs.

    Attributes:
        test_id: The test the task named (empty when no run was recorded).
        reason: Which half of the pair is missing.
    """

    test_id: str
    reason: str

    def render(self) -> str:
        """Return a one-line explanation naming the test and the gap."""
        subject = self.test_id or "<no test>"
        return f"red-to-green: {subject}: {self.reason}"


def red_to_green_finding(runs: Sequence[TaskTestRun]) -> RedToGreenFinding | None:
    """Return the red-to-green finding for one task's test runs, or ``None``.

    The task boundary passes when the named test ran red at least once
    before its first green run and the last recorded run is green.

    Args:
        runs: Every recorded run of the task's named test, oldest first.

    Returns:
        ``None`` when the red-then-green pair holds, else a finding naming
        the missing half: no run at all, no green run, a first green with
        no earlier red, or a final run that went red again.

    Raises:
        ValueError: ``runs`` mixes more than one test id.
    """
    if not runs:
        return RedToGreenFinding(test_id="", reason="no run of the named test was recorded")
    test_ids = {run.test_id for run in runs}
    if len(test_ids) > 1:
        raise ValueError(f"one task names one test; got {sorted(test_ids)}")
    test_id = runs[0].test_id
    outcomes = [run.outcome for run in runs]
    if RunOutcome.GREEN not in outcomes:
        return RedToGreenFinding(test_id=test_id, reason="the test never ran green")
    if RunOutcome.RED not in outcomes[: outcomes.index(RunOutcome.GREEN)]:
        return RedToGreenFinding(
            test_id=test_id,
            reason=f"first green at {runs[outcomes.index(RunOutcome.GREEN)].revision} "
            "has no earlier red run",
        )
    if outcomes[-1] is not RunOutcome.GREEN:
        return RedToGreenFinding(
            test_id=test_id, reason=f"the last run at {runs[-1].revision} is red"
        )
    return None


def kind_directory(kind: TestKind) -> str:
    """Return the repo-relative directory ``kind`` files into."""
    return f"{TESTS_ROOT}{kind.value}/"


def repo_relative_test_path(path: str) -> str:
    """Return ``path`` re-rooted at its ``tests/`` segment.

    Slashes are folded and the cut is made on a whole path segment, so a
    sibling directory whose name merely ends in ``tests`` is not mistaken
    for the suite root.

    Args:
        path: Any absolute or relative path, either slash flavour.

    Returns:
        The ``tests/...`` suffix when the path carries a ``tests``
        segment, else the slash-folded path unchanged.
    """
    norm = path.replace("\\", "/")
    parts = norm.split("/")
    if "tests" not in parts:
        return norm
    return "/".join(parts[parts.index("tests") :])


def kind_for_test_path(path: str) -> TestKind | None:
    """Return the kind ``path`` lives under, or ``None``.

    Args:
        path: A candidate test path, absolute or repo-relative.

    Returns:
        The :class:`TestKind` whose directory contains ``path``, or
        ``None`` when ``path`` is not a ``.py`` file under a kind
        directory (a non-kind sub-directory, the suite root, or a
        non-Python file).
    """
    norm = repo_relative_test_path(path)
    if not norm.endswith(".py"):
        return None
    for kind in TestKind:
        if norm.startswith(kind_directory(kind)):
            return kind
    return None


@dataclass(frozen=True)
class MarkerConflict:
    """A test whose declared kind marker contradicts its directory.

    Attributes:
        path: Repo-relative path of the offending test file.
        directory_kind: The kind the directory auto-applies.
        declared: The contradicting kind markers the file declares,
            sorted.
    """

    path: str
    directory_kind: TestKind
    declared: tuple[str, ...]

    def render(self) -> str:
        """Return a one-line explanation naming both sides of the clash."""
        declared = ", ".join(self.declared)
        return (
            f"{self.path} lives under {kind_directory(self.directory_kind)} so it is "
            f"auto-marked {self.directory_kind.value!r}, but it declares {declared}; "
            "move the file or drop the contradicting marker"
        )


def marker_conflict(
    *,
    path: str,
    marker_names: frozenset[str],
    grandfather: frozenset[str] = GRANDFATHERED_MARKER_CONFLICTS,
) -> MarkerConflict | None:
    """Return the kind-marker conflict for one test file, or ``None``.

    Args:
        path: The test file's path, absolute or repo-relative.
        marker_names: Marker names the item declares for itself (before
            the directory marker is applied).
        grandfather: Repo-relative paths exempt from the contract.

    Returns:
        A :class:`MarkerConflict` when the file sits under a kind
        directory and declares a *different* kind's marker, else
        ``None`` (including for files outside the taxonomy).
    """
    kind = kind_for_test_path(path)
    if kind is None:
        return None
    relative = repo_relative_test_path(path)
    if relative in grandfather:
        return None
    declared = tuple(sorted((KIND_MARKERS & marker_names) - {kind.value}))
    if not declared:
        return None
    return MarkerConflict(path=relative, directory_kind=kind, declared=declared)


def declared_markers(pyproject_text: str) -> frozenset[str]:
    """Return the marker names registered in ``[tool.pytest.ini_options]``.

    Args:
        pyproject_text: Raw ``pyproject.toml`` text.

    Returns:
        Each registered marker's bare name, with the ``name: help`` help
        text stripped.

    Raises:
        KeyError: the ``markers`` key is absent.
    """
    parsed = tomllib.loads(pyproject_text)
    rows = parsed["tool"]["pytest"]["ini_options"]["markers"]
    return frozenset(str(row).split(":", 1)[0].strip() for row in rows)


def load_kind_gates(pyproject_text: str) -> dict[str, str]:
    """Return the ``[tool.eawf.quality.kind_gates]`` kind -> lane table.

    Args:
        pyproject_text: Raw ``pyproject.toml`` text.

    Returns:
        The configured mapping of kind name to quality lane.

    Raises:
        KeyError: the table is absent.
        TypeError: a configured lane is not a string.
    """
    parsed = tomllib.loads(pyproject_text)
    table = parsed["tool"]["eawf"]["quality"]["kind_gates"]
    for key, value in table.items():
        if not isinstance(value, str):
            raise TypeError(
                f"quality lane for {key!r} must be a string, got {type(value).__name__}"
            )
    return dict(table)


def suite_directories(tests_root: Path) -> frozenset[str]:
    """Return the names of the immediate sub-directories of ``tests_root``.

    Args:
        tests_root: Path of the suite root.

    Returns:
        Directory names, excluding dot-prefixed and ``__pycache__``
        entries. An absent root yields an empty set.
    """
    if not tests_root.is_dir():
        return frozenset()
    return frozenset(
        entry.name
        for entry in tests_root.iterdir()
        if entry.is_dir() and not entry.name.startswith((".", "__"))
    )


def _marker_drift(markers: frozenset[str], non_kind_markers: frozenset[str]) -> list[str]:
    """Return drift between the registered markers and the enum."""
    drift = [
        f"marker {name!r} is registered in pyproject but is neither a TestKind nor a "
        "declared non-kind marker"
        for name in sorted(markers - non_kind_markers - KIND_MARKERS)
    ]
    drift.extend(
        f"kind {name!r} has no registered pytest marker, so --strict-markers rejects it"
        for name in sorted(KIND_MARKERS - markers)
    )
    return drift


def _directory_drift(directories: frozenset[str], non_kind_dirs: frozenset[str]) -> list[str]:
    """Return drift between the on-disk ``tests/`` layout and the enum."""
    drift = [
        f"tests/{name}/ is neither a TestKind directory nor a declared non-kind directory"
        for name in sorted(directories - non_kind_dirs - KIND_MARKERS)
    ]
    drift.extend(
        f"tests/{name}/ is registered as a non-kind directory but {name!r} is a TestKind"
        for name in sorted(non_kind_dirs & KIND_MARKERS)
    )
    return drift


def _gate_drift(kind_gates: Mapping[str, str], lanes: frozenset[str]) -> list[str]:
    """Return drift between the quality gate config and the enum."""
    drift = [
        f"quality gate config declares kind {name!r}, which is not a TestKind"
        for name in sorted(set(kind_gates) - KIND_MARKERS)
    ]
    drift.extend(
        f"kind {name!r} has no quality gate lane" for name in sorted(KIND_MARKERS - set(kind_gates))
    )
    drift.extend(
        f"kind {name!r} maps to unknown quality lane {lane!r}"
        for name, lane in sorted(kind_gates.items())
        if lane not in lanes
    )
    return drift


def _locus_drift(proof_loci: Mapping[TestKind, ProofLocus]) -> list[str]:
    """Return drift between the proof-locus mapping and the enum."""
    mapped = {str(key) for key in proof_loci}
    drift = [
        f"proof-locus mapping declares kind {name!r}, which is not a TestKind"
        for name in sorted(mapped - KIND_MARKERS)
    ]
    drift.extend(f"kind {name!r} has no proof locus" for name in sorted(KIND_MARKERS - mapped))
    drift.extend(
        f"kind {str(key)!r} maps to {value!r}, which is not a ProofLocus"
        for key, value in sorted(proof_loci.items(), key=lambda row: str(row[0]))
        if not isinstance(value, ProofLocus)
    )
    return drift


def _tier_drift(
    kind_tiers: Mapping[TestKind, GateTier], budgets: Mapping[GateTier, int]
) -> list[str]:
    """Return drift between the kind -> gate-tier ladder and the enum."""
    mapped = {str(key) for key in kind_tiers}
    drift = [
        f"gate-tier mapping declares kind {name!r}, which is not a TestKind"
        for name in sorted(mapped - KIND_MARKERS)
    ]
    drift.extend(f"kind {name!r} has no gate tier" for name in sorted(KIND_MARKERS - mapped))
    drift.extend(
        f"kind {str(key)!r} maps to {value!r}, which is not a GateTier"
        for key, value in sorted(kind_tiers.items(), key=lambda row: str(row[0]))
        if not isinstance(value, GateTier)
    )
    drift.extend(
        f"gate tier {tier.value!r} has no positive runtime budget"
        for tier in GateTier
        if budgets.get(tier, 0) <= 0
    )
    return drift


def taxonomy_drift(
    *,
    markers: frozenset[str],
    directories: frozenset[str],
    kind_gates: Mapping[str, str],
    proof_loci: Mapping[TestKind, ProofLocus] = KIND_PROOF_LOCI,
    non_kind_markers: frozenset[str] = NON_KIND_MARKERS,
    non_kind_dirs: frozenset[str] = NON_KIND_TEST_DIRS,
    lanes: frozenset[str] = QUALITY_LANES,
    kind_tiers: Mapping[TestKind, GateTier] = KIND_GATE_TIERS,
    tier_budgets: Mapping[GateTier, int] = TIER_RUNTIME_BUDGET_SECONDS,
) -> list[str]:
    """Return every disagreement between :class:`TestKind` and its projections.

    All surfaces are checked as one unit: a kind present in any one
    of them but absent from the enum is drift, and so is an enum member
    that a surface has no row for.

    Args:
        markers: Marker names registered under ``[tool.pytest.ini_options]``.
        directories: Immediate sub-directory names of ``tests/``.
        kind_gates: The ``[tool.eawf.quality.kind_gates]`` kind -> lane table.
        proof_loci: The kind -> :class:`ProofLocus` mapping.
        non_kind_markers: Registered markers that are modifiers, not kinds.
        non_kind_dirs: ``tests/`` sub-directories that partition by
            subsystem rather than by kind.
        lanes: The permitted quality-lane vocabulary.
        kind_tiers: The kind -> :class:`GateTier` ladder.
        tier_budgets: The per-tier runtime budget in seconds.

    Returns:
        One human-readable line per disagreement, marker drift first,
        then directory, gate, proof-locus, and gate-tier drift. Empty when the four
        surfaces agree with the enum.
    """
    return [
        *_marker_drift(markers, non_kind_markers),
        *_directory_drift(directories, non_kind_dirs),
        *_gate_drift(kind_gates, lanes),
        *_locus_drift(proof_loci),
        *_tier_drift(kind_tiers, tier_budgets),
    ]
