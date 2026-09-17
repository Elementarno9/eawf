"""Close-verb, docs-digest, golden-only and open-phase cases for ``tools/commit_prefix_lint.py``.

Four misjudgements are pinned here.

The single-wave close check read a close verb anywhere in a state subject, so
``add and claim W03 to close B152`` was rejected as a close of W03 while
``close W02 and claim W01`` passed because it named two waves. The verb now
governs only the wave tokens that directly follow it.

A bare docs commit that rewrites an artifact body could not carry the
state.json digest re-pin that rewrite produces. It now may, but only beside
the artifact.

Every ``test`` commit escaped the one-commit cap, although only the managed
golden refresh the snapshot-pairing gate forces is a legitimate second commit.

A commit with no wave carrier passed whenever no phase was current, so repair
work landed unattributed between a phase close and the next activation. It is
now rejected while any phase is PLANNED or ACTIVE.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from eawf.surfaces.cli.commands.snapshot import SNAPSHOT_SURFACES

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LINT_PATH = _REPO_ROOT / "tools" / "commit_prefix_lint.py"
_TOOL_DIR = _LINT_PATH.parent

_CLAUDE_TRAILER = "\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
_STATE_PATHS = [".ea/state.json", ".ea/store/evidence.jsonl"]
_ARTIFACT_PATH = ".ea/artifacts/audits/2026-09-16-closure.md"
_PRIOR_SHA = "a" * 40


def _load_module() -> Any:
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location("commit_prefix_lint", _LINT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["commit_prefix_lint"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def mod() -> Any:
    return _load_module()


@pytest.fixture(autouse=True)
def _no_prior_wave_commits(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every case to a wave with no commit yet on ``HEAD``.

    Without this the cap would shell out to the real repository, where the
    fixture wave ids genuinely exist. The cap's own cases re-patch it.
    """
    monkeypatch.setattr(mod, "_prior_wave_commits", lambda *_a, **_kw: [])


