"""Cross-surface integration test: every JSONL emitter writes under the
canonical layout ``<state_dir>/store/<StoreKind.value>.jsonl``.

This test guards rule W1 of the post-PR-2 fix-up: the JSONL store path
layout was previously fragmented across three different directories
(``<state>/<kind>.jsonl`` flat, ``<state>/store/<kind>.jsonl`` (plural
filename) and ``<state>/stores/<kind>.jsonl`` (plural directory)). After
W1 every CLI surface that emits a store record lands at
``<state>/store/<kind>.jsonl`` (singular subdir, singular filename
matching ``StoreKind.value``).

For each emitter we drive one CLI invocation, then assert:

1. The expected ``<state>/store/<kind>.jsonl`` file exists.
2. No record landed at the legacy flat layout
   (``<state>/<kind>.jsonl``) or the plural-directory layout
   (``<state>/stores/<kind>.jsonl``).
3. ``eawf store compact --kind <kind>`` reports ``records_in > 0`` for
   each emitted kind, proving the compactor reads the same path the
   writers used.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.state.enums import StoreKind

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "states"


def _seed_workspace(tmp_path: Path) -> Path:
    """Copy the empty-repo fixture into a fresh ``.ea/`` and return the workspace."""
    workspace = tmp_path / "ws"
    state_dir = workspace / ".ea"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_bytes((FIXTURES / "valid" / "01-empty-repo.json").read_bytes())
    return workspace


def _state_dir(workspace: Path) -> Path:
    return workspace / ".ea"


def _canonical_path(workspace: Path, kind: StoreKind) -> Path:
    return _state_dir(workspace) / "store" / f"{kind.value}.jsonl"


def _legacy_flat_path(workspace: Path, kind: StoreKind) -> Path:
    """Pre-W1 flat layout some callers wrote: ``<state>/<kind>.jsonl``."""
    return _state_dir(workspace) / f"{kind.value}.jsonl"


def _legacy_plural_dir_path(workspace: Path, kind: StoreKind) -> Path:
    """Pre-W1 plural-dir layout memory/session used: ``<state>/stores/<kind>.jsonl``."""
    return _state_dir(workspace) / "stores" / f"{kind.value}.jsonl"


def _record_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip())


def _compact(workspace: Path, kind: StoreKind) -> dict[str, Any]:
    """Run ``eawf store compact --kind <kind>`` and return its JSON envelope."""
    result = runner.invoke(
        app,
        [
            "--json",
            "-w",
            str(workspace),
            "store",
            "compact",
            "--kind",
            kind.value,
        ],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _assert_canonical_only(
    workspace: Path,
    kind: StoreKind,
    *,
    expect_records_at_least: int = 1,
) -> None:
    """Assert *kind* records land canonically and nowhere else."""
    canonical = _canonical_path(workspace, kind)
    flat = _legacy_flat_path(workspace, kind)
    plural = _legacy_plural_dir_path(workspace, kind)
    assert canonical.exists(), f"canonical path {canonical} must exist for kind={kind.value}"
    assert _record_count(canonical) >= expect_records_at_least, (
        f"canonical path {canonical} must hold >= "
        f"{expect_records_at_least} record(s); got {_record_count(canonical)}"
    )
    # Nothing should land at the legacy locations.
    assert not flat.exists() or _record_count(flat) == 0, (
        f"legacy flat path {flat} must be empty; found {_record_count(flat)} record(s)"
    )
    assert not plural.exists() or _record_count(plural) == 0, (
        f"legacy plural-dir path {plural} must be empty; found {_record_count(plural)} record(s)"
    )
    # The compactor reads the same canonical path the writers used.
    report = _compact(workspace, kind)
    assert report["records_in"] >= expect_records_at_least, (
        f"compact for kind={kind.value} must report records_in >= "
        f"{expect_records_at_least}; got {report['records_in']}"
    )
    assert report["path"].endswith(f"store/{kind.value}.jsonl"), (
        f"compact for kind={kind.value} must target canonical path; got {report['path']}"
    )


def test_lifecycle_phase_open_writes_event_under_canonical_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``phase open`` (lifecycle) writes only to ``<state>/store/event.jsonl``."""
    monkeypatch.delenv("EA_STATE", raising=False)
    workspace = _seed_workspace(tmp_path)

    result = runner.invoke(
        app,
        ["-w", str(workspace), "phase", "open", "--auto", "--title", "P1"],
    )
    assert result.exit_code == 0, result.output

    _assert_canonical_only(workspace, StoreKind.EVENT, expect_records_at_least=1)


def test_evidence_goal_define_writes_event_under_canonical_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``goal define`` writes its EVENT envelope to the canonical store."""
    monkeypatch.delenv("EA_STATE", raising=False)
    workspace = _seed_workspace(tmp_path)

    result = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "goal",
            "define",
            "G01",
            "--title",
            "Test goal",
        ],
    )
    assert result.exit_code == 0, result.output

    _assert_canonical_only(workspace, StoreKind.EVENT, expect_records_at_least=1)


def test_evidence_audit_add_writes_audit_and_event_under_canonical_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``audit add`` writes both AUDIT and EVENT envelopes to the canonical store."""
    monkeypatch.delenv("EA_STATE", raising=False)
    workspace = _seed_workspace(tmp_path)

    result = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "audit",
            "add",
            "AUD-001",
            "--scope-id",
            "QR",
            "--kind",
            "evaluation",
        ],
    )
    assert result.exit_code == 0, result.output

    _assert_canonical_only(workspace, StoreKind.AUDIT, expect_records_at_least=1)
    _assert_canonical_only(workspace, StoreKind.EVENT, expect_records_at_least=1)


