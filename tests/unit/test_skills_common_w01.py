"""Coverage-lift tests for :mod:`eawf.workflow.skills._common` (P27-I01-W01).

Covers the helper surface: ``_project_status`` mapping, the probe
fallback/raised paths in ``probe_skill_instruments``, ``emit_event``
append behaviour, ``_coerce_str_arg``, ``has_research_profile``, and
``env_or``. Probe internals are monkeypatched so the warn/hard-fail/raise
branches are deterministic without a real instrument inventory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.paths import store_path
from eawf.platform.install.instrument_probe import ProbeReport, ProbeResult
from eawf.workflow.skills import _common

# --- _project_status -----------------------------------------------------


def test_project_status_ok_maps_to_ok() -> None:
    assert _common._project_status("ok") == "ok"


def test_project_status_warn_maps_to_degraded() -> None:
    assert _common._project_status("warn") == "degraded"


def test_project_status_fail_maps_to_missing() -> None:
    assert _common._project_status("fail") == "missing"


# --- _probe_cache_path / _stub_report -----------------------------------


def test_probe_cache_path_is_sibling_of_state(tmp_path: Path) -> None:
    state = tmp_path / ".ea" / "state.json"
    assert _common._probe_cache_path(state) == tmp_path / ".ea" / "instrument-probe.json"


def test_stub_report_emits_warn_results_for_core() -> None:
    report = _common._stub_report(["core"])
    assert isinstance(report, ProbeReport)
    assert report.results, "core profile must yield at least one stub spec"
    assert all(r.status == "warn" for r in report.results)
    assert all("bypassed" in (r.detail or "") for r in report.results)


# --- probe_skill_instruments --------------------------------------------


def test_probe_skill_instruments_cache_parent_missing_uses_stub(tmp_path: Path) -> None:
    """When the cache parent does not exist the stub report drives degraded statuses."""
    missing_state = tmp_path / "nope" / "state.json"
    outcome = _common.probe_skill_instruments(profile_ids=["core"], state_path=missing_state)
    assert outcome.ok is True
    # Stub report statuses are ``warn`` -> projected to ``degraded``.
    assert outcome.instrument_probe
    assert all(status == "degraded" for status in outcome.instrument_probe.values())
    assert outcome.warnings


def test_probe_skill_instruments_probe_raises_returns_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A raising probe degrades to an ``ok=False`` outcome with a missing map."""
    state = tmp_path / ".ea" / "state.json"
    state.parent.mkdir(parents=True)

    def _boom(*_a: Any, **_k: Any) -> ProbeReport:
        raise RuntimeError("probe exploded")

    monkeypatch.setattr("eawf.workflow.skills._common.run_probe", _boom)
    outcome = _common.probe_skill_instruments(profile_ids=["core"], state_path=state)
    assert outcome.ok is False
    assert outcome.repair_commands == ["eawf doctor --reprobe"]
    assert outcome.instrument_probe
    assert all(status == "missing" for status in outcome.instrument_probe.values())
    assert any(w.code == "instrument_probe_failed" for w in outcome.warnings)


def test_probe_skill_instruments_warn_result_keeps_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A soft ``warn`` result flows through as a warning while ``ok`` stays True."""
    state = tmp_path / ".ea" / "state.json"
    state.parent.mkdir(parents=True)

    report = ProbeReport(
        probe_version=1,
        profile_ids=["core"],
        results=[ProbeResult(name="ripgrep", kind="soft", status="warn", detail="missing rg")],
    )
    monkeypatch.setattr("eawf.workflow.skills._common.run_probe", lambda *_a, **_k: report)
    outcome = _common.probe_skill_instruments(profile_ids=["core"], state_path=state)
    assert outcome.ok is True
    assert outcome.instrument_probe == {"ripgrep": "degraded"}
    assert any(w.code == "instrument_degraded" for w in outcome.warnings)
    assert "missing rg" in outcome.warnings[0].detail


def test_probe_skill_instruments_hard_fail_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A hard ``fail`` result short-circuits to ``ok=False`` with repair commands."""
    state = tmp_path / ".ea" / "state.json"
    state.parent.mkdir(parents=True)

    report = ProbeReport(
        probe_version=1,
        profile_ids=["core"],
        results=[ProbeResult(name="git", kind="hard", status="fail", detail="git absent")],
    )
    monkeypatch.setattr("eawf.workflow.skills._common.run_probe", lambda *_a, **_k: report)
    outcome = _common.probe_skill_instruments(profile_ids=["core"], state_path=state)
    assert outcome.ok is False
    assert outcome.instrument_probe == {"git": "missing"}
    assert any(w.code == "instrument_missing" for w in outcome.warnings)
    assert outcome.repair_commands
    assert "git" in outcome.repair_commands[0]


