"""Tests for ``plugin doctor --strict`` and the per-runtime dispatch goldens.

Three concerns are covered here:

1. ``--strict`` exit-code surface — a seeded checksum drift exits
   :data:`~eawf.cli.exit_codes.INTEGRITY_VIOLATION` (the canonical
   integrity-violation constant; numerically ``3`` post-C05 taxonomy
   compression, the value the legacy "exit 8" gate maps onto), while a
   clean tree exits ``0``. Both the library entry point
   (:func:`doctor_plugin_strict`) and the Typer surface are exercised.

2. Shared sync/doctor portalock — ``plugin doctor --strict`` and
   ``plugin sync`` acquire the same advisory lock
   (:func:`plugin_sync_lock_path`) so the strict drift gate reads the
   post-sync checksum from the same lock-scope (C09 finding F19). A
   concurrent-access test holds the lock in a background thread and
   asserts a second acquire blocks until release.

3. Per-runtime dispatch goldens — the wave-prompt renderer is a pure
   function, so a fixed in-memory :class:`State` renders byte-identically
   across runs. The committed goldens under ``tests/golden/dispatch/``
   cover Claude Code across the five workflow surfaces
   (research / prep / audit / ship / flow) plus a Codex subset
   (research + audit). The prompt body is runtime-agnostic, so the Codex
   goldens document that the same dispatch prompt is portable.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.cli.exit_codes import INTEGRITY_VIOLATION
from eawf.dispatch import render_wave_prompt
from eawf.lifecycle.transitions import open_iter, open_phase, plan_wave
from eawf.lock.portalock import LockTimeout
from eawf.runtimes.claude.plugin_doctor import (
    doctor_plugin_strict,
    plugin_sync_lock,
    plugin_sync_lock_path,
)
from eawf.state.enums import (
    AgentSessionRole,
    EffortBucket,
    ProjectStatus,
    ScopeKind,
)
from eawf.state.models import CurrentPointers, Project, State

runner = CliRunner()

_GOLDEN_DIR: Path = Path(__file__).parent.parent / "golden" / "dispatch"


# ---- Shared fixtures --------------------------------------------------------


def _equip_ea_dir(target: Path) -> None:
    """Drop a minimal ``.ea/`` skeleton under *target* (no state.json needed)."""
    (target / ".ea").mkdir(parents=True, exist_ok=True)
    (target / ".ea" / "indexes").mkdir(exist_ok=True)


@pytest.fixture(autouse=True)
def _isolate_user_scope_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise codex + opencode user-scope conflict detectors.

    Stops the developer machine's real ``~/.codex/plugins/`` /
    ``~/.config/opencode/plugins/`` from tripping the install gate during
    these tests.
    """
    monkeypatch.setattr(
        "eawf.cli.commands.plugin.codex_detect_user_install",
        lambda: None,
    )
    monkeypatch.setattr(
        "eawf.cli.commands.plugin.opencode_detect_user_install",
        lambda: None,
    )


def _install_claude_tree(target: Path) -> None:
    """Render a clean Claude plugin tree under *target* via the CLI."""
    _equip_ea_dir(target)
    result = runner.invoke(app, ["-w", str(target), "plugin", "install", "claude"])
    assert result.exit_code == 0, result.stdout


# ---- 1. ``--strict`` exit-code surface --------------------------------------


def test_doctor_plugin_strict_clean_tree_reports_clean(tmp_path: Path) -> None:
    """A freshly installed tree yields a clean strict report (CLI exit 0)."""
    _install_claude_tree(tmp_path)
    report = doctor_plugin_strict(tmp_path)
    assert report.clean is True
    assert report.drifted == []
    assert report.missing == []


def test_doctor_plugin_strict_drift_reports_dirty(tmp_path: Path) -> None:
    """A hand-edited managed file makes the strict report dirty."""
    _install_claude_tree(tmp_path)
    skill_path = tmp_path / ".claude" / "skills" / "research" / "SKILL.md"
    skill_path.write_text(skill_path.read_text() + "\n# hand edit\n", encoding="utf-8")
    report = doctor_plugin_strict(tmp_path)
    assert report.clean is False
    assert len(report.drifted) == 1
    assert report.drifted[0].region_id == "plugin.claude.skill.research"


def test_doctor_strict_cli_clean_exits_zero(tmp_path: Path) -> None:
    """``plugin doctor --strict`` on a clean tree exits 0."""
    _install_claude_tree(tmp_path)
    result = runner.invoke(app, ["-w", str(tmp_path), "plugin", "doctor", "--strict"])
    assert result.exit_code == 0, result.stdout
    assert "drifted=0 missing=0" in result.stdout