def _write_msg(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text(body.rstrip() + _CLAUDE_TRAILER, encoding="utf-8")
    return path


def _write_phases(
    tmp_path: Path,
    phases: dict[str, Any],
    *,
    current_phase_id: str | None = None,
) -> Path:
    """Write a state fixture carrying *phases* and an optional current phase."""
    payload = {
        "current": {"phase_id": current_phase_id, "iter_id": None},
        "phases": phases,
        "iters": {},
        "waves": {},
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _phase(status: str) -> dict[str, Any]:
    return {"status": status, "iter_ids": []}


# ---------------------------------------------------------------------------
# The close verb governs the wave tokens that follow it.
# ---------------------------------------------------------------------------


def test_add_and_claim_naming_a_backlog_close_is_accepted(tmp_path: Path, mod: Any) -> None:
    """The close verb governs a backlog id, so W03 is added and claimed, not closed."""
    msg = _write_msg(tmp_path, "[P33] state: add and claim W03 to close B152\n")

    code, diag = mod.lint(msg, _STATE_PATHS)

    assert code == 0, diag


def test_close_then_claim_rejects_the_closed_wave(tmp_path: Path, mod: Any) -> None:
    """The claim verb ends the close run, so the subject closes W02 alone."""
    msg = _write_msg(tmp_path, "[P33] state: close W02 and claim W01\n")

    code, diag = mod.lint(msg, _STATE_PATHS)

    assert code == 1
    assert "single-wave close bookkeeping rejected" in diag
    assert "close records for W02" in diag
    assert "an add or claim may ride the wave commit" in diag


def test_claim_then_close_rejects_the_closed_wave(tmp_path: Path, mod: Any) -> None:
    """A wave claimed before the close verb is not part of its run."""
    msg = _write_msg(tmp_path, "[P33] state: claim W01 and close W02\n")

    code, diag = mod.lint(msg, _STATE_PATHS)

    assert code == 1
    assert "close records for W02" in diag


def test_close_then_add_rejects_the_closed_wave(tmp_path: Path, mod: Any) -> None:
    """The shape P32 actually landed: one wave closed beside an appended one."""
    msg = _write_msg(tmp_path, "[P32] state: close W01 and add W44 for the adoption gap\n")

    code, diag = mod.lint(msg, _STATE_PATHS)

    assert code == 1
    assert "close records for W01" in diag


def test_participle_close_rejects_the_preceding_wave(tmp_path: Path, mod: Any) -> None:
    """``W27 closed`` records one wave's close as surely as ``close W27``."""
    msg = _write_msg(tmp_path, "[P31-I01] state: W27 closed\n")

    code, diag = mod.lint(msg, _STATE_PATHS)

    assert code == 1
    assert "close records for W27" in diag


@pytest.mark.parametrize(
    "subject",
    [
        "[P32] state: close W02, W10 and W30",
        "[P30-I26] state: close W08-W22 (CI-cost waves)",
        "[P31] state: close W27 + W28",
        "[P31] state: close W27 and close W28",
        "[P31] state: close waves W27 and W28",
    ],
    ids=["comma-list", "range", "plus", "two-verbs", "waves-noun"],
)
def test_close_of_several_waves_is_accepted(tmp_path: Path, mod: Any, subject: str) -> None:
    """A close run of two or more waves is a batch, not a single-wave close."""
    msg = _write_msg(tmp_path, f"{subject}\n")

    code, diag = mod.lint(msg, _STATE_PATHS)

    assert code == 0, diag


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ("", set()),
        ("close", set()),
        ("close W27", {"W27"}),
        ("Closing wave W27", {"W27"}),
        ("close W02 and claim W01", {"W02"}),
        ("add and claim W03 to close B152", set()),
        ("W27 closed", {"W27"}),
        ("W27 close evidence", set()),
        ("append W45 for the trailer commit form", set()),
        ("close iter + phase (audit=A-P31)", set()),
        ("closer look at W27", set()),
    ],
    ids=[
        "empty",
        "verb-only",
        "single",
        "gerund-with-noun",
        "close-then-claim",
        "backlog-close",
        "participle",
        "noun-use",
        "no-close-verb",
        "iter-close",
        "not-a-close-verb",
    ],
)
def test_closed_wave_tokens_boundaries(mod: Any, summary: str, expected: set[str]) -> None:
    """Boundary sweep of which wave tokens a close verb governs."""
    assert mod._closed_wave_tokens(summary) == expected


# ---------------------------------------------------------------------------
# A bare docs commit may refresh the digest of the artifact it stages.
# ---------------------------------------------------------------------------


def test_docs_commit_refreshes_artifact_digest_with_state(tmp_path: Path, mod: Any) -> None:
    """The artifact rewrite and its state.json digest re-pin land together."""
    msg = _write_msg(tmp_path, "[P33] docs: refresh the closure audit body\n")

    code, diag = mod.lint(msg, [_ARTIFACT_PATH, ".ea/state.json", ".secrets.baseline"])

    assert code == 0, diag


def test_docs_commit_iter_scope_refreshes_artifact_digest(tmp_path: Path, mod: Any) -> None:
    """The iter-scoped bare docs form admits the same companions."""
    msg = _write_msg(tmp_path, "[P33-I01] docs: refresh the iter audit body\n")

    code, diag = mod.lint(msg, [_ARTIFACT_PATH, ".ea/state.json"])

    assert code == 0, diag


def test_docs_commit_without_artifact_rejects_state(tmp_path: Path, mod: Any) -> None:
    """With no artifact staged, state.json is bare bookkeeping under a docs subject."""
    msg = _write_msg(tmp_path, "[P33] docs: pin a digest for nothing\n")

    code, diag = mod.lint(msg, [".ea/state.json", ".secrets.baseline"])

    assert code == 1
    assert "non-artifact paths: ['.ea/state.json', '.secrets.baseline']" in diag
    assert "refresh the digest" in diag


