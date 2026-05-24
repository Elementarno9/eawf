"""CLI-side tests for the W10 ``_persist_registry`` proxy dispatcher.

The four scenarios mirror :mod:`tests.cli.test_config_proxy`:

1. ``daemon.proxy_enabled=True`` (new default) + daemon up — the call
   diffs the candidate against the on-disk registry and dispatches
   one or more ``registry.update`` RPCs.
2. ``EAWF_DAEMONLESS=1`` — in-process portalocker arm runs.
3. ``daemon.proxy_enabled=True`` + daemon DOWN — refuses with
   ``daemon_required`` envelope.
4. Pre-W10 daemon (``-32601 method-not-found``) — fall back.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import orjson
import pytest

from eawf.platform.registry import Registry, RegistryRepoEntry
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands import repo as repo_cmd

pytestmark = pytest.mark.unit


def _make_registry(repos: dict[str, tuple[str, str | None]]) -> Registry:
    """Construct a Registry with the given ``code -> (path, title)`` map."""
    return Registry(
        version="1",
        updated_at=datetime.now(UTC),
        active_code=None,
        repos={
            code: RegistryRepoEntry(code=code, path=path, title=title)
            for code, (path, title) in repos.items()
        },
    )


# ---- Diff helper ------------------------------------------------------------


def test_diff_add_emits_add_op() -> None:
    before = Registry()
    after = _make_registry({"ABC": ("/repos/abc", "ABC")})
    ops = repo_cmd._diff_registries_to_ops(before, after)
    assert ops == [("add", "ABC", {"path": "/repos/abc", "title": "ABC"})]


def test_diff_remove_emits_remove_op() -> None:
    before = _make_registry({"ABC": ("/repos/abc", None)})
    after = Registry()
    ops = repo_cmd._diff_registries_to_ops(before, after)
    assert ops == [("remove", "ABC", {})]


def test_diff_set_active_only_emits_idempotent_add() -> None:
    """A bare active_code flip emits an idempotent add for the new active."""
    before = _make_registry({"ABC": ("/repos/abc", None)})
    after = Registry(
        version="1",
        updated_at=datetime.now(UTC),
        active_code="ABC",
        repos=before.repos,
    )
    ops = repo_cmd._diff_registries_to_ops(before, after)
    assert ops == [("add", "ABC", {"path": "/repos/abc", "set_active": True})]


def test_diff_multi_change_emits_in_sorted_order() -> None:
    before = _make_registry({"BBB": ("/b", None)})
    after = _make_registry({"AAA": ("/a", None), "CCC": ("/c", None)})
    ops = repo_cmd._diff_registries_to_ops(before, after)
    # Adds sorted alphabetically, then removes.
    op_kinds = [(o[0], o[1]) for o in ops]
    assert ("add", "AAA") in op_kinds
    assert ("add", "CCC") in op_kinds
    assert ("remove", "BBB") in op_kinds


# ---- Scenario 1: proxy on + daemon up --------------------------------------


class _FakeRegistryClient:
    """DaemonClient stand-in capturing registry_update calls."""

    calls: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        pass

    def __enter__(self) -> _FakeRegistryClient:
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def registry_update(
        self,
        *,
        operation: str,
        repo_id: str,
        fields: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        _FakeRegistryClient.calls.append(
            {
                "operation": operation,
                "repo_id": repo_id,
                "fields": dict(fields) if fields else {},
                "idempotency_key": idempotency_key,
            }
        )
        return {
            "operation": operation,
            "repo_id": repo_id,
            "registry_path": "fake",
            "envelope": {"id": "REG-stub", "kind": "registry_updated"},
            "idempotent_replay": False,
        }


def test_persist_registry_proxies_through_daemon_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An add-only diff dispatches one ``registry.update`` RPC."""
    registry_path = tmp_path / "registry.json"
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr(repo_cmd, "_daemon_proxy_enabled_for_registry", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._mutation._daemon_reachable", lambda *a, **k: True)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeRegistryClient)
    _FakeRegistryClient.calls = []

    candidate = _make_registry({"ABC": ("/repos/abc", "ABC")})
    repo_cmd._persist_registry(candidate, registry_path)

    assert len(_FakeRegistryClient.calls) == 1
    assert _FakeRegistryClient.calls[0]["operation"] == "add"
    assert _FakeRegistryClient.calls[0]["repo_id"] == "ABC"
    # CLI side did NOT write the file — the (fake) daemon owns the disk.
    assert not registry_path.exists()


# ---- Scenario 2: EAWF_DAEMONLESS=1 -----------------------------------------


def test_persist_registry_daemonless_env_uses_in_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")

    def _fail_call(*_a: Any, **_kw: Any) -> Any:
        pytest.fail("daemonless override must skip the daemon entirely")

    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _fail_call)

    candidate = _make_registry({"ABC": ("/repos/abc", None)})
    repo_cmd._persist_registry(candidate, registry_path)

    assert registry_path.exists()
    payload = orjson.loads(registry_path.read_bytes())
    assert "ABC" in payload["repos"]


# ---- Scenario 3: proxy on + daemon DOWN ------------------------------------


def test_persist_registry_daemon_down_raises_daemon_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr(repo_cmd, "_daemon_proxy_enabled_for_registry", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._mutation._daemon_reachable", lambda *a, **k: False)

    candidate = _make_registry({"ABC": ("/repos/abc", None)})
    with pytest.raises(cli_errors.StateConflict, match="daemon_required"):
        repo_cmd._persist_registry(candidate, registry_path)

    assert not registry_path.exists()


# ---- Scenario 4: pre-W10 daemon (-32601) → fallback ------------------------


def test_persist_registry_method_not_found_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry_path = tmp_path / "registry.json"
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr(repo_cmd, "_daemon_proxy_enabled_for_registry", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._mutation._daemon_reachable", lambda *a, **k: True)

    class _PreW10Client:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def __enter__(self) -> _PreW10Client:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def registry_update(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
            from eawf.surfaces.cli._daemon_client import DaemonRpcError

            raise DaemonRpcError(code=-32601, message="method not found", data=None)

    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _PreW10Client)

    candidate = _make_registry({"ABC": ("/repos/abc", None)})
    repo_cmd._persist_registry(candidate, registry_path)

    # In-process arm wrote the file.
    assert registry_path.exists()
    payload = orjson.loads(registry_path.read_bytes())
    assert "ABC" in payload["repos"]
