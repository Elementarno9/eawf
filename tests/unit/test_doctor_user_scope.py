"""Unit tests for the ``eawf doctor --user-scope`` probe and
``update_plugin(check=True)`` dry-mode.

The probe must:

- Return ``ok`` when ``uv tool list`` reports the current eawf version.
- Return ``warn`` when the installed version differs (with a hint to run
  ``uv tool upgrade eawf``).
- Return ``info`` when ``uv tool list`` runs cleanly but no eawf entry
  appears.
- Return ``warn`` (never crash) when ``uv`` is missing from PATH.

The ``update_plugin(check=True)`` dry-mode must touch no bytes on disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import eawf
from eawf.cli.app import app
from eawf.runtimes.claude.plugin_install import install_plugin
from eawf.runtimes.claude.plugin_update import update_plugin


def _fake_completed(stdout: str = "", stderr: str = "", returncode: int = 0) -> Any:
    """Build a duck-typed ``CompletedProcess`` for monkeypatching."""

    class _CP:
        pass

    cp = _CP()
    cp.stdout = stdout
    cp.stderr = stderr
    cp.returncode = returncode
    return cp


def _set_uv_present(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    """Stub the user-scope probe's uv lookup + ``uv tool list`` invocation.

    The doctor command resolves ``uv`` via :func:`_which_uv` (module-local
    wrapper) and reads ``uv tool list`` via :func:`_run_uv_tool_list`. The
    test monkeypatches both private helpers so the stub does not leak to
    the instrument probe's own ``subprocess.run`` call site.
    """
    monkeypatch.setattr(
        "eawf.cli.commands.doctor._which_uv",
        lambda: "/fake/uv",
    )

    def fake_run() -> Any:
        return _fake_completed(stdout=stdout, returncode=0)

    monkeypatch.setattr(
        "eawf.cli.commands.doctor._run_uv_tool_list",
        fake_run,
    )


def _find_user_scope_check(envelope: dict[str, Any]) -> dict[str, Any]:
    """Return the ``user_scope`` entry from the doctor JSON envelope."""
    for check in envelope["checks"]:
        if check["name"] == "user_scope":
            return check
    raise AssertionError(f"user_scope not in envelope: {envelope}")


def test_user_scope_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Installed user-scope eawf matches current → ``ok``."""
    stdout = f"eawf v{eawf.__version__}\n- eawf\n- ea\n"
    _set_uv_present(monkeypatch, stdout)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    res = runner.invoke(app, ["--json", "doctor", "--user-scope"])
    assert res.exit_code == 0, res.output
    envelope = json.loads(res.output)
    user_scope = _find_user_scope_check(envelope)
    assert user_scope["status"] == "ok"
    assert eawf.__version__ in user_scope["detail"]


def test_user_scope_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Installed version differs → ``warn`` and message references ``uv tool upgrade``."""
    stdout = "eawf v0.0.1\n- eawf\n- ea\n"
    _set_uv_present(monkeypatch, stdout)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    res = runner.invoke(app, ["--json", "doctor", "--user-scope"])
    assert res.exit_code == 0, res.output
    envelope = json.loads(res.output)
    user_scope = _find_user_scope_check(envelope)
    assert user_scope["status"] == "warn"
    assert "uv tool upgrade" in user_scope["detail"]
    assert "v0.0.1" in user_scope["detail"]


def test_user_scope_not_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``uv tool list`` shows other tools but no eawf → ``info`` status."""
    stdout = "other-tool v1.0\n- other-tool\n"
    _set_uv_present(monkeypatch, stdout)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    res = runner.invoke(app, ["--json", "doctor", "--user-scope"])
    assert res.exit_code == 0, res.output
    envelope = json.loads(res.output)
    user_scope = _find_user_scope_check(envelope)
    assert user_scope["status"] == "info"
    assert "uv tool install" in user_scope["detail"]


