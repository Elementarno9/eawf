"""``config profile enable`` and ``doctor`` resolve workspace overlay profiles.

Before this fix, ``eawf.kernel.config.profile.enable_profile`` checked a
profile id against ``KNOWN_PROFILES`` -- a module-level dict built once,
at import time, from the built-in profile bundle only (no
``<workspace>/.ea/profiles/`` overlay). A profile id defined solely by a
workspace overlay was refused by ``config profile enable`` and reported
"unknown" by ``eawf doctor``, even though the same id already resolved
fine through the workspace-overlay-aware registry the AGENTS.md renderer
uses (``eawf.platform.profiles.loader.list_profiles``/``load_profile``,
backed by ``eawf.platform.profiles.discovery.discover_profile`` --
workspace > user > built-in precedence). This suite pins both surfaces to
that same registry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.observability.doctor.checks import check_config_resolves
from eawf.platform.profiles import discovery
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clear_profile_cache():
    discovery._clear_cache_for_tests()
    yield
    discovery._clear_cache_for_tests()


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the user profile overlay (``~/.eawf/profiles``) + global config."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _write_overlay(ws: Path, profile_id: str, body: str) -> Path:
    target = ws / ".ea" / "profiles" / f"{profile_id}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


# --- config profile enable ---------------------------------------------------


def test_enable_accepts_workspace_overlay_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_home: Path
) -> None:
    """A profile id defined only under ``.ea/profiles/`` is accepted."""
    ws = _workspace(tmp_path)
    _write_overlay(ws, "overlay-test", "name: overlay-test\n")
    monkeypatch.chdir(ws)

    result = runner.invoke(
        app,
        ["--json", "--workspace", str(ws), "config", "profile", "enable", "overlay-test"],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["profile"] == "overlay-test"
    written = (ws / ".ea" / "config.yaml").read_text(encoding="utf-8")
    assert "overlay-test" in written


def test_enable_unknown_id_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_home: Path
) -> None:
    """An id present in no layer (workspace, user, or built-in) still fails."""
    ws = _workspace(tmp_path)
    monkeypatch.chdir(ws)

    result = runner.invoke(
        app,
        ["--workspace", str(ws), "config", "profile", "enable", "not-a-real-profile"],
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert "unknown profile" in result.output


def test_enable_workspace_overlay_shadows_builtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_home: Path
) -> None:
    """A workspace overlay sharing a built-in's id wins over the built-in body.

    This is the same workspace > user > built-in precedence
    ``eawf.platform.profiles.discovery.discover_profile`` applies for every
    other overlay-aware caller (the AGENTS.md renderer included), so
    ``enable_profile`` must not special-case a different order. The
    built-in ``python`` profile declares no required state fields; the
    overlay here declares one, so a non-empty materialised list proves the
    overlay body -- not the built-in one -- was resolved.
    """
    ws = _workspace(tmp_path)
    _write_overlay(
        ws,
        "python",
        "name: python\nstate_extensions:\n  fields_required: [overlay_marker]\n",
    )
    (ws / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(ws)

    result = runner.invoke(
        app,
        ["--json", "--workspace", str(ws), "config", "profile", "enable", "python"],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["state_keys_materialised"] == ["overlay_marker"]


def test_enable_malformed_overlay_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_home: Path
) -> None:
    """A workspace overlay whose id matches but whose body fails schema fails clearly."""
    ws = _workspace(tmp_path)
    _write_overlay(ws, "broken", "name: broken\nbogus_field: 1\n")
    monkeypatch.chdir(ws)

    result = runner.invoke(
        app,
        ["--workspace", str(ws), "config", "profile", "enable", "broken"],
    )
    assert result.exit_code == exit_codes.VALIDATION_ERROR
    assert "broken" in result.output
    # No partial write: the schema rejection happens before the layer file
    # (or state.json) is ever touched.
    assert not (ws / ".ea" / "config.yaml").exists()


# --- doctor -------------------------------------------------------------


def test_doctor_lists_overlay_profile_with_source(tmp_path: Path, fake_home: Path) -> None:
    """``config_resolves`` reports an enabled overlay profile as ok, with its source."""
    ws = _workspace(tmp_path)
    _write_overlay(ws, "overlay-test", "name: overlay-test\n")
    (ws / ".ea" / "config.yaml").write_text(
        "profiles:\n  enabled: [overlay-test]\n", encoding="utf-8"
    )

    result = check_config_resolves(workspace=ws)
    assert result.status == "ok"
    assert "overlay-test=workspace" in result.detail


def test_doctor_still_refuses_unknown_enabled_profile(tmp_path: Path, fake_home: Path) -> None:
    """An enabled id absent from every layer is still flagged unknown."""
    ws = _workspace(tmp_path)
    (ws / ".ea").mkdir(parents=True, exist_ok=True)
    (ws / ".ea" / "config.yaml").write_text(
        "profiles:\n  enabled: [not-a-real-profile]\n", encoding="utf-8"
    )

    result = check_config_resolves(workspace=ws)
    assert result.status == "warn"
    assert "not-a-real-profile" in result.detail
