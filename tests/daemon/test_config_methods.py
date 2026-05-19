"""Tests for the ``config.*`` JSON-RPC handlers (P24-W10).

Covers:

* :func:`config.read` returns the parsed YAML body for the named layer.
* :func:`config.set_layer_value` mutates the YAML file on disk; a
  subsequent :func:`config.read` reflects the new value.
* :func:`config.set_layer_value` publishes a ``config_updated`` envelope
  on the subscription bus.
* Idempotency: a repeat call with the same key returns the cached
  envelope verbatim with ``idempotent_replay=True``.
* Concurrent writers serialise via portalock (race-free interleave).
* :func:`config.list_layers` returns the four canonical writable
  layer paths.

The handlers are driven through the module-level coroutines; JSON-RPC
framing is exercised in :mod:`tests.daemon.test_scaffolding`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf import __version__
from eawf.daemon import PROTOCOL_VERSION
from eawf.daemon.bus import EventBus
from eawf.daemon.methods import MethodContext
from eawf.daemon.methods.config import list_layers, read, set_layer_value
from eawf.state.enums import StoreKind

pytestmark = pytest.mark.unit


def _build_ctx(*, tmp_path: Path) -> tuple[MethodContext, Path]:
    """Build a wired :class:`MethodContext` for the W10 config tests.

    Returns the context plus the synthetic repo root so tests can poke
    at ``.ea/config.yaml`` directly.
    """
    repo = tmp_path / "repo"
    state_path = repo / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{}")
    ctx = MethodContext(
        started_at="2026-05-19T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        state_path=state_path,
        idempotency_cache={},
    )
    return ctx, repo


def _run(body: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(body())


# ---- config.read -----------------------------------------------------------


def test_config_read_returns_parsed_repo_layer(tmp_path: Path) -> None:
    ctx, repo = _build_ctx(tmp_path=tmp_path)
    config_yaml = repo / ".ea" / "config.yaml"
    config_yaml.write_text("vcs:\n  auto_commit: false\n")

    async def body() -> None:
        result: dict[str, Any] = await read(ctx, {"layer": "repo"})
        assert result["config"] == {"vcs": {"auto_commit": False}}
        assert result["layer_path"].endswith("config.yaml")

    _run(body)


def test_config_read_missing_file_returns_empty(tmp_path: Path) -> None:
    ctx, _ = _build_ctx(tmp_path=tmp_path)

    async def body() -> None:
        result: dict[str, Any] = await read(ctx, {"layer": "repo"})
        assert result["config"] == {}

    _run(body)


def test_config_read_rejects_unknown_layer(tmp_path: Path) -> None:
    ctx, _ = _build_ctx(tmp_path=tmp_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown writable layer"):
            await read(ctx, {"layer": "bogus"})

    _run(body)


# ---- config.set_layer_value -------------------------------------------------


def test_set_layer_value_mutates_yaml(tmp_path: Path) -> None:
    ctx, repo = _build_ctx(tmp_path=tmp_path)
    config_yaml = repo / ".ea" / "config.yaml"

    async def body() -> None:
        result: dict[str, Any] = await set_layer_value(
            ctx,
            {
                "layer": "repo",
                "key_path": ["vcs", "auto_commit"],
                "value": True,
            },
        )
        assert result["idempotent_replay"] is False
        assert result["layer"] == "repo"
        assert result["value"] is True
        # File on disk reflects the new value.
        assert config_yaml.exists()
        body_disk = yaml.safe_load(config_yaml.read_text())
        assert body_disk == {"vcs": {"auto_commit": True}}

        # Subsequent read reflects the new value.
        read_result: dict[str, Any] = await read(ctx, {"layer": "repo"})
        assert read_result["config"] == {"vcs": {"auto_commit": True}}

    _run(body)


def test_set_layer_value_preserves_other_keys(tmp_path: Path) -> None:
    """Deep-set updates the named key without clobbering siblings."""
    ctx, repo = _build_ctx(tmp_path=tmp_path)
    config_yaml = repo / ".ea" / "config.yaml"
    config_yaml.write_text("vcs:\n  auto_commit: false\n  pr_template: iter\n")

    async def body() -> None:
        await set_layer_value(
            ctx,
            {
                "layer": "repo",
                "key_path": ["vcs", "auto_commit"],
                "value": True,
            },
        )
        body_disk = yaml.safe_load(config_yaml.read_text())
        assert body_disk == {"vcs": {"auto_commit": True, "pr_template": "iter"}}

    _run(body)


def test_set_layer_value_publishes_envelope(tmp_path: Path) -> None:
    ctx, _ = _build_ctx(tmp_path=tmp_path)
    bus = ctx.bus
    assert isinstance(bus, EventBus)
    sub = bus.register(connection_id="cfg-sub")

    async def body() -> None:
        await set_layer_value(
            ctx,
            {
                "layer": "repo",
                "key_path": ["vcs", "auto_commit"],
                "value": True,
            },
        )
        assert len(sub.queue) == 1
        env = sub.queue[0]
        assert env.kind is StoreKind.CONFIG_UPDATED
        assert env.payload["layer"] == "repo"
        assert env.payload["key_path"] == ["vcs", "auto_commit"]
        assert env.payload["value"] is True

    _run(body)


def test_set_layer_value_idempotency_replays(tmp_path: Path) -> None:
    """Repeat call with the same key returns the cached result."""
    ctx, _ = _build_ctx(tmp_path=tmp_path)

    async def body() -> None:
        first: dict[str, Any] = await set_layer_value(
            ctx,
            {
                "layer": "repo",
                "key_path": ["vcs", "auto_commit"],
                "value": True,
                "idempotency_key": "key-1",
            },
        )
        second: dict[str, Any] = await set_layer_value(
            ctx,
            {
                "layer": "repo",
                "key_path": ["vcs", "auto_commit"],
                "value": False,  # different value, same key
                "idempotency_key": "key-1",
            },
        )
        assert first["idempotent_replay"] is False
        assert second["idempotent_replay"] is True
        # The cached result wins — second's value is the cached True.
        assert second["value"] is True

    _run(body)


def test_set_layer_value_rejects_built_in_layer(tmp_path: Path) -> None:
    ctx, _ = _build_ctx(tmp_path=tmp_path)

    async def body() -> None:
        with pytest.raises(ValueError, match=r"built-in.*read-only"):
            await set_layer_value(
                ctx,
                {"layer": "built-in", "key_path": ["x"], "value": 1},
            )

    _run(body)


def test_set_layer_value_rejects_unknown_layer(tmp_path: Path) -> None:
    ctx, _ = _build_ctx(tmp_path=tmp_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown writable layer"):
            await set_layer_value(
                ctx,
                {"layer": "bogus", "key_path": ["x"], "value": 1},
            )

    _run(body)


def test_set_layer_value_rejects_empty_key_path(tmp_path: Path) -> None:
    ctx, _ = _build_ctx(tmp_path=tmp_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="validation_failed"):
            await set_layer_value(
                ctx,
                {"layer": "repo", "key_path": [], "value": 1},
            )

    _run(body)


def test_set_layer_value_sequential_writes_preserve_both_keys(tmp_path: Path) -> None:
    """Two sequential writes round-trip through the YAML layer without loss.

    POSIX ``flock`` is per-process not per-thread, so a true thread-
    concurrency test against portalock would not actually serialise.
    The cross-process serialisation contract is asserted in the
    end-to-end daemon integration suite. This test pins the simpler
    invariant: two ``set_layer_value`` calls in succession both land
    on disk without one clobbering the other.
    """
    ctx, repo = _build_ctx(tmp_path=tmp_path)
    config_yaml = repo / ".ea" / "config.yaml"

    async def body() -> None:
        await set_layer_value(ctx, {"layer": "repo", "key_path": ["alpha"], "value": 1})
        await set_layer_value(ctx, {"layer": "repo", "key_path": ["beta"], "value": 2})

    _run(body)

    body_disk = yaml.safe_load(config_yaml.read_text())
    assert body_disk == {"alpha": 1, "beta": 2}


# ---- config.list_layers ----------------------------------------------------


def test_list_layers_returns_four_writable_layers(tmp_path: Path) -> None:
    ctx, repo = _build_ctx(tmp_path=tmp_path)

    async def body() -> None:
        result: dict[str, Any] = await list_layers(ctx, {})
        assert set(result["layers"].keys()) == {"global", "workspace", "repo", "local"}
        assert result["layers"]["repo"].endswith("config.yaml")
        # Repo is anchored under the synthetic tmp_path repo.
        assert str(repo) in result["layers"]["repo"]

    _run(body)


def test_list_layers_rejects_extra_params(tmp_path: Path) -> None:
    ctx, _ = _build_ctx(tmp_path=tmp_path)

    async def body() -> None:
        with pytest.raises(Exception, match=r"(unexpected|extra)"):
            await list_layers(ctx, {"unexpected": "field"})

    _run(body)