def test_docs_commit_with_artifact_rejects_other_state_stores(tmp_path: Path, mod: Any) -> None:
    """Only the two digest companions ride along; the typed stores do not."""
    msg = _write_msg(tmp_path, "[P33] docs: refresh the closure audit body\n")

    code, diag = mod.lint(msg, [_ARTIFACT_PATH, ".ea/state.json", ".ea/store/evidence.jsonl"])

    assert code == 1
    assert "non-artifact paths: ['.ea/store/evidence.jsonl']" in diag


def test_docs_commit_with_artifact_rejects_source(tmp_path: Path, mod: Any) -> None:
    """An artifact path widens the whitelist by the companions only."""
    msg = _write_msg(tmp_path, "[P33] docs: refresh the closure audit body\n")

    code, diag = mod.lint(msg, [_ARTIFACT_PATH, ".ea/state.json", "src/eawf/x.py"])

    assert code == 1
    assert "non-artifact paths: ['src/eawf/x.py']" in diag


def test_docs_commit_with_empty_stage_is_accepted(tmp_path: Path, mod: Any) -> None:
    """Boundary (empty): a reword stages nothing to stray."""
    msg = _write_msg(tmp_path, "[P33] docs: reword the closure audit subject\n")

    code, diag = mod.lint(msg, [])

    assert code == 0, diag


# ---------------------------------------------------------------------------
# Only a golden-only test commit escapes the one-commit cap.
# ---------------------------------------------------------------------------


def _model_prior_commit(mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Model a wave that already has a commit and a fresh (non-amend) commit."""
    monkeypatch.setattr(mod, "_prior_wave_commits", lambda *_a, **_kw: [_PRIOR_SHA])
    monkeypatch.setattr(mod, "_head_identity", lambda *_a, **_kw: None)


@pytest.mark.parametrize(
    "staged",
    [
        ["tests/golden/state/closed_phase.json"],
        ["tests/snapshots/tui/golden/modes_feed.svg", "tests/golden/agents_md/core_only.md"],
        ["tests/golden/surfaces/cli/scenarios/wave_close.txt"],
    ],
    ids=["one-golden", "two-surfaces", "nested-surface"],
)
def test_golden_only_test_commit_skips_the_cap(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch, staged: list[str]
) -> None:
    """The paired golden refresh is the one second commit a wave may take."""
    _model_prior_commit(mod, monkeypatch)
    msg = _write_msg(tmp_path, "test: repin the goldens the fix moved\n\nEawf-Wave: P33-I01-W29\n")

    code, diag = mod.lint(msg, staged, subject_style="trailer")

    assert code == 0, diag


@pytest.mark.parametrize(
    "staged",
    [
        ["tests/unit/test_x.py"],
        ["tests/golden/state/closed_phase.json", "tests/unit/test_x.py"],
        ["tests/golden/cli/help.txt"],
        ["tests/golden/surfaces/cli/scenarios/conftest.py"],
        ["tests/golden/state_extra/closed_phase.json"],
        ["tests/golden/state/closed_phase.json", ".ea/state.json", "src/eawf/x.py"],
    ],
    ids=[
        "unit-test",
        "golden-plus-unit-test",
        "unmanaged-golden-tree",
        "golden-generator-python",
        "sibling-prefix",
        "golden-plus-source",
    ],
)
def test_non_golden_test_commit_is_capped(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch, staged: list[str]
) -> None:
    """A test commit that stages anything but managed goldens is a second commit."""
    _model_prior_commit(mod, monkeypatch)
    msg = _write_msg(tmp_path, "test: pin the fix with a unit case\n\nEawf-Wave: P33-I01-W29\n")

    code, diag = mod.lint(msg, staged, subject_style="trailer")

    assert code == 1
    assert "second commit for wave P33-I01-W29" in diag
    assert "managed snapshot golden" in diag


def test_non_test_commit_cap_diagnostic_omits_the_golden_note(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The golden exemption note is only actionable for a test commit."""
    _model_prior_commit(mod, monkeypatch)
    msg = _write_msg(tmp_path, "fix: a second bite\n\nEawf-Wave: P33-I01-W29\n")

    code, diag = mod.lint(msg, ["src/eawf/x.py"], subject_style="trailer")

    assert code == 1
    assert "second commit for wave P33-I01-W29" in diag
    assert "managed snapshot golden" not in diag


def test_state_commit_still_skips_the_cap(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the state type keeps its exemption."""
    _model_prior_commit(mod, monkeypatch)
    msg = _write_msg(tmp_path, "state: record the gate rows\n\nEawf-Wave: P33-I01-W29\n")

    code, diag = mod.lint(msg, _STATE_PATHS, subject_style="trailer")

    assert code == 0, diag


def test_managed_golden_dirs_match_the_snapshot_inventory(mod: Any) -> None:
    """The hook's hand-kept mirror equals the inventory the pairing gate watches."""
    inventory = tuple(sorted(f"{surface.golden_dir}/" for surface in SNAPSHOT_SURFACES.values()))
    assert inventory == mod._MANAGED_GOLDEN_DIRS


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("", False),
        ("tests/golden/state/", True),
        ("tests/golden/state/x.json", True),
        ("tests/golden/state/test_x.py", False),
        ("tests/golden/state", False),
        ("tests/golden/cli/x.txt", False),
        ("tests/snapshots/svg/golden/x.svg", True),
        ("tests/snapshots/statusline/x.txt", False),
    ],
    ids=[
        "empty",
        "dir-itself",
        "golden-file",
        "python",
        "no-trailing-slash",
        "unmanaged-tree",
        "snapshots-tree",
        "unmanaged-snapshot",
    ],
)
def test_is_managed_golden_boundaries(mod: Any, path: str, expected: bool) -> None:
    """Boundary sweep of the golden-only predicate."""
    assert mod._is_managed_golden(path) is expected


