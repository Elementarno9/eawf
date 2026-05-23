"""CLI-side tests for the generic daemon-proxy shim.

:func:`eawf.cli._dispatch._mutate_via_daemon` is the single generic
entry every mutating-verb wrapper routes through (superseding the
per-verb bespoke proxies + the dropped ``_mutation.state_mutate``).
This suite exercises the branching contract:

1. **Proxied end-to-end** — daemon up → the shim marshals one typed
   :class:`~eawf.state.mutations.Mutation` across ``state.mutate`` and
   returns the daemon's result dict; the *fallback* MUST NOT run.
2. **Method-not-found fallback** — a ``-32601`` (daemon predates the
   kind) routes to the *fallback* callable and returns its value.
3. **NotImplementedError fallback** — an RPC error whose message
   carries ``NotImplementedError`` (kind reserved but unwired) also
   falls back.
4. **Transport-error fallback** — a connect / mutate transport drop
   (``OSError`` / ``TimeoutError``) falls back to the in-process path.
5. **Validation rejection** — a ``-32002 validation_failed`` maps to
   :class:`~eawf.cli.errors.ValidationFailed`; the fallback MUST NOT
   run.
6. **Typed RPC-error mapping** — every other non-fallback RPC code
   (``-32001`` / ``-32003`` / ``-32005`` / unknown) maps onto its
   specific :class:`~eawf.cli.errors.CliError` via
   :func:`~eawf.cli.errors.cli_error_for_rpc` (NOT a bare
   ``DaemonRpcError`` that would escape the verb handler's
   ``except CliError``); the threaded kind survives into the envelope.
7. **Daemonless boundary** — ``--daemonless`` (flag or env) on the
   mutating verb is refused by the embedded escalation gate before any
   wire traffic; neither the client nor the fallback runs.

The escalation + client plumbing is monkeypatched at the module
boundary so the real branching logic runs without spawning a real
daemon. End-to-end against a live socket is covered in
:mod:`tests.daemon.test_daemon_client`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eawf.cli import _dispatch, exit_codes
from eawf.cli import errors as cli_errors
from eawf.state.mutations import MutationKind

pytestmark = pytest.mark.unit


_PARAMS: dict[str, Any] = {"wave_id": "P24-I01-W09", "outcome": "test"}


@pytest.fixture
def _no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the auto-spawn so the escalation gate never touches a real daemon.

    ``_mutate_via_daemon`` routes through :func:`escalate_mutation`,
    which auto-spawns when no daemon is up. The tests assert the
    proxy-vs-fallback branching, not the spawn flow, so the spawn is
    replaced with a fake PID. The daemonless boundary test deliberately
    skips this fixture's effect by re-asserting the spawn never runs.
    """
    monkeypatch.setattr(_dispatch, "ensure_daemon", lambda _runtime=None: 4242)


def _set_client(monkeypatch: pytest.MonkeyPatch, client: type) -> None:
    """Point the daemon-client constructor at *client* for the shim path."""
    monkeypatch.setattr("eawf.cli._daemon_client.DaemonClient", client)


# ---- Scenario 1: proxied end-to-end ----------------------------------------


class _FakeClient:
    """Minimal DaemonClient stand-in for the proxy-up scenario."""

    last_kind: MutationKind | None = None
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
        mutation: Any,
        *,
        idempotency_key: str | None = None,
        repo_root: str | None = None,
    ) -> dict[str, Any]:
        _FakeClient.last_kind = mutation.kind
        _FakeClient.last_idempotency = idempotency_key
        _FakeClient.last_repo_root = repo_root
        _FakeClient.call_count += 1
        return {
            "event": {"id": "EV-stub-1", "kind": "event", "scope_id": mutation.scope_id},
            "before_version": "before-x",
            "after_version": "after-x",
            "idempotent_replay": False,
        }


