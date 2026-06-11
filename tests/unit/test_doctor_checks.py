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


# ---- P30-I16-W22: anchor resolution (pwd-upward) ---------------------------


def test_resolve_anchor_returns_explicit_workspace(tmp_path: Path) -> None:
    """An explicit ``-w`` workspace is returned verbatim (no upward walk)."""
    assert checks._resolve_anchor(tmp_path) == tmp_path


def test_resolve_anchor_walks_pwd_upward_to_dot_ea(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``workspace=None`` walks UP from pwd to the nearest ``.ea/`` ancestor."""
    (tmp_path / ".ea").mkdir()
    nested = tmp_path / "src" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert checks._resolve_anchor(None) == tmp_path.resolve()


def test_resolve_anchor_none_when_no_dot_ea_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree with no ``.ea/`` ancestor resolves to ``None``."""
    monkeypatch.chdir(tmp_path)
    assert checks._resolve_anchor(None) is None


def test_check_manifest_in_sync_pwd_upward_when_workspace_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain doctor (``workspace=None``) resolves the manifest pwd-upward."""
    (tmp_path / ".ea").mkdir()
    monkeypatch.chdir(tmp_path)
    # No manifest file yet -> resolves the anchor and reports ``ok`` (nothing
    # to verify), NOT the old "no workspace anchor" warn.
    result = checks.check_manifest_in_sync(workspace=None)
    assert result.status == "ok"
    assert "nothing to verify" in (result.detail or "")


def test_check_manifest_in_sync_warns_only_without_any_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``warn`` (no anchor) survives only when even the upward walk finds none."""
    monkeypatch.chdir(tmp_path)
    result = checks.check_manifest_in_sync(workspace=None)
    assert result.status == "warn"
    assert "no workspace anchor" in (result.detail or "")


# ---- P30-I16-W22: manifest install-mode awareness --------------------------


def test_is_plugin_owned_discriminates_on_prefix_and_generator() -> None:
    """Only ``plugin.`` region id + ``eawf-plugin-`` generator counts as plugin."""
    from eawf.surfaces.render.manifest import ManifestEntry

    plugin = ManifestEntry(
        target=".claude/agents/auditor.md",
        region_id="plugin.claude.agent.auditor",
        version="1.0",
        hash="0" * 16,
        generator="eawf-plugin-claude",
        generated_at="2026-05-27T00:00:00+00:00",
    )
    region = ManifestEntry(
        target="AGENTS.md",
        region_id="non-negotiable-rules",
        version="1.0",
        hash="0" * 16,
        generator="eawf-sync",
        generated_at="2026-05-27T00:00:00+00:00",
    )
    assert checks._is_plugin_owned(plugin) is True
    assert checks._is_plugin_owned(region) is False


def test_check_manifest_in_sync_skips_plugin_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whole-file plugin renders are satisfied by cache, not the region detector.

    A manifest with only ``plugin.*`` entries pointing at files that carry NO
    region markers must NOT report ``missing`` -- they are satisfied by the
    plugin cache. The check stays ``ok`` and notes the cache-served count.
    """
    from eawf.surfaces.render.manifest import Manifest, ManifestEntry
    from eawf.surfaces.render.manifest import save_atomic as save_manifest_atomic

    manifest = Manifest(
        version=1,
        generated={
            ".claude/agents/auditor.md::plugin.claude.agent.auditor": ManifestEntry(
                target=".claude/agents/auditor.md",
                region_id="plugin.claude.agent.auditor",
                version="1.0",
                hash="0" * 16,
                generator="eawf-plugin-claude",
                generated_at="2026-05-27T00:00:00+00:00",
            ),
        },
    )
    manifest_path = tmp_path / ".ea" / "indexes" / "generated.json"
    save_manifest_atomic(manifest_path, manifest)
    # The .claude file does NOT exist -- under the old region detector it would
    # report ``missing``; install-mode awareness keeps it ``ok``.
    result = checks.check_manifest_in_sync(workspace=tmp_path)
    assert result.status == "ok"
    assert "plugin region(s) from cache" in (result.detail or "")


# ---- P30-I16-W22: probe-cache stray-write hygiene --------------------------


def test_resolve_probe_cache_path_is_per_user_not_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe cache lives in ``~/.eawf/cache`` -- never under the anchor's .ea/."""
    monkeypatch.delenv("EA_INSTRUMENT_PROBE", raising=False)
    fake_home = tmp_path / "home"
    monkeypatch.setattr("eawf.observability.doctor.checks.Path.home", lambda: fake_home)
    path = checks._resolve_probe_cache_path(tmp_path)
    assert path == fake_home / ".eawf" / "cache" / "instrument-probe.json"
    assert ".ea" not in path.parts


def test_resolve_probe_cache_path_honours_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    override = tmp_path / "scratch" / "probe.json"
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(override))
    assert checks._resolve_probe_cache_path(tmp_path) == override


def test_run_all_does_not_write_probe_into_anchor_dot_ea(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A doctor run never litters ``instrument-probe.json`` into the anchor's .ea/.

    Exercises the REAL probe (not the ``_stub_probe_ok`` fake) so the actual
    cache write lands -- then asserts it landed in the per-user home, NOT the
    anchor's ``.ea/``.
    """
    from eawf.platform.install import instrument_probe as _ip

    monkeypatch.delenv("EA_INSTRUMENT_PROBE", raising=False)
    # Every tool resolves so the real probe writes a green cache without a
    # version shell-out reaching the host. Force the cheap ``which`` probe so
    # no ``--version`` subprocess runs.
    monkeypatch.setattr(
        "eawf.platform.install.instrument_probe.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )
    monkeypatch.setattr(
        _ip,
        "INSTRUMENT_REQUIREMENTS",
        {"core": [_ip.InstrumentSpec(name="git", kind="hard", probe="which")]},
    )
    fake_home = tmp_path / "home"
    monkeypatch.setattr("eawf.observability.doctor.checks.Path.home", lambda: fake_home)
    (tmp_path / ".ea").mkdir()
    monkeypatch.chdir(tmp_path)
    checks.run_all(workspace=None)
    assert not (tmp_path / ".ea" / "instrument-probe.json").exists()
    # The cache landed in the per-user home instead.
    assert (fake_home / ".eawf" / "cache" / "instrument-probe.json").exists()