def test_evidence_decision_add_writes_decision_and_event_under_canonical_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``decision add`` writes both DECISION and EVENT envelopes to the canonical store."""
    monkeypatch.delenv("EA_STATE", raising=False)
    workspace = _seed_workspace(tmp_path)

    result = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "decision",
            "add",
            "D012",
            "--scope-id",
            "QR",
            "--summary",
            "Use phase-bundled PR",
            "--rationale",
            "Coupled refactor",
        ],
    )
    assert result.exit_code == 0, result.output

    _assert_canonical_only(workspace, StoreKind.DECISION, expect_records_at_least=1)
    _assert_canonical_only(workspace, StoreKind.EVENT, expect_records_at_least=1)


def test_evidence_incident_open_writes_incident_and_event_under_canonical_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``incident open`` writes both INCIDENT and EVENT envelopes to the canonical store."""
    monkeypatch.delenv("EA_STATE", raising=False)
    workspace = _seed_workspace(tmp_path)

    result = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "incident",
            "open",
            "INC-001",
            "--severity",
            "low",
            "--title",
            "minor blip",
        ],
    )
    assert result.exit_code == 0, result.output

    _assert_canonical_only(workspace, StoreKind.INCIDENT, expect_records_at_least=1)
    _assert_canonical_only(workspace, StoreKind.EVENT, expect_records_at_least=1)


def test_estimation_estimate_writes_estimate_and_event_under_canonical_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``estimate set`` writes both ESTIMATE and EVENT envelopes to the canonical store."""
    monkeypatch.delenv("EA_STATE", raising=False)
    workspace = _seed_workspace(tmp_path)

    result = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "estimate",
            "set",
            "P01-I01-W01",
            "--source",
            "prep",
        ],
    )
    assert result.exit_code == 0, result.output

    _assert_canonical_only(workspace, StoreKind.ESTIMATE, expect_records_at_least=1)
    _assert_canonical_only(workspace, StoreKind.EVENT, expect_records_at_least=1)


def test_estimation_actual_start_writes_actual_and_event_under_canonical_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``actual start`` writes both ACTUAL and EVENT envelopes to the canonical store."""
    monkeypatch.delenv("EA_STATE", raising=False)
    workspace = _seed_workspace(tmp_path)

    result = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "actual",
            "start",
            "P01-I01-W01",
            "--session",
            "SES-001",
        ],
    )
    assert result.exit_code == 0, result.output

    _assert_canonical_only(workspace, StoreKind.ACTUAL, expect_records_at_least=1)
    _assert_canonical_only(workspace, StoreKind.EVENT, expect_records_at_least=1)


def test_memory_add_writes_memory_and_event_under_canonical_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``memory add`` writes both MEMORY and EVENT envelopes to the canonical store."""
    monkeypatch.delenv("EA_STATE", raising=False)
    workspace = _seed_workspace(tmp_path)

    result = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "memory",
            "add",
            "--scope",
            "QR",
            "--title",
            "Use uv run",
            "--body",
            "All Python invocations go through uv.",
        ],
    )
    assert result.exit_code == 0, result.output

    _assert_canonical_only(workspace, StoreKind.MEMORY, expect_records_at_least=1)
    _assert_canonical_only(workspace, StoreKind.EVENT, expect_records_at_least=1)


def test_session_start_writes_event_under_canonical_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``session start`` writes its EVENT envelope to the canonical store."""
    monkeypatch.delenv("EA_STATE", raising=False)
    workspace = _seed_workspace(tmp_path)

    result = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "session",
            "start",
            "--role",
            "executor",
            "--scope",
            "QR",
            "--runtime",
            "claude",
        ],
    )
    assert result.exit_code == 0, result.output

    _assert_canonical_only(workspace, StoreKind.EVENT, expect_records_at_least=1)


def test_project_init_writes_event_under_canonical_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``project init`` (the bootstrap command) writes EVENT under the canonical store."""
    workspace = tmp_path / "fresh"
    state_path = workspace / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app,
        [
            "project",
            "init",
            "ZX",
            "--title",
            "ZX Project",
            "--domains",
            "demo",
        ],
    )
    assert result.exit_code == 0, result.output

    _assert_canonical_only(workspace, StoreKind.EVENT, expect_records_at_least=1)


def test_no_legacy_layouts_after_full_phase2_drive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive a multi-step lifecycle and assert no legacy layouts ever appear.

    This is the cross-cutting smoke test: if any handler regresses to the
    legacy flat or plural-directory layout the assertion below fires
    before the per-kind tests above can localise the offender.
    """
    monkeypatch.delenv("EA_STATE", raising=False)
    workspace = _seed_workspace(tmp_path)

    runner.invoke(
        app,
        ["-w", str(workspace), "phase", "open", "--auto", "--title", "P1"],
    )
    runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "audit",
            "add",
            "AUD-001",
            "--scope-id",
            "QR",
            "--kind",
            "evaluation",
        ],
    )
    runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "estimate",
            "set",
            "P01-I01-W01",
            "--source",
            "prep",
        ],
    )
    runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "actual",
            "start",
            "P01-I01-W01",
            "--session",
            "SES-001",
        ],
    )
    runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "memory",
            "add",
            "--scope",
            "QR",
            "--title",
            "t",
            "--body",
            "b",
        ],
    )

    state_dir = _state_dir(workspace)
    # No legacy flat-layout files anywhere.
    for kind in StoreKind:
        flat = state_dir / f"{kind.value}.jsonl"
        assert not flat.exists() or _record_count(flat) == 0, (
            f"legacy flat layout file {flat} appeared"
        )
    # No legacy plural-directory either.
    plural_dir = state_dir / "stores"
    assert not plural_dir.exists(), f"legacy plural-directory layout {plural_dir} appeared"
    # The canonical store dir is populated.
    assert (state_dir / "store").is_dir()
