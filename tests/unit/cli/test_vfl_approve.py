"""Unit tests for ``eawf vfl approve`` -- the FS17 golden-approval guard.

The wave-verifiable contract is the diff-detection + exit-code guard:

- **CR-2 (pending diff -> exit 0)** -- ``eawf vfl approve --kind <surface>``
  regenerates the surface's golden bytes; when the regeneration leaves a
  pending working-tree diff under the surface's ``golden_dir`` the verb
  approves it and exits ``0``.
- **CR-1 (no pending diff -> non-zero)** -- when the golden already matches
  current-code output (no pending diff after regeneration) the verb refuses
  with a "nothing to approve" message and exits ``USER_ERROR`` (1).

The tests are deterministic and do NOT require ``resvg``: the snapshot
regeneration subprocess (``run_regen``) and the ``git diff`` probe
(``golden_dir_has_diff``'s ``subprocess.run``) are both stubbed so the
diff-detection branch is exercised against a controlled signal rather than
a real renderer / repo.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import vfl as vfl_cmd
from eawf.surfaces.cli.commands.snapshot import SnapshotSurface, resolve_surface

runner = CliRunner()


def _ok_regen(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Stub ``run_regen`` -- a successful (exit 0) regeneration."""
    return subprocess.CompletedProcess(["pytest"], 0, stdout="1 passed", stderr="")


# --- vfl registration smoke -------------------------------------------------


def test_vfl_group_resolves() -> None:
    """`eawf vfl --help` resolves -- the group is mounted on the root app."""
    result = runner.invoke(app, ["vfl", "--help"])
    assert result.exit_code == 0, result.output
    assert "approve" in result.output


# --- CR-2: pending golden diff -> exit 0 ------------------------------------


def test_approve_with_pending_diff_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pending golden diff after regen -> approve exits 0 (CR-2)."""
    monkeypatch.setattr(vfl_cmd, "run_regen", _ok_regen)
    monkeypatch.setattr(vfl_cmd, "golden_dir_has_diff", lambda surface, *, workspace: True)

    result = runner.invoke(app, ["--json", "vfl", "approve", "--kind", "svg"])
    assert result.exit_code == exit_codes.OK, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "svg"
    assert payload["approved"] is True
    assert payload["golden_dir"] == "tests/snapshots/svg/golden"


# --- CR-1: no pending golden diff -> non-zero -------------------------------


def test_approve_without_pending_diff_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean golden tree (no pending diff) -> approve refuses, exits non-zero (CR-1)."""
    monkeypatch.setattr(vfl_cmd, "run_regen", _ok_regen)
    monkeypatch.setattr(vfl_cmd, "golden_dir_has_diff", lambda surface, *, workspace: False)

    result = runner.invoke(app, ["vfl", "approve", "--kind", "svg"])
    assert result.exit_code != exit_codes.OK
    assert result.exit_code == exit_codes.USER_ERROR, result.output
    assert "nothing to approve" in result.output


def test_approve_without_pending_diff_json_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """The no-diff refusal renders the canonical UserError envelope."""
    monkeypatch.setattr(vfl_cmd, "run_regen", _ok_regen)
    monkeypatch.setattr(vfl_cmd, "golden_dir_has_diff", lambda surface, *, workspace: False)

    result = runner.invoke(app, ["--json", "vfl", "approve", "--kind", "svg"])
    assert result.exit_code == exit_codes.USER_ERROR, result.output
    payload = json.loads(result.output)
    assert payload["error"] == "UserError"
    assert payload["exit_name"] == "USER_ERROR"
    assert payload["data"]["kind_surface"] == "svg"


# --- error path: unknown kind / regen failure -------------------------------


def test_approve_unknown_kind_exits_user_error() -> None:
    """`eawf vfl approve --kind bogus` exits USER_ERROR (1)."""
    result = runner.invoke(app, ["vfl", "approve", "--kind", "bogus"])
    assert result.exit_code == exit_codes.USER_ERROR, result.output


def test_approve_regen_failure_exits_user_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero regeneration subprocess maps onto USER_ERROR (1)."""

    def _failing_regen(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["pytest"], 1, stdout="boom", stderr="1 failed")

    monkeypatch.setattr(vfl_cmd, "run_regen", _failing_regen)
    result = runner.invoke(app, ["vfl", "approve", "--kind", "svg"])
    assert result.exit_code == exit_codes.USER_ERROR, result.output


# --- golden_dir_has_diff probe ----------------------------------------------


def test_golden_dir_has_diff_true_on_nonzero_git_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`git diff --quiet` exit 1 -> a pending diff is reported (boundary)."""
    surface = resolve_surface("svg")

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv[:3] == ["git", "diff", "--quiet"]
        assert surface.golden_dir in argv
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert vfl_cmd.golden_dir_has_diff(surface, workspace=None) is True


def test_golden_dir_has_diff_false_on_zero_git_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`git diff --quiet` exit 0 -> no pending diff is reported (boundary)."""
    surface = resolve_surface("svg")

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert vfl_cmd.golden_dir_has_diff(surface, workspace=None) is False


# --- inventory wiring -------------------------------------------------------


def test_svg_surface_registered() -> None:
    """The SVG/VFL golden surface is in the locked inventory (FS17 registration)."""
    surface = resolve_surface("svg")
    assert isinstance(surface, SnapshotSurface)
    assert surface.golden_dir == "tests/snapshots/svg/golden"
    assert surface.regen_target == "tests/snapshots/svg/test_svg_oracle.py"
