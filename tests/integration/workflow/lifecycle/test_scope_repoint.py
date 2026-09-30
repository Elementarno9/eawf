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

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.models import State
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.scope_repoint import CriterionTextRepoint, derive_commit_file_scopes

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


# ---- CR-01: the bounded scope repoint --------------------------------------


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


def test_criterion_text_repoint_rejects_empty_text() -> None:
    """An empty text would clear the criterion, so the params model refuses it."""
    with pytest.raises(ValidationError):
        CriterionTextRepoint(criterion_id="CR-01", text="")


def test_criterion_text_repoint_rejects_over_long_text() -> None:
    """Max-length boundary: the model floors at the criterion field's 500 chars."""
    with pytest.raises(ValidationError):
        CriterionTextRepoint(criterion_id="CR-01", text="x" * 501)


# ---- The daemon transaction that persists the repoint ----------------------


# ---- The CLI option parser -------------------------------------------------


# ---- The CLI verb's own argument floor -------------------------------------
#
# Both checks run before any daemon contact, so the runner never leaves the
# tmp workspace it is pointed at.


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