# ---------------------------------------------------------------------------
# A commit with no wave carrier is rejected while any phase is open.
# ---------------------------------------------------------------------------


def test_bare_chore_commit_rejected_while_a_phase_is_planned(tmp_path: Path, mod: Any) -> None:
    """The post-close stretch: nothing is current, but P33 is PLANNED."""
    state = _write_phases(tmp_path, {"P32": _phase("closed"), "P33": _phase("planned")})
    msg = _write_msg(tmp_path, "chore: repair the release notes\n")

    code, diag = mod.lint(msg, ["CHANGELOG.md"], state_path=state, subject_style="trailer")

    assert code == 1
    assert "missing Eawf-Wave trailer" in diag
    assert "a PLANNED or ACTIVE phase exists (P33)" in diag


def test_bare_chore_commit_rejected_while_a_phase_is_planned_under_bracket_style(
    tmp_path: Path, mod: Any
) -> None:
    """The bracket style rejects the same commit for its missing prefix."""
    state = _write_phases(tmp_path, {"P33": _phase("planned")})
    msg = _write_msg(tmp_path, "chore: repair the release notes\n")

    code, diag = mod.lint(msg, ["CHANGELOG.md"], state_path=state, subject_style="bracket")

    assert code == 1
    assert "bare conventional-commits subject rejected" in diag
    assert "every phase in state.json is CLOSED" in diag


def test_bare_chore_commit_rejected_while_a_phase_is_active_but_not_current(
    tmp_path: Path, mod: Any
) -> None:
    """An ACTIVE phase blocks even when ``current.phase_id`` was cleared."""
    state = _write_phases(tmp_path, {"P33": _phase("active")})
    msg = _write_msg(tmp_path, "fix: hotfix the hook\n")

    code, diag = mod.lint(msg, ["tools/x.py"], state_path=state, subject_style="trailer")

    assert code == 1
    assert "(P33)" in diag


def test_bare_chore_commit_rejection_names_every_open_phase(tmp_path: Path, mod: Any) -> None:
    """The diagnostic lists each blocking phase once, current phase included."""
    state = _write_phases(
        tmp_path,
        {"P32": _phase("closed"), "P33": _phase("active"), "P34": _phase("planned")},
        current_phase_id="P33",
    )
    msg = _write_msg(tmp_path, "chore: tidy\n")

    code, diag = mod.lint(msg, ["AGENTS.md"], state_path=state, subject_style="trailer")

    assert code == 1
    assert "(P33, P34)" in diag