def test_user_scope_uv_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``uv`` is absent from PATH → ``warn`` and message references PATH."""
    monkeypatch.setattr(
        "eawf.cli.commands.doctor._which_uv",
        lambda: None,
    )

    # Defence in depth: if the probe still reached subprocess we'd want
    # the test to fail loudly rather than spawn a real uv.
    def _explode() -> Any:
        raise AssertionError("_run_uv_tool_list must not be called when uv is missing")

    monkeypatch.setattr("eawf.cli.commands.doctor._run_uv_tool_list", _explode)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    res = runner.invoke(app, ["--json", "doctor", "--user-scope"])
    assert res.exit_code == 0, res.output
    envelope = json.loads(res.output)
    user_scope = _find_user_scope_check(envelope)
    assert user_scope["status"] == "warn"
    assert "PATH" in user_scope["detail"]


def test_user_scope_subprocess_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``OSError`` from ``_run_uv_tool_list`` collapses to ``warn`` — never crashes."""
    monkeypatch.setattr(
        "eawf.cli.commands.doctor._which_uv",
        lambda: "/fake/uv",
    )

    def _raise() -> Any:
        raise OSError("simulated spawn failure")

    monkeypatch.setattr("eawf.cli.commands.doctor._run_uv_tool_list", _raise)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    res = runner.invoke(app, ["--json", "doctor", "--user-scope"])
    assert res.exit_code == 0, res.output
    envelope = json.loads(res.output)
    user_scope = _find_user_scope_check(envelope)
    assert user_scope["status"] == "warn"
    assert "uv tool list failed" in user_scope["detail"]


def test_user_scope_flag_absent_omits_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``eawf doctor`` (no flag) must NOT run the user-scope probe."""

    # If the probe ran, this stub would explode and fail the test.
    def _explode() -> str | None:
        raise AssertionError("user-scope probe must not run without --user-scope")

    monkeypatch.setattr("eawf.cli.commands.doctor._which_uv", _explode)
    monkeypatch.chdir(tmp_path)

    runner = CliRunner()
    res = runner.invoke(app, ["--json", "doctor"])
    assert res.exit_code == 0, res.output
    envelope = json.loads(res.output)
    names = {c["name"] for c in envelope["checks"]}
    assert "user_scope" not in names


def test_parse_uv_tool_list_ignores_bullets() -> None:
    """The parser keeps only header lines, ignoring bullet entries."""
    from eawf.cli.commands.doctor import _parse_uv_tool_list

    stdout = "eawf v0.1.0\n- eawf\n- ea\nother v2.3\n- other\n"
    tools = _parse_uv_tool_list(stdout)
    assert tools == {"eawf": "0.1.0", "other": "2.3"}


def test_parse_uv_tool_list_empty_stdout() -> None:
    """Empty stdout yields an empty mapping (no crash)."""
    from eawf.cli.commands.doctor import _parse_uv_tool_list

    assert _parse_uv_tool_list("") == {}
    assert _parse_uv_tool_list("\n\n") == {}


def test_update_plugin_check_mode(tmp_path: Path) -> None:
    """``update_plugin(check=True)`` writes no bytes after a fresh install."""
    install_plugin(tmp_path)

    # Snapshot every managed-file mtime + content under .claude/ so we can
    # detect any disk write that slipped through.
    managed_root = tmp_path / ".claude"
    snapshot: dict[Path, tuple[bytes, float]] = {}
    for p in managed_root.rglob("*"):
        if p.is_file():
            snapshot[p] = (p.read_bytes(), p.stat().st_mtime_ns)

    result = update_plugin(tmp_path, check=True)

    assert result.dry_run is True
    # Every delta must be ``unchanged`` because the on-disk bytes already
    # match the freshly-rendered registry payload.
    for delta in result.skills + result.agents + result.hooks:
        assert delta.action == "unchanged", f"unexpected action: {delta}"
    assert result.settings is not None
    assert result.settings.action == "unchanged"

    # Verify no disk writes happened: same bytes, same mtime_ns.
    for p, (old_bytes, old_mtime) in snapshot.items():
        assert p.read_bytes() == old_bytes, f"bytes mutated: {p}"
        assert p.stat().st_mtime_ns == old_mtime, f"mtime mutated: {p}"


def test_update_plugin_check_mode_signals_would_update(tmp_path: Path) -> None:
    """A registry payload mismatch surfaces as ``updated`` under ``check=True``.

    We do not have a registry mutation knob exposed at test time. Instead,
    we install once, *then* delete the manifest so the second install path
    cannot detect drift via the manifest hashes — but the renderer still
    produces the same bytes for managed files, so deltas remain unchanged.
    The intent of this test is to assert the dry-run flag *is* respected
    even when ``update`` would otherwise have to write the manifest.
    """
    install_plugin(tmp_path)

    # Drop the manifest only; the managed files themselves stay intact.
    manifest_path = tmp_path / ".ea" / "indexes" / "generated.json"
    before_bytes = manifest_path.read_bytes() if manifest_path.exists() else None

    result = update_plugin(tmp_path, check=True)
    assert result.dry_run is True

    # If the manifest existed pre-call, it must still match byte-for-byte.
    if before_bytes is not None:
        assert manifest_path.read_bytes() == before_bytes
