"""CLI tests for Wave integration and dependency-barrier operator surfaces."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.state.enums import DependencyStage
from eawf.kernel.state.models import (
    State,
    WaveDependencyBarrier,
    wave_dependency_key,
)
from eawf.surfaces.cli.app import app
from eawf.workflow.lifecycle.integration import create_wave_integration
from tests.daemon.test_close_lock_split import _WAVE, _state_payload

_DOWNSTREAM = "P30-I23-W99"
_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40
_SHA_D = "d" * 40

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    state = State.model_validate(_state_payload())
    state.waves[_DOWNSTREAM].deps = [_WAVE]
    state.waves[_WAVE].blocks = [_DOWNSTREAM]
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("EA_STATE", str(state_path))
    yield tmp_path


def _write_state(workspace: Path, state: State) -> None:
    (workspace / ".ea" / "state.json").write_text(
        state.model_dump_json(),
        encoding="utf-8",
    )


def _read_state(workspace: Path) -> State:
    return State.model_validate_json((workspace / ".ea" / "state.json").read_bytes())


def test_wave_integration_show_lists_active_generation(workspace: Path) -> None:
    state = _read_state(workspace)
    integration = create_wave_integration(
        state,
        wave_id=_WAVE,
        base_sha=_SHA_A,
        candidate_sha=_SHA_B,
        integrated_sha=_SHA_C,
        tree_sha=_SHA_D,
        diff_digest="diff-digest",
        spec_digest="spec-digest",
    )
    _write_state(workspace, state)

    result = runner.invoke(
        app,
        ["--json", "wave", "integration", "show", _WAVE],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["active_integration_id"] == integration.id
    assert payload["integrations"][0]["integrated_sha"] == _SHA_C


def test_wave_integration_adopt_calls_daemon_rpc(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeClient:
        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            calls.append((method, params))
            return {
                "wave_id": _WAVE,
                "integration": {
                    "id": "integration-adopted",
                    "integrated_sha": _SHA_C,
                },
                "ancestry_verified": True,
                "tree_verified": True,
            }

    monkeypatch.setattr(
        "eawf.surfaces.cli._dispatch.escalate_mutation",
        lambda *args, **kwargs: 1,
    )
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", FakeClient)

    result = runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(workspace),
            "wave",
            "integration",
            "adopt",
            _WAVE,
            "--commit",
            "HEAD~1",
            "--reason",
            "record exact historical integration",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "integration.adopt",
            {
                "repo_root": str(workspace),
                "wave_id": _WAVE,
                "commit": "HEAD~1",
                "reason": "record exact historical integration",
            },
        )
    ]


def test_wave_graph_renders_legacy_closed_closed_barrier(workspace: Path) -> None:
    result = runner.invoke(
        app,
        ["--json", "wave", "graph", "--iter", "P30-I23"],
    )

    assert result.exit_code == 0, result.output
    payload = orjson.loads(result.stdout)
    downstream = next(row for row in payload["waves"] if row["id"] == _DOWNSTREAM)
    assert downstream["dependency_barriers"] == [
        {
            "dep_wave_id": _WAVE,
            "start_after": DependencyStage.CLOSED.value,
            "land_after": DependencyStage.CLOSED.value,
            "explicit": False,
            "reason": None,
        }
    ]


def test_roadmap_show_renders_explicit_barrier(workspace: Path) -> None:
    state = _read_state(workspace)
    key = wave_dependency_key(_DOWNSTREAM, _WAVE)
    state.wave_dependency_barriers[key] = WaveDependencyBarrier(
        wave_id=_DOWNSTREAM,
        dep_wave_id=_WAVE,
        start_after=DependencyStage.INTEGRATED,
        land_after=DependencyStage.VERIFIED,
        reason="consume immutable code, then wait for proof",
    )
    _write_state(workspace, state)

    result = runner.invoke(app, ["--json", "roadmap", "show", "--phase", "P30"])

    assert result.exit_code == 0, result.output
    payload = orjson.loads(result.stdout)
    wave = next(
        row for row in payload["phases"][0]["iters"][0]["waves"] if row["id"] == _DOWNSTREAM
    )
    assert wave["dependency_barriers"][0]["start_after"] == "integrated"
    assert wave["dependency_barriers"][0]["land_after"] == "verified"
    assert wave["dependency_barriers"][0]["explicit"] is True


def test_roadmap_revise_set_dep_barrier_calls_daemon(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeClient:
        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            calls.append((method, params))
            return {
                "wave_id": _DOWNSTREAM,
                "dep_wave_id": _WAVE,
                "barrier_key": wave_dependency_key(_DOWNSTREAM, _WAVE),
                "barrier": {
                    "wave_id": _DOWNSTREAM,
                    "dep_wave_id": _WAVE,
                    "start_after": "integrated",
                    "land_after": "verified",
                    "reason": "execute after integration and land after proof",
                },
            }

    monkeypatch.setattr(
        "eawf.surfaces.cli._dispatch.escalate_mutation",
        lambda *args, **kwargs: 1,
    )
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", FakeClient)

    result = runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(workspace),
            "roadmap",
            "revise",
            "P30",
            "--iter",
            "P30-I23",
            "--set-dep-barrier",
            "W99:W09:integrated:verified",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][0] == "dependency_barrier.set"
    assert calls[0][1]["wave_id"] == _DOWNSTREAM
    assert calls[0][1]["dep_wave_id"] == _WAVE
    assert calls[0][1]["start_after"] == "integrated"
    assert calls[0][1]["land_after"] == "verified"
    assert calls[0][1]["repo_root"] == str(workspace)
    assert calls[0][1]["reason"] == ("explicit dependency barrier authored via roadmap revise")