def test_probe_skill_instruments_resolves_state_path_when_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``state_path=None`` falls back to :func:`resolve_with_reason`."""
    state = tmp_path / ".ea" / "state.json"
    state.parent.mkdir(parents=True)
    monkeypatch.setattr(
        "eawf.workflow.skills._common.resolve_with_reason",
        lambda workspace: (state, "pwd_upward"),
    )
    report = ProbeReport(
        probe_version=1,
        profile_ids=["core"],
        results=[ProbeResult(name="git", kind="hard", status="ok")],
    )
    monkeypatch.setattr("eawf.workflow.skills._common.run_probe", lambda *_a, **_k: report)
    outcome = _common.probe_skill_instruments()
    assert outcome.ok is True
    assert outcome.instrument_probe == {"git": "ok"}


# --- emit_event ----------------------------------------------------------


def test_emit_event_appends_envelope_and_returns_id(tmp_path: Path) -> None:
    state = tmp_path / ".ea" / "state.json"
    (state.parent / "store").mkdir(parents=True)
    event_id = _common.emit_event(
        state_path=state,
        scope_id="P27-I01-W01",
        event_type="wave.claim",
        summary="claimed wave",
        payload={"extra": "value"},
    )
    assert event_id.startswith("EV-")
    events_file = store_path(state, StoreKind.EVENT)
    assert events_file.exists()
    content = events_file.read_text(encoding="utf-8")
    assert event_id in content
    assert "wave.claim" in content
    assert "value" in content


def test_emit_event_append_failure_reraises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / ".ea" / "state.json"

    def _boom(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("eawf.workflow.skills._common.append_envelope", _boom)
    with pytest.raises(OSError, match="disk full"):
        _common.emit_event(
            state_path=state,
            scope_id="P27-I01-W01",
            event_type="wave.claim",
            summary="claimed wave",
        )


# --- resolve_active_state_path ------------------------------------------


def test_resolve_active_state_path_returns_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / ".ea" / "state.json"
    monkeypatch.setattr(
        "eawf.workflow.skills._common.resolve_with_reason",
        lambda workspace: (state, "workspace_flag"),
    )
    assert _common.resolve_active_state_path(workspace=tmp_path) == state


# --- _coerce_str_arg -----------------------------------------------------


def test_coerce_str_arg_none_returns_default() -> None:
    assert _common._coerce_str_arg(None, "fallback") == "fallback"


def test_coerce_str_arg_int_coerces_to_str() -> None:
    assert _common._coerce_str_arg(42, "fallback") == "42"


def test_coerce_str_arg_passthrough_string() -> None:
    assert _common._coerce_str_arg("value", "fallback") == "value"


# --- has_research_profile ------------------------------------------------


def test_has_research_profile_true_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / ".ea" / "state.json"
    merged = {"profiles": {"enabled": ["core", "research"]}}
    monkeypatch.setattr(
        "eawf.kernel.config.layered.merge_config",
        lambda **_k: (merged, {}),
    )
    assert _common.has_research_profile(state) is True


def test_has_research_profile_false_when_not_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / ".ea" / "state.json"
    merged = {"profiles": {"enabled": ["core"]}}
    monkeypatch.setattr(
        "eawf.kernel.config.layered.merge_config",
        lambda **_k: (merged, {}),
    )
    assert _common.has_research_profile(state) is False


def test_has_research_profile_false_when_merge_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / ".ea" / "state.json"

    def _boom(**_k: Any) -> tuple[dict[str, Any], dict[str, str]]:
        raise RuntimeError("merge boom")

    monkeypatch.setattr("eawf.kernel.config.layered.merge_config", _boom)
    assert _common.has_research_profile(state) is False


def test_has_research_profile_false_when_profiles_not_dict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / ".ea" / "state.json"
    monkeypatch.setattr(
        "eawf.kernel.config.layered.merge_config",
        lambda **_k: ({"profiles": ["not", "a", "dict"]}, {}),
    )
    assert _common.has_research_profile(state) is False


def test_has_research_profile_false_when_enabled_not_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = tmp_path / ".ea" / "state.json"
    monkeypatch.setattr(
        "eawf.kernel.config.layered.merge_config",
        lambda **_k: ({"profiles": {"enabled": "core"}}, {}),
    )
    assert _common.has_research_profile(state) is False


# --- env_or --------------------------------------------------------------


def test_env_or_returns_first_non_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EA_TEST_A", raising=False)
    monkeypatch.setenv("EA_TEST_B", "second")
    assert _common.env_or("default", "EA_TEST_A", "EA_TEST_B") == "second"


def test_env_or_returns_default_when_all_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EA_TEST_A", raising=False)
    monkeypatch.delenv("EA_TEST_B", raising=False)
    assert _common.env_or("default", "EA_TEST_A", "EA_TEST_B") == "default"


def test_env_or_skips_empty_string_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TEST_A", "")
    monkeypatch.setenv("EA_TEST_B", "value")
    assert _common.env_or("default", "EA_TEST_A", "EA_TEST_B") == "value"
