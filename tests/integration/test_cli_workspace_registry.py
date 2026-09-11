"""Integration tests for the ``eawf workspace`` record verbs.

Covers the packet surface that reads and writes ``WorkspaceRecord``
rows in the user registry:

- ``workspace add`` / ``member add`` / ``member remove`` - mutations,
  through both the daemonless local arm and the daemon-proxy arm.
- ``workspace show`` / ``list`` - reads, including the resolution
  refusals (``workspace_ambiguous`` / ``workspace_not_registered``).
- ``workspace select`` - session-local only: no registry byte moves and
  no state document appears.

The daemon-proxy arm is exercised by pointing the CLI's
:class:`DaemonClient` at the real ``registry.workspace.*`` coroutines
running in-process, so the test proves the two arms agree on the strict
model rather than mocking the result.

Every command is given ``--registry-path`` under ``tmp_path``; the
operator's real ``~/.eawf/registry.json`` is never opened.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf import __version__
from eawf.platform.registry import WorkspaceRecord, read_registry
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.registry_workspace import (
    create as rpc_create,
)
from eawf.runtime.daemon.methods.registry_workspace import (
    get as rpc_get,
)
from eawf.runtime.daemon.methods.registry_workspace import (
    list_method as rpc_list,
)
from eawf.runtime.daemon.methods.registry_workspace import (
    update_membership_method as rpc_update_membership,
)
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands.workspace import SESSION_WORKSPACE_ENV

runner = CliRunner()

#: The ``registry.workspace.*`` coroutines the in-process daemon stub serves.
_RPC_HANDLERS = {
    "registry.workspace.create": rpc_create,
    "registry.workspace.update_membership": rpc_update_membership,
    "registry.workspace.get": rpc_get,
    "registry.workspace.list": rpc_list,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx() -> MethodContext:
    """Build a daemon method context with a live bus."""
    return MethodContext(
        started_at="2026-09-08T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        state_path=None,
        idempotency_cache={},
    )


def _rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Drive one ``registry.workspace.*`` handler in-process."""
    return asyncio.run(_RPC_HANDLERS[method](_ctx(), params))


class _InProcessDaemonClient:
    """Stand-in for :class:`DaemonClient` that runs the real handlers.

    The CLI's daemon arm is otherwise untestable without spawning a
    daemon; routing ``call`` at the registered coroutines keeps the
    params + result contract honest while staying in-process.
    """

    def __enter__(self) -> _InProcessDaemonClient:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return _rpc(method, params or {})


