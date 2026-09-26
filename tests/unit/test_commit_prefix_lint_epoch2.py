"""``tools/commit_prefix_lint.py`` on a root the epoch-2 cutover has marked.

After the cut ``.ea/state.json`` is frozen and the lifecycle lives in the
selected generation, so the hook's claimed proof, hierarchy check and
phase-blocking check read the generation instead. The generation here is
built by the real cutover over a corpus with one ACTIVE phase and iter and
a CLAIMED, a PLANNED and a RUNNING wave, so the stdlib reader is proved
against the importer's own output rather than a hand-written imitation.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.migration.epoch2.canary import (
    CANARY_DECLARATION_FILENAME,
    GENERATIONS_DIRNAME,
    MARKER_FILENAME,
    OPT_IN_DECLARATION_FILENAME,
)
from eawf.kernel.state.epoch2.authority import resolve_authority
from tests.integration.kernel.migration._cutover_harness import (
    APPLIED_AT,
    apply_once,
    declared_canary,
    staged_corpus,
)

pytestmark = pytest.mark.unit

_TOOL_DIR = Path(__file__).resolve().parents[2] / "tools"
_CLAUDE_TRAILER = "\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n"
_CLAIMED, _PLANNED, _RUNNING = "P02-I01-W01", "P02-I01-W02", "P02-I01-W03"
_WAVE_STATUSES = {_CLAIMED: "claimed", _PLANNED: "pending", _RUNNING: "in_progress"}


def _load(name: str) -> Any:
    if str(_TOOL_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOL_DIR))
    spec = importlib.util.spec_from_file_location(name, _TOOL_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def mod(monkeypatch: pytest.MonkeyPatch) -> Any:
    lint = _load("commit_prefix_lint")
    # The one-commit-per-wave cap shells out to the real history; these
    # cases are about which state the proof reads, not about the cap.
    monkeypatch.setattr(lint, "_prior_wave_commits", lambda *_a, **_kw: [])
    return lint


@pytest.fixture()
def view_mod() -> Any:
    return _load("epoch2_lifecycle_view")


def _in_flight_document(corpus: Path) -> dict[str, Any]:
    """Put P02 and P02-I01 in flight with one wave in each live status."""
    document_path = corpus / "document.json"
    document = json.loads(document_path.read_text(encoding="utf-8"))
    document["phases"]["P02"].update(status="active", iter_ids=["P02-I01"])
    template = document["waves"]["P02-I01-W01"]
    for wave_id, status in _WAVE_STATUSES.items():
        document["waves"][wave_id] = {
            **copy.deepcopy(template),
            "id": wave_id,
            "status": status,
            "title": f"Land {wave_id}",
        }
    document["iters"]["P02-I01"].update(status="active", wave_ids=list(_WAVE_STATUSES))
    document["current"] = {"phase_id": "P02", "iter_id": "P02-I01", "wave_id": None}
    document_path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    return document


@pytest.fixture(scope="module")
def cut(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, Any]]:
    """A marked ``.ea`` root cut over from the in-flight corpus, and that corpus."""
    root = tmp_path_factory.mktemp("epoch2_lint")
    corpus = staged_corpus(root)
    document = _in_flight_document(corpus)
    target = declared_canary(root / ".ea")
    apply_once(corpus=corpus, target_root=target, applied_at=APPLIED_AT)
    return target, document


@pytest.fixture()
def marked(cut: tuple[Path, dict[str, Any]], tmp_path: Path) -> Path:
    """A private copy of the marked root, whose frozen document is stale on purpose.

    The frozen ``state.json`` claims every wave, so a lint that read it
    instead of the generation would accept the PLANNED wave.
    """
    root = tmp_path / ".ea"
    shutil.copytree(cut[0], root)
    stale = copy.deepcopy(cut[1])
    for wave_id in _WAVE_STATUSES:
        stale["waves"][wave_id]["status"] = "claimed"
    (root / "state.json").write_text(json.dumps(stale), encoding="utf-8")
    return root


@pytest.fixture()
def unmarked(cut: tuple[Path, dict[str, Any]], tmp_path: Path) -> Path:
    """An epoch-1 root holding the same corpus as its live document."""
    root = tmp_path / "epoch1" / ".ea"
    root.mkdir(parents=True)
    (root / "state.json").write_text(json.dumps(cut[1]), encoding="utf-8")
    return root


def _message(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "COMMIT_EDITMSG"
    path.write_text(body.rstrip() + _CLAUDE_TRAILER, encoding="utf-8")
    return path


def _lint(mod: Any, tmp_path: Path, ea_dir: Path, body: str, staged: list[str]) -> tuple[int, str]:
    state = ea_dir / "state.json"
    return mod.lint(
        _message(tmp_path, body),
        staged,
        env={},
        state_path=state,
        canonical_state_path=state,
        subject_style="trailer",
    )


# --- the epoch test mirrors the package resolver ------------------------------


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload, "utf-8")


_MARKER = {
    "schema_version": "1",
    "epoch": 2,
    "generation_id": "gen-0123456789abcdef",
    "manifest_digest": "a" * 64,
    "generation_digest": "b" * 64,
    "written_at": "2026-03-03T00:00:00Z",
}
_CANARY = {"disposable": True, "declared_by": "lint-test", "purpose": "epoch parity"}
_OPT_IN = {
    "opt_in": True,
    "declared_by": "lint-test",
    "purpose": "epoch parity",
    "backup_ts": "2026-02-02T00-00-00Z",
    "backup_digest": "c" * 64,
}


@pytest.mark.parametrize(
    "files",
    [
        {},
        {CANARY_DECLARATION_FILENAME: _CANARY},
        {f"{GENERATIONS_DIRNAME}/{MARKER_FILENAME}": _MARKER},
        {CANARY_DECLARATION_FILENAME: _CANARY, f"{GENERATIONS_DIRNAME}/{MARKER_FILENAME}": _MARKER},
        {OPT_IN_DECLARATION_FILENAME: _OPT_IN, f"{GENERATIONS_DIRNAME}/{MARKER_FILENAME}": _MARKER},
        {
            CANARY_DECLARATION_FILENAME: _CANARY,
            OPT_IN_DECLARATION_FILENAME: _OPT_IN,
            f"{GENERATIONS_DIRNAME}/{MARKER_FILENAME}": _MARKER,
        },
        {CANARY_DECLARATION_FILENAME: _CANARY, f"{GENERATIONS_DIRNAME}/{MARKER_FILENAME}": "{"},
        {
            CANARY_DECLARATION_FILENAME: {**_CANARY, "disposable": False},
            f"{GENERATIONS_DIRNAME}/{MARKER_FILENAME}": _MARKER,
        },
        {
            CANARY_DECLARATION_FILENAME: _CANARY,
            f"{GENERATIONS_DIRNAME}/{MARKER_FILENAME}": {**_MARKER, "generation_id": "G-1"},
        },
    ],
    ids=[
        "bare",
        "declared-only",
        "marker-only",
        "canary",
        "opt-in",
        "both-declarations",
        "torn-marker",
        "false-claim",
        "bad-generation-id",
    ],
)
def test_epoch_test_agrees_with_the_package_resolver(
    view_mod: Any, tmp_path: Path, files: dict[str, object]
) -> None:
    root = tmp_path / ".ea"
    root.mkdir()
    for locator, payload in files.items():
        _write(root / locator, payload)

    generation = view_mod.epoch2_generation(root)
    authority = resolve_authority(root)

    assert (generation is not None) == (authority.epoch == 2)
    if generation is not None:
        assert generation.name == authority.generation_id


def test_the_view_projects_the_importer_output_back_onto_the_corpus(
    view_mod: Any, cut: tuple[Path, dict[str, Any]]
) -> None:
    """Statuses and both hierarchy directions round-trip through the cutover."""
    root, document = cut
    generation = view_mod.epoch2_generation(root)
    assert generation is not None
    view = view_mod.generation_lifecycle_view(generation)

    assert {key: row["status"] for key, row in view["phases"].items()} == {
        key: row["status"] for key, row in document["phases"].items()
    }
    assert {key: row["status"] for key, row in view["iters"].items()} == {
        key: row["status"] for key, row in document["iters"].items()
    }
    assert {key: row["status"] for key, row in view["waves"].items()} == {
        key: row["status"] for key, row in document["waves"].items()
    }
    for key, row in document["waves"].items():
        assert view["waves"][key]["iter_id"] == row["iter_id"]
        assert key in view["iters"][row["iter_id"]]["wave_ids"]
    for key, row in document["iters"].items():
        assert view["iters"][key]["phase_id"] == row["phase_id"]
        assert key in view["phases"][row["phase_id"]]["iter_ids"]
    assert view["current"] == {"phase_id": "P02", "iter_id": "P02-I01"}


# --- the trailer proof reads the generation -----------------------------------


@pytest.mark.parametrize("wave_id", [_CLAIMED, _RUNNING])
def test_marked_root_accepts_a_trailer_naming_a_live_imported_task(
    mod: Any, tmp_path: Path, marked: Path, wave_id: str
) -> None:
    code, diag = _lint(
        mod, tmp_path, marked, f"feat: land it\n\nEawf-Wave: {wave_id}\n", ["src/eawf/x.py"]
    )

    assert code == 0, diag


def test_marked_root_refuses_a_trailer_naming_a_planned_task(
    mod: Any, tmp_path: Path, marked: Path
) -> None:
    """Gate-fire: the frozen document claims the wave, the generation does not."""
    code, diag = _lint(
        mod, tmp_path, marked, f"feat: land it\n\nEawf-Wave: {_PLANNED}\n", ["src/eawf/x.py"]
    )

    assert code == 1
    assert f"claimed proof rejected for wave {_PLANNED!r}" in diag
    assert "'pending'" in diag


def test_marked_root_refuses_a_trailer_naming_a_task_the_generation_lacks(
    mod: Any, tmp_path: Path, marked: Path
) -> None:
    code, diag = _lint(
        mod, tmp_path, marked, "feat: land it\n\nEawf-Wave: P02-I01-W09\n", ["src/eawf/x.py"]
    )

    assert code == 1
    assert "unknown wave reference" in diag


def test_marked_root_blocks_a_bare_subject_while_its_milestone_is_active(
    mod: Any, tmp_path: Path, marked: Path
) -> None:
    code, diag = _lint(mod, tmp_path, marked, "fix: drive-by\n", ["src/eawf/x.py"])

    assert code == 1
    assert "P02" in diag


def test_marked_root_with_a_torn_generation_refuses_rather_than_reading_the_frozen_file(
    mod: Any, tmp_path: Path, marked: Path
) -> None:
    generation = next(p for p in (marked / GENERATIONS_DIRNAME).iterdir() if p.name[:4] == "gen-")
    (generation / "state.json").write_text("{", encoding="utf-8")

    code, diag = _lint(
        mod, tmp_path, marked, f"feat: land it\n\nEawf-Wave: {_CLAIMED}\n", ["src/eawf/x.py"]
    )

    assert code == 1
    assert "managed state decode failed" in diag


# --- the state-commit whitelist -----------------------------------------------

_EPOCH2_PATHS = [
    ".ea/generations/gen-0123456789abcdef/ledger/task.jsonl",
    ".ea/generations/gen-0123456789abcdef/state.json",
    ".ea/epoch2-opt-in.json",
]


def test_marked_root_admits_generation_paths_in_a_state_commit(
    mod: Any, tmp_path: Path, marked: Path
) -> None:
    code, diag = _lint(mod, tmp_path, marked, "[P02] state: record the move\n", _EPOCH2_PATHS)

    assert code == 0, diag


def test_marked_root_still_refuses_source_paths_in_a_state_commit(
    mod: Any, tmp_path: Path, marked: Path
) -> None:
    code, diag = _lint(
        mod, tmp_path, marked, "[P02] state: record\n", [*_EPOCH2_PATHS, "src/eawf/x.py"]
    )

    assert code == 1
    assert "['src/eawf/x.py']" in diag


# --- an unmarked root keeps the epoch-1 behaviour ------------------------------


def test_unmarked_root_refuses_generation_paths_in_a_state_commit(
    mod: Any, tmp_path: Path, unmarked: Path
) -> None:
    code, diag = _lint(mod, tmp_path, unmarked, "[P02] state: record\n", _EPOCH2_PATHS)

    assert code == 1
    assert "state-type commit touches non-state paths" in diag
    assert ".ea/generations" not in diag.split("\n", 1)[1]


def test_unmarked_root_proves_the_trailer_against_its_live_document(
    mod: Any, tmp_path: Path, unmarked: Path
) -> None:
    accepted = _lint(
        mod, tmp_path, unmarked, f"feat: a\n\nEawf-Wave: {_CLAIMED}\n", ["src/eawf/x.py"]
    )
    refused = _lint(
        mod, tmp_path, unmarked, f"feat: b\n\nEawf-Wave: {_PLANNED}\n", ["src/eawf/x.py"]
    )

    assert accepted[0] == 0, accepted[1]
    assert refused[0] == 1
    assert "'pending'" in refused[1]


# --- the hook stays runnable under system python3 ------------------------------


@pytest.mark.parametrize("name", ["commit_prefix_lint", "epoch2_lifecycle_view"])
def test_the_hook_imports_only_the_standard_library_and_sibling_tools(name: str) -> None:
    tree = ast.parse((_TOOL_DIR / f"{name}.py").read_text(encoding="utf-8"))
    siblings = {path.stem for path in _TOOL_DIR.glob("*.py")}
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert imported <= sys.stdlib_module_names | siblings, imported
