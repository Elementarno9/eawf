"""A cutover carries every MCP server and grant into the generation, and the counts balance.

The pinned corpus holds no MCP registry. The suite adds two servers and two
grants to it and applies the cutover, so the rows it reads back were
written by the production importer, and a grant naming a server the
source never held refuses the plan.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.migration.epoch2.errors import MigrationCountMismatchError
from eawf.kernel.migration.epoch2.mcp import McpImportCensus, check_mcp_import
from eawf.kernel.migration.epoch2.plan import CorpusImportPlan
from eawf.kernel.migration.epoch2.snapshot import SourceSnapshot
from eawf.kernel.store.compaction import read_document
from eawf.runtime.mcp.book import generation_document, granted, read_grants, read_servers
from tests.integration.kernel.migration._cutover_harness import (
    ALLOWLIST,
    APPLIED_AT,
    apply_once,
    declared_canary,
    staged_corpus,
)


def _server(server_id: str) -> dict[str, Any]:
    return {
        "id": server_id,
        "owner": "eawf",
        "command": f"{server_id}-mcp",
        "args": ["--stdio"],
        "env_refs": ["${ENV:DEMO_KEY}"],
        "risk": "read",
        "write_capable": False,
        "status": "installed",
        "installed_targets": ["claude"],
    }


def _grant(grant_id: str, server_id: str) -> dict[str, Any]:
    return {
        "id": grant_id,
        "scope_kind": "wave",
        "scope_id": "P01-I01-W01",
        "server_id": server_id,
        "granted_at": "2026-01-15T00:00:00Z",
    }


def _seed_registry(corpus: Path, grants: dict[str, dict[str, Any]]) -> None:
    document_path = corpus / "document.json"
    document = json.loads(document_path.read_text(encoding="utf-8"))
    document["mcp_servers"] = {sid: _server(sid) for sid in ("fs", "search")}
    document["mcp_grants"] = grants
    document_path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")


def test_cutover_imports_the_mcp_registry_with_reconciling_counts(tmp_path: Path) -> None:
    corpus = staged_corpus(tmp_path)
    _seed_registry(corpus, {"GRANT-1": _grant("GRANT-1", "fs"), "GRANT-2": _grant("GRANT-2", "fs")})

    plan = CorpusImportPlan.build(snapshot=SourceSnapshot.read(corpus), allowlist_path=ALLOWLIST)
    assert plan.native.mcp == McpImportCensus(
        server_rows=2,
        capability_records=2,
        grant_rows=2,
        tool_authority_records=2,
        unresolved_grants=0,
    )

    target_root = declared_canary(tmp_path / ".ea")
    apply_once(corpus=corpus, target_root=target_root, applied_at=APPLIED_AT)
    document_path = generation_document(target_root)
    assert document_path is not None
    document = read_document(document_path)

    servers = read_servers(document)
    assert sorted(servers) == ["fs", "search"]
    assert all(s.revision == 1 for s in servers.values())
    fs = servers["fs"].server
    assert fs is not None
    assert fs.env_refs == ["${ENV:DEMO_KEY}"]
    assert fs.installed_targets == ["claude"]
    grants = granted(read_grants(document))
    assert sorted(grants) == ["GRANT-1", "GRANT-2"]
    assert {g.server_id for g in grants.values()} == {"fs"}


def test_a_grant_naming_an_unregistered_server_refuses_the_plan(tmp_path: Path) -> None:
    """Error path: the import never writes a grant wider than the source recorded."""
    corpus = staged_corpus(tmp_path)
    _seed_registry(corpus, {"GRANT-1": _grant("GRANT-1", "ghost")})
    with pytest.raises(MigrationCountMismatchError, match="name a server"):
        CorpusImportPlan.build(snapshot=SourceSnapshot.read(corpus), allowlist_path=ALLOWLIST)


def test_an_empty_registry_balances_at_zero(tmp_path: Path) -> None:
    """Boundary: a tree that never registered a server imports nothing."""
    corpus = staged_corpus(tmp_path)
    plan = CorpusImportPlan.build(snapshot=SourceSnapshot.read(corpus), allowlist_path=ALLOWLIST)
    assert plan.native.mcp == McpImportCensus(
        server_rows=0,
        capability_records=0,
        grant_rows=0,
        tool_authority_records=0,
        unresolved_grants=0,
    )


@pytest.mark.parametrize(
    ("census", "message"),
    [
        (
            McpImportCensus(
                server_rows=2,
                capability_records=1,
                grant_rows=0,
                tool_authority_records=0,
                unresolved_grants=0,
            ),
            "capability records",
        ),
        (
            McpImportCensus(
                server_rows=0,
                capability_records=0,
                grant_rows=1,
                tool_authority_records=2,
                unresolved_grants=0,
            ),
            "tool_authority records",
        ),
    ],
)
def test_an_unbalanced_census_is_refused(census: McpImportCensus, message: str) -> None:
    with pytest.raises(MigrationCountMismatchError, match=message):
        check_mcp_import(census)
