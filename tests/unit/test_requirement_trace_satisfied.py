"""The typed satisfied disposition in ``tools/requirement_trace.py``.

An id a closed phase already built, but no wave text cites, is accounted for
by a satisfaction naming the admitting decision, the closed phase and the
commit that built it. The gate-fire proofs are the refusals: a commit the
repository cannot resolve, a phase that is open or missing, a commit that
names another phase and a listed test that does not exist each raise rather
than letting an id read as satisfied on evidence nobody can inspect.

Every fixture uses placeholder families (``ABC``, ``DEF``) so this module
never reads as a test of a real packet id when the live trace scans it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tools.requirement_trace import (
    CATALOG_PATH,
    STATE_PATH,
    Catalog,
    Deferral,
    Satisfaction,
    StateView,
    TraceInputError,
    TraceStatus,
    _named_phases,
    build_trace,
    load_catalog,
    load_state,
    main,
    render,
)

pytestmark = pytest.mark.unit

_TITLES = {
    "ABC-001": "Owned thing",
    "ABC-002": "Built thing",
    "ABC-003": "Another built thing",
    "DEF-001": "Deferred thing",
}
_UNKNOWN_SHA = "0" * 40
_REPO_ROOT = Path(__file__).resolve().parents[2]
#: The committed catalog's first satisfaction: a commit this repository's
#: history holds, and the phase that commit names.
_BUILT = load_catalog(_REPO_ROOT / CATALOG_PATH).satisfactions[0]
_OTHER_CLOSED_PHASE = "P01" if _BUILT.phase != "P01" else "P02"
_BUILT_TEST = "tests/test_built.py"


def _state(*, phase_status: str = "closed", decision_status: str = "active") -> dict[str, Any]:
    return {
        "waves": {
            "P09-I01-W01": {"id": "P09-I01-W01", "status": "closed", "title": "Land ABC-001"}
        },
        "decisions": {
            "D01": {"id": "D01", "status": "active"},
            "D02": {"id": "D02", "status": decision_status},
        },
        "phases": {
            _BUILT.phase: {"status": phase_status},
            _OTHER_CLOSED_PHASE: {"status": "closed"},
            "P99": {"status": "active"},
        },
    }


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An epoch-1 tree whose git calls resolve against this repository's history."""
    monkeypatch.setenv("GIT_DIR", str(_REPO_ROOT / ".git"))
    (tmp_path / ".ea").mkdir()
    (tmp_path / STATE_PATH).write_text(json.dumps(_state()), encoding="utf-8")
    (tmp_path / _BUILT_TEST).parent.mkdir()
    (tmp_path / _BUILT_TEST).touch()
    return tmp_path


def _head(_repo: Path) -> str:
    """Return a commit this repository's history holds: the committed catalog's first."""
    return _BUILT.commit


def _blob_sha(path: Path) -> str:
    """Return the git object id of *path*'s content, which names a blob, not a commit."""
    content = path.read_bytes()
    return hashlib.sha1(b"blob %d\x00" % len(content) + content, usedforsecurity=False).hexdigest()


def _satisfaction(repo: Path, **overrides: Any) -> Satisfaction:
    fields: dict[str, Any] = {
        "decision": "D02",
        "ids": ("ABC-002", "ABC-003"),
        "phase": _BUILT.phase,
        "commit": _head(repo),
        "tests": (_BUILT_TEST,),
    }
    fields.update(overrides)
    return Satisfaction.model_validate(fields)


def _trace(repo: Path, satisfactions: tuple[Satisfaction, ...], **state: Any) -> Catalog:
    return build_trace(
        titles=_TITLES,
        deferrals=(Deferral(decision="D01", release="9.9.1", ids=("DEF-001",)),),
        satisfactions=satisfactions,
        state=StateView.model_validate(_state(**state)),
        repo_root=repo,
    )


def test_a_satisfied_disposition_gives_its_ids_status_satisfied(repo: Path) -> None:
    catalog = _trace(repo, (_satisfaction(repo),))

    rows = {row.id: row for row in catalog.requirements}
    assert rows["ABC-001"].status is TraceStatus.OWNED
    assert rows["ABC-002"].status is TraceStatus.SATISFIED
    assert rows["ABC-002"].satisfied_by == _head(repo)
    assert rows["ABC-003"].status is TraceStatus.SATISFIED
    assert rows["DEF-001"].status is TraceStatus.DEFERRED
    assert (catalog.summary.satisfied, catalog.summary.unowned) == (2, 0)


def test_without_the_disposition_the_built_ids_are_unowned(repo: Path) -> None:
    catalog = _trace(repo, ())

    assert catalog.summary.unowned == 2
    assert catalog.summary.satisfied == 0


def test_a_planted_unknown_sha_raises(repo: Path) -> None:
    with pytest.raises(TraceInputError, match="cannot resolve"):
        _trace(repo, (_satisfaction(repo, commit=_UNKNOWN_SHA),))


