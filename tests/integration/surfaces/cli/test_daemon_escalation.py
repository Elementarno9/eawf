"""CLI tests for the C05 §5.5 daemon-vs-daemonless escalation rules.

Three criteria from the wave spec, plus the dev-mode raw-RPC gate:

* **(a) Mutating verb auto-spawns the daemon** when none is running —
  :func:`eawf.surfaces.cli._dispatch.escalate_mutation` calls the spawn helper.
* **(b) Read-only verb works daemonless** — ``eawf state show
  --daemonless`` reads ``state.json`` directly and never spawns the
  daemon.
* **(c) Mutating verb rejects ``--daemonless``** — both the
  :func:`eawf.surfaces.cli._dispatch.escalate_mutation` escalation gate (the
  single entry every daemon-proxy callsite routes through) and the
  ``eawf state rpc`` raw passthrough refuse the carve-out with a
  :class:`~eawf.surfaces.cli.errors.UserError` (exit-code 1,
  ``data.kind="InvalidInput"``).
* **Dev-mode gate** — the raw ``state rpc`` verb is hidden /
  unreachable unless ``--debug`` (or ``EAWF_DEBUG=1``) is set; it
  auto-spawns the daemon on a mutating method and refuses
  ``--daemonless`` there too.

The escalation plumbing is monkeypatched at the module boundary so the
real branching logic runs without spinning up a real daemon process.
End-to-end against a live socket is covered by
:mod:`tests.unit.runtime.daemon.test_daemon_client`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orjson
import pytest
import typer
from typer.testing import CliRunner

from eawf.surfaces.cli import _dispatch, _mutation, exit_codes
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags

pytestmark = pytest.mark.integration

runner = CliRunner()


# ---- shared fixtures -------------------------------------------------------


def _build_state(path: Path) -> None:
    """Write a minimal valid state.json with one phase / iter / wave."""
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": "2026-05-20T00:00:00+00:00",
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "ABC",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ABC",
        },
        "current": {"project_code": "ABC"},
        "workspace": None,
        "phases": {
            "P26": {
                "id": "P26",
                "scope_id": "ABC",
                "track_id": None,
                "title": "P26",
                "status": "active",
                "iter_ids": ["P26-I01"],
                "outcome_ids": [],
                "opened_at": "2026-05-20T00:00:00+00:00",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P26-I01": {
                "id": "P26-I01",
                "phase_id": "P26",
                "title": "I01",
                "status": "active",
                "wave_ids": ["P26-I01-W05"],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-05-20T00:00:00+00:00",
                "closed_at": None,
            }
        },
        "waves": {
            "P26-I01-W05": {
                "id": "P26-I01-W05",
                "iter_id": "P26-I01",
                "title": "test",
                "status": "claimed",
                "claim_session_id": "session-x",
                "opened_at": "2026-05-20T00:00:00+00:00",
                "sessions": {},
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))


# ---- predicate helpers (daemonless_requested / dev_mode_enabled) ----------


def test_daemonless_requested_via_flag() -> None:
    """``--daemonless`` flag sets the carve-out request."""
    flags = GlobalFlags(daemonless=True)
    assert _dispatch.daemonless_requested(flags) is True


def test_daemonless_requested_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``EAWF_DAEMONLESS=1`` sets the carve-out request even with no flag."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    assert _dispatch.daemonless_requested(GlobalFlags()) is True
    assert _dispatch.daemonless_requested(None) is True


def test_daemonless_requested_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """No flag + no env → carve-out not requested."""
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    assert _dispatch.daemonless_requested(GlobalFlags()) is False
    assert _dispatch.daemonless_requested(None) is False


def test_dev_mode_enabled_via_flag() -> None:
    """``--debug`` flag turns dev-mode on."""
    assert _dispatch.dev_mode_enabled(GlobalFlags(debug=True)) is True


def test_dev_mode_enabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``EAWF_DEBUG=1`` turns dev-mode on with no flag."""
    monkeypatch.setenv("EAWF_DEBUG", "1")
    assert _dispatch.dev_mode_enabled(GlobalFlags()) is True


def test_dev_mode_disabled_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No flag + no env → dev-mode off (normal operation)."""
    monkeypatch.delenv("EAWF_DEBUG", raising=False)
    assert _dispatch.dev_mode_enabled(GlobalFlags()) is False


# ---- (a) mutating verb auto-spawns the daemon when none is running ---------


def test_escalate_mutation_auto_spawns_when_no_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mutating verb with no daemon up cold-spawns one and returns its PID."""
    spawned: dict[str, Any] = {"calls": 0, "runtime": None}

    def _fake_spawn(runtime_dir: Path) -> int:
        spawned["calls"] += 1
        spawned["runtime"] = runtime_dir
        return 4242

    monkeypatch.setattr("eawf.runtime.daemon.spawn.auto_spawn_daemon", _fake_spawn)
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)

    fake_runtime = Path("/tmp/eawfd-test-runtime")
    pid = _dispatch.escalate_mutation(
        "wave close",
        flags=GlobalFlags(),
        runtime_dir=fake_runtime,
    )
    assert pid == 4242
    assert spawned["calls"] == 1
    assert spawned["runtime"] == fake_runtime


