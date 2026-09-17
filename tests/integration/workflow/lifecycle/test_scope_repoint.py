"""Tests for the bounded file-scope + criterion-text repoint on a wave row.

A wave's recorded ``file_scopes`` can end up contradicting the very
commit the wave pinned, and its criterion prose can name the wrong
symbol or carry a shape a downstream reader refuses. ``spec sync``
refuses a non-PENDING wave and there is no wave-level reopen, so the
repoint verb is the only repair path for either.

Coverage:

* CR-01 -- the mutation re-derives ``file_scopes`` from ``git show
  --name-only`` of the pinned commit (``.ea/`` paths dropped) and
  rewrites named criterion text, while the status, the recorded verdict,
  ``closed_at``, the commit pin and the gates compare equal before and
  after; a candidate list that moves anything else is rolled back and
  refused, as are a non-CLOSED scope repoint and an unknown criterion id;
* CR-02 -- a text repoint needs a non-empty reason and an EAWF021-clean
  replacement, and the named criterion's ``id`` / ``kind`` / ``gate_ids``
  / ``measurable_signal`` / ``response`` stay byte-identical; the
  ``spec.repoint_scopes`` daemon transaction persists the repoint with
  the reason on its event row, and its dry run writes nothing;
* CR-03 -- the ``epoch1-full`` synthetic-row manifest names every
  invented row and each one really exists in the snapshot;
* boundary + error paths -- an empty request, a duplicate criterion id, a
  wave with no pinned commit, a commit that touches only ``.ea/``, an
  unknown commit, an empty / over-long replacement text, and a no-op
  replay;
* the CLI ``--criterion CR-01=<text>`` parser and the verb's own
  argument floor.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import typer
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf import __version__
from eawf.kernel.spec.common import CriterionSpec
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.spec_repoint import repoint_scopes
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands.spec import parse_criterion_repoint
from eawf.surfaces.cli.errors import UserError
from eawf.surfaces.cli.exit_codes import USER_ERROR
from eawf.workflow.lifecycle import scope_repoint
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.scope_repoint import (
    CriterionTextRepoint,
    build_criterion_text_repoint,
    derive_commit_file_scopes,
    frozen_record_fingerprint,
    repoint_closed_wave_record,
)

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
_WAVE_ID = "P32-I01-W20"

#: The scope list the wave recorded at plan time, which its own commit
#: went on to contradict.
_PLANNED_SCOPES: list[str] = ["src/eawf/workflow/lifecycle/planned_only.py"]

#: What the pinned commit actually touched, minus the state tree.
_COMMITTED_PATHS: tuple[str, ...] = (
    "src/eawf/workflow/lifecycle/scope_repoint.py",
    "tests/integration/workflow/lifecycle/test_scope_repoint.py",
)

#: Criterion prose that clears EAWF021: it carries an observation verb
#: (``exits``) and a proof locus (``pytest``).
_REPLACEMENT_TEXT = "the repointed file scopes match the pinned commit; exits zero under pytest"

_REASON = "the recorded prose named a shorthand the wave never shipped"

#: The synthetic-row manifest for the epoch1-full migration corpus.
_FIXTURE_DIR = Path(__file__).resolve().parents[3] / "fixtures" / "migration" / "epoch1-full"


def _criterion(criterion_id: str = "CR-01") -> dict[str, Any]:
    """A typed criterion row as a closed wave records it."""
    return {
        "id": criterion_id,
        "text": "the recorded file scopes name the shipped module; exits zero under pytest",
        "kind": "deterministic",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "gate_ids": ["G-01"],
        "quality_dimension": "functional_suitability",
        "measurable_signal": "the scope-repoint test module exits zero under pytest",
    }


def _gate() -> dict[str, Any]:
    """A ``command_exit_zero`` gate row bound to ``CR-01``."""
    return {
        "id": "G-01",
        "criterion_id": "CR-01",
        "kind": "command_exit_zero",
        "args": {"argv": ["pytest", "tests/integration/workflow/lifecycle/test_scope_repoint.py"]},
        "policy": "block",
        "cadence": "every-wave",
    }


def _state_payload(
    *,
    wave_status: str = "closed",
    commit: str | None = "a" * 40,
    criteria: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A minimal valid State with one P32 wave carrying a pinned commit."""
    closed_at = _T0.isoformat() if wave_status == "closed" else None
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": _T0.isoformat(),
        "project": {
            "code": "EAWF",
            "slug": "eawf",
            "title": "EAWF",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:EAWF",
        },
        "current": {"project_code": "EAWF"},
        "workspace": None,
        "phases": {
            "P32": {
                "id": "P32",
                "scope_id": "EAWF",
                "track_id": None,
                "title": "P32",
                "status": "active",
                "iter_ids": ["P32-I01"],
                "outcome_ids": [],
                "opened_at": _T0.isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P32-I01": {
                "id": "P32-I01",
                "phase_id": "P32",
                "title": "I01",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _T0.isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P32-I01",
                "title": "wave whose record outlived its own commit",
                "status": wave_status,
                "file_scopes": list(_PLANNED_SCOPES),
                "success_criteria": criteria if criteria is not None else [_criterion()],
                "gates": [_gate()],
                "effort_bucket": "S",
                "agent_role": "executor",
                "opened_at": _T0.isoformat(),
                "closed_at": closed_at,
                "commit": commit,
                "outcome": "closed green on the recorded gate",
                "sessions": {},
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _state(**kwargs: Any) -> State:
    """Build a validated :class:`State` from :func:`_state_payload`."""
    return State.model_validate(_state_payload(**kwargs))


def _write_state(state_path: Path, state: State) -> None:
    """Persist *state* under a tmp ``.ea/`` root."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run one git command in *cwd* and return the completed process."""
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)


def _seeded_repo(tmp_path: Path, *, paths: tuple[str, ...] = _COMMITTED_PATHS) -> tuple[Path, str]:
    """Build a repo whose HEAD commit touches *paths* plus a ``.ea/`` file.

    Args:
        tmp_path: Per-test scratch root.
        paths: Repo-relative non-state paths the commit touches.

    Returns:
        Tuple of repository root and the 40-hex HEAD SHA.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    _run_git(["git", "init", "-b", "main"], repo_root)
    _run_git(["git", "config", "user.email", "test@example.invalid"], repo_root)
    _run_git(["git", "config", "user.name", "Test"], repo_root)
    for relpath in (*paths, ".ea/state.json"):
        target = repo_root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
    _run_git(["git", "add", "-A"], repo_root)
    _run_git(["git", "commit", "-m", "seed"], repo_root)
    sha = _run_git(["git", "rev-parse", "HEAD"], repo_root).stdout.strip()
    return repo_root, sha


def _build_ctx(tmp_path: Path, state_path: Path) -> MethodContext:
    """A daemon method context wired to the tmp state / event / WAL paths."""
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    return MethodContext(
        started_at="2026-08-03T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        event_path=store_path(state_path, StoreKind.EVENT),
        state_path=state_path,
        wal_dir=wal_dir,
        idempotency_cache={},
    )


def _drive(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Drive a daemon coroutine to completion."""
    asyncio.run(body())


# ---- CR-01: the bounded scope repoint --------------------------------------


def test_repoint_closed_wave_file_scopes(tmp_path: Path) -> None:
    """CR-01: scopes follow the pinned commit; the rest of the row does not."""
    repo_root, sha = _seeded_repo(tmp_path)
    state = _state(commit=sha)
    wave = state.waves[_WAVE_ID]

    before_criteria = [row.model_copy(deep=True) for row in wave.success_criteria]
    before_fingerprint = frozen_record_fingerprint(
        wave, elide_scopes=True, elided_criterion_ids=frozenset()
    )

    report = repoint_closed_wave_record(state, wave_id=_WAVE_ID, scope_source=repo_root)

    assert report.scopes_before == _PLANNED_SCOPES
    assert report.scopes_after == sorted(_COMMITTED_PATHS)
    assert report.scopes_changed is True
    assert report.changed_criteria == []
    assert report.unchanged_criterion_ids == ["CR-01"]
    assert wave.file_scopes == sorted(_COMMITTED_PATHS)
    # The state tree the daemon rewrites on every close never enters the record.
    assert not any(path.startswith(".ea/") for path in wave.file_scopes)

    # Everything around file_scopes is frozen.
    assert wave.success_criteria == before_criteria
    assert wave.status.value == "closed"
    assert wave.outcome == "closed green on the recorded gate"
    assert wave.closed_at == _T0
    assert wave.commit == sha
    assert (
        frozen_record_fingerprint(wave, elide_scopes=True, elided_criterion_ids=frozenset())
        == before_fingerprint
    )


def test_repoint_reports_noop_when_scopes_already_match(tmp_path: Path) -> None:
    """Replaying an applied scope repoint reports no change, not a rewrite."""
    repo_root, sha = _seeded_repo(tmp_path)
    state = _state(commit=sha)
    repoint_closed_wave_record(state, wave_id=_WAVE_ID, scope_source=repo_root)

    report = repoint_closed_wave_record(state, wave_id=_WAVE_ID, scope_source=repo_root)

    assert report.scopes_changed is False
    assert report.scopes_before == report.scopes_after == sorted(_COMMITTED_PATHS)


def test_repoint_refuses_and_rolls_back_a_tampered_candidate_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A candidate list that moves a non-text criterion field is refused."""
    repo_root, sha = _seeded_repo(tmp_path)
    state = _state(commit=sha)
    wave = state.waves[_WAVE_ID]
    tampered = [
        wave.success_criteria[0].model_copy(update={"measurable_signal": "smuggled signal text"})
    ]
    monkeypatch.setattr(
        scope_repoint,
        "build_criterion_text_repoint",
        lambda wave, repoints: tampered,
    )

    with pytest.raises(LifecycleError, match="changes more than"):
        repoint_closed_wave_record(
            state,
            wave_id=_WAVE_ID,
            scope_source=repo_root,
            criterion_texts=[CriterionTextRepoint(criterion_id="CR-01", text=_REPLACEMENT_TEXT)],
            reason=_REASON,
        )

    assert wave.file_scopes == _PLANNED_SCOPES
    assert wave.success_criteria[0].measurable_signal == (
        "the scope-repoint test module exits zero under pytest"
    )


def test_repoint_refuses_scope_leg_on_a_claimed_wave(tmp_path: Path) -> None:
    """A scope repoint reads a pinned commit, which only a CLOSED wave carries."""
    repo_root, sha = _seeded_repo(tmp_path)
    state = _state(wave_status="claimed", commit=sha)

    with pytest.raises(LifecycleError, match="a file-scope repoint needs one of"):
        repoint_closed_wave_record(state, wave_id=_WAVE_ID, scope_source=repo_root)
    assert state.waves[_WAVE_ID].file_scopes == _PLANNED_SCOPES


def test_repoint_refuses_pending_wave() -> None:
    """A PENDING wave is plan-time scope and belongs to ``spec sync``."""
    state = _state(wave_status="pending")

    with pytest.raises(LifecycleError, match="eawf spec sync"):
        repoint_closed_wave_record(
            state,
            wave_id=_WAVE_ID,
            criterion_texts=[CriterionTextRepoint(criterion_id="CR-01", text=_REPLACEMENT_TEXT)],
            reason=_REASON,
        )


def test_repoint_refuses_unknown_wave() -> None:
    """An unknown wave id is refused before anything is inspected."""
    state = _state()

    with pytest.raises(LifecycleError, match="unknown wave"):
        repoint_closed_wave_record(state, wave_id="P32-I01-W99", scope_source=Path("."))


def test_repoint_refuses_an_empty_request() -> None:
    """A repoint that repoints nothing is a caller bug, not a no-op."""
    state = _state()

    with pytest.raises(LifecycleError, match="requests nothing"):
        repoint_closed_wave_record(state, wave_id=_WAVE_ID)


def test_repoint_refuses_unknown_criterion_id() -> None:
    """A repoint never creates a criterion, so an unrecorded id is refused."""
    state = _state()

    with pytest.raises(LifecycleError, match="records no criterion"):
        repoint_closed_wave_record(
            state,
            wave_id=_WAVE_ID,
            criterion_texts=[CriterionTextRepoint(criterion_id="CR-99", text=_REPLACEMENT_TEXT)],
            reason=_REASON,
        )
    assert state.waves[_WAVE_ID].success_criteria[0].id == "CR-01"


def test_repoint_refuses_wave_without_a_pinned_commit(tmp_path: Path) -> None:
    """Without a commit pin there is nothing to derive the scopes from."""
    repo_root, _sha = _seeded_repo(tmp_path)
    state = _state(commit=None)

    with pytest.raises(LifecycleError, match="records no pinned commit"):
        repoint_closed_wave_record(state, wave_id=_WAVE_ID, scope_source=repo_root)


def test_repoint_refuses_duplicate_criterion_id() -> None:
    """Two requests for one criterion leave the applied text ambiguous."""
    state = _state()

    with pytest.raises(LifecycleError, match="duplicate criterion repoint"):
        repoint_closed_wave_record(
            state,
            wave_id=_WAVE_ID,
            criterion_texts=[
                CriterionTextRepoint(criterion_id="CR-01", text=_REPLACEMENT_TEXT),
                CriterionTextRepoint(criterion_id="CR-01", text=f"{_REPLACEMENT_TEXT} twice"),
            ],
            reason=_REASON,
        )


def test_build_criterion_text_repoint_refuses_empty_repoints() -> None:
    """The builder floors the request at one row rather than returning a copy."""
    state = _state()

    with pytest.raises(LifecycleError, match="no criterion text repoints supplied"):
        build_criterion_text_repoint(state.waves[_WAVE_ID], [])


def test_build_criterion_text_repoint_moves_one_criterion_of_many() -> None:
    """Off-by-one boundary: only the named criterion of a two-row wave moves."""
    state = _state(criteria=[_criterion("CR-01"), _criterion("CR-02")])
    wave = state.waves[_WAVE_ID]

    candidates = build_criterion_text_repoint(
        wave, [CriterionTextRepoint(criterion_id="CR-02", text=_REPLACEMENT_TEXT)]
    )

    assert [row.id for row in candidates] == ["CR-01", "CR-02"]
    assert candidates[0].text == wave.success_criteria[0].text
    assert candidates[1].text == _REPLACEMENT_TEXT


# ---- derive_commit_file_scopes ---------------------------------------------


def test_derive_commit_file_scopes_drops_state_paths(tmp_path: Path) -> None:
    """The derivation returns the sorted non-state paths of the commit."""
    repo_root, sha = _seeded_repo(tmp_path)

    assert derive_commit_file_scopes(repo_root, sha) == sorted(_COMMITTED_PATHS)


def test_derive_commit_file_scopes_handles_a_single_path_commit(tmp_path: Path) -> None:
    """Single-element boundary: a one-file commit derives a one-element list."""
    repo_root, sha = _seeded_repo(tmp_path, paths=("src/eawf/only.py",))

    assert derive_commit_file_scopes(repo_root, sha) == ["src/eawf/only.py"]


def test_derive_commit_file_scopes_refuses_a_state_only_commit(tmp_path: Path) -> None:
    """A commit that touched only ``.ea/`` would clear the record, so it refuses."""
    repo_root, _sha = _seeded_repo(tmp_path, paths=("src/eawf/only.py",))
    (repo_root / ".ea" / "state.json").write_text("y\n", encoding="utf-8")
    _run_git(["git", "add", "-A"], repo_root)
    _run_git(["git", "commit", "-m", "state only"], repo_root)
    state_only = _run_git(["git", "rev-parse", "HEAD"], repo_root).stdout.strip()

    with pytest.raises(LifecycleError, match="touches no path outside"):
        derive_commit_file_scopes(repo_root, state_only)


def test_derive_commit_file_scopes_refuses_an_unreachable_commit(tmp_path: Path) -> None:
    """A commit git cannot resolve is named in the refusal, not swallowed."""
    repo_root, _sha = _seeded_repo(tmp_path)

    with pytest.raises(LifecycleError, match="unreachable"):
        derive_commit_file_scopes(repo_root, "b" * 40)


# ---- CR-02: the bounded criterion-text repoint ------------------------------


def test_repoint_criterion_text_keeps_every_sibling_field(tmp_path: Path) -> None:
    """The text moves; id, kind, gate_ids, measurable_signal and response do not."""
    state = _state()
    wave = state.waves[_WAVE_ID]
    recorded = wave.success_criteria[0].model_copy(deep=True)

    report = repoint_closed_wave_record(
        state,
        wave_id=_WAVE_ID,
        criterion_texts=[CriterionTextRepoint(criterion_id="CR-01", text=_REPLACEMENT_TEXT)],
        reason=_REASON,
    )

    applied = wave.success_criteria[0]
    assert applied.text == _REPLACEMENT_TEXT
    assert applied.id == recorded.id
    assert applied.kind == recorded.kind
    assert applied.gate_ids == recorded.gate_ids
    assert applied.measurable_signal == recorded.measurable_signal
    assert applied.response == recorded.response
    assert [change.criterion_id for change in report.changed_criteria] == ["CR-01"]
    assert report.changed_criteria[0].before_text == recorded.text
    assert report.scopes_changed is False


def test_repoint_criterion_text_runs_on_an_in_progress_wave() -> None:
    """The prose that blocks a close is repaired while the wave is still open."""
    state = _state(wave_status="in_progress", commit=None)

    report = repoint_closed_wave_record(
        state,
        wave_id=_WAVE_ID,
        criterion_texts=[CriterionTextRepoint(criterion_id="CR-01", text=_REPLACEMENT_TEXT)],
        reason=_REASON,
    )

    assert state.waves[_WAVE_ID].success_criteria[0].text == _REPLACEMENT_TEXT
    assert state.waves[_WAVE_ID].status.value == "in_progress"
    assert len(report.changed_criteria) == 1


def test_repoint_criterion_text_requires_a_reason() -> None:
    """A blank reason leaves the rewrite unauditable, so it is refused."""
    state = _state()

    with pytest.raises(LifecycleError, match="requires a non-empty reason"):
        repoint_closed_wave_record(
            state,
            wave_id=_WAVE_ID,
            criterion_texts=[CriterionTextRepoint(criterion_id="CR-01", text=_REPLACEMENT_TEXT)],
            reason="   ",
        )
    assert state.waves[_WAVE_ID].success_criteria[0].text != _REPLACEMENT_TEXT


def test_repoint_criterion_text_refuses_a_vague_rewrite() -> None:
    """EAWF021 still fires on the replacement prose."""
    state = _state()

    with pytest.raises(LifecycleError, match="EAWF021"):
        repoint_closed_wave_record(
            state,
            wave_id=_WAVE_ID,
            criterion_texts=[
                CriterionTextRepoint(
                    criterion_id="CR-01",
                    text="the repoint works properly; exits zero under pytest",
                )
            ],
            reason=_REASON,
        )


def test_repoint_criterion_text_refuses_text_without_an_observation_contract() -> None:
    """Prose with no observation verb plus proof locus cannot be observed at all."""
    state = _state()

    with pytest.raises(LifecycleError, match="EAWF021"):
        repoint_closed_wave_record(
            state,
            wave_id=_WAVE_ID,
            criterion_texts=[
                CriterionTextRepoint(criterion_id="CR-01", text="the file scopes are nicer now")
            ],
            reason=_REASON,
        )


def test_criterion_text_repoint_rejects_empty_text() -> None:
    """An empty text would clear the criterion, so the params model refuses it."""
    with pytest.raises(ValidationError):
        CriterionTextRepoint(criterion_id="CR-01", text="")


def test_criterion_text_repoint_rejects_over_long_text() -> None:
    """Max-length boundary: the model floors at the criterion field's 500 chars."""
    with pytest.raises(ValidationError):
        CriterionTextRepoint(criterion_id="CR-01", text="x" * 501)


def test_criterion_spec_still_rejects_an_over_broad_rewrite() -> None:
    """The model validators fire at the rebuild seam, not only at plan time."""
    state = _state()
    over_broad = (
        "returns a value and raises on error and exits nonzero and emits a line "
        "and validates the row and matches pattern under pytest"
    )

    with pytest.raises(LifecycleError, match="rejected"):
        build_criterion_text_repoint(
            state.waves[_WAVE_ID],
            [CriterionTextRepoint(criterion_id="CR-01", text=over_broad)],
        )


def test_criterion_spec_rebuild_is_a_validated_parse() -> None:
    """The candidate row is a real CriterionSpec, not a shallow copy."""
    state = _state()

    candidates = build_criterion_text_repoint(
        state.waves[_WAVE_ID],
        [CriterionTextRepoint(criterion_id="CR-01", text=_REPLACEMENT_TEXT)],
    )

    assert isinstance(candidates[0], CriterionSpec)


# ---- The daemon transaction that persists the repoint ----------------------


def test_repoint_scopes_rpc_persists_repoint(tmp_path: Path) -> None:
    """The daemon mutator writes both legs and an audit envelope carrying the reason."""
    repo_root, sha = _seeded_repo(tmp_path)
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state(commit=sha))
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        result = await repoint_scopes(
            ctx,
            {
                "wave_id": _WAVE_ID,
                "from_commit": True,
                "criterion_texts": [{"criterion_id": "CR-01", "text": _REPLACEMENT_TEXT}],
                "reason": _REASON,
                "repo_root": str(repo_root),
            },
        )
        assert result["scopes_changed"] is True
        assert result["scopes_after"] == sorted(_COMMITTED_PATHS)
        assert result["changed_count"] == 1
        assert result["changed_criteria"][0]["after_text"] == _REPLACEMENT_TEXT
        payload = result["envelope"]["payload"]
        assert payload["event_type"] == "state.mutate.spec_repoint_scopes"
        assert payload["extras"]["changed_criterion_ids"] == "CR-01"
        assert payload["extras"]["reason"] == _REASON
        assert result["before_version"] != result["after_version"]

    _drive(body)
    wave = State.model_validate_json(state_path.read_text(encoding="utf-8")).waves[_WAVE_ID]
    assert wave.file_scopes == sorted(_COMMITTED_PATHS)
    assert wave.success_criteria[0].text == _REPLACEMENT_TEXT
    assert wave.success_criteria[0].measurable_signal == (
        "the scope-repoint test module exits zero under pytest"
    )
    assert wave.status.value == "closed"
    assert wave.closed_at == _T0
    assert wave.commit == sha
    assert wave.outcome == "closed green on the recorded gate"


