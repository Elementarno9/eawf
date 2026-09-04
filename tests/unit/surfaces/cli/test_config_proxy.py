"""CLI-side tests for the W10 ``_save_value_to_layer`` proxy dispatcher.

The proxy entry point :func:`eawf.surfaces.cli.commands.config._save_value_to_layer`
became a daemon-proxy dispatcher in W10. The four scenarios:

1. ``daemon.proxy_enabled=True`` (new default) + daemon up — the call
   routes through ``config.set_layer_value`` RPC. The local YAML file
   is not touched by the CLI; the (fake) daemon owns the write.
2. ``EAWF_DAEMONLESS=1`` env-var override — even with the default
   ``proxy_enabled=True``, the in-process portalocker arm runs.
3. ``daemon.proxy_enabled=True`` + daemon DOWN — the call refuses
   with :class:`cli_errors.IntegrityViolation` carrying the
   ``daemon_required`` envelope.
4. Pre-W10 daemon (``-32601 method-not-found``) — fall back to the
   in-process arm so old daemons stay usable.

The tests monkeypatch the proxy plumbing at the module boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands import config as config_cmd

pytestmark = pytest.mark.unit


# ---- Scenario 1: proxy on + daemon up --------------------------------------


class _FakeConfigClient:
    """Minimal DaemonClient stand-in capturing config_set_layer_value calls."""

    last_args: dict[str, Any] | None = None
    call_count: int = 0
    unset_last_args: dict[str, Any] | None = None
    unset_call_count: int = 0

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def __enter__(self) -> _FakeConfigClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def config_set_layer_value(
        self,
        *,
        layer: str,
        key_path: list[str],
        value: Any,
        idempotency_key: str | None = None,
        repo_root: str | None = None,
    ) -> dict[str, Any]:
        _FakeConfigClient.last_args = {
            "layer": layer,
            "key_path": list(key_path),
            "value": value,
            "idempotency_key": idempotency_key,
            "repo_root": repo_root,
        }
        _FakeConfigClient.call_count += 1
        return {
            "layer": layer,
            "layer_path": "fake-path",
            "key_path": list(key_path),
            "value": value,
            "envelope": {"id": "CFG-stub-1", "kind": "config_updated"},
            "idempotent_replay": False,
        }

    def config_unset_layer_value(
        self,
        *,
        layer: str,
        key_path: list[str],
        idempotency_key: str | None = None,
        repo_root: str | None = None,
    ) -> dict[str, Any]:
        _FakeConfigClient.unset_last_args = {
            "layer": layer,
            "key_path": list(key_path),
            "idempotency_key": idempotency_key,
            "repo_root": repo_root,
        }
        _FakeConfigClient.unset_call_count += 1
        return {
            "layer": layer,
            "layer_path": "fake-path",
            "key_path": list(key_path),
            "removed": True,
            "envelope": {"id": "CFG-stub-unset", "kind": "config_updated"},
            "idempotent_replay": False,
        }


def test_save_value_to_layer_proxies_through_daemon_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With proxy enabled + daemon up, the RPC runs and the local file is untouched."""
    repo = tmp_path / "repo"
    config_yaml = repo / ".ea" / "config.yaml"
    config_yaml.parent.mkdir(parents=True)
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr(config_cmd, "_daemon_proxy_enabled", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._mutation._daemon_reachable", lambda *a, **k: True)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeConfigClient)
    monkeypatch.setattr(
        config_cmd, "_layer_label_for_path", lambda _p: "repo"
    )  # short-circuit the reverse lookup
    _FakeConfigClient.last_args = None
    _FakeConfigClient.call_count = 0

    config_cmd._save_value_to_layer(
        target_path=config_yaml,
        key="vcs.auto_commit",
        value=True,
        repo_root=repo,
    )

    assert _FakeConfigClient.call_count == 1
    assert _FakeConfigClient.last_args == {
        "layer": "repo",
        "key_path": ["vcs", "auto_commit"],
        "value": True,
        "idempotency_key": None,
        "repo_root": str(repo),
    }
    # Local file untouched — the (fake) daemon owns the write.
    assert not config_yaml.exists()


# ---- Scenario 2: EAWF_DAEMONLESS=1 override --------------------------------


def test_save_value_to_layer_daemonless_env_uses_in_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``EAWF_DAEMONLESS=1`` forces the in-process arm regardless of config."""
    repo = tmp_path / "repo"
    config_yaml = repo / ".ea" / "config.yaml"
    config_yaml.parent.mkdir(parents=True)
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")

    # Even when reachable, we should NOT call the daemon.
    def _fail_call(*_a: Any, **_kw: Any) -> Any:
        pytest.fail("daemonless override must skip the daemon entirely")

    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _fail_call)

    config_cmd._save_value_to_layer(
        target_path=config_yaml,
        key="vcs.auto_commit",
        value=False,
    )

    assert config_yaml.exists()
    body = yaml.safe_load(config_yaml.read_text())
    assert body == {"vcs": {"auto_commit": False}}


# ---- Scenario 3: proxy on + daemon DOWN ------------------------------------


def test_save_value_to_layer_daemon_down_raises_daemon_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """proxy_enabled=True + unreachable + writer verb → IntegrityViolation."""
    repo = tmp_path / "repo"
    config_yaml = repo / ".ea" / "config.yaml"
    config_yaml.parent.mkdir(parents=True)
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr(config_cmd, "_daemon_proxy_enabled", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._mutation._daemon_reachable", lambda *a, **k: False)
    monkeypatch.setattr(config_cmd, "_layer_label_for_path", lambda _p: "repo")

    with pytest.raises(cli_errors.StateConflict, match="daemon_required"):
        config_cmd._save_value_to_layer(
            target_path=config_yaml,
            key="vcs.auto_commit",
            value=True,
        )

    # No local write.
    assert not config_yaml.exists()


# ---- Scenario 4: pre-W10 daemon (-32601) → fallback ------------------------


def test_save_value_to_layer_method_not_found_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``-32601 method-not-found`` reply triggers the in-process arm."""
    repo = tmp_path / "repo"
    config_yaml = repo / ".ea" / "config.yaml"
    config_yaml.parent.mkdir(parents=True)
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr(config_cmd, "_daemon_proxy_enabled", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._mutation._daemon_reachable", lambda *a, **k: True)
    monkeypatch.setattr(config_cmd, "_layer_label_for_path", lambda _p: "repo")

    class _PreW10Client:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            pass

        def __enter__(self) -> _PreW10Client:
            return self

        def __exit__(self, *_a: Any) -> None:
            return None

        def config_set_layer_value(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
            from eawf.surfaces.cli._daemon_client import DaemonRpcError

            raise DaemonRpcError(code=-32601, message="method not found", data=None)

    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _PreW10Client)

    config_cmd._save_value_to_layer(
        target_path=config_yaml,
        key="vcs.auto_commit",
        value=True,
    )

    # In-process arm ran — local file written.
    assert config_yaml.exists()
    body = yaml.safe_load(config_yaml.read_text())
    assert body == {"vcs": {"auto_commit": True}}


# ---- Layer-label reverse-resolver -------------------------------------------


def test_layer_label_for_path_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    config_yaml = repo / ".ea" / "config.yaml"
    config_yaml.parent.mkdir(parents=True)
    config_yaml.write_text("")
    assert config_cmd._layer_label_for_path(config_yaml) == "repo"


def test_layer_label_for_path_local(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    local_yaml = repo / ".ea" / "local" / "config.yaml"
    local_yaml.parent.mkdir(parents=True)
    local_yaml.write_text("")
    assert config_cmd._layer_label_for_path(local_yaml) == "local"


def test_layer_label_for_path_unmapped_returns_none(tmp_path: Path) -> None:
    """A non-canonical path → None → caller drops to in-process arm."""
    weird = tmp_path / "weird" / "config.yaml"
    weird.parent.mkdir(parents=True)
    weird.write_text("")
    assert config_cmd._layer_label_for_path(weird) is None


# ---- config unset proxy + daemonless fallback -----------------------------


def test_unset_value_from_layer_proxies_through_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    config_yaml = repo / ".ea" / "config.yaml"
    config_yaml.parent.mkdir(parents=True)
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr(config_cmd, "_daemon_proxy_enabled", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._mutation._daemon_reachable", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeConfigClient)
    _FakeConfigClient.unset_last_args = None
    _FakeConfigClient.unset_call_count = 0

    result = config_cmd._unset_value_from_layer(
        target_path=config_yaml,
        key="audit.fix_safe",
        layer="repo",
        repo_root=repo,
    )

    assert result["removed"] is True
    assert _FakeConfigClient.unset_call_count == 1
    assert _FakeConfigClient.unset_last_args == {
        "layer": "repo",
        "key_path": ["audit", "fix_safe"],
        "idempotency_key": None,
        "repo_root": str(repo),
    }
    assert not config_yaml.exists()


def test_unset_value_from_layer_daemonless_prunes_empty_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_yaml = tmp_path / "repo" / ".ea" / "config.yaml"
    config_yaml.parent.mkdir(parents=True)
    config_yaml.write_text("audit:\n  fix_safe: true\nvcs:\n  auto_commit: false\n")
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")

    result = config_cmd._unset_value_from_layer(
        target_path=config_yaml,
        key="audit.fix_safe",
        layer="repo",
    )

    assert result["removed"] is True
    assert result["envelope"] is None
    assert yaml.safe_load(config_yaml.read_text()) == {"vcs": {"auto_commit": False}}


def test_unset_value_from_layer_absent_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_yaml = tmp_path / "repo" / ".ea" / "config.yaml"
    config_yaml.parent.mkdir(parents=True)
    config_yaml.write_text("vcs:\n  auto_commit: false\n")
    before = config_yaml.read_bytes()
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")

    result = config_cmd._unset_value_from_layer(
        target_path=config_yaml,
        key="audit.fix_safe",
        layer="repo",
    )

    assert result["removed"] is False
    assert config_yaml.read_bytes() == before


def test_unset_value_from_layer_rejects_unknown_or_locked_leaf(tmp_path: Path) -> None:
    config_yaml = tmp_path / "repo" / ".ea" / "config.yaml"
    with pytest.raises(ValueError, match="unknown config key"):
        config_cmd._unset_value_from_layer(
            target_path=config_yaml,
            key="not.real",
            layer="repo",
        )
    with pytest.raises(ValueError, match="not writable from the repo"):
        config_cmd._unset_value_from_layer(
            target_path=config_yaml,
            key="schema_version",
            layer="repo",
        )
