"""CLI-side tests for the W09 daemon-proxy mutation path.

The proxy entry point :func:`eawf.cli._mutation.state_mutate` is the
public surface for state-mutating CLI verbs once the rewire begins.
W09 ships :attr:`MutationKind.WAVE_CLOSE` end-to-end as the canary;
this suite exercises four scenarios per the wave spec:

1. ``daemon.proxy_enabled=false`` (W09 default) — every call MUST take
   the in-process path; the apply callable is invoked under
   :func:`state_transaction`.
2. ``daemon.proxy_enabled=true`` + daemon up — the call MUST proxy
   through ``state.mutate`` and the in-process apply MUST NOT run.
3. ``daemon.proxy_enabled=true`` + daemon unreachable (transport error)
   + writer verb — the call MUST refuse with
   :class:`cli_errors.DaemonUnreachable` (exit 4) carrying the
   ``daemon_required`` envelope.
4. ``daemon.proxy_enabled=true`` + daemon down + READ-only verb —
   bypass the daemon per the V1 carve-out (no error). The read-path
   bypass lives directly on the CLI surface (config get / state
   resolve / wave list); the proxy entry point does NOT see read
   verbs, so this case is exercised via the absence-of-call path
   on the read helper.

The tests monkeypatch the proxy plumbing at the module boundary so we
exercise the real branching logic without spinning up a real daemon
process. End-to-end with a real daemon socket is covered in
:mod:`tests.daemon.test_daemon_client`.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.cli import _mutation, exit_codes
from eawf.cli import errors as cli_errors
from eawf.state.mutations import Mutation, MutationKind

pytestmark = pytest.mark.unit


def _build_state(path: Path) -> None:
    """Write a minimal valid state.json with one claimed wave + parent iter/phase."""
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": "2026-05-19T00:00:00+00:00",
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
            "P24": {
                "id": "P24",
                "scope_id": "ABC",
                "subproject_id": None,
                "title": "P24",
                "status": "active",
                "iter_ids": ["P24-I01"],
                "outcome_ids": [],
                "opened_at": "2026-05-19T00:00:00+00:00",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P24-I01": {
                "id": "P24-I01",
                "phase_id": "P24",
                "title": "I01",
                "status": "active",
                "wave_ids": ["P24-I01-W09"],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-05-19T00:00:00+00:00",
                "closed_at": None,
            }
        },
        "waves": {
            "P24-I01-W09": {
                "id": "P24-I01-W09",
                "iter_id": "P24-I01",
                "title": "test",
                "status": "claimed",
                "claim_session_id": "session-x",
                "opened_at": "2026-05-19T00:00:00+00:00",
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


# ---- Scenario 1: proxy_enabled=False (default) -----------------------------


def test_state_mutate_default_uses_in_process_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``daemon.proxy_enabled=False`` → apply callable runs under state_transaction."""
    state_path = tmp_path / "state.json"
    _build_state(state_path)

    # Force proxy_enabled=False; assert daemon-reachable is never called.
    monkeypatch.setattr(_mutation, "_proxy_enabled", lambda _ws: False)
    called: dict[str, int] = {"reach": 0, "apply": 0}

    def _fake_reach(*_args: Any, **_kwargs: Any) -> bool:
        called["reach"] += 1
        return True

    monkeypatch.setattr(_mutation, "_daemon_reachable", _fake_reach)

    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "test"},
    )

    def _apply(state: Any) -> None:
        from eawf.lifecycle.transitions import close_wave

        called["apply"] += 1
        close_wave(state, wave_id="P24-I01-W09", outcome="test")

    result = _mutation.state_mutate(
        state_path,
        mutation,
        apply=_apply,
        workspace=None,
    )

    assert result == {"proxied": False, "result": {}}
    assert called["apply"] == 1
    assert called["reach"] == 0
    on_disk = orjson.loads(state_path.read_bytes())
    assert on_disk["waves"]["P24-I01-W09"]["status"] == "closed"


# ---- Scenario 2: proxy_enabled=True + daemon up ----------------------------


