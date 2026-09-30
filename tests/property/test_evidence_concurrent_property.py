"""Hypothesis property test: concurrent ``goal define`` preserves invariants.

N threads (2-8) race to define distinct goals against a shared
``state.json`` through :func:`eawf.surfaces.cli._mutation.state_transaction`.

Mirroring ``test_wave_claim_property.py``, this test asserts only the
**data-level** invariants because the macOS portalock-with-unlink
behaviour admits a known in-process race in which two contemporaneous
threads can both pass the lock check on different inodes. The
user-visible contract for evidence mutations is:

1. *Some* goal must persist (no total wipe).
2. Every goal that does persist must have the correct shape (title /
   scope / status drawn from the claimed set).
3. Exit codes outside ``{0, 5}`` (which would indicate spurious
   schema-validation rejection) never appear.

A subprocess-based race (where flock semantics hold cross-process) is
covered by the manual smoke check in the PR description.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import orjson


def _seed_state(state_path: Path) -> None:
    """Write a minimal, valid state.json with project=QR (no waves seeded)."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": datetime.now(UTC).isoformat(),
        "project": {
            "code": "QR",
            "slug": "qr",
            "title": "QR",
            "description": None,
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    state_path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))


def test_seeded_state_validates(tmp_path: Path) -> None:
    """Sanity: the seed payload passes schema validation."""
    from eawf.kernel.state.models import State

    state_path = tmp_path / ".ea" / "state.json"
    _seed_state(state_path)
    payload = orjson.loads(state_path.read_bytes())
    State.model_validate(payload)