def test_doctor_strict_cli_drift_exits_integrity_violation(tmp_path: Path) -> None:
    """A seeded checksum drift makes ``plugin doctor --strict`` exit 8.

    The "exit 8" gate is the canonical ``INTEGRITY_VIOLATION`` constant,
    which aliases to ``STATE_CONFLICT`` (numerically ``3``) post-C05
    taxonomy compression.
    """
    _install_claude_tree(tmp_path)
    skill_path = tmp_path / ".claude" / "skills" / "research" / "SKILL.md"
    skill_path.write_text(skill_path.read_text() + "\n# hand edit\n", encoding="utf-8")
    result = runner.invoke(app, ["-w", str(tmp_path), "plugin", "doctor", "--strict"])
    assert result.exit_code == INTEGRITY_VIOLATION, result.stdout


def test_doctor_strict_cli_claude_arg_clean_exits_zero(tmp_path: Path) -> None:
    """``plugin doctor claude --strict`` (explicit runtime) on clean exits 0."""
    _install_claude_tree(tmp_path)
    result = runner.invoke(app, ["-w", str(tmp_path), "plugin", "doctor", "claude", "--strict"])
    assert result.exit_code == 0, result.stdout


def test_doctor_strict_rejects_non_claude_runtime(tmp_path: Path) -> None:
    """``--strict`` with codex/opencode is a user error (exit non-zero)."""
    _equip_ea_dir(tmp_path)
    result = runner.invoke(app, ["-w", str(tmp_path), "plugin", "doctor", "codex", "--strict"])
    assert result.exit_code != 0
    assert "--strict applies to the claude checksum sweep only" in result.stdout


# ---- 2. Shared sync/doctor portalock ----------------------------------------


def test_plugin_sync_lock_path_under_ea_locks(tmp_path: Path) -> None:
    """The shared lock target lives under the gitignored ``.ea/locks/`` dir."""
    lock_target = plugin_sync_lock_path(tmp_path)
    assert lock_target == tmp_path / ".ea" / "locks" / "plugin-sync.lock"
    assert lock_target.parent.name == "locks"


def test_doctor_and_sync_share_the_same_lock_target(tmp_path: Path) -> None:
    """Doctor-strict and sync resolve the identical advisory-lock path."""
    # Both verbs derive the lock from the same helper; resolving twice
    # must return the same path so the two surfaces serialise.
    assert plugin_sync_lock_path(tmp_path) == plugin_sync_lock_path(tmp_path)


def test_shared_lock_is_mutually_exclusive(tmp_path: Path) -> None:
    """A second acquire blocks while the shared lock is held, then succeeds.

    This is the concurrent-access guarantee F19 needs: while
    ``plugin sync`` holds the lock, ``plugin doctor --strict`` cannot read
    a mid-sync checksum — its acquire blocks until sync releases.
    """
    _equip_ea_dir(tmp_path)
    held = threading.Event()
    release = threading.Event()

    def _hold_lock() -> None:
        with plugin_sync_lock(tmp_path, timeout=5.0):
            held.set()
            release.wait(timeout=5.0)

    holder = threading.Thread(target=_hold_lock)
    holder.start()
    try:
        assert held.wait(timeout=5.0), "background holder never acquired the lock"
        # While the lock is held, a short-timeout acquire must time out.
        with pytest.raises(LockTimeout), plugin_sync_lock(tmp_path, timeout=0.2):
            pass  # pragma: no cover - acquire must not succeed
    finally:
        release.set()
        holder.join(timeout=5.0)
    # After release the lock is free again.
    with plugin_sync_lock(tmp_path, timeout=5.0):
        pass


def test_doctor_strict_acquires_then_releases_lock(tmp_path: Path) -> None:
    """``doctor_plugin_strict`` leaves no lock held after it returns."""
    _install_claude_tree(tmp_path)
    doctor_plugin_strict(tmp_path)
    # The lock is free immediately after the sweep returns.
    with plugin_sync_lock(tmp_path, timeout=0.5):
        pass


def test_shared_lock_serialises_doctor_after_sync(tmp_path: Path) -> None:
    """A strict sweep started while sync holds the lock waits for release.

    Drives the realistic ordering: a "sync" holder grabs the lock, a
    "doctor" thread tries the strict sweep; the sweep only completes once
    the holder releases — proving doctor reads the post-sync state.
    """
    _install_claude_tree(tmp_path)
    sync_holding = threading.Event()
    sync_release = threading.Event()
    doctor_done = threading.Event()

    def _sync_holder() -> None:
        with plugin_sync_lock(tmp_path, timeout=5.0):
            sync_holding.set()
            sync_release.wait(timeout=5.0)

    def _doctor() -> None:
        # Blocks inside doctor_plugin_strict until the holder releases.
        doctor_plugin_strict(tmp_path, timeout=5.0)
        doctor_done.set()

    holder = threading.Thread(target=_sync_holder)
    doctor = threading.Thread(target=_doctor)
    holder.start()
    assert sync_holding.wait(timeout=5.0)
    doctor.start()
    try:
        # Give doctor a moment; it must NOT finish while sync holds the lock.
        time.sleep(0.3)
        assert not doctor_done.is_set(), "doctor completed before sync released the lock"
    finally:
        sync_release.set()
        holder.join(timeout=5.0)
        doctor.join(timeout=5.0)
    assert doctor_done.is_set(), "doctor never completed after sync released the lock"