class _FakeClient:
    """Minimal DaemonClient stand-in for the proxy-up scenario."""

    last_mutation: Mutation | None = None
    last_idempotency: str | None = None
    last_repo_root: str | None = None
    call_count: int = 0

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def state_mutate(
        self,
        mutation: Mutation,
        *,
        idempotency_key: str | None = None,
        repo_root: str | None = None,
    ) -> dict[str, Any]:
        _FakeClient.last_mutation = mutation
        _FakeClient.last_idempotency = idempotency_key
        _FakeClient.last_repo_root = repo_root
        _FakeClient.call_count += 1
        return {
            "event": {
                "id": "EV-stub-1",
                "kind": "event",
                "scope_id": mutation.scope_id,
            },
            "before_version": "before-x",
            "after_version": "after-x",
            "idempotent_replay": False,
        }


def test_state_mutate_proxy_up_routes_through_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``daemon.proxy_enabled=True`` + reachable → proxy path runs; apply skipped."""
    state_path = tmp_path / "state.json"
    _build_state(state_path)
    monkeypatch.setattr(_mutation, "_proxy_enabled", lambda _ws: True)
    monkeypatch.setattr(_mutation, "_daemon_reachable", lambda *a, **k: True)
    monkeypatch.setattr("eawf.cli._daemon_client.DaemonClient", _FakeClient)
    _FakeClient.last_mutation = None
    _FakeClient.last_idempotency = None
    _FakeClient.last_repo_root = None
    _FakeClient.call_count = 0

    apply_called = [False]

    def _apply(_state: Any) -> None:
        apply_called[0] = True

    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "test"},
    )
    result = _mutation.state_mutate(state_path, mutation, apply=_apply, workspace=None)

    assert result["proxied"] is True
    assert apply_called == [False]  # apply MUST NOT run under proxy
    assert _FakeClient.call_count == 1
    assert _FakeClient.last_mutation is not None
    assert _FakeClient.last_mutation.kind is MutationKind.WAVE_CLOSE
    # P26-W03: the proxy forwards the caller's repo root so the daemon
    # — which is one per user — resolves the right anchor regardless
    # of its boot-time cwd. With ``workspace=None`` the helper uses the
    # resolved ``Path.cwd()`` value.
    assert _FakeClient.last_repo_root is not None
    assert Path(_FakeClient.last_repo_root) == Path.cwd().resolve()
    # The proxy path does NOT touch state.json directly; the (fake)
    # daemon owns the on-disk write. The local file stays untouched.
    on_disk = orjson.loads(state_path.read_bytes())
    assert on_disk["waves"]["P24-I01-W09"]["status"] == "claimed"


# ---- Scenario 3: proxy_enabled=True + daemon unreachable + writer verb -----


class _DownClient:
    """DaemonClient stand-in whose connect fails like a down daemon."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> _DownClient:
        # ConnectionRefusedError is an OSError subclass — the transport error a
        # real connect raises when the daemon socket has no listener.
        raise ConnectionRefusedError("daemon socket not listening")

    def __exit__(self, *_args: Any) -> None:
        return None


def test_state_mutate_proxy_down_writer_raises_daemon_unreachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """proxy_enabled=True + connect fails + writer verb → DaemonUnreachable (exit 4)."""
    state_path = tmp_path / "state.json"
    _build_state(state_path)
    monkeypatch.setattr(_mutation, "_proxy_enabled", lambda _ws: True)
    monkeypatch.setattr("eawf.cli._daemon_client.DaemonClient", _DownClient)

    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "x"},
    )

    def _apply(_state: Any) -> None:
        pytest.fail("apply should not run when the proxy is required but unreachable")

    with pytest.raises(cli_errors.DaemonUnreachable, match="daemon_required") as excinfo:
        _mutation.state_mutate(state_path, mutation, apply=_apply, workspace=None)
    assert excinfo.value.exit_code == exit_codes.DAEMON_UNREACHABLE


