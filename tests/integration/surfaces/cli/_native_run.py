"""A native tree holding one Run bound under a sealed capsule, for the report-hook tests.

The tree is provisioned by the real canary birth, so it carries a selected
generation and no v1 ``state.json``; the Run sits in the generation document
and its binding, carrying the capsule that grants its role, on the run ledger.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.control import RunBinding
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import canary_ref, provision_canary

AT = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
CODE = "RPT"
PREFIX = f"eawf://WSP-{CODE}/PRJ-{CODE}/REP-{CODE}"
RUN_KEY = "RUN-00000001"
RUN_URN = f"{PREFIX}/run/{RUN_KEY}"
TASK_KEY = f"{CODE}-0001"
TASK_URN = f"{PREFIX}/task/{TASK_KEY}"


def run_urn(key: str) -> str:
    """Return the URN of Run ``key`` in the fixture repository."""
    return f"{PREFIX}/run/{key}"


def run_row(key: str) -> dict[str, Any]:
    """Return one completed, task-scoped Run row as the document stores it."""
    return {
        "uid": "0a1b2c3d-0000-5000-8000-000000000000",
        "key": key,
        "urn": run_urn(key),
        "origin": {
            "confidence": "exact",
            "kind": "native",
            "mapping_basis": "native",
            "source_digest": None,
            "source_id": None,
            "source_kind": None,
            "source_schema_version": None,
            "source_urn": None,
        },
        "revision": 3,
        "created_at": AT.isoformat(),
        "updated_at": AT.isoformat(),
        "scope": {
            "purpose": "implement",
            "scope_kind": "task",
            "task_ref": TASK_URN,
            "write_set": ["src"],
        },
        "status": "COMPLETED",
        "started_at": AT.isoformat(),
        "ended_at": (AT + timedelta(hours=1)).isoformat(),
        "suspension_reason": None,
        "failure": None,
    }


def sealed_capsule(urn: str, role: str) -> AuthorityCapsule:
    """Return a real capsule sealing Run ``urn`` under ``role``."""
    return AuthorityCapsule.seal(
        {
            "run_ref": urn,
            "scope_ref": TASK_URN,
            "scope_digest": f"sha256:{'a' * 64}",
            "agent_role": role,
            "purpose": "implement",
            "authority": {
                "state": "proposal_only",
                "workspace": "scoped_write",
                "git": "read_metadata",
            },
            "tool_grants": ["repo_read", "submit_report"],
            "budget": {"wall_seconds": 2400, "output_bytes": 1_048_576},
            "criteria_digest": f"sha256:{'b' * 64}",
            "policy_digest": f"sha256:{'c' * 64}",
            "compiled_spec_digest": f"sha256:{'d' * 64}",
            "report_schema_ref": f"schema://report/{role}/v1",
            "stop_conditions": ["scope_ambiguity"],
        }
    )


def document_path(repo: Path) -> Path:
    """Return the selected generation's document of the tree at ``repo``."""
    authority = resolve_authority(repo / ".ea")
    assert authority.target is not None and authority.generation_id is not None
    return authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT


def add_run(repo: Path, key: str, *, role: str | None) -> None:
    """Place Run ``key`` in the document and bind it, sealed under ``role`` unless ``None``."""
    path = document_path(repo)
    document = read_document(path)
    document.setdefault(Epoch2Collection.RUN.value, {})[key] = run_row(key)
    write_document(path, document)
    capsule = None if role is None else sealed_capsule(run_urn(key), role)
    binding = RunBinding(
        run_ref=parse_qualified_urn(run_urn(key)),
        compiled_spec_digest=f"sha256:{'d' * 64}",
        authority_capsule_digest=(
            f"sha256:{'e' * 64}" if capsule is None else capsule.contract_digest
        ),
        route_policy_revision=1,
        bound_at=AT,
        capsule=capsule,
    )
    append_ledger_record(
        ledger_path(path, Epoch2Collection.RUN),
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=f"BND-{key}",
            status="bound",
            recorded_at=AT,
            payload=binding.model_dump(mode="json"),
        ),
    )


def native_repo(root: Path, *, role: str = "executor") -> Path:
    """Provision a native tree at ``root`` holding Run :data:`RUN_KEY` sealed under ``role``.

    Returns:
        ``root``, which holds no ``.ea/state.json``.
    """
    root.mkdir(parents=True, exist_ok=True)
    provision_canary(repo_root=root, ref=canary_ref(CODE), provisioned_at=AT)
    add_run(root, RUN_KEY, role=role)
    return root