def test_repoint_scopes_rpc_dry_run_writes_nothing(tmp_path: Path) -> None:
    """``--dry-run`` reports the would-change set with the state bytes intact."""
    repo_root, sha = _seeded_repo(tmp_path)
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state(commit=sha))
    before_bytes = state_path.read_bytes()
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        result = await repoint_scopes(
            ctx,
            {
                "wave_id": _WAVE_ID,
                "from_commit": True,
                "criterion_texts": [{"criterion_id": "CR-01", "text": _REPLACEMENT_TEXT}],
                "reason": _REASON,
                "dry_run": True,
                "repo_root": str(repo_root),
            },
        )
        assert result["dry_run"] is True
        assert result["scopes_changed"] is True
        assert result["changed_count"] == 1
        assert result["envelope"] is None
        assert result["before_version"] is None

    _drive(body)
    assert state_path.read_bytes() == before_bytes


def test_repoint_scopes_rpc_refuses_missing_reason(tmp_path: Path) -> None:
    """The daemon maps the missing-reason refusal onto a validation error."""
    repo_root, sha = _seeded_repo(tmp_path)
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state(commit=sha))
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="non-empty reason"):
            await repoint_scopes(
                ctx,
                {
                    "wave_id": _WAVE_ID,
                    "criterion_texts": [{"criterion_id": "CR-01", "text": _REPLACEMENT_TEXT}],
                    "repo_root": str(repo_root),
                },
            )

    _drive(body)
    wave = State.model_validate_json(state_path.read_text(encoding="utf-8")).waves[_WAVE_ID]
    assert wave.success_criteria[0].text != _REPLACEMENT_TEXT


