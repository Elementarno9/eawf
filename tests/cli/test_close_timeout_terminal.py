"""Close-RPC timeout is terminal; transport fallback stamps ``daemon-fallback``.

P30-I23-W03 splits the bespoke ``_wave_close_via_daemon`` proxy's single
``(RuntimeError, OSError, TimeoutError)`` catch into two outcomes:

* **TimeoutError is terminal.** The close request already reached the daemon, so
  the daemon may still be mid-close under the lock. Falling through to the
  ungated in-process close could double-apply it. The proxy now emits a typed
  :class:`~eawf.surfaces.cli.errors.DaemonMutationIndeterminate` envelope and
  exits non-zero WITHOUT writing state -- no in-process retry.
* **RuntimeError / OSError still fall back**, but the in-process close event is
  stamped with a distinct ``close_mechanism = "daemon-fallback"`` on its
  ``extras`` map so an audit can tell it from a gate-passed ``"daemon"`` close.

Both drive the REAL ``eawf wave close`` CLI with a faked daemon client so the
bespoke proxy path runs, plus pure-function coverage of the new
``transport_fallback`` parameter on :func:`resolve_close_mechanism` /
:func:`close_event_extras`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.paths import store_path
from eawf.surfaces.cli._mutation import close_event_extras, resolve_close_mechanism
from eawf.surfaces.cli.app import app
from tests._session_helpers import seed_active_session_on_disk
from tests.conftest import make_claim_criterion

pytestmark = pytest.mark.unit

runner = CliRunner()

_WAVE_ID = "P01-I01-W01"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Temp workspace whose bootstrap mutations run daemonless (in-process).

    The per-test proxy-up scenario clears ``EAWF_DAEMONLESS`` and enables
    proxying just before the ``wave close`` under test (see :func:`_enable_proxy`).
    """
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    yield tmp_path


def _state_path(workspace: Path) -> Path:
    return workspace / ".ea" / "state.json"


def _event_path(workspace: Path) -> Path:
    return store_path(_state_path(workspace), StoreKind.EVENT)


def _read_wave_status(workspace: Path) -> str:
    state = State.model_validate(orjson.loads(_state_path(workspace).read_bytes()))
    return state.waves[_WAVE_ID].status.value


def _close_events(workspace: Path) -> list[dict[str, Any]]:
    """Decode the ``wave close`` mutation event rows from the event store."""
    path = _event_path(workspace)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = orjson.loads(line)
        if row["payload"]["event_type"] == "wave close":
            rows.append(row)
    return rows


def _bootstrap_claimed_wave(workspace: Path) -> None:
    """Bring state up to one CLAIMED wave under an ACTIVE P01-I01."""
    assert (
        runner.invoke(
            app, ["project", "init", "QR", "--title", "Quant", "--domains", "quant"]
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["phase", "open", "--auto", "--title", "P1"]).exit_code == 0
    assert runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "I1"]).exit_code == 0
    assert (
        runner.invoke(
            app,
            [
                "wave",
                "plan",
                "P01-I01",
                "--id",
                _WAVE_ID,
                "--title",
                "w",
                "--files",
                "src/",
                "--effort-bucket",
                "M",
            ],
        ).exit_code
        == 0
    )
    state = State.model_validate(orjson.loads(_state_path(workspace).read_bytes()))
    state.waves[_WAVE_ID].success_criteria = [make_claim_criterion()]
    _state_path(workspace).write_bytes(orjson.dumps(state.model_dump(mode="json")))
    seed_active_session_on_disk(_state_path(workspace), session_id="S-1")
    assert runner.invoke(app, ["wave", "claim", _WAVE_ID, "--session", "S-1"]).exit_code == 0