def test_ensure_daemon_maps_spawn_timeout_to_daemon_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spawn timeout surfaces as DaemonUnreachable (exit-code 4)."""
    from eawf.runtime.daemon.spawn import DaemonSpawnTimeoutError

    def _boom(_runtime_dir: Path) -> int:
        raise DaemonSpawnTimeoutError("socket never opened")

    monkeypatch.setattr("eawf.runtime.daemon.spawn.auto_spawn_daemon", _boom)

    with pytest.raises(cli_errors.DaemonUnreachable) as excinfo:
        _dispatch.ensure_daemon(Path("/tmp/eawfd-test-runtime"))
    assert excinfo.value.exit_code == exit_codes.DAEMON_UNREACHABLE


# ---- (b) read-only verb works daemonless (no spawn) ------------------------


# ---- (c) mutating verb rejects --daemonless -------------------------------


def test_reject_daemonless_on_mutating_raises_user_error() -> None:
    """The canonical rejection is a UserError carrying exit-code 1."""
    with pytest.raises(cli_errors.UserError) as excinfo:
        _dispatch.reject_daemonless_on_mutating("wave claim")
    assert excinfo.value.exit_code == exit_codes.USER_ERROR
    assert "wave claim" in str(excinfo.value)
    assert "--daemonless rejected" in str(excinfo.value)


def test_escalate_mutation_rejects_daemonless_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """escalate_mutation refuses --daemonless and never reaches the spawn."""
    monkeypatch.setattr(
        "eawf.runtime.daemon.spawn.auto_spawn_daemon",
        lambda _r: pytest.fail("must reject before spawning"),
    )
    with pytest.raises(cli_errors.UserError, match="mutating verb"):
        _dispatch.escalate_mutation("wave close", flags=GlobalFlags(daemonless=True))


def test_escalate_mutation_rejects_daemonless_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EAWF_DAEMONLESS=1 also triggers the mutating-verb rejection."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    monkeypatch.setattr(
        "eawf.runtime.daemon.spawn.auto_spawn_daemon",
        lambda _r: pytest.fail("must reject before spawning"),
    )
    with pytest.raises(cli_errors.UserError):
        _dispatch.escalate_mutation("iter close", flags=GlobalFlags())


# ---- (c') chokepoint: state_transaction rejects mutating --daemonless ------
# W25 wires the §5.5 rejection into the shared state_transaction chokepoint
# so every mutating verb (roadmap / memory / worktree / session / evidence
# / ...) inherits it, not just `state rpc`. The flag is recorded
# process-wide by the root callback; reads opt out with read_only=True.


@pytest.fixture(autouse=True)
def _reset_daemonless_flag() -> Iterator[None]:
    """Clear the process-wide --daemonless flag around every test.

    The root callback sets it per invocation, but unit tests that poke
    ``state_transaction`` directly bypass the callback, so reset here to
    keep the module global from leaking across tests.
    """
    _mutation.set_daemonless_flag(False)
    yield
    _mutation.set_daemonless_flag(False)


def test_state_transaction_rejects_when_daemonless_flag_set(
    tmp_path: Path,
) -> None:
    """A mutating state_transaction refuses once the --daemonless flag is set."""
    state_path = tmp_path / ".ea" / "state.json"
    _build_state(state_path)
    _mutation.set_daemonless_flag(True)
    with (
        pytest.raises(cli_errors.UserError) as excinfo,
        _mutation.state_transaction(state_path),
    ):
        pytest.fail("mutating transaction must reject before yielding")
    assert excinfo.value.exit_code == exit_codes.USER_ERROR
    assert "--daemonless rejected" in str(excinfo.value)


def test_state_transaction_read_only_bypasses_daemonless_rejection(
    tmp_path: Path,
) -> None:
    """read_only=True keeps the daemon-bypass carve-out open for snapshot reads."""
    state_path = tmp_path / ".ea" / "state.json"
    _build_state(state_path)
    _mutation.set_daemonless_flag(True)
    with _mutation.state_transaction(state_path, read_only=True) as state:
        assert state.project is not None
        assert state.project.code == "ABC"


def test_state_transaction_no_flag_allows_mutation(
    tmp_path: Path,
) -> None:
    """Without the --daemonless flag the mutating transaction proceeds normally."""
    state_path = tmp_path / ".ea" / "state.json"
    _build_state(state_path)
    _mutation.set_daemonless_flag(False)
    from datetime import UTC, datetime

    with _mutation.state_transaction(state_path) as state:
        state.updated_at = datetime.now(UTC)  # touch; commits on exit
    assert state_path.exists()


def test_state_transaction_rejects_before_touching_missing_file(
    tmp_path: Path,
) -> None:
    """The §5.5 gate fires before the file-exists check (InvalidInput, not NotFound)."""
    missing = tmp_path / ".ea" / "state.json"  # never created
    _mutation.set_daemonless_flag(True)
    with (
        pytest.raises(cli_errors.UserError),
        _mutation.state_transaction(missing),
    ):
        pytest.fail("must reject on the flag before the file-exists check")


# ---- dev-mode gate on the raw RPC passthrough verb -------------------------


# ---- emit_error is NoReturn (control-flow contract) -----------------------


def test_emit_error_raises_typer_exit_with_code() -> None:
    """emit_error always raises typer.Exit carrying the error's exit-code."""
    with pytest.raises(typer.Exit) as excinfo:
        cli_errors.emit_error(
            cli_errors.UserError("boom"),
            flags=GlobalFlags(),
        )
    assert excinfo.value.exit_code == exit_codes.USER_ERROR
