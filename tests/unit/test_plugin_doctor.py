"""Unit tests for :mod:`eawf.runtime.runtimes.plugin_doctor` — 5 drift kinds.

The top-level doctor enumerates five drift kinds:

1. ``manifest-vs-disk`` — :class:`PluginManifest` ``managed.source_files``
   paths resolve on disk.
2. ``registry-vs-disk`` — per-runtime renderer's expected bytes vs the
   on-disk bytes.
3. ``capability-vs-probe`` — capability matrix declared cells vs live
   probe results (delegates to W13).
4. ``helper-LOC-overflow`` — KISS-004 budget enforcement on
   :mod:`eawf.runtime.runtimes.helpers`.
5. ``orphan-disk-vs-registry`` — the reverse walk: on-disk
   ``.claude/skills/<name>/`` directories with no ``SKILL_REGISTRY`` row.

These tests cover boundary cases (no manifests / clean tree / empty
helpers / no probes / no skills tree / all-registered) AND drift
detection (broken source files, hand edits, drift probe rows, oversize
helpers, orphan skill directories).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml

from eawf.runtime.runtimes.capabilities import ProbeResult
from eawf.runtime.runtimes.claude.plugin_install import install_plugin
from eawf.runtime.runtimes.plugin_doctor import (
    DRIFT_KINDS,
    HELPER_LOC_BUDGET,
    DriftFinding,
    DriftKindReport,
    PluginDoctorReport,
    check_capability_vs_probe,
    check_helper_loc_overflow,
    check_manifest_vs_disk,
    check_orphan_disk_vs_registry,
    check_registry_vs_disk,
    run_doctor,
)
from eawf.surfaces.render.skills.registry import SKILL_REGISTRY

# ---------------------------------------------------------------------------
# Constants — boundary checks
# ---------------------------------------------------------------------------


def test_drift_kinds_enumerates_exactly_five() -> None:
    """The doctor commits to five kinds — guardrail against silent drift."""
    assert len(DRIFT_KINDS) == 5
    assert set(DRIFT_KINDS) == {
        "manifest-vs-disk",
        "registry-vs-disk",
        "capability-vs-probe",
        "helper-LOC-overflow",
        "orphan-disk-vs-registry",
    }


def test_helper_loc_budget_is_three_hundred() -> None:
    """KISS-004 names 300 as the cap — guardrail against silent expansion."""
    assert HELPER_LOC_BUDGET == 300


# ---------------------------------------------------------------------------
# Kind 1 — manifest-vs-disk
# ---------------------------------------------------------------------------


def _write_manifest(
    workspace: Path,
    runtime_build_dir: str,
    source_files: list[str],
) -> Path:
    """Write a minimal manifest yaml under ``build/<runtime>-plugin/``."""
    build_dir = workspace / "build" / runtime_build_dir
    build_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = build_dir / "manifest.yaml"
    body = {
        "schema_version": "1.0",
        "plugin": {
            "name": "eawf",
            "version": "0.3.0",
            "description": "test plugin",
            "runtime": "claude-code"
            if runtime_build_dir == "claude-plugin"
            else ("codex" if runtime_build_dir == "codex-plugin" else "opencode"),
            "generator": "eawf-plugin-test",
        },
        "contributes": {"skills": [], "agents": [], "hooks": {}},
        "managed": {
            "body_hash_field": "managed.body_hash",
            "timestamp_field": "managed.generated_at",
            "source_files": source_files,
        },
    }
    manifest_path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return manifest_path


def test_check_manifest_vs_disk_skipped_when_no_manifest(tmp_path: Path) -> None:
    """No build/manifest.yaml under any runtime → kind is skipped."""
    report = check_manifest_vs_disk(tmp_path, runtimes=("claude-code", "codex", "opencode"))
    assert report.kind == "manifest-vs-disk"
    assert report.skipped is True
    assert report.clean is True
    assert report.findings == []


def test_check_manifest_vs_disk_clean_when_sources_present(tmp_path: Path) -> None:
    """All declared source files resolve on disk → clean."""
    (tmp_path / "AGENTS.md").write_text("rules", encoding="utf-8")
    (tmp_path / "src.py").write_text("body", encoding="utf-8")
    _write_manifest(tmp_path, "claude-plugin", ["AGENTS.md", "src.py"])
    report = check_manifest_vs_disk(tmp_path, runtimes=("claude-code",))
    assert report.clean is True
    assert report.skipped is False
    assert report.findings == []


def test_check_manifest_vs_disk_detects_missing_source(tmp_path: Path) -> None:
    """A declared source file that is gone fires a finding."""
    (tmp_path / "AGENTS.md").write_text("rules", encoding="utf-8")
    _write_manifest(tmp_path, "claude-plugin", ["AGENTS.md", "missing/path.py"])
    report = check_manifest_vs_disk(tmp_path, runtimes=("claude-code",))
    assert report.clean is False
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.runtime == "claude-code"
    assert finding.location == "missing/path.py"
    assert "manifest source file missing" in finding.detail


# ---------------------------------------------------------------------------
# Kind 2 — registry-vs-disk
# ---------------------------------------------------------------------------


def test_check_registry_vs_disk_clean_after_install(tmp_path: Path) -> None:
    """Fresh install → no drift across the rendered tree."""
    install_plugin(tmp_path)
    report = check_registry_vs_disk(tmp_path, runtimes=("claude-code",))
    assert report.kind == "registry-vs-disk"
    assert report.clean is True
    assert report.findings == []


def test_check_registry_vs_disk_detects_hand_edit(tmp_path: Path) -> None:
    """A hand edit to a rendered file fires a registry-vs-disk finding."""
    install_plugin(tmp_path)
    skill = tmp_path / ".claude" / "skills" / "polish" / "SKILL.md"
    skill.write_text(skill.read_text() + "\n# drift\n", encoding="utf-8")
    report = check_registry_vs_disk(tmp_path, runtimes=("claude-code",))
    assert report.clean is False
    assert any(f.runtime == "claude-code" and "polish" in f.location for f in report.findings)


def test_check_registry_vs_disk_detects_missing_file(tmp_path: Path) -> None:
    """A deleted rendered file fires a missing finding."""
    install_plugin(tmp_path)
    hook = tmp_path / ".claude" / "hooks" / "agent_end.sh"
    hook.unlink()
    report = check_registry_vs_disk(tmp_path, runtimes=("claude-code",))
    assert report.clean is False
    assert any("agent_end" in f.location for f in report.findings)


# ---------------------------------------------------------------------------
# Kind 3 — capability-vs-probe
# ---------------------------------------------------------------------------


def test_check_capability_vs_probe_skipped_without_probes() -> None:
    """No probes injected → kind reports skipped."""
    report = check_capability_vs_probe(runtimes=("claude-code",), probes=None)
    assert report.kind == "capability-vs-probe"
    assert report.skipped is True
    assert report.clean is True


def test_check_capability_vs_probe_clean_with_supporting_evidence() -> None:
    """Probe carries every supported-cell flag → no drift."""
    probe = ProbeResult(
        runtime_id="claude-code",
        installed=True,
        observed_flags=(
            "--continue",
            "--session-id",
            "--resume",
            "--allowedTools",
            "--allowed-tools",
            "--output-format",
        ),
    )
    report = check_capability_vs_probe(
        runtimes=("claude-code",),
        probes={"claude-code": probe},
    )
    assert report.clean is True
    assert report.findings == []


def test_check_capability_vs_probe_detects_drift() -> None:
    """Declared supported but probe carries no evidence → DRIFT finding."""
    # Claude declares session_resume + tool_use + streaming as supported;
    # an empty probe carries no flag evidence → three DRIFT rows.
    probe = ProbeResult(runtime_id="claude-code", installed=True, observed_flags=())
    report = check_capability_vs_probe(
        runtimes=("claude-code",),
        probes={"claude-code": probe},
    )
    assert report.clean is False
    # Each probe-checked capability with declared=supported but empty
    # observed_flags fires a finding.
    locations = {f.location for f in report.findings}
    assert "session_resume" in locations or "tool_use" in locations


def test_check_capability_vs_probe_reports_missing_probe() -> None:
    """Probe entry missing for a requested runtime → finding."""
    report = check_capability_vs_probe(
        runtimes=("claude-code",),
        probes={},  # explicit empty (not None) -> requested runtime has no probe
    )
    assert report.clean is False
    assert any("probe missing" in f.detail for f in report.findings)


# ---------------------------------------------------------------------------
# Kind 4 — helper-LOC-overflow
# ---------------------------------------------------------------------------


def test_check_helper_loc_overflow_clean_at_packaged_helpers() -> None:
    """The packaged helpers/ dir stays under the budget (guard for KISS-004)."""
    report = check_helper_loc_overflow()
    assert report.kind == "helper-LOC-overflow"
    assert report.clean is True
    assert report.findings == []


def test_check_helper_loc_overflow_skipped_when_dir_absent(tmp_path: Path) -> None:
    """Override path absent → kind is skipped."""
    report = check_helper_loc_overflow(tmp_path / "does-not-exist")
    assert report.skipped is True
    assert report.clean is True


def test_check_helper_loc_overflow_fires_when_budget_exceeded(tmp_path: Path) -> None:
    """Synthetic helpers dir over the cap → finding."""
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    # 50 lines per file x 4 files = 200 lines; budget=100 -> over.
    for idx in range(4):
        (helpers / f"mod_{idx}.py").write_text("# line\n" * 50, encoding="utf-8")
    report = check_helper_loc_overflow(helpers, budget=100)
    assert report.clean is False
    assert len(report.findings) == 1
    assert "exceeds KISS-004 budget" in report.findings[0].detail


# ---------------------------------------------------------------------------
# Kind 5 — orphan-disk-vs-registry
# ---------------------------------------------------------------------------


def test_check_orphan_skipped_when_no_skills_tree(tmp_path: Path) -> None:
    """No .claude/skills/ tree (zero skills on disk) → kind is skipped."""
    report = check_orphan_disk_vs_registry(tmp_path)
    assert report.kind == "orphan-disk-vs-registry"
    assert report.skipped is True
    assert report.clean is True
    assert report.findings == []


def test_check_orphan_clean_when_all_registered(tmp_path: Path) -> None:
    """Fresh install renders only registered skills → no orphan finding."""
    install_plugin(tmp_path)
    report = check_orphan_disk_vs_registry(tmp_path)
    assert report.kind == "orphan-disk-vs-registry"
    assert report.skipped is False
    assert report.clean is True
    assert report.findings == []


def test_check_orphan_clean_when_skills_dir_empty(tmp_path: Path) -> None:
    """Skills tree present but empty (no child dirs) → clean, not skipped."""
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    report = check_orphan_disk_vs_registry(tmp_path)
    assert report.skipped is False
    assert report.clean is True
    assert report.findings == []


def test_check_orphan_detects_unregistered_skill_dir(tmp_path: Path) -> None:
    """A synthetic skills/<fake>/ dir with no registry row fires a finding."""
    install_plugin(tmp_path)
    orphan = tmp_path / ".claude" / "skills" / "totally-made-up-skill"
    orphan.mkdir()
    (orphan / "SKILL.md").write_text("# orphan\n", encoding="utf-8")
    report = check_orphan_disk_vs_registry(tmp_path)
    assert report.clean is False
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.runtime == "claude-code"
    assert finding.location == ".claude/skills/totally-made-up-skill"
    assert "no SKILL_REGISTRY row" in finding.detail


def test_check_orphan_ignores_loose_files_in_skills_root(tmp_path: Path) -> None:
    """A loose file (not a directory) under skills/ is not an orphan."""
    skills_root = tmp_path / ".claude" / "skills"
    skills_root.mkdir(parents=True)
    (skills_root / "README.txt").write_text("not a skill dir", encoding="utf-8")
    report = check_orphan_disk_vs_registry(tmp_path)
    assert report.clean is True
    assert report.findings == []


def test_check_orphan_does_not_register_the_orphan(tmp_path: Path) -> None:
    """Flagging an orphan must NOT mutate SKILL_REGISTRY (explicit-only)."""
    install_plugin(tmp_path)
    before = {spec.skill_name for spec in SKILL_REGISTRY}
    orphan = tmp_path / ".claude" / "skills" / "sneaky-orphan"
    orphan.mkdir()
    check_orphan_disk_vs_registry(tmp_path)
    after = {spec.skill_name for spec in SKILL_REGISTRY}
    assert after == before
    assert "sneaky-orphan" not in after


def test_run_doctor_orphan_drives_non_clean_and_drift_exit(tmp_path: Path) -> None:
    """An orphan dir makes the aggregate report non-clean.

    The non-clean aggregate is what drives the ``eawf plugin doctor``
    CLI to ``raise typer.Exit(exit_codes.STATE_CONFLICT)`` — the
    canonical drift exit code. ``STATE_CONFLICT`` is the doctor's drift
    exit; the legacy ``INTEGRITY_VIOLATION`` (== 8 in the pre-P28 0..9
    scheme) was collapsed into ``STATE_CONFLICT`` when the exit-code
    surface was canonicalised to 0..5.
    """
    from eawf.surfaces.cli import exit_codes

    install_plugin(tmp_path)
    (tmp_path / ".claude" / "skills" / "ghost-skill").mkdir()
    report = run_doctor(tmp_path, runtimes=("claude-code",))
    assert report.clean is False
    dirty = [k for k in report.kinds if not k.clean]
    assert [k.kind for k in dirty] == ["orphan-disk-vs-registry"]
    # The CLI maps a non-clean report onto STATE_CONFLICT (the drift
    # exit); assert the constant the handler raises.
    assert exit_codes.STATE_CONFLICT == 3


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def test_run_doctor_aggregates_five_kinds(tmp_path: Path) -> None:
    """``run_doctor`` returns exactly five kind reports in canonical order."""
    install_plugin(tmp_path)
    report = run_doctor(tmp_path, runtimes=("claude-code",))
    assert isinstance(report, PluginDoctorReport)
    assert report.runtimes == ("claude-code",)
    assert len(report.kinds) == 5
    assert [k.kind for k in report.kinds] == list(DRIFT_KINDS)


def test_run_doctor_clean_after_fresh_install(tmp_path: Path) -> None:
    """Fresh install + no manifests + no probes → clean (skipped kinds OK)."""
    install_plugin(tmp_path)
    report = run_doctor(tmp_path, runtimes=("claude-code",))
    # manifest-vs-disk is skipped (no build/ tree); registry-vs-disk
    # passes; capability-vs-probe is skipped (no probes); helper-LOC
    # passes — overall clean.
    assert report.clean is True


def test_run_doctor_surfaces_drift_when_hand_edit_present(tmp_path: Path) -> None:
    """Hand edit → ``clean=False`` and the offending kind is dirty."""
    install_plugin(tmp_path)
    skill = tmp_path / ".claude" / "skills" / "polish" / "SKILL.md"
    skill.write_text("hand-edited\n", encoding="utf-8")
    report = run_doctor(tmp_path, runtimes=("claude-code",))
    assert report.clean is False
    dirty = [k for k in report.kinds if not k.clean]
    assert len(dirty) == 1
    assert dirty[0].kind == "registry-vs-disk"


# ---------------------------------------------------------------------------
# Pydantic dataclass surface guard (extra="forbid"-equivalent shape)
# ---------------------------------------------------------------------------


def test_drift_finding_is_frozen() -> None:
    """:class:`DriftFinding` is frozen — attribute writes raise FrozenInstanceError."""
    finding = DriftFinding(runtime="claude-code", location="x", detail="y")
    with pytest.raises(dataclasses.FrozenInstanceError):
        finding.detail = "z"  # type: ignore[misc]


def test_drift_kind_report_default_findings_empty() -> None:
    """``findings`` defaults to an empty list (sentinel for clean kind)."""
    report = DriftKindReport(kind="manifest-vs-disk", clean=True)
    assert report.findings == []
    assert report.skipped is False