def test_a_sha_naming_a_blob_rather_than_a_commit_raises(repo: Path) -> None:
    blob = _blob_sha(_REPO_ROOT / "LICENSE")

    with pytest.raises(TraceInputError, match="cannot resolve"):
        _trace(repo, (_satisfaction(repo, commit=blob),))


def test_a_phase_that_is_not_closed_raises(repo: Path) -> None:
    with pytest.raises(TraceInputError, match="not a closed phase"):
        _trace(repo, (_satisfaction(repo, phase="P99"),))


def test_a_phase_the_state_lacks_raises(repo: Path) -> None:
    with pytest.raises(TraceInputError, match="P77, which is not a closed phase"):
        _trace(repo, (_satisfaction(repo, phase="P77"),))


def test_a_commit_naming_another_phase_raises(repo: Path) -> None:
    """Gate-fire: a closed phase cited for a commit whose carriers name another."""
    with pytest.raises(
        TraceInputError,
        match=f"cites {_OTHER_CLOSED_PHASE}, but the commit names {_BUILT.phase}",
    ):
        _trace(repo, (_satisfaction(repo, phase=_OTHER_CLOSED_PHASE),))


def test_a_listed_test_that_does_not_exist_raises(repo: Path) -> None:
    with pytest.raises(TraceInputError, match=r"tests that do not exist: tests/test_gone\.py"):
        _trace(repo, (_satisfaction(repo, tests=(_BUILT_TEST, "tests/test_gone.py")),))


def test_named_phases_reads_trailers_and_the_bracketed_subject() -> None:
    assert _named_phases("") == set()
    assert _named_phases("fix: no carrier\n\nsee Eawf-Wave: P05-I01-W01\n") == set()
    assert _named_phases("[P03-W02] feat: x\n") == {"P03"}
    assert _named_phases("[P03] state: x\n") == {"P03"}
    assert _named_phases("feat: x\n\nEawf-Wave: P33-I01-W01\nEawf-Wave: P34-I01-W02\n") == {
        "P33",
        "P34",
    }


def test_an_imported_milestone_completed_status_counts_as_closed(repo: Path) -> None:
    catalog = _trace(repo, (_satisfaction(repo),), phase_status="COMPLETED")

    assert catalog.summary.satisfied == 2


def test_a_decision_the_state_lacks_raises(repo: Path) -> None:
    with pytest.raises(TraceInputError, match="D99 satisfaction"):
        _trace(repo, (_satisfaction(repo, decision="D99"),))


def test_a_decision_out_of_force_satisfies_nothing(repo: Path) -> None:
    catalog = _trace(repo, (_satisfaction(repo),), decision_status="superseded")

    assert catalog.summary.satisfied == 0
    assert catalog.summary.unowned == 2


def test_an_id_the_catalog_lacks_raises(repo: Path) -> None:
    with pytest.raises(TraceInputError, match="ids the catalog lacks: ABC-404"):
        _trace(repo, (_satisfaction(repo, ids=("ABC-404",)),))


def test_an_id_both_deferred_and_satisfied_raises(repo: Path) -> None:
    with pytest.raises(TraceInputError, match="a deferral moves out: DEF-001"):
        _trace(repo, (_satisfaction(repo, ids=("DEF-001",)),))


@pytest.mark.parametrize(
    "overrides",
    [
        {"ids": ()},
        {"commit": "abc123"},
        {"commit": "G" * 40},
        {"phase": "P8"},
        {"phase": "I08"},
        {"decision": "72"},
        {"note": "an unknown field"},
    ],
)
def test_the_model_refuses_malformed_dispositions(repo: Path, overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _satisfaction(repo, **overrides)


def test_a_catalog_with_an_extra_satisfaction_key_does_not_load(repo: Path) -> None:
    catalog = _trace(repo, (_satisfaction(repo),))
    document = json.loads(render(catalog))
    document["satisfactions"][0]["cluster"] = "not declared"
    (repo / CATALOG_PATH).write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(TraceInputError, match="cannot read catalog"):
        load_catalog(repo / CATALOG_PATH)


def test_check_require_owned_passes_on_satisfied_ids_and_reds_on_a_planted_sha(
    repo: Path,
) -> None:
    catalog = _trace(repo, (_satisfaction(repo),))
    (repo / CATALOG_PATH).write_text(render(catalog), encoding="utf-8")
    assert load_state(repo / STATE_PATH).phases[_BUILT.phase].closed

    assert main(["--repo-root", str(repo), "check", "--require-owned"]) == 0

    document = json.loads(render(catalog))
    document["satisfactions"][0]["commit"] = _UNKNOWN_SHA
    (repo / CATALOG_PATH).write_text(json.dumps(document), encoding="utf-8")

    assert main(["--repo-root", str(repo), "check", "--require-owned"]) == 2