@pytest.fixture(autouse=True)
def _isolated_session(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force the daemonless arm and neutralise the session selection.

    ``monkeypatch.setenv`` registers both variables for restore, so a
    ``workspace select`` that writes ``EAWF_WORKSPACE_KEY`` directly
    cannot leak into a sibling test.
    """
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    monkeypatch.setenv(SESSION_WORKSPACE_ENV, "")
    yield


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    """Return the tmp registry file every command is pointed at."""
    return tmp_path / "eawf" / "registry.json"


def _invoke(*args: str) -> Any:
    """Run the CLI with JSON output and return the completed result."""
    return runner.invoke(app, ["--json", "workspace", *args])


def _payload(result: Any) -> dict[str, Any]:
    """Parse the JSON envelope a successful command printed."""
    return json.loads(result.stdout)


def _add(registry_path: Path, key: str, *, home: str, members: tuple[str, ...] = ()) -> Any:
    """Register one workspace through the CLI."""
    args = ["add", key, "--home", home]
    for code in members:
        args += ["--member", code]
    args += ["--registry-path", str(registry_path)]
    return _invoke(*args)


def _seed_repo(registry_path: Path, code: str, root: Path) -> None:
    """Write a repo entry straight into the registry file.

    Repo registration is ``eawf repo add``'s job; this test only needs
    the pointer row to exist so resolution has something to match.
    """
    payload: dict[str, Any] = (
        orjson.loads(registry_path.read_bytes())
        if registry_path.exists()
        else {"version": "1", "updated_at": "2026-09-08T00:00:00+00:00", "repos": {}}
    )
    payload.setdefault("repos", {})[code] = {"code": code, "path": str(root)}
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_bytes(orjson.dumps(payload))


# ---------------------------------------------------------------------------
# workspace add
# ---------------------------------------------------------------------------


def test_workspace_add_registers_a_record(registry_path: Path) -> None:
    result = _add(registry_path, "MONO", home="EAWF", members=("DEMO",))
    assert result.exit_code == 0, result.stdout
    body = _payload(result)
    assert body["key"] == "MONO"
    assert body["members"] == ["DEMO", "EAWF"]
    assert body["home"] == "EAWF"
    assert body["revision"] == 1
    stored = read_registry(path=registry_path).workspaces["MONO"]
    assert stored.member_project_codes == frozenset({"DEMO", "EAWF"})


def test_workspace_add_implies_the_home_repo_as_a_member(registry_path: Path) -> None:
    assert _add(registry_path, "SOLO", home="EAWF").exit_code == 0
    assert _payload(_invoke("show", "SOLO", "--registry-path", str(registry_path)))["members"] == [
        "EAWF"
    ]


def test_workspace_add_refuses_a_duplicate_key(registry_path: Path) -> None:
    assert _add(registry_path, "MONO", home="EAWF").exit_code == 0
    result = _add(registry_path, "MONO", home="EAWF")
    assert result.exit_code == 1
    assert _payload(result)["data"]["code"] == "workspace_already_registered"


def test_workspace_add_refuses_a_malformed_key(registry_path: Path) -> None:
    result = _add(registry_path, "lower", home="EAWF")
    assert result.exit_code == 1
    assert "not a project code" in result.stdout
    assert not registry_path.exists()


# ---------------------------------------------------------------------------
# workspace member add / remove
# ---------------------------------------------------------------------------


def test_workspace_member_add_bumps_the_revision(registry_path: Path) -> None:
    assert _add(registry_path, "MONO", home="EAWF").exit_code == 0
    result = _invoke("member", "add", "MONO", "DEMO", "--registry-path", str(registry_path))
    assert result.exit_code == 0, result.stdout
    body = _payload(result)
    assert body["members"] == ["DEMO", "EAWF"]
    assert body["revision"] == 2


def test_workspace_member_remove_drops_the_code(registry_path: Path) -> None:
    assert _add(registry_path, "MONO", home="EAWF", members=("DEMO",)).exit_code == 0
    result = _invoke("member", "remove", "MONO", "DEMO", "--registry-path", str(registry_path))
    assert result.exit_code == 0, result.stdout
    assert _payload(result)["members"] == ["EAWF"]


def test_workspace_member_remove_refuses_the_home_repo(registry_path: Path) -> None:
    assert _add(registry_path, "MONO", home="EAWF", members=("DEMO",)).exit_code == 0
    result = _invoke("member", "remove", "MONO", "EAWF", "--registry-path", str(registry_path))
    assert result.exit_code == 1
    assert "home_project_code" in result.stdout


def test_workspace_member_add_refuses_an_unregistered_workspace(registry_path: Path) -> None:
    result = _invoke("member", "add", "GHOST", "DEMO", "--registry-path", str(registry_path))
    assert result.exit_code == 1
    assert _payload(result)["data"]["code"] == "workspace_not_registered"


# ---------------------------------------------------------------------------
# workspace show / list
# ---------------------------------------------------------------------------


def test_workspace_show_resolves_from_an_exact_repo_root(
    registry_path: Path, tmp_path: Path
) -> None:
    root = tmp_path / "eawf-repo"
    root.mkdir()
    _seed_repo(registry_path, "EAWF", root)
    assert _add(registry_path, "MONO", home="EAWF").exit_code == 0
    result = _invoke("show", "--repo-root", str(root), "--registry-path", str(registry_path))
    assert result.exit_code == 0, result.stdout
    body = _payload(result)
    assert body["key"] == "MONO"
    assert body["source"] == "repo_root"


def test_workspace_show_refuses_an_ambiguous_root(registry_path: Path, tmp_path: Path) -> None:
    root = tmp_path / "eawf-repo"
    root.mkdir()
    _seed_repo(registry_path, "EAWF", root)
    assert _add(registry_path, "MONO", home="EAWF").exit_code == 0
    assert _add(registry_path, "ALT", home="EAWF").exit_code == 0
    result = _invoke("show", "--repo-root", str(root), "--registry-path", str(registry_path))
    assert result.exit_code == 1
    body = _payload(result)
    assert body["data"]["code"] == "workspace_ambiguous"
    assert body["data"]["candidates"] == ["ALT", "MONO"]


def test_workspace_show_refuses_an_unregistered_root(registry_path: Path, tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    _seed_repo(registry_path, "EAWF", parent)
    assert _add(registry_path, "MONO", home="EAWF").exit_code == 0
    result = _invoke("show", "--repo-root", str(child), "--registry-path", str(registry_path))
    assert result.exit_code == 1
    assert _payload(result)["data"]["code"] == "workspace_not_registered"


def test_workspace_list_is_empty_without_a_registry(registry_path: Path) -> None:
    result = _invoke("list", "--registry-path", str(registry_path))
    assert result.exit_code == 0, result.stdout
    assert _payload(result) == {
        "count": 0,
        "workspaces": [],
        "registry_path": str(registry_path),
    }


def test_workspace_list_is_ordered_by_key(registry_path: Path) -> None:
    for key in ("ZED", "ABC", "MID"):
        assert _add(registry_path, key, home="EAWF").exit_code == 0
    body = _payload(_invoke("list", "--registry-path", str(registry_path)))
    assert [row["key"] for row in body["workspaces"]] == ["ABC", "MID", "ZED"]


# ---------------------------------------------------------------------------
# workspace select
# ---------------------------------------------------------------------------


def test_workspace_select_writes_no_registry_or_state_row(
    registry_path: Path, tmp_path: Path
) -> None:
    assert _add(registry_path, "MONO", home="EAWF").exit_code == 0
    before = registry_path.read_bytes()
    result = _invoke("select", "MONO", "--registry-path", str(registry_path))
    assert result.exit_code == 0, result.stdout
    body = _payload(result)
    assert body == {
        "key": "MONO",
        "source": "explicit",
        "export": f"{SESSION_WORKSPACE_ENV}=MONO",
        "persisted": False,
    }
    assert registry_path.read_bytes() == before
    assert not (tmp_path / ".ea").exists()
    assert os.environ[SESSION_WORKSPACE_ENV] == "MONO"


def test_workspace_select_feeds_the_session_rung_of_show(
    registry_path: Path, tmp_path: Path
) -> None:
    root = tmp_path / "eawf-repo"
    root.mkdir()
    _seed_repo(registry_path, "EAWF", root)
    assert _add(registry_path, "MONO", home="EAWF").exit_code == 0
    assert _add(registry_path, "ALT", home="EAWF").exit_code == 0
    assert _invoke("select", "ALT", "--registry-path", str(registry_path)).exit_code == 0
    body = _payload(
        _invoke("show", "--repo-root", str(root), "--registry-path", str(registry_path))
    )
    assert body["key"] == "ALT"
    assert body["source"] == "environment"


def test_workspace_select_refuses_an_unregistered_key(registry_path: Path) -> None:
    result = _invoke("select", "GHOST", "--registry-path", str(registry_path))
    assert result.exit_code == 1
    assert _payload(result)["data"]["code"] == "workspace_not_registered"


# ---------------------------------------------------------------------------
# CLI / RPC parity
# ---------------------------------------------------------------------------


def test_workspace_rpc_and_cli_share_the_strict_model(registry_path: Path) -> None:
    """A record minted by the RPC reads back identically through the CLI."""
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    record = WorkspaceRecord(
        key="MONO",
        title="Mono",
        member_project_codes=frozenset({"EAWF", "DEMO"}),
        home_project_code="EAWF",
    )
    _rpc(
        "registry.workspace.create",
        {"record": record.model_dump(mode="json"), "registry_path": str(registry_path)},
    )
    body = _payload(_invoke("show", "MONO", "--registry-path", str(registry_path)))
    assert body["members"] == ["DEMO", "EAWF"]
    assert body["home"] == "EAWF"
    rpc_body = _rpc("registry.workspace.get", {"key": "MONO", "registry_path": str(registry_path)})
    assert WorkspaceRecord.model_validate(rpc_body["workspace"]) == record


def test_workspace_rpc_rejects_an_unknown_record_key(registry_path: Path) -> None:
    with pytest.raises(ValueError, match="validation_failed"):
        _rpc(
            "registry.workspace.create",
            {
                "record": {
                    "key": "MONO",
                    "member_project_codes": ["EAWF"],
                    "home_project_code": "EAWF",
                    "owner": "nobody",
                },
                "registry_path": str(registry_path),
            },
        )


def test_workspace_rpc_refuses_a_stale_revision(registry_path: Path) -> None:
    assert _add(registry_path, "MONO", home="EAWF").exit_code == 0
    with pytest.raises(ValueError, match="workspace_revision_conflict"):
        _rpc(
            "registry.workspace.update_membership",
            {
                "key": "MONO",
                "add": ["DEMO"],
                "expected_revision": 99,
                "registry_path": str(registry_path),
            },
        )


def test_workspace_rpc_list_matches_the_cli_list(registry_path: Path) -> None:
    assert _add(registry_path, "MONO", home="EAWF", members=("DEMO",)).exit_code == 0
    rpc_body = _rpc("registry.workspace.list", {"registry_path": str(registry_path)})
    cli_body = _payload(_invoke("list", "--registry-path", str(registry_path)))
    assert [row["key"] for row in rpc_body["workspaces"]] == [
        row["key"] for row in cli_body["workspaces"]
    ]
    assert rpc_body["workspaces"][0]["member_project_codes"] == cli_body["workspaces"][0]["members"]


def test_workspace_add_via_the_daemon_arm_matches_the_local_arm(
    registry_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the proxy on, the CLI consumes the RPC result rather than writing."""
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr("eawf.surfaces.cli._mutation._proxy_enabled", lambda _: True)
    monkeypatch.setattr("eawf.surfaces.cli._mutation._daemon_reachable", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _InProcessDaemonClient)
    result = _add(registry_path, "MONO", home="EAWF", members=("DEMO",))
    assert result.exit_code == 0, result.stdout
    assert _payload(result)["members"] == ["DEMO", "EAWF"]
    # The daemon arm wrote the file, so the record is durable either way.
    assert read_registry(path=registry_path).workspaces["MONO"].revision == 1
