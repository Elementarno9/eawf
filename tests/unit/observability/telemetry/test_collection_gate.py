"""``telemetry.enabled`` gates collection at the producer, not only the display.

The session store's ``session_closed`` event exists only for the telemetry
projection, so with collection off a terminal session must leave no such row
behind while its human-timeline ``session.close`` row is still written.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from eawf.kernel.config import layered
from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus, StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.paths import store_path
from eawf.observability.telemetry.gate import telemetry_enabled
from eawf.runtime.session.store import (
    close_session,
    start_session,
    terminalize_session,
)
from eawf.surfaces.cli.errors import ValidationError

_OPENED = datetime(2026, 6, 3, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolate_global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the global config layer at a tmp file and clear the telemetry env leaf."""
    fake_global = tmp_path / "global-config.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    monkeypatch.delenv("EAWF_TELEMETRY__ENABLED", raising=False)
    return fake_global


def _write_yaml(path: Path, body: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"schema_version": "1.0", **body}), encoding="utf-8")


def _repo(tmp_path: Path, *, enabled: bool | None) -> Path:
    """Return a repo root whose repo layer sets ``telemetry.enabled`` (or nothing)."""
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    if enabled is not None:
        _write_yaml(repo / ".ea" / "config.yaml", {"telemetry": {"enabled": enabled}})
    return repo


def _make_state() -> State:
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": "repo",
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": "2026-06-03T00:00:00Z",
            "project": {
                "code": "QR",
                "slug": "quant",
                "title": "Quant",
                "domains": ["quant"],
                "default_branch": "main",
                "status": "active",
                "repo_urn": "urn:eawf:v1:repo:QR",
            },
            "current": {
                "project_code": "QR",
                "track_id": None,
                "phase_id": None,
                "iter_id": None,
                "active_wave_ids": [],
                "active_session_ids": [],
            },
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _event_types(event_path: Path) -> list[str]:
    if not event_path.is_file():
        return []
    return [
        json.loads(line)["payload"].get("event_type", "")
        for line in event_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _open(state: State, event_path: Path) -> str:
    started = start_session(
        state=state,
        events_path=event_path,
        role=AgentSessionRole.EXECUTOR,
        scope_id="P32-I01-W36",
        runtime="claude-code",
        now=_OPENED,
    )
    return started.session.id


# -- the gate itself -------------------------------------------------------


def test_gate_defaults_to_enabled_without_any_config(tmp_path: Path) -> None:
    assert telemetry_enabled(_repo(tmp_path, enabled=None)) is True


@pytest.mark.parametrize("enabled", [True, False])
def test_gate_follows_the_repo_layer(tmp_path: Path, enabled: bool) -> None:
    assert telemetry_enabled(_repo(tmp_path, enabled=enabled)) is enabled


def test_gate_follows_the_global_layer(tmp_path: Path, _isolate_global_config: Path) -> None:
    _write_yaml(_isolate_global_config, {"telemetry": {"enabled": False}})
    assert telemetry_enabled(_repo(tmp_path, enabled=None)) is False


def test_gate_repo_layer_overrides_the_global_layer(
    tmp_path: Path, _isolate_global_config: Path
) -> None:
    _write_yaml(_isolate_global_config, {"telemetry": {"enabled": False}})
    assert telemetry_enabled(_repo(tmp_path, enabled=True)) is True


def test_gate_on_a_root_with_no_ea_dir_reads_the_default(tmp_path: Path) -> None:
    assert telemetry_enabled(tmp_path / "absent") is True


def test_gate_raises_on_malformed_config(tmp_path: Path) -> None:
    repo = _repo(tmp_path, enabled=None)
    (repo / ".ea" / "config.yaml").write_text("telemetry: [1, 2\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="malformed YAML"):
        telemetry_enabled(repo)


# -- the session_closed producer -------------------------------------------


@pytest.mark.parametrize(("enabled", "expected"), [(True, 1), (False, 0)])
def test_close_session_writes_session_closed_only_when_enabled(
    tmp_path: Path, enabled: bool, expected: int
) -> None:
    repo = _repo(tmp_path, enabled=enabled)
    event_path = store_path(repo / ".ea" / "state.json", StoreKind.EVENT)
    state = _make_state()
    session_id = _open(state, event_path)

    close_session(
        state=state,
        events_path=event_path,
        session_id=session_id,
        status=AgentSessionStatus.CLOSED,
        now=_OPENED + timedelta(minutes=5),
    )

    types = _event_types(event_path)
    assert types.count("session_closed") == expected
    assert "session.close" in types


@pytest.mark.parametrize(("enabled", "expected"), [(True, 1), (False, 0)])
def test_terminalize_session_honours_the_gate(tmp_path: Path, enabled: bool, expected: int) -> None:
    repo = _repo(tmp_path, enabled=enabled)
    event_path = store_path(repo / ".ea" / "state.json", StoreKind.EVENT)
    state = _make_state()

    terminalize_session(
        state=state,
        events_path=event_path,
        session_id=_open(state, event_path),
        status=AgentSessionStatus.FAILED,
        now=_OPENED + timedelta(minutes=1),
    )

    assert _event_types(event_path).count("session_closed") == expected


def test_malformed_config_skips_the_event_but_still_closes(tmp_path: Path) -> None:
    repo = _repo(tmp_path, enabled=None)
    event_path = store_path(repo / ".ea" / "state.json", StoreKind.EVENT)
    state = _make_state()
    session_id = _open(state, event_path)
    (repo / ".ea" / "config.yaml").write_text("telemetry: [1, 2\n", encoding="utf-8")

    result = close_session(
        state=state,
        events_path=event_path,
        session_id=session_id,
        status=AgentSessionStatus.CLOSED,
        now=_OPENED + timedelta(minutes=5),
    )

    assert result.session.status is AgentSessionStatus.CLOSED
    assert "session_closed" not in _event_types(event_path)
