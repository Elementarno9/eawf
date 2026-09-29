"""SURF-150: the concurrency plan is written into each runtime's fan-out keys.

A runtime that exposes fan-out configuration gets the plan written into
it by the plugin installer, and a configuration that leaves a plan key
unwritten fails the plugin doctor's preflight. A runtime that exposes no
key records the absence. The coordinator's own reasoning effort is read
and held; only the fan-out effort is stepped down.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from eawf.kernel.economics.governor import DEFAULT_GOVERNOR, InFlightGovernor
from eawf.platform.install.managed_block import ManagedBlockError
from eawf.runtime.harness.fan_out import (
    FAN_OUT_DEPTH,
    HOST_FAN_OUT,
    HostFanOutKeys,
    fan_out_values,
    step_down_effort,
    unwritten_plan_keys,
)
from eawf.runtime.runtimes.claude.plugin_doctor import doctor_plugin as claude_doctor
from eawf.runtime.runtimes.claude.plugin_install import install_plugin as claude_install
from eawf.runtime.runtimes.codex.plugin_doctor import doctor_plugin as codex_doctor
from eawf.runtime.runtimes.codex.plugin_install import install_plugin as codex_install

pytestmark = pytest.mark.integration


def _codex_config(root: Path, payload: str) -> Path:
    config = root / ".codex" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(payload, encoding="utf-8")
    return config


def test_surf_150_codex_install_writes_the_plan_into_the_agents_keys(tmp_path: Path) -> None:
    codex_install(tmp_path, home=tmp_path / "home")
    document = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert document["agents"] == {
        "max_concurrent_threads_per_session": DEFAULT_GOVERNOR.max_concurrent_runs,
        "max_depth": FAN_OUT_DEPTH,
    }


def test_surf_150_codex_threads_follow_the_configured_governor(tmp_path: Path) -> None:
    (tmp_path / ".ea").mkdir()
    (tmp_path / ".ea" / "config.yaml").write_text(
        "economics:\n  governor:\n    max_concurrent_runs: 3\n"
        "    max_in_flight_tokens: 1000000\n    admission: queue\n",
        encoding="utf-8",
    )
    codex_install(tmp_path, home=tmp_path / "home")
    document = tomllib.loads((tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8"))
    assert document["agents"]["max_concurrent_threads_per_session"] == 3


def test_surf_150_codex_steps_down_fan_out_effort_and_holds_the_coordinator(
    tmp_path: Path,
) -> None:
    config = _codex_config(tmp_path, 'model_reasoning_effort = "high"\n')
    codex_install(tmp_path, home=tmp_path / "home")
    document = tomllib.loads(config.read_text(encoding="utf-8"))
    assert document["model_reasoning_effort"] == "high"
    assert document["agents"]["default_subagent_reasoning_effort"] == "medium"


def test_surf_150_codex_refuses_an_operator_owned_agents_table(tmp_path: Path) -> None:
    config = _codex_config(tmp_path, "[agents]\nmax_depth = 3\n")
    with pytest.raises(ManagedBlockError, match="agents"):
        codex_install(tmp_path, home=tmp_path / "home")
    assert config.read_text(encoding="utf-8") == "[agents]\nmax_depth = 3\n"


def test_surf_150_codex_keeps_operator_agent_role_subtables(tmp_path: Path) -> None:
    config = _codex_config(tmp_path, '[agents.reviewer]\ndescription = "reads"\n')
    codex_install(tmp_path, home=tmp_path / "home")
    document = tomllib.loads(config.read_text(encoding="utf-8"))
    assert document["agents"]["reviewer"] == {"description": "reads"}
    assert document["agents"]["max_depth"] == FAN_OUT_DEPTH


def test_surf_150_codex_refuses_an_unknown_coordinator_effort(tmp_path: Path) -> None:
    _codex_config(tmp_path, 'model_reasoning_effort = "extreme"\n')
    with pytest.raises(ManagedBlockError, match="model_reasoning_effort"):
        codex_install(tmp_path, home=tmp_path / "home")


def test_surf_150_codex_preflight_fails_on_unwritten_fan_out_keys(tmp_path: Path) -> None:
    codex_install(tmp_path, home=tmp_path / "home")
    assert "plugin.codex.fan_out" not in {
        entry.region_id for entry in codex_doctor(tmp_path).drifted
    }
    _codex_config(tmp_path, "[plugins.eawf]\nenabled = true\n")
    report = codex_doctor(tmp_path)
    assert "plugin.codex.fan_out" in {entry.region_id for entry in report.drifted}
    assert not report.clean


def test_surf_150_claude_install_writes_the_plan_into_env(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"env": {"OPERATOR_KEY": "kept"}}), encoding="utf-8")
    claude_install(tmp_path)
    env = json.loads(settings.read_text(encoding="utf-8"))["env"]
    assert env == {
        "OPERATOR_KEY": "kept",
        "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": str(DEFAULT_GOVERNOR.max_concurrent_runs),
        "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": str(FAN_OUT_DEPTH),
    }


def test_surf_150_claude_preflight_fails_on_unwritten_fan_out_keys(tmp_path: Path) -> None:
    claude_install(tmp_path)
    assert "plugin.claude.fan_out" not in {
        entry.region_id for entry in claude_doctor(tmp_path).drifted
    }
    settings = tmp_path / ".claude" / "settings.json"
    document = json.loads(settings.read_text(encoding="utf-8"))
    del document["env"]["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"]
    settings.write_text(json.dumps(document), encoding="utf-8")
    assert "plugin.claude.fan_out" in {entry.region_id for entry in claude_doctor(tmp_path).drifted}


def test_surf_150_runtime_without_keys_records_the_absence() -> None:
    record = HOST_FAN_OUT["opencode"]
    assert record.keys == {}
    assert record.absent_reason
    assert fan_out_values("opencode", governor=DEFAULT_GOVERNOR) == {}
    assert unwritten_plan_keys("opencode", {}) == ()


def test_surf_150_record_must_name_keys_or_the_reason_for_none() -> None:
    with pytest.raises(ValueError, match="not both"):
        HostFanOutKeys(runtime="claude", keys={}, source="none")
    with pytest.raises(ValueError, match="not both"):
        HostFanOutKeys(
            runtime="claude", keys={"max_depth": "env.X"}, source="binary", absent_reason="none"
        )


def test_surf_150_unwritten_keys_are_named_in_plan_order() -> None:
    assert unwritten_plan_keys("codex", {}) == (
        "agents.max_concurrent_threads_per_session",
        "agents.max_depth",
    )
    assert unwritten_plan_keys("codex", {"agents": {"max_depth": 1}}) == (
        "agents.max_concurrent_threads_per_session",
    )
    assert unwritten_plan_keys("codex", {"agents": "not a table"}) == (
        "agents.max_concurrent_threads_per_session",
        "agents.max_depth",
    )


@pytest.mark.parametrize(
    ("effort", "stepped"),
    [("xhigh", "high"), ("high", "medium"), ("low", "minimal"), ("minimal", "minimal")],
)
def test_surf_150_effort_steps_down_one_rung(effort: str, stepped: str) -> None:
    assert step_down_effort(effort) == stepped


def test_surf_150_unknown_effort_is_refused() -> None:
    with pytest.raises(ValueError, match="reasoning effort"):
        step_down_effort("")


def test_surf_150_fan_out_values_carry_the_governor_ceiling() -> None:
    governor = InFlightGovernor(max_concurrent_runs=1, max_in_flight_tokens=10, admission="deny")
    assert fan_out_values("codex", governor=governor, coordinator_effort="medium") == {
        "agents.max_concurrent_threads_per_session": 1,
        "agents.max_depth": FAN_OUT_DEPTH,
        "agents.default_subagent_reasoning_effort": "low",
    }
    assert fan_out_values("claude", governor=governor, coordinator_effort="medium") == {
        "env.CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": 1,
        "env.CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": FAN_OUT_DEPTH,
    }