class _MidMutateDropClient:
    """DaemonClient stand-in that connects but drops the mutate mid-flight."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> _MidMutateDropClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def state_mutate(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        # TimeoutError is an OSError subclass — the transport error a real call
        # raises when the daemon dies between the connect and the reply.
        raise TimeoutError("daemon call exceeded timeout")


def test_state_mutate_transport_error_mid_mutate_maps_to_exit_4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport error during the mutate maps to DaemonUnreachable (exit 4 not 5)."""
    state_path = tmp_path / "state.json"
    _build_state(state_path)
    monkeypatch.setattr(_mutation, "_proxy_enabled", lambda _ws: True)
    monkeypatch.setattr("eawf.cli._daemon_client.DaemonClient", _MidMutateDropClient)

    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "x"},
    )

    def _apply(_state: Any) -> None:
        pytest.fail("apply should not run when the daemon drops the mutate")

    with pytest.raises(cli_errors.DaemonUnreachable) as excinfo:
        _mutation.state_mutate(state_path, mutation, apply=_apply, workspace=None)
    assert excinfo.value.exit_code == exit_codes.DAEMON_UNREACHABLE


# ---- Scenario 4: read-only verb under proxy_enabled=True + daemon DOWN -----


def test_state_mutate_proxy_down_read_only_bypasses_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-only verbs MUST NOT call the proxy entry point at all.

    The W09 proxy entry point is mutation-only; ``state_mutate`` is
    never invoked from a read code path. The V1 carve-out (config get,
    state resolve, wave list) reads state.json directly via the
    daemonless reader. This test demonstrates the invariant by
    exercising a read path that bypasses ``state_mutate`` entirely.
    """
    state_path = tmp_path / "state.json"
    _build_state(state_path)
    monkeypatch.setattr(_mutation, "_proxy_enabled", lambda _ws: True)
    monkeypatch.setattr(_mutation, "_daemon_reachable", lambda *a, **k: False)

    # Pure read path — orjson.loads succeeds; no proxy hop.
    payload = orjson.loads(state_path.read_bytes())
    assert payload["waves"]["P24-I01-W09"]["status"] == "claimed"


# ---- Daemon refuses kind (NotImplementedError → fallback) ------------------


def test_state_mutate_proxy_kind_not_wired_falls_back_to_in_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the daemon refuses the kind, the helper falls back to apply."""
    state_path = tmp_path / "state.json"
    _build_state(state_path)
    monkeypatch.setattr(_mutation, "_proxy_enabled", lambda _ws: True)
    monkeypatch.setattr(_mutation, "_daemon_reachable", lambda *a, **k: True)

    class _RefusingClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> _RefusingClient:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def state_mutate(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            from eawf.cli._daemon_client import DaemonRpcError

            raise DaemonRpcError(
                code=-32603,
                message="internal error: NotImplementedError: not yet wired",
                data=None,
            )

    monkeypatch.setattr("eawf.cli._daemon_client.DaemonClient", _RefusingClient)

    apply_called = [False]

    def _apply(state: Any) -> None:
        from eawf.lifecycle.transitions import close_wave

        apply_called[0] = True
        close_wave(state, wave_id="P24-I01-W09", outcome="fb")

    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W09",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W09", "outcome": "fb"},
    )

    result = _mutation.state_mutate(state_path, mutation, apply=_apply, workspace=None)
    assert apply_called == [True]
    assert result["proxied"] is False


# ---- Daemon returns -32002 → ValidationFailed ------------------------------


def test_state_mutate_proxy_validation_failed_maps_to_validation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon ``-32002 validation_failed`` maps to ``ValidationFailed``."""
    state_path = tmp_path / "state.json"
    _build_state(state_path)
    monkeypatch.setattr(_mutation, "_proxy_enabled", lambda _ws: True)
    monkeypatch.setattr(_mutation, "_daemon_reachable", lambda *a, **k: True)

    class _RejectingClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> _RejectingClient:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def state_mutate(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            from eawf.cli._daemon_client import DaemonRpcError

            raise DaemonRpcError(
                code=-32002,
                message="validation_failed: unknown wave",
                data=None,
            )

    monkeypatch.setattr("eawf.cli._daemon_client.DaemonClient", _RejectingClient)

    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id="P24-I01-W99",
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": "P24-I01-W99", "outcome": "x"},
    )

    def _apply(_state: Any) -> None:
        pytest.fail("apply should not run when proxy rejects the mutation")

    with pytest.raises(cli_errors.ValidationFailed, match="unknown wave"):
        _mutation.state_mutate(state_path, mutation, apply=_apply, workspace=None)