@pytest.mark.parametrize(
    "phases",
    [
        {},
        {"P31": _phase("closed")},
        {"P30": _phase("archived"), "P31": _phase("closed")},
    ],
    ids=["no-phases", "one-closed", "closed-and-archived"],
)
def test_bare_chore_commit_accepted_when_every_phase_is_closed(
    tmp_path: Path, mod: Any, phases: dict[str, Any]
) -> None:
    """With nothing in flight a bare commit has no wave to name."""
    state = _write_phases(tmp_path, phases)
    msg = _write_msg(tmp_path, "chore: pre-flight scrub before the next propose\n")

    code, diag = mod.lint(msg, ["AGENTS.md"], state_path=state, subject_style="trailer")

    assert code == 0, diag


@pytest.mark.parametrize(
    "phase",
    [{"status": "paused"}, {"iter_ids": []}, "closed"],
    ids=["unknown-status", "missing-status", "non-object-row"],
)
def test_bare_chore_commit_rejected_for_an_unreadable_phase_row(
    tmp_path: Path, mod: Any, phase: object
) -> None:
    """Error path: a phase row that does not say CLOSED reads as in flight."""
    state = _write_phases(tmp_path, {"P33": phase})
    msg = _write_msg(tmp_path, "chore: tidy\n")

    code, diag = mod.lint(msg, ["AGENTS.md"], state_path=state, subject_style="trailer")

    assert code == 1
    assert "(P33)" in diag


def test_bare_state_commit_accepted_while_a_phase_is_planned(tmp_path: Path, mod: Any) -> None:
    """A bare state subject keeps the current-phase rule: PLANNED does not block it."""
    state = _write_phases(tmp_path, {"P33": _phase("planned")})
    msg = _write_msg(tmp_path, "state: record the P33 proposal\n")

    code, diag = mod.lint(msg, [".ea/state.json"], state_path=state, subject_style="trailer")

    assert code == 0, diag


def test_bare_state_commit_rejected_while_a_phase_is_current(tmp_path: Path, mod: Any) -> None:
    """Regression: a current phase still blocks a bare state subject."""
    state = _write_phases(tmp_path, {"P33": _phase("active")}, current_phase_id="P33")
    msg = _write_msg(tmp_path, "state: record something\n")

    code, diag = mod.lint(msg, [".ea/state.json"], state_path=state, subject_style="trailer")

    assert code == 1
    assert "missing Eawf-Wave trailer" in diag


def test_blocking_phase_ids_finds_state_from_the_working_directory(
    tmp_path: Path, mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no explicit path the census walks up from cwd to ``.ea/state.json``."""
    (tmp_path / ".ea").mkdir()
    payload = {
        "current": {"phase_id": None, "iter_id": None},
        "phases": {"P33": _phase("planned")},
        "iters": {},
        "waves": {},
    }
    (tmp_path / ".ea" / "state.json").write_text(json.dumps(payload), encoding="utf-8")
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert mod._blocking_phase_ids(None, commit_type="chore") == ["P33"]
    assert mod._blocking_phase_ids(None, commit_type="state") == []


def test_blocking_phase_ids_reads_corrupt_state_as_nothing_in_flight(
    tmp_path: Path, mod: Any
) -> None:
    """Error path: undecodable state yields no blocking phase (the lint rejects it upstream)."""
    state = tmp_path / "state.json"
    state.write_text("{not-json", encoding="utf-8")

    assert mod._blocking_phase_ids(state, commit_type="chore") == []


def test_module_docstring_states_the_golden_only_and_open_phase_rules(mod: Any) -> None:
    """The lint's own docstring carries both rules a reader needs."""
    doc = mod.__doc__
    assert "Open-phase trailer rule" in doc
    assert "PLANNED or ACTIVE" in doc
    assert "golden-only" in doc
    assert "managed snapshot golden" in doc