class _TimeoutClient:
    """DaemonClient stand-in: ping OK, but ``state_mutate`` times out."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> _TimeoutClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return {}  # daemon.ping -> reachable

    def state_mutate(
        self, mutation: Any, *, idempotency_key: str | None = None, repo_root: str | None = None
    ) -> dict[str, Any]:
        raise TimeoutError("close RPC read timed out")


class _TransportErrorClient:
    """DaemonClient stand-in: ping OK, but ``state_mutate`` fails at transport."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> _TransportErrorClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return {}  # daemon.ping -> reachable

    def state_mutate(
        self, mutation: Any, *, idempotency_key: str | None = None, repo_root: str | None = None
    ) -> dict[str, Any]:
        raise RuntimeError("connection reset mid-write")


def _enable_proxy(monkeypatch: pytest.MonkeyPatch, *, client: type) -> None:
    """Switch from the daemonless bootstrap to a proxy-up scenario."""
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr("eawf.surfaces.cli._mutation._proxy_enabled", lambda _ws: True)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", client)


# --- CR-01: close-RPC TimeoutError is terminal ------------------------------


def test_close_rpc_timeout_is_terminal_no_state_write(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close-RPC ``TimeoutError`` exits non-zero and writes NO state.

    The proxy must not silently fall through to the ungated in-process close --
    the daemon may still be mid-close under the lock. The wave stays CLAIMED and
    no ``wave close`` event lands.
    """
    _bootstrap_claimed_wave(workspace)
    _enable_proxy(monkeypatch, client=_TimeoutClient)

    res = runner.invoke(app, ["wave", "close", _WAVE_ID, "--outcome", "done"])

    assert res.exit_code != 0, res.output
    # DaemonMutationIndeterminate maps onto the DAEMON_UNREACHABLE (4) bucket.
    assert res.exit_code == 4, res.output
    assert "timed out" in res.output
    assert _WAVE_ID in res.output
    # No in-process retry: the wave never flipped to CLOSED and no close event
    # landed.
    assert _read_wave_status(workspace) == "claimed"
    assert _close_events(workspace) == []


# --- CR-02: transport fallback stamps ``daemon-fallback`` -------------------


def test_transport_fallback_close_stamps_daemon_fallback(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RuntimeError transport fallback closes in-process, stamped distinctly.

    The close still lands (the in-process WAL-backed writer carries it), but its
    event's ``close_mechanism`` is ``"daemon-fallback"`` -- distinct from a
    gate-passed ``"daemon"`` close -- so an audit can see it skipped the daemon
    close gate on a fallback.
    """
    _bootstrap_claimed_wave(workspace)
    _enable_proxy(monkeypatch, client=_TransportErrorClient)

    res = runner.invoke(app, ["wave", "close", _WAVE_ID, "--outcome", "done"])

    assert res.exit_code == 0, res.output
    assert _read_wave_status(workspace) == "closed"
    closes = _close_events(workspace)
    assert len(closes) == 1
    assert closes[0]["payload"]["extras"]["close_mechanism"] == "daemon-fallback"


# --- pure-function coverage of the new ``transport_fallback`` parameter ------


def test_resolve_close_mechanism_transport_fallback_wins() -> None:
    """``transport_fallback=True`` yields ``"daemon-fallback"`` regardless of gate/waiver."""
    assert (
        resolve_close_mechanism(gate_bearing=False, waived=False, transport_fallback=True)
        == "daemon-fallback"
    )
    assert (
        resolve_close_mechanism(gate_bearing=True, waived=True, transport_fallback=True)
        == "daemon-fallback"
    )


def test_resolve_close_mechanism_default_is_daemon() -> None:
    """Boundary: the default (no transport fallback, no daemonless env) is ``"daemon"``."""
    assert resolve_close_mechanism(gate_bearing=True, waived=False) == "daemon"


def test_close_event_extras_transport_fallback_preserves_base() -> None:
    """``close_event_extras`` folds ``daemon-fallback`` in while preserving base extras."""
    extras = close_event_extras(
        {"readiness_warnings_count": 3}, gate_bearing=False, waived=False, transport_fallback=True
    )
    assert extras["close_mechanism"] == "daemon-fallback"
    assert extras["readiness_warnings_count"] == 3