def test_mutate_via_daemon_proxy_up_routes_through_daemon(
    monkeypatch: pytest.MonkeyPatch,
    _no_spawn: None,
) -> None:
    """Daemon up → the shim proxies through ``state.mutate``; fallback skipped."""
    _set_client(monkeypatch, _FakeClient)
    _FakeClient.last_kind = None
    _FakeClient.last_idempotency = None
    _FakeClient.last_repo_root = None
    _FakeClient.call_count = 0

    fallback_called = [False]

    def _fallback() -> dict[str, Any]:
        fallback_called[0] = True
        return {"proxied": False}

    result = _dispatch._mutate_via_daemon(
        MutationKind.WAVE_CLOSE,
        _PARAMS,
        None,
        scope_id="P24-I01-W09",
        verb="wave close",
        fallback=_fallback,
    )

    assert fallback_called == [False]  # fallback MUST NOT run under proxy
    assert _FakeClient.call_count == 1
    assert _FakeClient.last_kind is MutationKind.WAVE_CLOSE
    # The shim returns the daemon's result dict verbatim.
    assert result["event"]["id"] == "EV-stub-1"
    assert result["after_version"] == "after-x"
    # The shim forwards the caller's repo root so the one-per-user daemon
    # resolves the right anchor; with ``flags=None`` it uses ``Path.cwd()``.
    assert _FakeClient.last_repo_root is not None
    assert Path(_FakeClient.last_repo_root) == Path.cwd().resolve()


def test_mutate_via_daemon_forwards_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
    _no_spawn: None,
) -> None:
    """An explicit idempotency key reaches the client call."""
    _set_client(monkeypatch, _FakeClient)
    _FakeClient.last_idempotency = None
    _FakeClient.call_count = 0

    _dispatch._mutate_via_daemon(
        MutationKind.WAVE_CLOSE,
        _PARAMS,
        None,
        scope_id="P24-I01-W09",
        verb="wave close",
        fallback=lambda: {},
        idempotency_key="retry-key-1",
    )
    assert _FakeClient.last_idempotency == "retry-key-1"


# ---- Scenario 2 + 3: method-not-found / NotImplementedError fallback -------


def _refusing_client(code: int, message: str) -> type:
    """Build a DaemonClient stand-in whose mutate raises a fixed RPC error."""

    class _RefusingClient:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def __enter__(self) -> _RefusingClient:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def state_mutate(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            from eawf.cli._daemon_client import DaemonRpcError

            raise DaemonRpcError(code=code, message=message, data=None)

    return _RefusingClient


def test_mutate_via_daemon_method_not_found_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    _no_spawn: None,
) -> None:
    """A ``-32601`` method-not-found routes to the fallback and returns its value."""
    _set_client(monkeypatch, _refusing_client(-32601, "method not found"))

    sentinel = object()

    def _fallback() -> object:
        return sentinel

    result = _dispatch._mutate_via_daemon(
        MutationKind.WAVE_CLOSE,
        _PARAMS,
        None,
        scope_id="P24-I01-W09",
        verb="wave close",
        fallback=_fallback,
    )
    assert result is sentinel


def test_mutate_via_daemon_not_implemented_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    _no_spawn: None,
) -> None:
    """An RPC error carrying ``NotImplementedError`` in the message falls back."""
    _set_client(
        monkeypatch,
        _refusing_client(-32603, "internal error: NotImplementedError: not yet wired"),
    )

    fallback_called = [False]

    def _fallback() -> str:
        fallback_called[0] = True
        return "fell-back"

    result = _dispatch._mutate_via_daemon(
        MutationKind.WAVE_CLOSE,
        _PARAMS,
        None,
        scope_id="P24-I01-W09",
        verb="wave close",
        fallback=_fallback,
    )
    assert fallback_called == [True]
    assert result == "fell-back"


# ---- Scenario 4: transport-error fallback ----------------------------------


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


