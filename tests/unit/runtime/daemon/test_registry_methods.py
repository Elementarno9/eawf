"""Tests for the ``registry.*`` JSON-RPC handlers.

Covers:

* :func:`registry.read` returns the parsed registry JSON.
* :func:`registry.update` (add) inserts a new entry and round-trips
  through the on-disk file.
* :func:`registry.update` (remove) drops an existing entry.
* :func:`registry.update` (rename) re-keys an entry.
* :func:`registry.update` publishes a ``registry_updated`` envelope.
* Idempotency: a repeat call with the same key replays the cached
  result.
* Concurrent writers serialise via portalock.

The handlers are driven through the module-level coroutines.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf import __version__
from eawf.kernel.state.enums import StoreKind
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.registry import read, update

pytestmark = pytest.mark.unit


def _build_ctx(*, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[MethodContext, Path]:
    """Wire a :class:`MethodContext` with a per-test registry path.

    Sets ``EAWF_REGISTRY_PATH`` so the daemon resolver lands on a
    ``tmp_path``-rooted file rather than the operator's real registry.
    """
    registry_path = tmp_path / "registry.json"
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(registry_path))
    ctx = MethodContext(
        started_at="2026-05-19T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        state_path=None,
        idempotency_cache={},
    )
    return ctx, registry_path


def _run(body: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(body())


# ---- registry.read ---------------------------------------------------------


def test_registry_read_missing_file_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        result: dict[str, Any] = await read(ctx, {})
        assert result["registry"]["repos"] == {}
        assert result["registry"]["version"] == "1"

    _run(body)


def test_registry_read_returns_parsed_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, registry_path = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)
    # Seed the file via update so the on-disk shape matches the schema.

    async def body() -> None:
        await update(
            ctx,
            {
                "operation": "add",
                "repo_id": "ABC",
                "fields": {"path": "/repos/abc", "title": "ABC"},
            },
        )
        result: dict[str, Any] = await read(ctx, {})
        assert "ABC" in result["registry"]["repos"]
        assert result["registry"]["repos"]["ABC"]["path"] == "/repos/abc"
        assert result["registry_path"] == str(registry_path)

    _run(body)


# ---- registry.update — add -------------------------------------------------


def test_update_add_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, registry_path = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        result: dict[str, Any] = await update(
            ctx,
            {
                "operation": "add",
                "repo_id": "ABC",
                "fields": {"path": "/repos/abc", "title": "ABC"},
            },
        )
        assert result["operation"] == "add"
        assert result["repo_id"] == "ABC"
        assert result["idempotent_replay"] is False
        # On-disk file present with the new entry.
        payload = orjson.loads(registry_path.read_bytes())
        assert payload["repos"]["ABC"]["path"] == "/repos/abc"

    _run(body)


def test_update_add_idempotent_same_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        await update(
            ctx,
            {
                "operation": "add",
                "repo_id": "ABC",
                "fields": {"path": "/repos/abc"},
            },
        )
        # Same code + same path = no schema change (different envelope id).
        await update(
            ctx,
            {
                "operation": "add",
                "repo_id": "ABC",
                "fields": {"path": "/repos/abc"},
            },
        )
        result: dict[str, Any] = await read(ctx, {})
        assert len(result["registry"]["repos"]) == 1

    _run(body)


def test_update_add_set_active_flips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        await update(
            ctx,
            {
                "operation": "add",
                "repo_id": "ABC",
                "fields": {"path": "/repos/abc"},
            },
        )
        await update(
            ctx,
            {
                "operation": "add",
                "repo_id": "DEF",
                "fields": {"path": "/repos/def", "set_active": True},
            },
        )
        result: dict[str, Any] = await read(ctx, {})
        assert result["registry"]["active_code"] == "DEF"

    _run(body)


def test_update_add_conflicting_path_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        await update(
            ctx,
            {
                "operation": "add",
                "repo_id": "ABC",
                "fields": {"path": "/repos/abc"},
            },
        )
        with pytest.raises(ValueError, match="already registered"):
            await update(
                ctx,
                {
                    "operation": "add",
                    "repo_id": "ABC",
                    "fields": {"path": "/repos/different"},
                },
            )

    _run(body)


# ---- registry.update — remove ----------------------------------------------


def test_update_remove_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, registry_path = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        await update(
            ctx,
            {
                "operation": "add",
                "repo_id": "ABC",
                "fields": {"path": "/repos/abc"},
            },
        )
        await update(ctx, {"operation": "remove", "repo_id": "ABC"})
        payload = orjson.loads(registry_path.read_bytes())
        assert "ABC" not in payload["repos"]

    _run(body)


def test_update_remove_missing_repo_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        with pytest.raises(ValueError, match="not registered"):
            await update(ctx, {"operation": "remove", "repo_id": "MISSING"})

    _run(body)


# ---- registry.update — rename ----------------------------------------------


def test_update_rename_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        await update(
            ctx,
            {
                "operation": "add",
                "repo_id": "ABC",
                "fields": {"path": "/repos/abc", "title": "Title"},
            },
        )
        await update(
            ctx,
            {
                "operation": "rename",
                "repo_id": "ABC",
                "fields": {"new_code": "XYZ"},
            },
        )
        result: dict[str, Any] = await read(ctx, {})
        assert "ABC" not in result["registry"]["repos"]
        assert "XYZ" in result["registry"]["repos"]
        assert result["registry"]["repos"]["XYZ"]["title"] == "Title"
        assert result["registry"]["repos"]["XYZ"]["path"] == "/repos/abc"

    _run(body)


def test_update_rename_missing_new_code_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        await update(
            ctx,
            {
                "operation": "add",
                "repo_id": "ABC",
                "fields": {"path": "/repos/abc"},
            },
        )
        with pytest.raises(ValueError, match=r"new_code.*required"):
            await update(ctx, {"operation": "rename", "repo_id": "ABC", "fields": {}})

    _run(body)


def test_update_rename_target_already_exists_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        await update(
            ctx,
            {
                "operation": "add",
                "repo_id": "ABC",
                "fields": {"path": "/repos/abc"},
            },
        )
        await update(
            ctx,
            {
                "operation": "add",
                "repo_id": "XYZ",
                "fields": {"path": "/repos/xyz"},
            },
        )
        with pytest.raises(ValueError, match="already registered"):
            await update(
                ctx,
                {
                    "operation": "rename",
                    "repo_id": "ABC",
                    "fields": {"new_code": "XYZ"},
                },
            )

    _run(body)


# ---- registry.update — bus publish + idempotency ---------------------------


def test_update_publishes_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)
    bus = ctx.bus
    assert isinstance(bus, EventBus)
    sub = bus.register(connection_id="reg-sub")

    async def body() -> None:
        await update(
            ctx,
            {
                "operation": "add",
                "repo_id": "ABC",
                "fields": {"path": "/repos/abc"},
            },
        )
        assert len(sub.queue) == 1
        env = sub.queue[0]
        assert env.kind is StoreKind.REGISTRY_UPDATED
        assert env.payload["operation"] == "add"
        assert env.payload["repo_id"] == "ABC"

    _run(body)


def test_update_idempotency_replays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        first: dict[str, Any] = await update(
            ctx,
            {
                "operation": "add",
                "repo_id": "ABC",
                "fields": {"path": "/repos/abc"},
                "idempotency_key": "key-1",
            },
        )
        # Same key, different payload — cached result wins.
        second: dict[str, Any] = await update(
            ctx,
            {
                "operation": "remove",
                "repo_id": "ABC",
                "idempotency_key": "key-1",
            },
        )
        assert first["idempotent_replay"] is False
        assert second["idempotent_replay"] is True
        # The cached "add" envelope replayed; "ABC" still on disk.
        result: dict[str, Any] = await read(ctx, {})
        assert "ABC" in result["registry"]["repos"]

    _run(body)


def test_update_unknown_operation_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown operation"):
            await update(ctx, {"operation": "delete", "repo_id": "ABC"})

    _run(body)


def test_update_sequential_writes_preserve_both_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sequential ``add`` calls round-trip without clobbering each other.

    POSIX ``flock`` is per-process not per-thread; a true thread-
    concurrency test would not actually serialise via portalock. The
    cross-process serialisation contract is asserted end-to-end in
    the integration suite. This test pins the simpler invariant: two
    adds in succession both land on disk.
    """
    ctx, registry_path = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        await update(
            ctx,
            {
                "operation": "add",
                "repo_id": "ABC",
                "fields": {"path": "/repos/abc"},
            },
        )
        await update(
            ctx,
            {
                "operation": "add",
                "repo_id": "DEF",
                "fields": {"path": "/repos/def"},
            },
        )

    _run(body)

    payload = orjson.loads(registry_path.read_bytes())
    assert set(payload["repos"].keys()) == {"ABC", "DEF"}
