"""A stand-in daemon for the CLI verb-contract tests.

The native verbs are dispatch over the daemon's RPCs, so the contract is
observable at the wire: which method was sent, with which parameters, and
how the answer was printed and exited. :class:`FakeDaemon` records the
calls and answers with a fixed result or raises a fixed error, and the
escalation gate is replaced so no test spawns a real daemon.
"""

from __future__ import annotations

from typing import Any

import pytest


class FakeDaemon:
    """A daemon client double recording every call it is handed.

    Attributes:
        calls: Every ``(method, params)`` pair sent, oldest first.
        result: What each call answers.
        error: What each call raises instead, when set.
        connect_error: What entering the client raises, when set.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.result: dict[str, Any] = {}
        self.error: Exception | None = None
        self.connect_error: Exception | None = None

    def __call__(self, *_args: object, **_kwargs: object) -> FakeDaemon:
        return self

    def __enter__(self) -> FakeDaemon:
        if self.connect_error is not None:
            raise self.connect_error
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, dict(params)))
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> FakeDaemon:
    """Point the native verbs at a fake daemon and disarm the escalation gate."""
    fake = FakeDaemon()
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", lambda *_a, **_k: 0)
    monkeypatch.setattr("eawf.surfaces.cli.commands.domain.DaemonClient", fake)
    return fake