def test_mutate_via_daemon_connect_transport_error_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    _no_spawn: None,
) -> None:
    """A connect transport drop routes to the in-process fallback."""
    _set_client(monkeypatch, _DownClient)

    fallback_called = [False]

    def _fallback() -> str:
        fallback_called[0] = True
        return "in-process"

    result = _dispatch._mutate_via_daemon(
        MutationKind.WAVE_CLOSE,
        _PARAMS,
        None,
        scope_id="P24-I01-W09",
        verb="wave close",
        fallback=_fallback,
    )
    assert fallback_called == [True]
    assert result == "in-process"


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


def test_mutate_via_daemon_mid_mutate_transport_error_falls_back(
    monkeypatch: pytest.MonkeyPatch,
    _no_spawn: None,
) -> None:
    """A transport drop during the mutate also routes to the fallback."""
    _set_client(monkeypatch, _MidMutateDropClient)

    fallback_called = [False]

    def _fallback() -> str:
        fallback_called[0] = True
        return "in-process"

    result = _dispatch._mutate_via_daemon(
        MutationKind.WAVE_CLOSE,
        _PARAMS,
        None,
        scope_id="P24-I01-W09",
        verb="wave close",
        fallback=_fallback,
    )
    assert fallback_called == [True]
    assert result == "in-process"


# ---- Scenario 5: validation rejection --------------------------------------


def test_mutate_via_daemon_validation_failed_maps_to_validation_error(
    monkeypatch: pytest.MonkeyPatch,
    _no_spawn: None,
) -> None:
    """A daemon ``-32002 validation_failed`` maps to ``ValidationFailed``; no fallback."""
    _set_client(monkeypatch, _refusing_client(-32002, "validation_failed: unknown wave"))

    def _fallback() -> None:
        pytest.fail("fallback must not run when the daemon rejects with -32002")

    with pytest.raises(cli_errors.ValidationFailed, match="unknown wave"):
        _dispatch._mutate_via_daemon(
            MutationKind.WAVE_CLOSE,
            _PARAMS,
            None,
            scope_id="P24-I01-W99",
            verb="wave close",
            fallback=_fallback,
        )


@pytest.mark.parametrize(
    ("rpc_code", "rpc_message", "expected_cls", "expected_kind"),
    [
        (-32001, "sibling lock held", cli_errors.StateConflict, "LockConflict"),
        (-32005, "runtime ladder exhausted", cli_errors.StateConflict, "RuntimeUnavailable"),
        (-32003, "no such wave", cli_errors.UserError, "NotFound"),
    ],
)
def test_mutate_via_daemon_maps_rpc_error_to_typed_cli_error(
    monkeypatch: pytest.MonkeyPatch,
    _no_spawn: None,
    rpc_code: int,
    rpc_message: str,
    expected_cls: type[cli_errors.CliError],
    expected_kind: str,
) -> None:
    """A non-fallback RPC error maps onto its specific typed ``CliError``.

    Regression guard: a bare ``raise`` of the ``DaemonRpcError`` (a
    ``RuntimeError``, not a ``CliError``) escaped the verb handler's
    ``except CliError`` and surfaced as an uncaught traceback. The shim
    must now route every non-``-32601``/``-32002`` code through
    :func:`cli_errors.cli_error_for_rpc` so the operator sees a proper
    error envelope.
    """
    from eawf.cli._daemon_client import DaemonRpcError

    _set_client(monkeypatch, _refusing_client(rpc_code, rpc_message))

    def _fallback() -> None:
        pytest.fail("fallback must not run on a mapped RPC error")

    with pytest.raises(expected_cls) as excinfo:
        _dispatch._mutate_via_daemon(
            MutationKind.WAVE_CLOSE,
            _PARAMS,
            None,
            scope_id="P24-I01-W09",
            verb="wave close",
            fallback=_fallback,
        )
    # Specific bucket, not the bare DaemonRpcError/RuntimeError that leaked.
    assert not isinstance(excinfo.value, DaemonRpcError)
    assert str(excinfo.value) == rpc_message
    # The fine-grained kind tag rides on the typed error so the envelope
    # preserves per-cause specificity inside the five exit buckets.
    assert excinfo.value.kind == expected_kind


