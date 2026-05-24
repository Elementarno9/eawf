"""Unit tests for :mod:`eawf.doctor.checks`.

Each check is exercised in isolation with the instrument probe stubbed out
so the suite stays hermetic (no calls to the host's ``shutil.which``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eawf.doctor import checks
from eawf.surfaces.cli.errors import UserError


def _stub_probe_ok(monkeypatch: pytest.MonkeyPatch, results: list[Any]) -> None:
    """Replace :func:`eawf.install.instrument_probe.probe` with a fixed return."""
    from eawf.install.instrument_probe import ProbeReport

    def fake(profile_ids: list[str], *, cache_path: Path, reprobe: bool = False) -> ProbeReport:
        return ProbeReport(probe_version=1, profile_ids=profile_ids, results=results)

    monkeypatch.setattr("eawf.doctor.checks.probe", fake)


def _stub_probe_raises(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def fake(*_args: object, **_kwargs: object) -> None:
        raise exc

    monkeypatch.setattr("eawf.doctor.checks.probe", fake)


def test_check_tools_available_all_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eawf.install.instrument_probe import ProbeResult

    _stub_probe_ok(
        monkeypatch,
        [
            ProbeResult(name="git", kind="hard", status="ok", path="/x/git"),
            ProbeResult(name="python", kind="hard", status="ok", path="/x/python"),
            ProbeResult(name="uv", kind="hard", status="ok", path="/x/uv"),
        ],
    )
    result = checks.check_tools_available(workspace=tmp_path)
    assert result.status == "ok"
    assert result.name == "tools_available"


def test_check_tools_available_soft_missing_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eawf.install.instrument_probe import ProbeResult

    _stub_probe_ok(
        monkeypatch,
        [
            ProbeResult(name="git", kind="hard", status="ok", path="/x/git"),
            ProbeResult(
                name="optional-tool",
                kind="soft",
                status="warn",
                detail="optional-tool not on PATH",
            ),
        ],
    )
    result = checks.check_tools_available(workspace=tmp_path)
    assert result.status == "warn"
    assert "optional-tool" in (result.detail or "")


def test_check_tools_available_hard_missing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_probe_raises(monkeypatch, UserError("git missing", kind="InstrumentMissing"))
    with pytest.raises(UserError):
        checks.check_tools_available(workspace=tmp_path)


def test_check_state_present_ok(tmp_path: Path) -> None:
    state = tmp_path / ".ea" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{}", encoding="utf-8")
    result = checks.check_state_present(workspace=tmp_path)
    assert result.status == "ok"
    assert "state.json" in (result.detail or "")


def test_check_state_present_missing_warns(tmp_path: Path) -> None:
    result = checks.check_state_present(workspace=tmp_path)
    assert result.status == "warn"


def test_check_config_resolves_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The check returns ``ok`` against a clean tmp workspace with no overlays."""
    monkeypatch.chdir(tmp_path)
    result = checks.check_config_resolves(workspace=tmp_path)
    assert result.status == "ok"


def test_check_config_resolves_unknown_profile_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "eawf.doctor.checks.merge_config",
        lambda **_: ({"profiles": {"enabled": ["bogus"]}}, {}),
    )
    result = checks.check_config_resolves(workspace=tmp_path)
    assert result.status == "warn"
    assert "bogus" in (result.detail or "")


def test_run_all_returns_full_check_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """W08 extends ``run_all`` with manifest_in_sync + render_output_roundtrip."""
    from eawf.install.instrument_probe import ProbeResult

    _stub_probe_ok(
        monkeypatch,
        [ProbeResult(name="git", kind="hard", status="ok", path="/x/git")],
    )
    monkeypatch.chdir(tmp_path)
    results = checks.run_all(workspace=tmp_path)
    assert len(results) == 6
    assert {r.name for r in results} == {
        "tools_available",
        "state_present",
        "config_resolves",
        "manifest_in_sync",
        "mcp_drift",
        "render_output_roundtrip",
    }
