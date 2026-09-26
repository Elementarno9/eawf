"""``tools/requirement_trace.py`` over a repository the epoch-2 cutover has marked.

After the cut ``.ea/state.json`` is frozen, so the trace reads its waves and
decisions from the selected generation. The generation here is built by the
real cutover over a corpus whose waves cite requirement ids in every field
the epoch-1 trace reads, so parity is checked against the importer's own
output: the same corpus must trace to the same owners and deferrals whichever
epoch it is read in.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.store.compaction import read_document, write_document
from tests.integration.kernel.migration._cutover_harness import (
    APPLIED_AT,
    apply_once,
    declared_canary,
    staged_corpus,
)
from tools.requirement_trace import (
    CATALOG_PATH,
    STATE_PATH,
    Deferral,
    StateView,
    TraceInputError,
    build_trace,
    load_state,
    load_state_view,
    main,
    render,
)

pytestmark = pytest.mark.unit

_TITLES = {f"REQ-{number:03d}": f"Requirement {number}" for number in range(1, 10)}
_DEFERRAL = Deferral(decision="D01", release="v0.8", ids=("REQ-007",))

#: One wave per cited field, each owning its own id, plus the shapes that own
#: nothing: an abandoned wave, a failed wave and a backlog row.
_CITING_WAVES: dict[str, dict[str, Any]] = {
    "P02-I01-W01": {"status": "closed", "title": "Land REQ-001"},
    "P02-I01-W02": {"status": "closed", "description": "covers REQ-002"},
    "P02-I01-W03": {"status": "in_progress", "intent": "deliver REQ-005"},
    "P02-I01-W04": {"status": "claimed", "outcome": "shipped REQ-004"},
    "P02-I01-W05": {"status": "pending", "criterion": "the gate proves REQ-003"},
    "P02-I01-W06": {"status": "abandoned", "title": "Land REQ-006"},
    "P02-I01-W07": {"status": "failed", "title": "Land REQ-009"},
}
_DROPPED_OWNER = "P02-I01-W01"


def _criterion(text: str) -> dict[str, Any]:
    return {
        "acceptance_style": "binary",
        "evidence_kind": "attested",
        "gate_ids": [],
        "id": "CR-01",
        "kind": "attested",
        "measurable_signal": "the attested gate run exits zero",
        "oracle_tier": None,
        "quality_dimension": "maintainability",
        "required": True,
        "response": None,
        "text": text,
        "waiver_reason": None,
    }


def _citing_document(corpus: Path) -> dict[str, Any]:
    """Seed the corpus with waves, a backlog row and a decision that cite ids."""
    document_path = corpus / "document.json"
    document = json.loads(document_path.read_text(encoding="utf-8"))
    document["phases"]["P02"].update(status="active")
    document["iters"]["P02-I01"].update(status="active", wave_ids=list(_CITING_WAVES))
    template = document["waves"]["P02-I01-W01"]
    for wave_id, spec in _CITING_WAVES.items():
        row = {**copy.deepcopy(template), "id": wave_id, "status": spec["status"]}
        row["title"] = spec.get("title", f"Land {wave_id}")
        for field in ("description", "intent", "outcome"):
            if field in spec:
                row[field] = spec[field]
        if "criterion" in spec:
            row["success_criteria"] = [_criterion(spec["criterion"])]
        document["waves"][wave_id] = row
    first_backlog = next(iter(document["backlog"]))
    document["backlog"][first_backlog]["title"] = "Follow up on REQ-008"
    document["decisions"]["D01"]["status"] = "active"
    document["current"] = {"phase_id": "P02", "iter_id": "P02-I01", "wave_id": None}
    document_path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    return document


@pytest.fixture(scope="module")
def cut(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    """A repository whose ``.ea`` was cut over from the citing corpus."""
    root = tmp_path_factory.mktemp("epoch2_trace")
    corpus = staged_corpus(root)
    document = _citing_document(corpus)
    apply_once(corpus=corpus, target_root=declared_canary(root / ".ea"), applied_at=APPLIED_AT)
    return root, document


@pytest.fixture()
def repo(cut: tuple[Path, dict[str, Any]], tmp_path: Path) -> Path:
    """A private copy of the marked repository, with nothing to scan beside ``.ea``."""
    root = tmp_path / "repo"
    shutil.copytree(cut[0] / ".ea", root / ".ea")
    return root


@pytest.fixture()
def epoch1(cut: tuple[Path, dict[str, Any]], tmp_path: Path) -> Path:
    """An unmarked repository whose live document is the same corpus."""
    root = tmp_path / "epoch1"
    (root / ".ea").mkdir(parents=True)
    (root / STATE_PATH).write_text(json.dumps(cut[1]), encoding="utf-8")
    return root


def _trace(state: StateView, repo_root: Path) -> dict[str, tuple[str, tuple[str, ...]]]:
    catalog = build_trace(titles=_TITLES, deferrals=(_DEFERRAL,), state=state, repo_root=repo_root)
    return {row.id: (row.status.value, row.owners) for row in catalog.requirements}


def _generation_document(repo_root: Path) -> Path:
    generations = repo_root / ".ea" / "generations"
    return next(path for path in generations.iterdir() if path.name[:4] == "gen-") / "state.json"


def test_marked_repository_traces_like_the_epoch1_reader_over_the_same_corpus(
    repo: Path, epoch1: Path
) -> None:
    epoch2_trace = _trace(load_state_view(repo), repo)
    epoch1_trace = _trace(load_state(epoch1 / STATE_PATH), epoch1)

    assert epoch2_trace == epoch1_trace
    assert epoch2_trace == {
        "REQ-001": ("owned", ("P02-I01-W01",)),
        "REQ-002": ("owned", ("P02-I01-W02",)),
        "REQ-003": ("owned", ("P02-I01-W05",)),
        "REQ-004": ("owned", ("P02-I01-W04",)),
        "REQ-005": ("owned", ("P02-I01-W03",)),
        "REQ-006": ("unowned", ()),
        "REQ-007": ("deferred", ()),
        "REQ-008": ("unowned", ()),
        "REQ-009": ("unowned", ()),
    }


def test_marked_repository_ignores_the_frozen_document(repo: Path) -> None:
    """The frozen ``state.json`` owns nothing; only the generation is read."""
    (repo / STATE_PATH).write_text(json.dumps({"waves": {}, "decisions": {}}), encoding="utf-8")

    assert _trace(load_state_view(repo), repo)["REQ-001"] == ("owned", ("P02-I01-W01",))


def test_unmarked_repository_reads_state_json(epoch1: Path) -> None:
    assert load_state_view(epoch1) == load_state(epoch1 / STATE_PATH)


def test_native_tasks_cite_their_intent_and_criteria(repo: Path) -> None:
    document_path = _generation_document(repo)
    document = read_document(document_path)
    document["task"]["EAWF-0001"] = {
        "key": "EAWF-0001",
        "status": "PLANNED",
        "intent": "native work on REQ-006",
        "criteria": [{"id": "CR-01", "text": "and on REQ-009"}],
    }
    document["task"]["EAWF-0002"] = {
        "key": "EAWF-0002",
        "status": "CANCELLED",
        "intent": "abandoned work on REQ-008",
        "criteria": [],
    }
    write_document(document_path, document)

    trace = _trace(load_state_view(repo), repo)

    assert trace["REQ-006"] == ("owned", ("EAWF-0001",))
    assert trace["REQ-009"] == ("owned", ("EAWF-0001",))
    assert trace["REQ-008"] == ("unowned", ())


def test_a_superseded_decision_in_the_ledger_defers_nothing(repo: Path) -> None:
    ledger = _generation_document(repo).parent / "ledger" / "decision.jsonl"
    lines = ledger.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["payload"]["payload"]["superseded_by"] = "D02"
    ledger.write_text("\n".join([*lines, json.dumps(first)]) + "\n", encoding="utf-8")

    assert _trace(load_state_view(repo), repo)["REQ-007"] == ("unowned", ())


def test_check_is_fresh_on_a_marked_repository_and_reds_when_an_owner_is_dropped(
    repo: Path, epoch1: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Gate-fire: the census written from epoch 1 is stale once one owning Task goes."""
    stored = build_trace(
        titles=_TITLES,
        deferrals=(_DEFERRAL,),
        state=load_state(epoch1 / STATE_PATH),
        repo_root=epoch1,
    )
    (repo / CATALOG_PATH).write_text(render(stored), encoding="utf-8")
    assert main(["--repo-root", str(repo), "check"]) == 0

    ledger = _generation_document(repo).parent / "ledger" / "task.jsonl"
    kept = [
        line
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["record_key"] != _DROPPED_OWNER
    ]
    ledger.write_text("\n".join(kept) + "\n", encoding="utf-8")

    assert main(["--repo-root", str(repo), "check"]) == 1
    assert "REQ-001" in capsys.readouterr().err


def test_an_unreadable_generation_is_a_trace_input_error(repo: Path) -> None:
    _generation_document(repo).write_text("{", encoding="utf-8")

    with pytest.raises(TraceInputError, match="cannot read generation"):
        load_state_view(repo)
    assert main(["--repo-root", str(repo), "check"]) == 2
