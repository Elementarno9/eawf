"""CLI tests for ``eawf mcp grant`` / ``eawf mcp revoke`` on an epoch-2 tree.

Each handler sends one native ``mcp.*`` verb; the daemon writes the grant
as a ``tool_authority`` row of the selected generation's document and
refuses a grant naming a server that is not registered.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.kernel.store.compaction import read_document
from eawf.runtime.mcp.book import StandingGrant, generation_document, read_grants
from eawf.surfaces.cli.app import app
from tests.integration._memory_native import native_memory_tree

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.fixture
def tmp_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield from native_memory_tree(tmp_path, monkeypatch)


def _add_server(server_id: str = "filesystem") -> None:
    res = runner.invoke(
        app,
        [
            "mcp",
            "add",
            server_id,
            "--command",
            "/usr/local/bin/mcp",
            "--idempotency-key",
            f"add-{server_id}",
        ],
    )
    assert res.exit_code == 0, res.output


def _grant(*argv: str, key: str) -> Any:
    return runner.invoke(app, ["--json", "mcp", "grant", *argv, "--idempotency-key", key])


def _revoke(grant_id: str, revision: int = 1, *, json_mode: bool = True) -> Any:
    argv = ["mcp", "revoke", grant_id, "--expected-revision", str(revision)]
    argv += ["--idempotency-key", f"revoke-{grant_id}-{revision}"]
    return runner.invoke(app, ["--json", *argv] if json_mode else argv)


def _grants(state_path: Path) -> dict[str, StandingGrant]:
    document = generation_document(state_path.parent)
    assert document is not None, "the tree answers in epoch 2"
    return read_grants(read_document(document))


# ---- grant ---------------------------------------------------------------------


def test_grant_cmd_writes_grant_and_emits_json_envelope(tmp_state: Path) -> None:
    _add_server("filesystem")
    frozen = tmp_state.read_bytes()
    res = _grant("wave", "P10-I01-W04", "filesystem", key="g1")
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["id"] == "GRANT-1"
    assert payload["scope_kind"] == "wave"
    assert payload["scope_id"] == "P10-I01-W04"
    assert payload["server_id"] == "filesystem"
    assert payload["revision"] == 1

    row = _grants(tmp_state)["GRANT-1"]
    assert row.grant is not None
    assert row.grant.server_id == "filesystem"
    assert row.grant.granted_at == datetime.fromisoformat(payload["granted_at"])
    assert tmp_state.read_bytes() == frozen


def test_grant_cmd_text_mode_emits_single_line_summary(tmp_state: Path) -> None:
    _add_server("filesystem")
    res = runner.invoke(
        app,
        ["mcp", "grant", "wave", "P10-I01-W04", "filesystem", "--idempotency-key", "g"],
    )
    assert res.exit_code == 0, res.output
    body = res.output.strip()
    assert "\n" not in body
    assert body.startswith("mcp granted: GRANT-1")
    assert "wave=P10-I01-W04" in body


def test_grant_cmd_auto_increments_grant_ids(tmp_state: Path) -> None:
    _add_server("filesystem")
    _add_server("fs-write")
    a = _grant("wave", "P10-I01-W04", "filesystem", key="a")
    b = _grant("profile", "research", "fs-write", key="b")
    assert json.loads(a.output)["id"] == "GRANT-1"
    assert json.loads(b.output)["id"] == "GRANT-2"


def test_grant_cmd_honours_explicit_grant_id_override(tmp_state: Path) -> None:
    _add_server("filesystem")
    res = _grant("global", "global", "filesystem", "--grant-id", "GRANT-42", key="g")
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["id"] == "GRANT-42"
    nxt = _grant("global", "global", "filesystem", key="next")
    assert json.loads(nxt.output)["id"] == "GRANT-43"


def test_grant_cmd_replays_under_the_same_key(tmp_state: Path) -> None:
    _add_server("filesystem")
    first = _grant("wave", "P10-I01-W04", "filesystem", key="same")
    second = _grant("wave", "P10-I01-W04", "filesystem", key="same")
    assert json.loads(first.output) == json.loads(second.output)
    assert sorted(_grants(tmp_state)) == ["GRANT-1"]


def test_grant_cmd_rejects_unknown_scope_kind(tmp_state: Path) -> None:
    _add_server("filesystem")
    res = _grant("team", "research", "filesystem", key="g")
    assert res.exit_code == 1, res.output
    assert "scope_kind" in res.output


def test_grant_cmd_refuses_an_unregistered_server(tmp_state: Path) -> None:
    """Error path: no grant ever authorises a tool nobody registered."""
    res = _grant("wave", "P10-I01-W04", "ghost-server", key="g")
    assert res.exit_code == 1, res.output
    assert "mcp_server_not_found" in res.output
    assert _grants(tmp_state) == {}


def test_grant_cmd_rejects_duplicate_grant_id(tmp_state: Path) -> None:
    _add_server("filesystem")
    first = _grant("wave", "W", "filesystem", "--grant-id", "GRANT-7", key="one")
    assert first.exit_code == 0, first.output
    second = _grant("wave", "W", "filesystem", "--grant-id", "GRANT-7", key="two")
    assert second.exit_code == 1, second.output
    assert "already exists" in second.output


# ---- revoke --------------------------------------------------------------------


def test_revoke_cmd_revokes_and_keeps_the_row(tmp_state: Path) -> None:
    _add_server("filesystem")
    grant_id = json.loads(_grant("wave", "P10-I01-W04", "filesystem", key="g").output)["id"]
    res = _revoke(grant_id)
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["id"] == grant_id
    assert payload["removed_from_state"] is True
    assert payload["revision"] == 2
    row = _grants(tmp_state)[grant_id]
    assert row.grant is None
    assert row.revision == 2
    # A revoked id is never handed out again.
    again = _grant("wave", "P10-I01-W04", "filesystem", key="g2")
    assert json.loads(again.output)["id"] == "GRANT-2"


def test_revoke_cmd_keeps_other_grants_intact(tmp_state: Path) -> None:
    _add_server("filesystem")
    _add_server("fs-write")
    _grant("wave", "P10-I01-W04", "filesystem", key="a")
    _grant("profile", "research", "fs-write", key="b")
    assert _revoke("GRANT-1").exit_code == 0
    rows = _grants(tmp_state)
    assert rows["GRANT-1"].grant is None
    assert rows["GRANT-2"].grant is not None


def test_revoke_cmd_at_a_stale_revision_is_refused(tmp_state: Path) -> None:
    _add_server("filesystem")
    _grant("wave", "W", "filesystem", key="a")
    res = _revoke("GRANT-1", revision=3)
    assert res.exit_code != 0, res.output
    assert "revision_conflict" in res.output
    assert _grants(tmp_state)["GRANT-1"].grant is not None


def test_revoke_cmd_returns_not_found_on_missing_id(tmp_state: Path) -> None:
    res = _revoke("GRANT-404")
    assert res.exit_code == 1, res.output
    assert "GRANT-404" in res.output


def test_revoke_cmd_text_mode_one_line_summary(tmp_state: Path) -> None:
    _add_server("filesystem")
    _grant("wave", "P10-I01-W04", "filesystem", key="a")
    res = _revoke("GRANT-1", json_mode=False)
    assert res.exit_code == 0, res.output
    body = res.output.strip()
    assert body.startswith("mcp revoked: GRANT-1")
    assert "wave=P10-I01-W04" in body


def test_list_carries_standing_grants_with_their_revisions(tmp_state: Path) -> None:
    _add_server("filesystem")
    _grant("wave", "W", "filesystem", key="a")
    _grant("wave", "X", "filesystem", key="b")
    _revoke("GRANT-1")
    listed = json.loads(runner.invoke(app, ["--json", "mcp", "list"]).output)
    assert [(g["id"], g["revision"]) for g in listed["grants"]] == [("GRANT-2", 1)]
