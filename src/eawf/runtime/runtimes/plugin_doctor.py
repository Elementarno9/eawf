"""Top-level plugin doctor — enumerates 5 drift kinds.

The per-runtime plugin doctors at
:mod:`eawf.runtime.runtimes.{claude,codex,opencode}.plugin_doctor` answer
"does the rendered tree on disk still match what the installer would
emit?". This module composes them into a single multi-runtime sweep
plus four additional drift kinds:

1. ``manifest-vs-disk`` — :class:`PluginManifest` source files are
   declared on the manifest's ``managed.source_files`` list; this
   kind asserts every source file resolves on disk (a missing
   ``AGENTS.md`` / source module under the named path is drift in
   the manifest's source-of-truth contract).
2. ``registry-vs-disk`` — the per-runtime renderer's expected
   bytes vs the on-disk bytes of every rendered file. Aggregates
   the existing per-runtime ``doctor_plugin`` reports across all
   three runtimes (or the requested subset). This is the existing
   byte-equality sweep promoted to a kind.
3. ``capability-vs-probe`` — capability-matrix declared cell vs
   live probe results, via the matrix detector
   (:func:`eawf.runtime.runtimes.capabilities.detect_drift`).
4. ``helper-LOC-overflow`` — LOC-budget enforcement; the
   ``src/eawf/runtimes/helpers/`` module + its submodules must
   stay under 300 LOC combined (see
   :mod:`eawf.runtime.runtimes.helpers` module docstring). Drift fires
   when the total exceeds the cap.
5. ``orphan-disk-vs-registry`` — the reverse of ``registry-vs-disk``.
   The first four kinds walk expected(registry) -> disk; this kind
   walks the on-disk ``.claude/skills/<name>/`` directories of an
   installed Claude plugin tree and flags any skill directory with no
   corresponding :class:`~eawf.surfaces.render.skills.render.SkillSpec`
   row in :data:`~eawf.surfaces.render.skills.registry.SKILL_REGISTRY`
   (an *orphan* — a skill rendered or hand-dropped on disk that the
   registry no longer knows about). The kind FLAGS orphans only; it
   never auto-registers or imports them, honouring the
   explicit-registry-only policy (the registry grows solely via
   explicit registration).

Each kind is an isolated check function that returns a typed
:class:`DriftKindReport`. The aggregate :class:`PluginDoctorReport`
exposes ``clean`` (no drift across any kind) and the per-kind
lists so the CLI surface can render one consolidated table.

Composition
-----------

* ``PluginManifest`` sources the ``manifest-vs-disk`` rule; the
  manifest is loaded from ``build/<runtime>-plugin/manifest.yaml``
  when present, else skipped (the doctor reports an empty kind row
  rather than failing) — the manifest is a build-time artifact.
* :mod:`eawf.runtime.runtimes.capabilities` sources the
  ``capability-vs-probe`` rule; the doctor wraps
  :func:`~eawf.runtime.runtimes.capabilities.detect_drift` per runtime.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

import yaml

from eawf.runtime.runtimes.capabilities import (
    DriftRow as CapabilityDriftRow,
)
from eawf.runtime.runtimes.capabilities import (
    ProbeResult,
    detect_drift,
)
from eawf.runtime.runtimes.claude.plugin_doctor import doctor_plugin as claude_doctor_plugin
from eawf.runtime.runtimes.codex.plugin_doctor import doctor_plugin as codex_doctor_plugin
from eawf.runtime.runtimes.manifest import PluginManifest, RuntimeId
from eawf.runtime.runtimes.opencode.plugin_doctor import doctor_plugin as opencode_doctor_plugin
from eawf.surfaces.render.skills.registry import SKILL_REGISTRY

logger = logging.getLogger(__name__)


DriftKind = Literal[
    "manifest-vs-disk",
    "registry-vs-disk",
    "capability-vs-probe",
    "helper-LOC-overflow",
    "orphan-disk-vs-registry",
]
"""Closed set of drift kinds the top-level doctor enumerates."""

DRIFT_KINDS: Final[tuple[DriftKind, ...]] = (
    "manifest-vs-disk",
    "registry-vs-disk",
    "capability-vs-probe",
    "helper-LOC-overflow",
    "orphan-disk-vs-registry",
)
"""Canonical ordering used when rendering the consolidated report."""


HELPER_LOC_BUDGET: Final[int] = 300
"""KISS-004 budget per :mod:`eawf.runtime.runtimes.helpers` docstring."""


_HELPER_MODULE_PATH: Final[Path] = Path(__file__).parent / "helpers"
"""Resolved path to the helpers package (used by the LOC sweep)."""


_RUNTIME_TO_BUILD_DIR: Final[dict[RuntimeId, str]] = {
    "claude-code": "claude-plugin",
    "codex": "codex-plugin",
    "opencode": "opencode-plugin",
}
"""Map from canonical runtime id to its ``build/<dir>/manifest.yaml`` stem."""


@dataclass(frozen=True)
class DriftFinding:
    """One drift finding inside a :class:`DriftKindReport`.

    Attributes:
        runtime: Canonical runtime id the finding applies to, or
            ``None`` when the finding is runtime-agnostic
            (``helper-LOC-overflow``).
        location: Short identifier for the offending surface
            (file path / capability row / region id).
        detail: Human-readable detail string.
    """

    runtime: RuntimeId | None
    location: str
    detail: str


@dataclass(frozen=True)
class DriftKindReport:
    """Per-kind report for the top-level doctor.

    Attributes:
        kind: One of the five :data:`DriftKind` literals.
        clean: ``True`` when no findings recorded.
        findings: List of :class:`DriftFinding` rows.
        skipped: ``True`` when the check was skipped (e.g. manifest
            file absent, probe runtime offline). Surfaced as a
            passing row so operators distinguish "nothing wrong"
            from "could not check".
    """

    kind: DriftKind
    clean: bool
    findings: list[DriftFinding] = field(default_factory=list)
    skipped: bool = False


@dataclass(frozen=True)
class PluginDoctorReport:
    """Aggregate result of one :func:`run_doctor` sweep.

    Attributes:
        target_dir: Workspace root the sweep ran against.
        runtimes: Tuple of runtime ids the registry/manifest checks
            ran across (``capability-vs-probe`` walks the same set).
        kinds: Per-kind report list, ordered per
            :data:`DRIFT_KINDS`.
        clean: ``True`` when every kind is clean.
    """

    target_dir: Path
    runtimes: tuple[RuntimeId, ...]
    kinds: list[DriftKindReport] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """``True`` when every kind reports clean."""
        return all(k.clean for k in self.kinds)


# ---------------------------------------------------------------------------
# Kind 1: manifest-vs-disk
# ---------------------------------------------------------------------------


def _load_manifest(build_root: Path, runtime: RuntimeId) -> PluginManifest | None:
    """Load ``build/<runtime>-plugin/manifest.yaml`` when present.

    Returns ``None`` (not an error) when the manifest is absent — the
    canonical YAML source is a build-time artifact; the doctor reports
    an empty manifest-vs-disk row in that case rather than failing.
    """
    build_dir = _RUNTIME_TO_BUILD_DIR[runtime]
    candidate = build_root / "build" / build_dir / "manifest.yaml"
    if not candidate.exists():
        return None
    raw = yaml.safe_load(candidate.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"manifest top-level must be a mapping: {candidate}")
    return PluginManifest.model_validate(raw)


def check_manifest_vs_disk(
    target_dir: Path,
    *,
    runtimes: tuple[RuntimeId, ...],
) -> DriftKindReport:
    """Verify every ``managed.source_files`` entry resolves on disk.

    For each requested runtime, loads the canonical
    ``build/<runtime>-plugin/manifest.yaml`` and asserts every path
    listed under ``managed.source_files`` resolves on disk. A missing
    source file is a contract break — the renderer references a path
    that no longer exists.

    Args:
        target_dir: Workspace root (used to resolve
            ``build/<runtime>-plugin/manifest.yaml``).
        runtimes: Subset of runtime ids to sweep.

    Returns:
        :class:`DriftKindReport` with one finding per missing
        source file. ``skipped=True`` when no manifests were found.
    """
    findings: list[DriftFinding] = []
    seen_any = False
    for runtime in runtimes:
        manifest = _load_manifest(target_dir, runtime)
        if manifest is None:
            continue
        seen_any = True
        for source_path in manifest.managed.source_files:
            resolved = target_dir / source_path
            if not resolved.exists():
                findings.append(
                    DriftFinding(
                        runtime=runtime,
                        location=source_path,
                        detail=f"manifest source file missing: {resolved}",
                    )
                )
    return DriftKindReport(
        kind="manifest-vs-disk",
        clean=not findings,
        findings=findings,
        skipped=not seen_any,
    )


# ---------------------------------------------------------------------------
# Kind 2: registry-vs-disk
# ---------------------------------------------------------------------------


def check_registry_vs_disk(
    target_dir: Path,
    *,
    runtimes: tuple[RuntimeId, ...],
) -> DriftKindReport:
    """Aggregate per-runtime ``doctor_plugin`` reports into one kind.

    Walks each requested runtime's existing ``doctor_plugin``
    function and lifts ``drifted`` + ``missing`` entries into the
    consolidated kind report. ``ok`` entries are dropped — the
    consolidated kind report only carries findings (the per-runtime
    sub-reports preserve the OK detail).

    Args:
        target_dir: Workspace root.
        runtimes: Subset of runtime ids to sweep.

    Returns:
        :class:`DriftKindReport` enumerating every drifted /
        missing rendered file across the requested runtimes.
    """
    findings: list[DriftFinding] = []
    for runtime in runtimes:
        # Each per-runtime DoctorReport is a distinct dataclass but
        # shares the structural surface (drifted + missing lists of
        # DoctorEntry with region_id + path). The branches below
        # extract findings from each branch's typed report directly so
        # we don't need a structural union helper.
        if runtime == "claude-code":
            claude_report = claude_doctor_plugin(target_dir)
            for entry in claude_report.drifted:
                findings.append(
                    DriftFinding(
                        runtime=runtime,
                        location=entry.region_id,
                        detail=f"rendered file drifted: {entry.path}",
                    )
                )
            for entry in claude_report.missing:
                findings.append(
                    DriftFinding(
                        runtime=runtime,
                        location=entry.region_id,
                        detail=f"rendered file missing: {entry.path}",
                    )
                )
        elif runtime == "codex":
            codex_report = codex_doctor_plugin(target_dir)
            for codex_entry in codex_report.drifted:
                findings.append(
                    DriftFinding(
                        runtime=runtime,
                        location=codex_entry.region_id,
                        detail=f"rendered file drifted: {codex_entry.path}",
                    )
                )
            for codex_entry in codex_report.missing:
                findings.append(
                    DriftFinding(
                        runtime=runtime,
                        location=codex_entry.region_id,
                        detail=f"rendered file missing: {codex_entry.path}",
                    )
                )
        elif runtime == "opencode":
            opencode_report = opencode_doctor_plugin(target_dir)
            for oc_entry in opencode_report.drifted:
                findings.append(
                    DriftFinding(
                        runtime=runtime,
                        location=oc_entry.region_id,
                        detail=f"rendered file drifted: {oc_entry.path}",
                    )
                )
            for oc_entry in opencode_report.missing:
                findings.append(
                    DriftFinding(
                        runtime=runtime,
                        location=oc_entry.region_id,
                        detail=f"rendered file missing: {oc_entry.path}",
                    )
                )
    return DriftKindReport(
        kind="registry-vs-disk",
        clean=not findings,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# Kind 3: capability-vs-probe
# ---------------------------------------------------------------------------


def check_capability_vs_probe(
    *,
    runtimes: tuple[RuntimeId, ...],
    probes: dict[RuntimeId, ProbeResult] | None = None,
) -> DriftKindReport:
    """Compare declared capability cells against probe results.

    Delegates to :func:`eawf.runtime.runtimes.capabilities.detect_drift` per
    runtime. When *probes* is ``None`` the doctor skips this kind
    (probe results are an injectable dependency — the daemon
    supplies them when running live; tests supply crafted probes).

    Args:
        runtimes: Subset of runtime ids to sweep.
        probes: Optional mapping from runtime id to its probe
            result. When absent, the kind is reported as
            ``skipped=True``.

    Returns:
        :class:`DriftKindReport` aggregating drift rows across
        runtimes. Each capability x runtime cell with status
        ``DRIFT`` becomes a finding; ``OK`` / ``UNKNOWN`` /
        ``MISSING`` rows are dropped (the per-runtime detail is
        available on the underlying detector).
    """
    if probes is None:
        return DriftKindReport(kind="capability-vs-probe", clean=True, skipped=True)
    findings: list[DriftFinding] = []
    for runtime in runtimes:
        probe = probes.get(runtime)
        if probe is None:
            # Probe absent for this runtime — record as a finding
            # so the operator sees the gap (probe coverage is part
            # of the contract).
            findings.append(
                DriftFinding(
                    runtime=runtime,
                    location="<probe>",
                    detail="capability probe missing for runtime",
                )
            )
            continue
        rows: tuple[CapabilityDriftRow, ...] = detect_drift(runtime, probe)
        for row in rows:
            if row.status == "DRIFT":
                findings.append(
                    DriftFinding(
                        runtime=runtime,
                        location=row.capability,
                        detail=row.detail,
                    )
                )
    return DriftKindReport(
        kind="capability-vs-probe",
        clean=not findings,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# Kind 4: helper-LOC-overflow
# ---------------------------------------------------------------------------


def _count_helper_loc(helpers_dir: Path) -> int:
    """Sum non-empty line counts across every ``.py`` file under *helpers_dir*."""
    total = 0
    for path in sorted(helpers_dir.glob("*.py")):
        with path.open(encoding="utf-8") as handle:
            total += sum(1 for _ in handle)
    return total


def check_helper_loc_overflow(
    helpers_dir: Path | None = None,
    *,
    budget: int = HELPER_LOC_BUDGET,
) -> DriftKindReport:
    """Enforce the KISS-004 helper-module LOC budget.

    Per :mod:`eawf.runtime.runtimes.helpers` docstring, the entire helpers
    package must stay under :data:`HELPER_LOC_BUDGET` (300 LOC).
    Exceeding the cap is drift — the shared helper module is
    growing into a mini-framework that violates KISS-004.

    Args:
        helpers_dir: Override path for tests. Defaults to the
            packaged helpers directory.
        budget: Override LOC budget for tests. Defaults to
            :data:`HELPER_LOC_BUDGET`.

    Returns:
        :class:`DriftKindReport` with one finding when the cap is
        exceeded, else clean.
    """
    target_dir = helpers_dir if helpers_dir is not None else _HELPER_MODULE_PATH
    if not target_dir.exists():
        return DriftKindReport(
            kind="helper-LOC-overflow",
            clean=True,
            skipped=True,
        )
    total = _count_helper_loc(target_dir)
    findings: list[DriftFinding] = []
    if total > budget:
        findings.append(
            DriftFinding(
                runtime=None,
                location=str(target_dir),
                detail=f"helper LOC {total} exceeds KISS-004 budget {budget}",
            )
        )
    return DriftKindReport(
        kind="helper-LOC-overflow",
        clean=not findings,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# Kind 5: orphan-disk-vs-registry
# ---------------------------------------------------------------------------


_CLAUDE_SKILLS_SUBPATH: Final[tuple[str, str]] = (".claude", "skills")
"""Relative segments from the target dir to the rendered Claude skills tree."""


def check_orphan_disk_vs_registry(target_dir: Path) -> DriftKindReport:
    """Flag on-disk skill directories with no registry row (orphans).

    The reverse of :func:`check_registry_vs_disk`: instead of walking
    expected(registry) -> disk, this walks the rendered Claude plugin
    tree's ``.claude/skills/<name>/`` directories and flags any whose
    name has no matching
    :class:`~eawf.surfaces.render.skills.render.SkillSpec` in
    :data:`~eawf.surfaces.render.skills.registry.SKILL_REGISTRY`. Such a
    directory is an *orphan* — a skill rendered (or hand-dropped) on
    disk that the registry no longer declares.

    The check FLAGS orphans only. It never auto-registers or imports a
    discovered skill: per the explicit-registry-only policy the registry
    grows solely via explicit registration, so reconciliation is an
    operator decision the doctor surfaces rather than performs.

    Args:
        target_dir: Workspace root holding the installed
            ``.claude/skills/`` tree.

    Returns:
        :class:`DriftKindReport` with one finding per orphan directory.
        ``skipped=True`` when the skills tree is absent (the plugin was
        never installed here, so there is nothing to reconcile against).
    """
    skills_root = target_dir.joinpath(*_CLAUDE_SKILLS_SUBPATH)
    if not skills_root.is_dir():
        return DriftKindReport(
            kind="orphan-disk-vs-registry",
            clean=True,
            skipped=True,
        )
    registered = {spec.skill_name for spec in SKILL_REGISTRY}
    findings: list[DriftFinding] = []
    for child in sorted(skills_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name in registered:
            continue
        findings.append(
            DriftFinding(
                runtime="claude-code",
                location=f".claude/skills/{child.name}",
                detail=(
                    f"on-disk skill directory has no SKILL_REGISTRY row: "
                    f"{child.name!r} (flagged only — register it explicitly "
                    f"or remove the directory)"
                ),
            )
        )
    return DriftKindReport(
        kind="orphan-disk-vs-registry",
        clean=not findings,
        findings=findings,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_doctor(
    target_dir: Path,
    *,
    runtimes: tuple[RuntimeId, ...] = ("claude-code", "codex", "opencode"),
    probes: dict[RuntimeId, ProbeResult] | None = None,
    helpers_dir: Path | None = None,
) -> PluginDoctorReport:
    """Run all five drift kinds and aggregate the result.

    Args:
        target_dir: Workspace root.
        runtimes: Runtime ids to sweep (defaults to all three).
        probes: Optional probe-result map for the
            ``capability-vs-probe`` kind. When ``None`` that kind
            is reported as ``skipped=True``.
        helpers_dir: Override for the ``helper-LOC-overflow`` check.

    Returns:
        :class:`PluginDoctorReport` enumerating per-kind findings.
    """
    resolved_target = Path(target_dir).resolve()
    kinds: list[DriftKindReport] = [
        check_manifest_vs_disk(resolved_target, runtimes=runtimes),
        check_registry_vs_disk(resolved_target, runtimes=runtimes),
        check_capability_vs_probe(runtimes=runtimes, probes=probes),
        check_helper_loc_overflow(helpers_dir),
        check_orphan_disk_vs_registry(resolved_target),
    ]
    logger.info(
        f"run_doctor target={resolved_target} runtimes={len(runtimes)} "
        f"kinds={len(kinds)} clean={all(k.clean for k in kinds)}"
    )
    return PluginDoctorReport(
        target_dir=resolved_target,
        runtimes=runtimes,
        kinds=kinds,
    )


__all__ = [
    "DRIFT_KINDS",
    "HELPER_LOC_BUDGET",
    "DriftFinding",
    "DriftKind",
    "DriftKindReport",
    "PluginDoctorReport",
    "check_capability_vs_probe",
    "check_helper_loc_overflow",
    "check_manifest_vs_disk",
    "check_orphan_disk_vs_registry",
    "check_registry_vs_disk",
    "run_doctor",
]