def test_repoint_scopes_rpc_refuses_unknown_wave(tmp_path: Path) -> None:
    """An unknown wave is a validation error, not a silent no-op."""
    repo_root, sha = _seeded_repo(tmp_path)
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state(commit=sha))
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="unknown wave"):
            await repoint_scopes(
                ctx,
                {
                    "wave_id": "P32-I01-W99",
                    "from_commit": True,
                    "repo_root": str(repo_root),
                },
            )

    _drive(body)


def test_repoint_scopes_rpc_refuses_non_wave_scope(tmp_path: Path) -> None:
    """A phase scope is refused: a repoint is authorised one wave at a time."""
    repo_root, sha = _seeded_repo(tmp_path)
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state(commit=sha))
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="wave scope"):
            await repoint_scopes(
                ctx,
                {"wave_id": "P32", "from_commit": True, "repo_root": str(repo_root)},
            )

    _drive(body)


def test_repoint_scopes_rpc_refuses_an_empty_request(tmp_path: Path) -> None:
    """Neither leg requested is a caller bug the daemon names."""
    repo_root, sha = _seeded_repo(tmp_path)
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state(commit=sha))
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="requests nothing"):
            await repoint_scopes(ctx, {"wave_id": _WAVE_ID, "repo_root": str(repo_root)})

    _drive(body)


