"""A native mutator never runs against a tree that is not in epoch 2.

The probe is registered through the production fence and, if it ever ran,
would write a file into the addressed tree. Every epoch-1 variant --
a plain production tree, a declared canary nobody activated, an activated
tree nobody declared, a tree whose marker does not parse -- is dispatched
through the production method registry, and each must be refused with the
one stable code while every byte of the tree, lock files included, stays
exactly as it was. A provisioned canary is the positive control: the same
probe runs there, receives the epoch-2 answer, and its write lands.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.migration.epoch2.canary import CANARY_DECLARATION_FILENAME
from eawf.kernel.migration.epoch2.generation import tree_digests
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.platform.install.canary import canary_ref, provision_canary
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import VALIDATION_FAILED, DaemonValidationError, MethodContext
from eawf.runtime.daemon.native_guard import (
    NativeAuthorityRefusedError,
    native_mutator,
    native_root,
)
from eawf.runtime.daemon.server import _process_frame

PROBE_METHOD = "domain.fence_probe.write"
PROBE_FILENAME = "native-probe.json"
PROVISIONED_AT = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class Probe:
    """The registered probe and every call that reached its body."""

    calls: list[RootAuthority] = field(default_factory=list)


@pytest.fixture(autouse=True)
def canary_runtime_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Allocate every canary runtime directory under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> Iterator[Probe]:
    """Register a native probe in a private copy of the method registry."""
    monkeypatch.setattr(methods, "_REGISTRY", dict(methods._REGISTRY))
    recorded = Probe()

    @native_mutator(PROBE_METHOD)
    async def write_probe(
        ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
    ) -> dict[str, Any]:
        del ctx
        recorded.calls.append(authority)
        target = Path(str(params.get("repo_root", authority.root.parent))) / ".ea"
        (target / PROBE_FILENAME).write_text('{"wrote": true}\n', encoding="utf-8")
        return {"generation_id": authority.generation_id}

    yield recorded


def _ctx(state_path: Path | None = None) -> MethodContext:
    return MethodContext(
        started_at="2026-01-01T00:00:00+00:00",
        pid=1,
        protocol_version="1",
        version="test",
        state_path=state_path,
    )


def _production_tree(repo: Path) -> Path:
    """Build an epoch-1 tree with authority surfaces worth protecting."""
    ea = repo / ".ea"
    (ea / "store").mkdir(parents=True)
    (ea / "state.json").write_text('{"schema_version": "1.20"}\n', encoding="utf-8")
    (ea / "config.yaml").write_text("schema_version: '1.0'\n", encoding="utf-8")
    (ea / "store" / "audit.jsonl").write_text('{"id": "AUD-1"}\n', encoding="utf-8")
    return repo


def _declared_only(repo: Path) -> Path:
    _production_tree(repo)
    declaration = {"declared_by": "fence-test", "disposable": True, "purpose": "throwaway"}
    (repo / ".ea" / CANARY_DECLARATION_FILENAME).write_text(
        json.dumps(declaration), encoding="utf-8"
    )
    return repo


def _canary(repo: Path) -> Path:
    provision_canary(repo_root=repo, ref=canary_ref("FENCE"), provisioned_at=PROVISIONED_AT)
    return repo


def _marked_only(repo: Path) -> Path:
    _canary(repo)
    (repo / ".ea" / CANARY_DECLARATION_FILENAME).unlink()
    return repo


def _marker_unreadable(repo: Path) -> Path:
    _canary(repo)
    (repo / ".ea" / "generations" / "EPOCH2_ACTIVE.json").write_text("{", encoding="utf-8")
    return repo


EPOCH1_TREES = [
    pytest.param(_production_tree, "undeclared", id="production-tree"),
    pytest.param(_declared_only, "marker_absent", id="declared-not-activated"),
    pytest.param(_marked_only, "undeclared", id="activated-not-declared"),
    pytest.param(_marker_unreadable, "marker_unreadable", id="marker-unreadable"),
]


def _dispatch(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(methods.dispatch(PROBE_METHOD, ctx, params))


@pytest.mark.parametrize(("build", "gap"), EPOCH1_TREES)
def test_native_mutator_epoch1_root_refused_with_zero_writes(
    tmp_path: Path, probe: Probe, build: Any, gap: str
) -> None:
    repo = build(tmp_path / "repo")
    before = tree_digests(repo)

    with pytest.raises(NativeAuthorityRefusedError) as caught:
        _dispatch(_ctx(), {"repo_root": str(repo)})

    assert caught.value.code == "native_authority_required"
    assert isinstance(caught.value, DaemonValidationError)
    assert str(caught.value).startswith("validation_failed: native_authority_required: ")
    assert gap in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    assert probe.calls == []
    assert tree_digests(repo) == before


def test_native_mutator_bound_epoch1_root_refused_with_zero_writes(
    tmp_path: Path, probe: Probe
) -> None:
    repo = _production_tree(tmp_path / "repo")
    before = tree_digests(repo)

    with pytest.raises(NativeAuthorityRefusedError, match="native_authority_required"):
        _dispatch(_ctx(state_path=repo / ".ea" / "state.json"), {})

    assert probe.calls == []
    assert tree_digests(repo) == before


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({}, id="no-root-no-binding"),
        pytest.param({"repo_root": ""}, id="empty-root"),
        pytest.param({"repo_root": 7}, id="non-string-root"),
    ],
)
def test_native_mutator_unaddressable_request_refused(probe: Probe, params: dict[str, Any]) -> None:
    with pytest.raises(NativeAuthorityRefusedError, match="native_authority_required"):
        _dispatch(_ctx(), params)

    assert probe.calls == []


def test_native_mutator_refusal_maps_to_validation_failed_on_the_wire(
    tmp_path: Path, probe: Probe
) -> None:
    repo = _production_tree(tmp_path / "repo")
    before = tree_digests(repo)
    frame = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": PROBE_METHOD, "params": {"repo_root": str(repo)}}
    ).encode()

    response = asyncio.run(_process_frame(frame, _ctx()))

    assert response["error"]["code"] == VALIDATION_FAILED
    assert "native_authority_required" in response["error"]["message"]
    assert probe.calls == []
    assert tree_digests(repo) == before


def test_native_mutator_epoch2_canary_reaches_handler(tmp_path: Path, probe: Probe) -> None:
    repo = _canary(tmp_path / "repo")

    result = _dispatch(_ctx(), {"repo_root": str(repo)})

    assert len(probe.calls) == 1
    assert probe.calls[0].epoch == 2
    assert result == {"generation_id": probe.calls[0].generation_id}
    assert (repo / ".ea" / PROBE_FILENAME).is_file()


def test_native_mutator_epoch2_bound_root_reaches_handler(tmp_path: Path, probe: Probe) -> None:
    repo = _canary(tmp_path / "repo")

    _dispatch(_ctx(state_path=repo / ".ea" / "state.json"), {})

    assert [call.epoch for call in probe.calls] == [2]


def test_native_mutator_duplicate_name_is_rejected(probe: Probe) -> None:
    async def again(
        ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
    ) -> dict[str, Any]:
        return {}

    with pytest.raises(ValueError, match="already registered"):
        native_mutator(PROBE_METHOD)(again)


def test_native_root_prefers_named_root_over_binding(tmp_path: Path) -> None:
    named = tmp_path / "named"
    bound = tmp_path / "bound" / ".ea" / "state.json"

    assert native_root(_ctx(state_path=bound), {"repo_root": str(named)}) == named / ".ea"
    assert native_root(_ctx(state_path=bound), {}) == bound.parent
    assert native_root(_ctx(), {}) is None