def test_mutate_via_daemon_mapped_rpc_error_envelope_carries_kind(
    monkeypatch: pytest.MonkeyPatch,
    _no_spawn: None,
) -> None:
    """The typed error built from an RPC code surfaces its kind in the envelope.

    End-to-end of the specificity contract: a ``-32001`` lock conflict
    maps to :class:`~eawf.cli.errors.StateConflict`, and its threaded
    ``LockConflict`` kind lands in ``ErrorEnvelope.data.kind`` so CI
    scripts pivot on the precise cause without the exit-code surface
    having to grow.
    """
    _set_client(monkeypatch, _refusing_client(-32001, "sibling lock held"))

    with pytest.raises(cli_errors.StateConflict) as excinfo:
        _dispatch._mutate_via_daemon(
            MutationKind.WAVE_CLOSE,
            _PARAMS,
            None,
            scope_id="P24-I01-W09",
            verb="wave close",
            fallback=lambda: pytest.fail("fallback must not run"),
        )

    env = cli_errors.build_envelope(excinfo.value)
    assert env.error == "StateConflict"
    assert env.exit_code == exit_codes.STATE_CONFLICT
    assert env.data["kind"] == "LockConflict"


def test_mutate_via_daemon_unknown_rpc_code_maps_to_internal_error(
    monkeypatch: pytest.MonkeyPatch,
    _no_spawn: None,
) -> None:
    """An unrecognised RPC code maps to ``InternalError`` (no fallback, no leak)."""
    from eawf.cli._daemon_client import DaemonRpcError

    _set_client(monkeypatch, _refusing_client(-32000, "server error: boom"))

    def _fallback() -> None:
        pytest.fail("fallback must not run on an unrecognised RPC error")

    with pytest.raises(cli_errors.InternalError) as excinfo:
        _dispatch._mutate_via_daemon(
            MutationKind.WAVE_CLOSE,
            _PARAMS,
            None,
            scope_id="P24-I01-W09",
            verb="wave close",
            fallback=_fallback,
        )
    assert not isinstance(excinfo.value, DaemonRpcError)
    assert excinfo.value.exit_code == exit_codes.INTERNAL_ERROR


# ---- Scenario 6: daemonless boundary ---------------------------------------


def test_mutate_via_daemon_rejects_daemonless_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--daemonless`` is refused by the escalation gate before any wire traffic."""
    from eawf.cli.flags import GlobalFlags

    # The reject must fire before the spawn AND before the client opens.
    monkeypatch.setattr(
        _dispatch,
        "ensure_daemon",
        lambda _runtime=None: pytest.fail("must reject before spawning"),
    )
    monkeypatch.setattr(
        "eawf.cli._daemon_client.DaemonClient",
        lambda *a, **k: pytest.fail("must reject before opening a client"),
    )

    def _fallback() -> None:
        pytest.fail("fallback must not run on a rejected daemonless mutation")

    with pytest.raises(cli_errors.UserError, match="wave close") as excinfo:
        _dispatch._mutate_via_daemon(
            MutationKind.WAVE_CLOSE,
            _PARAMS,
            GlobalFlags(daemonless=True),
            scope_id="P24-I01-W09",
            verb="wave close",
            fallback=_fallback,
        )
    assert excinfo.value.exit_code == exit_codes.USER_ERROR


def test_mutate_via_daemon_rejects_daemonless_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``EAWF_DAEMONLESS=1`` also triggers the mutating-verb rejection."""
    from eawf.cli.flags import GlobalFlags

    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    monkeypatch.setattr(
        _dispatch,
        "ensure_daemon",
        lambda _runtime=None: pytest.fail("must reject before spawning"),
    )

    def _fallback() -> None:
        pytest.fail("fallback must not run on a rejected daemonless mutation")

    with pytest.raises(cli_errors.UserError):
        _dispatch._mutate_via_daemon(
            MutationKind.WAVE_CLOSE,
            _PARAMS,
            GlobalFlags(),
            scope_id="P24-I01-W09",
            verb="wave close",
            fallback=_fallback,
        )