# ---- The CLI option parser -------------------------------------------------


def test_parse_criterion_repoint_takes_the_text_verbatim() -> None:
    """The prose survives whitespace and quoting untouched."""
    row = parse_criterion_repoint(f"  CR-02 = {_REPLACEMENT_TEXT}  ")

    assert row == {"criterion_id": "CR-02", "text": _REPLACEMENT_TEXT}


def test_parse_criterion_repoint_keeps_a_later_equals_sign() -> None:
    """Only the first ``=`` separates; a text may contain more."""
    row = parse_criterion_repoint("CR-01=exits 0 when mode=strict under pytest")

    assert row["text"] == "exits 0 when mode=strict under pytest"


def test_parse_criterion_repoint_rejects_missing_separator() -> None:
    """A value with no ``=`` names no criterion."""
    with pytest.raises(typer.BadParameter, match="expected <criterion-id>=<text>"):
        parse_criterion_repoint("exits zero under pytest")


def test_parse_criterion_repoint_rejects_empty_criterion_id() -> None:
    """An empty left-hand side is refused rather than defaulted."""
    with pytest.raises(typer.BadParameter, match="empty criterion id"):
        parse_criterion_repoint("=exits zero under pytest")


def test_parse_criterion_repoint_rejects_empty_text() -> None:
    """An empty right-hand side would clear the criterion text."""
    with pytest.raises(typer.BadParameter, match="empty text for criterion"):
        parse_criterion_repoint("CR-01=   ")


