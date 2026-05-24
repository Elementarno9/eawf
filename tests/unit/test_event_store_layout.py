"""10-per-kind JSONL store layout tests (P25-W06 / C07b).

Verifies the C07b §5.5 file layout:

* ``.ea/store/event.jsonl``      every state mutation envelope
* ``.ea/store/audit.jsonl``      audit-DSL evaluations
* ``.ea/store/decision.jsonl``   decision rows
* ``.ea/store/incident.jsonl``   incident timeline entries
* ``.ea/store/estimate.jsonl``   EU estimate snapshots
* ``.ea/store/actual.jsonl``     EU actual rollups
* ``.ea/store/memory.jsonl``     memory appends
* ``.ea/store/research.jsonl``   promoted research records
* ``.ea/store/flow.jsonl``       /flow execution checkpoints
* ``.ea/store/<role>_report.jsonl``  one JSONL per AgentSessionRole

The ``<role>_report`` family is a single conceptual kind whose
envelope routes to a per-role file based on the StoreKind discriminator
(per dispatch §5.4 enumeration).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store import (
    Envelope,
    append_envelope,
    store_dir,
    store_path,
    store_paths,
)

# The 10 conceptual kinds named in C07b §5.5. ``<role>_report`` expands
# to one JSONL per AgentSessionRole — verified separately below.
TEN_BASE_KINDS = (
    StoreKind.EVENT,
    StoreKind.AUDIT,
    StoreKind.DECISION,
    StoreKind.INCIDENT,
    StoreKind.ESTIMATE,
    StoreKind.ACTUAL,
    StoreKind.MEMORY,
    StoreKind.RESEARCH,
    StoreKind.FLOW,
)

ROLE_REPORT_KINDS = (
    StoreKind.RESEARCHER_REPORT,
    StoreKind.PLANNER_REPORT,
    StoreKind.EXECUTOR_REPORT,
    StoreKind.AUDITOR_REPORT,
    StoreKind.REVIEWER_REPORT,
    StoreKind.POLISHER_REPORT,
    StoreKind.OPERATOR_REPORT,
    StoreKind.DOMAIN_SPECIALIST_REPORT,
)


def _envelope(kind: StoreKind, *, env_id: str) -> Envelope:
    """Build a minimal valid envelope for *kind* — payload contents are
    not validated here (the per-kind payload tests live in test_kinds.py).
    """
    return Envelope(
        id=env_id,
        kind=kind,
        scope_id=None,
        created_at=datetime(2026, 5, 19, tzinfo=UTC),
        updated_at=None if kind == StoreKind.EVENT else datetime(2026, 5, 19, tzinfo=UTC),
        summary=f"layout-test {kind.value}",
        payload={},
    )


# ---------------------------------------------------------------------------
# Path resolver maps kind -> singular-stem JSONL under store/
# ---------------------------------------------------------------------------


def test_store_dir_resolves_to_store_subdir(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    assert store_dir(state_path) == tmp_path / "store"


def test_store_path_uses_singular_stem(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    for kind in TEN_BASE_KINDS:
        path = store_path(state_path, kind)
        assert path == tmp_path / "store" / f"{kind.value}.jsonl"


def test_store_paths_covers_every_store_kind(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    paths = store_paths(state_path)
    for kind in StoreKind:
        assert kind in paths, f"StoreKind.{kind} missing from store_paths()"


# ---------------------------------------------------------------------------
# First-write-creates-file invariant (parent dir + file land on first append)
# ---------------------------------------------------------------------------


def test_first_append_creates_parent_directory(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    path = store_path(state_path, StoreKind.EVENT)
    assert not path.parent.exists()
    append_envelope(path, _envelope(StoreKind.EVENT, env_id="ev-001"))
    assert path.parent.is_dir()
    assert path.is_file()


def test_first_append_per_kind_creates_distinct_file(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    for idx, kind in enumerate(TEN_BASE_KINDS):
        path = store_path(state_path, kind)
        append_envelope(path, _envelope(kind, env_id=f"rec-{idx:03d}"))
    for kind in TEN_BASE_KINDS:
        assert store_path(state_path, kind).is_file()


def test_role_report_kinds_each_open_distinct_file(tmp_path: Path) -> None:
    """C07b §5.5: <role>_report.jsonl — one JSONL per AgentSessionRole."""
    state_path = tmp_path / "state.json"
    for idx, kind in enumerate(ROLE_REPORT_KINDS):
        path = store_path(state_path, kind)
        append_envelope(path, _envelope(kind, env_id=f"rep-{idx:03d}"))
    paths = {store_path(state_path, kind) for kind in ROLE_REPORT_KINDS}
    assert len(paths) == len(ROLE_REPORT_KINDS)
    for path in paths:
        assert path.is_file()
        assert path.name.endswith("_report.jsonl")


# ---------------------------------------------------------------------------
# Append correctness: one line per record, JSON parseable
# ---------------------------------------------------------------------------


def test_append_writes_one_record_per_line(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    path = store_path(state_path, StoreKind.DECISION)
    for idx in range(3):
        append_envelope(path, _envelope(StoreKind.DECISION, env_id=f"dec-{idx}"))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    for line in lines:
        Envelope.model_validate_json(line)


def test_append_event_envelope_preserves_kind_event(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    path = store_path(state_path, StoreKind.EVENT)
    append_envelope(path, _envelope(StoreKind.EVENT, env_id="ev-001"))
    reloaded = Envelope.model_validate_json(path.read_text(encoding="utf-8").strip())
    assert reloaded.kind == StoreKind.EVENT
    assert reloaded.updated_at is None