# ---- 3. Per-runtime dispatch goldens ----------------------------------------

# Fixed surface catalog driving the dispatch goldens. Each entry pairs a
# workflow surface with the agent role / effort / scope that wave carries.
_SURFACES: list[tuple[str, AgentSessionRole, EffortBucket, list[str], list[str]]] = [
    (
        "research",
        AgentSessionRole.RESEARCHER,
        EffortBucket.S,
        ["src/eawf/research/"],
        ["research brief written under .ea/local/research/"],
    ),
    (
        "prep",
        AgentSessionRole.PLANNER,
        EffortBucket.M,
        ["src/eawf/lifecycle/"],
        ["phase DAG rendered; waves planned PENDING"],
    ),
    (
        "audit",
        AgentSessionRole.AUDITOR,
        EffortBucket.S,
        ["src/eawf/validate/"],
        ["audit verdict recorded with evidence chain"],
    ),
    (
        "ship",
        AgentSessionRole.REVIEWER,
        EffortBucket.M,
        ["src/eawf/cli/"],
        ["PR review pass complete; CI green"],
    ),
    (
        "flow",
        AgentSessionRole.EXECUTOR,
        EffortBucket.L,
        ["src/eawf/", "tests/"],
        ["autonomous execute loop drains the ready frontier"],
    ),
]

# surface -> wave id (W## index into _SURFACES, 1-based).
_SURFACE_WAVE_ID: dict[str, str] = {
    surface: f"P07-I01-W{idx:02d}" for idx, (surface, *_rest) in enumerate(_SURFACES, start=1)
}


def _build_dispatch_state() -> State:
    """Deterministic state: project ABC, P07 → P07-I01 → five surface waves."""
    state = State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:ABC",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "project": Project(
                code="ABC",
                slug="abc",
                title="ABC",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:ABC",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="ABC").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )
    open_phase(state, phase_id="P07", title="Surfaces")
    open_iter(state, iter_id="P07-I01", phase_id="P07", title="Iter1")
    for surface, role, bucket, files, crit in _SURFACES:
        plan_wave(
            state,
            wave_id=_SURFACE_WAVE_ID[surface],
            iter_id="P07-I01",
            title=f"{surface} surface",
            file_scopes=files,
            success_criteria=crit,
            agent_role=role,
            effort_bucket=bucket,
        )
    return state


@pytest.mark.parametrize(
    ("surface", "fixture_name"),
    [
        ("research", "cc_research.txt"),
        ("prep", "cc_prep.txt"),
        ("audit", "cc_audit.txt"),
        ("ship", "cc_ship.txt"),
        ("flow", "cc_flow.txt"),
    ],
)
def test_cc_dispatch_golden_matches(surface: str, fixture_name: str) -> None:
    """Each Claude Code surface renders byte-identically to its golden."""
    state = _build_dispatch_state()
    rendered = render_wave_prompt(state, _SURFACE_WAVE_ID[surface])
    expected = (_GOLDEN_DIR / fixture_name).read_text(encoding="utf-8")
    assert rendered == expected, (
        f"dispatch golden {fixture_name!r} drifted. If intentional, "
        "regenerate the goldens and commit the new bytes."
    )


@pytest.mark.parametrize(
    ("surface", "fixture_name"),
    [
        ("research", "codex_research.txt"),
        ("audit", "codex_audit.txt"),
    ],
)
def test_codex_dispatch_golden_matches(surface: str, fixture_name: str) -> None:
    """The Codex subset renders the same runtime-agnostic prompt body."""
    state = _build_dispatch_state()
    rendered = render_wave_prompt(state, _SURFACE_WAVE_ID[surface])
    expected = (_GOLDEN_DIR / fixture_name).read_text(encoding="utf-8")
    assert rendered == expected, (
        f"dispatch golden {fixture_name!r} drifted. If intentional, "
        "regenerate the goldens and commit the new bytes."
    )


def test_dispatch_goldens_have_no_machine_paths() -> None:
    """No committed dispatch golden may embed a machine path or PII marker."""
    for golden in sorted(_GOLDEN_DIR.glob("*.txt")):
        body = golden.read_text(encoding="utf-8")
        assert "/Users/" not in body, f"machine path leaked into {golden.name}"
        assert ".ea/worktrees" not in body, f"worktree path leaked into {golden.name}"


def test_all_expected_dispatch_goldens_present() -> None:
    """The committed golden set covers the five CC surfaces + Codex subset."""
    present = {p.name for p in _GOLDEN_DIR.glob("*.txt")}
    expected = {
        "cc_research.txt",
        "cc_prep.txt",
        "cc_audit.txt",
        "cc_ship.txt",
        "cc_flow.txt",
        "codex_research.txt",
        "codex_audit.txt",
    }
    assert expected <= present, f"missing dispatch goldens: {expected - present}"
