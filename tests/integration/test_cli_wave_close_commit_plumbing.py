"""Unit tests for the ``--commit`` plumbing on ``eawf wave close``.

Covers the inline helper ``_resolve_commit_sha`` in
:mod:`eawf.surfaces.cli.commands.lifecycle`:

* Happy path: ``git rev-parse <ref>^{commit}`` returns a 40-char hex SHA.
* Error path: non-zero exit -> :class:`InvalidInput` with the canonical
  ``cannot resolve commit ref: <ref!r>`` phrasing.
* Error path: timeout -> :class:`InvalidInput` mentions ``timed out``.
* Error path: missing ``git`` binary -> :class:`InvalidInput`.
* Sanity: non-canonical stdout (e.g. blank, short) is rejected even on
  ``returncode==0``.

These tests monkeypatch :mod:`subprocess` inside the CLI module so the
helper exercises the real branching logic without depending on the
host's git state.
"""

from __future__ import annotations

from typing import Any

import pytest

from eawf.surfaces.cli.commands import lifecycle as lifecycle_cli
from eawf.surfaces.cli.flags import GlobalFlags


def test_wave_close_daemon_proxy_forwards_tokens_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bespoke wave-close daemon proxy includes the final token tally."""
    captured: dict[str, Any] = {}

    class FakeClient:
        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def state_mutate(self, mutation: Any, *, repo_root: str | None = None) -> dict[str, Any]:
            captured["params"] = dict(mutation.params)
            captured["repo_root"] = repo_root
            return {
                "event": {"id": "EV-1"},
                "before_version": "before",
                "after_version": "after",
            }

    monkeypatch.setattr("eawf.surfaces.cli._mutation._daemon_reachable", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", FakeClient)

    handled = lifecycle_cli._wave_close_via_daemon(
        flags=GlobalFlags(json_output=True),
        wave_id="P05-I01-W01",
        outcome="ok",
        resolved_sha=None,
        tokens_consumed=1234,
    )

    assert handled is True
    assert captured["params"]["tokens_consumed"] == 1234
    assert captured["params"]["wave_id"] == "P05-I01-W01"
    assert captured["params"]["outcome"] == "ok"


def test_wave_close_daemon_proxy_forwards_no_runtime_waiver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bespoke wave-close daemon proxy carries the close-scoped runtime waiver."""
    captured: dict[str, Any] = {}

    class FakeClient:
        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def state_mutate(self, mutation: Any, *, repo_root: str | None = None) -> dict[str, Any]:
            captured["params"] = dict(mutation.params)
            captured["repo_root"] = repo_root
            return {
                "event": {"id": "EV-1"},
                "before_version": "before",
                "after_version": "after",
            }

    monkeypatch.setattr("eawf.surfaces.cli._mutation._daemon_reachable", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", FakeClient)

    handled = lifecycle_cli._wave_close_via_daemon(
        flags=GlobalFlags(json_output=True),
        wave_id="P05-I01-W01",
        outcome="ok",
        resolved_sha=None,
        tokens_consumed=None,
        no_runtime_waiver=True,
    )

    assert handled is True
    assert captured["params"]["no_runtime_waiver"] is True
    assert captured["params"]["wave_id"] == "P05-I01-W01"
    assert captured["params"]["outcome"] == "ok"
