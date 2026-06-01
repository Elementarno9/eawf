"""Unit tests for :mod:`eawf.observability.doctor.checks`.

Each check is exercised in isolation with the instrument probe stubbed out
so the suite stays hermetic (no calls to the host's ``shutil.which``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eawf.observability.doctor import checks
from eawf.surfaces.cli.errors import UserError


def _stub_probe_ok(monkeypatch: pytest.MonkeyPatch, results: list[Any]) -> None:
    """Replace :func:`eawf.platform.install.instrument_probe.probe` with a fixed return."""
    from eawf.platform.install.instrument_probe import ProbeReport

    def fake(profile_ids: list[str], *, cache_path: Path, reprobe: bool = False) -> ProbeReport:
        return ProbeReport(probe_version=1, profile_ids=profile_ids, results=results)

    monkeypatch.setattr("eawf.observability.doctor.checks.probe", fake)


def _stub_probe_raises(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def fake(*_args: object, **_kwargs: object) -> None:
        raise exc

    monkeypatch.setattr("eawf.observability.doctor.checks.probe", fake)


def test_check_tools_available_all_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eawf.platform.install.instrument_probe import ProbeResult

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
    from eawf.platform.install.instrument_probe import ProbeResult

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
        "eawf.observability.doctor.checks.merge_config",
        lambda **_: ({"profiles": {"enabled": ["bogus"]}}, {}),
    )
    result = checks.check_config_resolves(workspace=tmp_path)
    assert result.status == "warn"
    assert "bogus" in (result.detail or "")


def _stub_state_with_wave_count(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    """Force ``check_state_scale_ceiling`` to see a state of *count* waves.

    The check only reads ``len(state.waves)``, so a :class:`SimpleNamespace`
    with a sized ``waves`` mapping is a faithful stand-in — and far cheaper
    than validating thousands of real :class:`Wave` rows near the ceiling.
    """
    from types import SimpleNamespace

    stub_state = SimpleNamespace(waves=dict.fromkeys(range(count)))

    def fake_load(workspace: Path, *, name: str) -> tuple[object, Path]:
        return stub_state, workspace / ".ea" / "state.json"

    monkeypatch.setattr("eawf.observability.doctor.checks._load_state_for_check", fake_load)


def test_check_state_scale_ceiling_no_workspace_ok() -> None:
    """A missing workspace anchor is informational, not a failure."""
    result = checks.check_state_scale_ceiling(workspace=None)
    assert result.status == "ok"
    assert result.name == "state_scale_ceiling"


def test_check_state_scale_ceiling_no_state_ok(tmp_path: Path) -> None:
    """No resolvable ``state.json`` yields ``ok`` (state_present owns absence)."""
    result = checks.check_state_scale_ceiling(workspace=tmp_path)
    assert result.status == "ok"
    assert result.name == "state_scale_ceiling"


def test_check_state_scale_ceiling_zero_waves_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: an empty wave map is well under the ceiling."""
    _stub_state_with_wave_count(monkeypatch, 0)
    result = checks.check_state_scale_ceiling(workspace=tmp_path)
    assert result.status == "ok"
    assert "0 wave(s)" in (result.detail or "")


def test_check_state_scale_ceiling_just_below_threshold_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: one wave below the warn threshold stays ``ok``."""
    _stub_state_with_wave_count(monkeypatch, checks.STATE_WAVE_WARN_THRESHOLD - 1)
    result = checks.check_state_scale_ceiling(workspace=tmp_path)
    assert result.status == "ok"


def test_check_state_scale_ceiling_at_threshold_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: exactly at the warn threshold flips to the advisory warn."""
    _stub_state_with_wave_count(monkeypatch, checks.STATE_WAVE_WARN_THRESHOLD)
    result = checks.check_state_scale_ceiling(workspace=tmp_path)
    assert result.status == "warn"
    assert "shard" in (result.detail or "")


def test_check_state_scale_ceiling_above_ceiling_warns_not_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Above the ceiling stays ``warn`` — never ``fail`` (advisory only)."""
    _stub_state_with_wave_count(monkeypatch, checks.STATE_WAVE_SCALE_CEILING + 500)
    result = checks.check_state_scale_ceiling(workspace=tmp_path)
    assert result.status == "warn"
    assert result.status != "fail"


def test_state_wave_warn_threshold_derives_from_ceiling_and_fraction() -> None:
    """The materialised threshold is the ceiling times the warn fraction."""
    assert (
        int(checks.STATE_WAVE_SCALE_CEILING * checks.STATE_WAVE_WARN_FRACTION)
        == checks.STATE_WAVE_WARN_THRESHOLD
    )


def test_run_all_returns_full_check_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_all`` includes the state-scale-ceiling advisory (P29-I01-W04)."""
    from eawf.platform.install.instrument_probe import ProbeResult

    _stub_probe_ok(
        monkeypatch,
        [ProbeResult(name="git", kind="hard", status="ok", path="/x/git")],
    )
    monkeypatch.chdir(tmp_path)
    results = checks.run_all(workspace=tmp_path)
    assert len(results) == 7
    assert {r.name for r in results} == {
        "tools_available",
        "state_present",
        "config_resolves",
        "manifest_in_sync",
        "mcp_drift",
        "state_scale_ceiling",
        "render_output_roundtrip",
    }
