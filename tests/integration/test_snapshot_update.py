"""Unit tests for ``eawf snapshot update`` + the CI snapshot-pairing gate.

Covers the three load-bearing guarantees of P27-W19:

- **Surface dispatch** — ``eawf snapshot update --kind <surface>`` resolves
  the surface from the locked C09 §5.6 inventory and drives its golden
  regeneration under ``EAWF_REFRESH_GOLDEN=1``, honouring ``--out`` so the
  rewritten subset can be verified against a tmp fixture directory.
- **Unknown kind** — an unrecognised ``--kind`` exits ``USER_ERROR`` (1).
- **CI pairing gate** — ``tools/snapshot_pairing_gate.py`` fails when a
  *managed* golden surface is mutated (status ``M`` / ``D`` / ``R``)
  without a paired ``[P##-W##] test:`` commit, and passes for paired
  mutations, pure additions, and non-inventory golden trees.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands.snapshot import (
    SNAPSHOT_SURFACES,
    SnapshotSurface,
    resolve_surface,
    run_regen,
)

runner = CliRunner()

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE_PATH = _REPO_ROOT / "tools" / "snapshot_pairing_gate.py"


def _load_gate() -> Any:
    """Import ``tools/snapshot_pairing_gate.py`` as a module."""
    tool_dir = _GATE_PATH.parent
    if str(tool_dir) not in sys.path:
        sys.path.insert(0, str(tool_dir))
    spec = importlib.util.spec_from_file_location("snapshot_pairing_gate", _GATE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["snapshot_pairing_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def gate() -> Any:
    return _load_gate()


# --- Surface inventory ------------------------------------------------------


def test_inventory_kinds_are_unique_and_sorted_paths() -> None:
    """Each surface key matches its ``kind`` and maps to a distinct dir."""
    for key, surface in SNAPSHOT_SURFACES.items():
        assert key == surface.kind
    dirs = [s.golden_dir for s in SNAPSHOT_SURFACES.values()]
    assert len(dirs) == len(set(dirs)), "golden dirs must be unique per surface"


def test_inventory_covers_spec_locked_kinds() -> None:
    """The §5.6 locked surface set is present (boundary: full inventory).

    ``tui_config_modal`` extends the locked set: its committed bytes live
    at ``tests/golden/tui_config_modal/`` and must be guarded by the
    pairing gate alongside the primary ``tui`` surface. ``svg`` extends it
    with the visual-fidelity oracle golden render, guarded under
    ``tests/snapshots/svg/golden/`` and driven by ``eawf vfl approve``.
    """
    expected = {
        "state",
        "envelope",
        "dispatch",
        "plan_view",
        "tui",
        "tui_config_modal",
        "spec",
        "agent_report",
        "plugin_install",
        "audit_dsl",
        "scenarios",
        "telemetry",
        "metrics_export",
        "agents_md",
        "svg",
    }
    assert set(SNAPSHOT_SURFACES) == expected


def test_inventory_tui_golden_dir_points_at_real_bytes() -> None:
    """The ``tui`` surface watches the real Textual golden tree on disk.

    The pre-fix value ``tests/golden/tui`` does not exist, so the gate
    watched an empty set and never fired for the primary TUI surface.
    The bytes actually live under ``tests/snapshots/tui/golden/``.
    """
    surface = resolve_surface("tui")
    assert surface.golden_dir == "tests/snapshots/tui/golden"
    bytes_dir = _REPO_ROOT / surface.golden_dir
    assert bytes_dir.is_dir(), f"tui golden dir missing on disk: {surface.golden_dir!r}"


def test_inventory_tui_config_modal_is_guarded() -> None:
    """The ``tui_config_modal`` surface is in the inventory and on disk."""
    surface = resolve_surface("tui_config_modal")
    assert surface.golden_dir == "tests/golden/tui_config_modal"
    bytes_dir = _REPO_ROOT / surface.golden_dir
    assert bytes_dir.is_dir(), (
        f"tui_config_modal golden dir missing on disk: {surface.golden_dir!r}"
    )


def test_resolve_surface_returns_typed_surface() -> None:
    surface = resolve_surface("envelope")
    assert isinstance(surface, SnapshotSurface)
    assert surface.golden_dir == "tests/golden/envelope"


def test_resolve_surface_unknown_raises_user_error() -> None:
    """Unknown kind raises ``UserError`` (USER_ERROR bucket)."""
    from eawf.surfaces.cli import errors as cli_errors

    with pytest.raises(cli_errors.UserError) as excinfo:
        resolve_surface("bogus")
    assert "bogus" in str(excinfo.value)


# --- run_regen plumbing -----------------------------------------------------


def test_run_regen_sets_both_refresh_envs_and_writes_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_regen`` sets both refresh switches plus EAWF_SNAPSHOT_OUT and
    regeneration writes the subset into the tmp ``--out`` directory.

    The pytest subprocess is replaced by a stub that honours
    ``EAWF_SNAPSHOT_OUT`` exactly as a refresh-aware golden test would —
    proving the env+out plumbing end-to-end against a tmp fixture without
    a 400 s real pytest run.
    """
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        seen["argv"] = argv
        seen["env"] = env
        out = env.get("EAWF_SNAPSHOT_OUT")
        # Mimic a refresh-aware golden test: write the regenerated subset
        # to EAWF_SNAPSHOT_OUT when set.
        if out is not None:
            Path(out, "expected.json").write_text('{"ok": true}\n', encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="1 passed", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    surface = resolve_surface("envelope")
    out_dir = tmp_path / "regen"
    out_dir.mkdir()
    completed = run_regen(surface, workspace=None, output_dir=out_dir)

    assert completed.returncode == 0
    assert seen["env"]["EAWF_REFRESH_GOLDEN"] == "1"
    assert seen["env"]["EAWF_SNAPSHOT_REGEN"] == "1"
    assert seen["env"]["EAWF_SNAPSHOT_OUT"] == str(out_dir)
    assert surface.regen_target in seen["argv"]
    assert (out_dir / "expected.json").read_text(encoding="utf-8") == '{"ok": true}\n'


def test_run_regen_omits_snapshot_out_when_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-place regen (no ``--out``) does not set EAWF_SNAPSHOT_OUT."""
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    run_regen(resolve_surface("state"), workspace=None, output_dir=None)
    assert "EAWF_SNAPSHOT_OUT" not in seen["env"]
    assert seen["env"]["EAWF_REFRESH_GOLDEN"] == "1"
    assert seen["env"]["EAWF_SNAPSHOT_REGEN"] == "1"


# --- CLI surface ------------------------------------------------------------


def test_cli_snapshot_list_json() -> None:
    """`eawf snapshot list --json` enumerates the surface inventory."""
    import json

    result = runner.invoke(app, ["--json", "snapshot", "list"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    kinds = {row["kind"] for row in payload["surfaces"]}
    assert kinds == set(SNAPSHOT_SURFACES)


def test_cli_snapshot_update_regenerates_into_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`eawf snapshot update --kind envelope --out <tmp>` regenerates the
    subset into the tmp fixture dir and reports success."""
    import json

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        out = kwargs["env"].get("EAWF_SNAPSHOT_OUT")
        assert out is not None
        Path(out, "ok.json").write_text("{}\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="1 passed", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out_dir = tmp_path / "fixtures"
    result = runner.invoke(
        app,
        ["--json", "snapshot", "update", "--kind", "envelope", "--out", str(out_dir)],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "envelope"
    assert payload["written_to"] == str(out_dir)
    assert (out_dir / "ok.json").exists()


def test_cli_snapshot_update_unknown_kind_exits_user_error() -> None:
    """`eawf snapshot update --kind bogus` exits USER_ERROR (1)."""
    result = runner.invoke(app, ["snapshot", "update", "--kind", "bogus"])
    assert result.exit_code == exit_codes.USER_ERROR, result.output


def test_cli_snapshot_update_unknown_kind_json_envelope() -> None:
    """The unknown-kind error renders the canonical UserError envelope."""
    import json

    result = runner.invoke(app, ["--json", "snapshot", "update", "--kind", "bogus"])
    assert result.exit_code == exit_codes.USER_ERROR, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "UserError"
    assert payload["exit_name"] == "USER_ERROR"


def test_cli_snapshot_update_regen_failure_exits_user_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero regeneration subprocess maps onto USER_ERROR (1)."""

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, stdout="boom", stderr="1 failed")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = runner.invoke(app, ["snapshot", "update", "--kind", "envelope"])
    assert result.exit_code == exit_codes.USER_ERROR, result.output


# --- CI pairing gate --------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "commit", "--allow-empty", "-q", "-m", "[P00] state: seed")
    return repo


def _write_golden(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _head(repo: Path) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def test_gate_passes_when_no_golden_change(
    tmp_path: Path, gate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No golden mutation in range → gate passes."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    _write_golden(repo, "src/x.py", "print('hi')\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "[P27-W19] feat: unrelated change")
    monkeypatch.chdir(repo)
    assert gate.find_unpaired(base, _head(repo)) == []


def test_gate_fails_on_unpaired_golden_mutation(
    tmp_path: Path, gate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modifying a *managed* golden under ``feat:`` → gate fails."""
    repo = _init_repo(tmp_path)
    # Seed an existing golden under a managed surface dir, then mutate it.
    _write_golden(repo, "tests/golden/envelope/ok.json", '{"v": 1}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "[P27-W18] test: seed envelope golden")
    base = _head(repo)
    _write_golden(repo, "tests/golden/envelope/ok.json", '{"v": 2}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "[P27-W19] feat: sneak golden drift")
    monkeypatch.chdir(repo)
    offenders = gate.find_unpaired(base, _head(repo))
    assert len(offenders) == 1
    assert "feat: sneak golden drift" in offenders[0][1]


def test_gate_passes_on_paired_golden_mutation(
    tmp_path: Path, gate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modifying a managed golden under a wave-form ``test:`` → gate passes."""
    repo = _init_repo(tmp_path)
    _write_golden(repo, "tests/golden/dispatch/cc_research.txt", "v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "[P27-W18] test: seed dispatch golden")
    base = _head(repo)
    _write_golden(repo, "tests/golden/dispatch/cc_research.txt", "v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "[P27-W19] test: snapshot update dispatch")
    monkeypatch.chdir(repo)
    assert gate.find_unpaired(base, _head(repo)) == []


def test_gate_fails_on_unpaired_tui_golden_mutation(
    tmp_path: Path, gate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TUI snapshot byte change under ``feat:`` → gate fails.

    Regression for the primary failure mode: before the fix the ``tui``
    surface watched the non-existent ``tests/golden/tui`` dir, so a
    Textual golden byte change at ``tests/snapshots/tui/golden/`` rode
    in unpaired and the gate never fired.
    """
    repo = _init_repo(tmp_path)
    _write_golden(repo, "tests/snapshots/tui/golden/repo_screen.txt", "screen v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "[P27-W18] test: seed tui golden")
    base = _head(repo)
    _write_golden(repo, "tests/snapshots/tui/golden/repo_screen.txt", "screen v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "[P27-W19] feat: sneak tui golden drift")
    monkeypatch.chdir(repo)
    offenders = gate.find_unpaired(base, _head(repo))
    assert len(offenders) == 1
    assert "feat: sneak tui golden drift" in offenders[0][1]


def test_gate_passes_on_paired_tui_golden_mutation(
    tmp_path: Path, gate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TUI golden byte change paired with a wave-form ``test:`` → passes."""
    repo = _init_repo(tmp_path)
    _write_golden(repo, "tests/snapshots/tui/golden/help_overlay.txt", "v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "[P27-W18] test: seed tui golden")
    base = _head(repo)
    _write_golden(repo, "tests/snapshots/tui/golden/help_overlay.txt", "v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "[P27-W19] test: snapshot update tui")
    monkeypatch.chdir(repo)
    assert gate.find_unpaired(base, _head(repo)) == []


def test_gate_fails_on_unpaired_tui_config_modal_mutation(
    tmp_path: Path, gate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``tui_config_modal`` golden byte change under ``feat:`` → gate fails."""
    repo = _init_repo(tmp_path)
    _write_golden(repo, "tests/golden/tui_config_modal/modal_default.txt", "modal v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "[P27-W18] test: seed config modal golden")
    base = _head(repo)
    _write_golden(repo, "tests/golden/tui_config_modal/modal_default.txt", "modal v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "[P27-W19] feat: sneak config modal drift")
    monkeypatch.chdir(repo)
    offenders = gate.find_unpaired(base, _head(repo))
    assert len(offenders) == 1
    assert "feat: sneak config modal drift" in offenders[0][1]


def test_watched_dirs_include_both_tui_surfaces(gate: Any) -> None:
    """The gate's watch set covers both TUI golden trees (boundary)."""
    assert "tests/snapshots/tui/golden/" in gate._WATCHED_DIRS
    assert "tests/golden/tui_config_modal/" in gate._WATCHED_DIRS


def test_gate_exempts_pure_addition_under_feat(
    tmp_path: Path, gate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding a brand-new managed golden under ``feat:`` is exempt."""
    repo = _init_repo(tmp_path)
    base = _head(repo)
    _write_golden(repo, "tests/golden/dispatch/codex_ship.txt", "new\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "[P27-W19] feat: introduce dispatch surface")
    monkeypatch.chdir(repo)
    assert gate.find_unpaired(base, _head(repo)) == []


def test_gate_exempts_non_inventory_golden_dir(
    tmp_path: Path, gate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A golden tree outside the §5.6 inventory (``tests/golden/cli/``) is
    exempt even when modified under ``feat:`` (matches W18 reality)."""
    repo = _init_repo(tmp_path)
    _write_golden(repo, "tests/golden/cli/help_panels.golden.txt", "panel v1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "[P27-W18] feat: seed cli golden")
    base = _head(repo)
    _write_golden(repo, "tests/golden/cli/help_panels.golden.txt", "panel v2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "[P27-W19] feat: add a command (help golden drifts)")
    monkeypatch.chdir(repo)
    assert gate.find_unpaired(base, _head(repo)) == []


def test_gate_rejects_w00_in_paired_subject(gate: Any) -> None:
    """W00 is rejected — wave indices are 1-based (boundary)."""
    assert gate.is_paired("[P27-W19] test: ok") is True
    assert gate.is_paired("[P27-W00] test: bad") is False
    assert gate.is_paired("[P27-I00-W01] test: bad") is False


def test_gate_accepts_iter_and_core_variants(gate: Any) -> None:
    """Iter, legacy CORE, and out-of-phase forms satisfy pairing grammar."""
    assert gate.is_paired("[P27-I02-W03] test: snapshot update spec") is True
    assert gate.is_paired("[P27-CORE] test: snapshot update agents_md") is True
    assert gate.is_paired("[P27-I02-CORE] test: snapshot update tui") is True
    assert gate.is_paired("test: snapshot update tui") is True


def test_gate_rejects_non_test_type(gate: Any) -> None:
    """Only ``test:`` satisfies the pairing grammar (feat/fix rejected)."""
    assert gate.is_paired("[P27-W19] feat: x") is False
    assert gate.is_paired("[P27-W19] fix: x") is False
    assert gate.is_paired("[P27-W19] chore: x") is False
    assert gate.is_paired("feat: x") is False
    assert gate.is_paired("test:") is False


def test_gate_main_no_base_is_noop(gate: Any) -> None:
    """No PR base/head → gate no-ops with exit 0 (push build)."""
    assert gate.main(["prog", "", ""]) == 0


def test_iter_key_extracts_phase_and_iter_scope(gate: Any) -> None:
    """iter_key maps a subject to its phase/iter scope key (boundary forms)."""
    assert gate.iter_key("[P27-I04-W04] feat: x") == "P27-I04"
    assert gate.iter_key("[P27-W19] test: x") == "P27"
    assert gate.iter_key("[P27-CORE] state: x") == "P27"
    assert gate.iter_key("no tag here") is None


def test_gate_main_phase_pr_does_not_block(
    tmp_path: Path, gate: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A multi-iter (phase-PR) range surfaces unpaired golden commits, not blocks.

    The whole phase ships as one reviewed unit and the snapshot tests pin
    golden freshness, so per-commit ``test:`` pairing is deferred: the gate
    lists the bundled golden commit and exits 0.
    """
    repo = _init_repo(tmp_path)
    _write_golden(repo, "tests/golden/envelope/ok.json", '{"v": 1}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "[P27-I02-W01] test: seed envelope golden")
    base = _head(repo)
    _write_golden(repo, "tests/golden/envelope/ok.json", '{"v": 2}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "[P27-I04-W04] feat: bundle golden drift")
    _git(repo, "commit", "--allow-empty", "-q", "-m", "[P27-I05-W01] chore: second iter")
    monkeypatch.chdir(repo)
    assert gate.main(["prog", base, _head(repo)]) == 0
    out = capsys.readouterr().out
    assert "phase PR detected" in out
    assert "bundle golden drift" in out


def test_gate_main_single_iter_blocks(
    tmp_path: Path, gate: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A single-iter (small-CL) range still hard-fails an unpaired golden mutation."""
    repo = _init_repo(tmp_path)
    _write_golden(repo, "tests/golden/envelope/ok.json", '{"v": 1}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "[P27-I05-W01] test: seed envelope golden")
    base = _head(repo)
    _write_golden(repo, "tests/golden/envelope/ok.json", '{"v": 2}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "[P27-I05-W02] feat: sneak golden drift")
    monkeypatch.chdir(repo)
    assert gate.main(["prog", base, _head(repo)]) == 1
    err = capsys.readouterr().err
    assert "unpaired golden-surface mutation" in err


def test_gate_main_usage_error(gate: Any) -> None:
    """Missing args → usage error exit 1."""
    assert gate.main(["prog"]) == 1