# ---- The CLI verb's own argument floor -------------------------------------
#
# Both checks run before any daemon contact, so the runner never leaves the
# tmp workspace it is pointed at.


def test_repoint_scopes_cli_refuses_a_request_with_neither_leg(tmp_path: Path) -> None:
    """A verb call that names no leg is a user error, not an empty mutation."""
    result = CliRunner().invoke(app, ["-w", str(tmp_path), "spec", "repoint-scopes", _WAVE_ID])

    assert isinstance(result.exception, UserError), result.output
    assert result.exception.exit_code == USER_ERROR
    assert "--from-commit or at least one --criterion" in str(result.exception)


def test_repoint_scopes_cli_refuses_a_criterion_without_a_reason(tmp_path: Path) -> None:
    """The CLI floors the reason at the boundary, before the RPC hop."""
    result = CliRunner().invoke(
        app,
        [
            "-w",
            str(tmp_path),
            "spec",
            "repoint-scopes",
            _WAVE_ID,
            "--criterion",
            f"CR-01={_REPLACEMENT_TEXT}",
        ],
    )

    assert isinstance(result.exception, UserError), result.output
    assert result.exception.exit_code == USER_ERROR
    assert "non-empty --reason" in str(result.exception)


# ---- CR-03: the epoch1-full synthetic-row manifest -------------------------


def test_synthetic_rows_manifest_names_rows_present_in_the_fixture() -> None:
    """Every row the manifest declares invented exists in the snapshot as declared."""
    manifest = json.loads((_FIXTURE_DIR / "synthetic_rows.json").read_text(encoding="utf-8"))
    document = json.loads((_FIXTURE_DIR / "snapshot" / "document.json").read_text(encoding="utf-8"))

    assert manifest["rows"], "the manifest declares no synthetic rows"
    for row in manifest["rows"]:
        collection = document[row["collection"]]
        assert row["id"] in collection, (
            f"manifest names {row['collection']}/{row['id']}, which the snapshot does not carry"
        )
        assert collection[row["id"]]["status"] == row["status"]
        assert row["reason"].strip(), f"{row['collection']}/{row['id']} carries no reason"


def test_synthetic_rows_manifest_covers_the_two_invented_rows() -> None:
    """The archived phase and the closed backlog row are both declared."""
    manifest = json.loads((_FIXTURE_DIR / "synthetic_rows.json").read_text(encoding="utf-8"))

    declared = {(row["collection"], row["id"]) for row in manifest["rows"]}
    assert declared == {("phases", "P03"), ("backlog", "B003")}
    assert manifest["added_by"] == "P32-I01-W11"
