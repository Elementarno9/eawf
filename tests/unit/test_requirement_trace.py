"""Unit tests for ``tools/requirement_trace.py``, the LINT-038 requirement census.

Every fixture uses placeholder families (``ABC``, ``DEF``) so this module
never reads as a test of a real packet id when the live trace scans it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tools.requirement_trace import (
    CATALOG_PATH,
    MAX_RANGE_SPAN,
    STATE_PATH,
    TITLE_MAX_CHARS,
    Catalog,
    Deferral,
    StateView,
    TraceInputError,
    TraceStatus,
    build_trace,
    cited_ids,
    extract_catalog,
    load_catalog,
    main,
    render,
    scan_citations,
    stale_ids,
)

_FAMILIES = frozenset({"ABC", "DEF"})
_TITLES = {"ABC-001": "First thing", "ABC-002": "Second thing", "DEF-001": "Deferred thing"}


def _wave(wave_id: str, text: str, status: str = "closed") -> dict[str, Any]:
    return {
        "id": wave_id,
        "status": status,
        "title": "t",
        "description": text,
        "intent": None,
        "success_criteria": [],
        "outcome": None,
        "unrelated": {"kept": "out of the view"},
    }


def _state(waves: list[dict[str, Any]], *, decision_status: str = "active") -> dict[str, Any]:
    return {
        "schema_version": "1.19",
        "waves": {wave["id"]: wave for wave in waves},
        "decisions": {"D01": {"id": "D01", "status": decision_status, "superseded_by": None}},
    }


def _repo(tmp_path: Path, state: dict[str, Any]) -> Path:
    (tmp_path / ".ea").mkdir(exist_ok=True)
    (tmp_path / STATE_PATH).write_text(json.dumps(state), encoding="utf-8")
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "test_x.py").write_text("# pins ABC-001\n", encoding="utf-8")
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "mod.py").write_text("# produces ABC-001..002\n", encoding="utf-8")
    return tmp_path


def _deferrals() -> tuple[Deferral, ...]:
    return (Deferral(decision="D01", release="9.9.1", ids=("DEF-001",)),)


def _write_census(repo: Path) -> Catalog:
    catalog = build_trace(
        titles=_TITLES,
        deferrals=_deferrals(),
        state=StateView.model_validate_json((repo / STATE_PATH).read_bytes()),
        repo_root=repo,
    )
    (repo / CATALOG_PATH).write_text(render(catalog), encoding="utf-8")
    return catalog


def _rows(catalog: Catalog) -> dict[str, Any]:
    return {row.id: row for row in catalog.requirements}


# --- cited_ids -------------------------------------------------------------


def test_cited_ids_empty_text() -> None:
    assert cited_ids("", _FAMILIES) == set()


def test_cited_ids_single_id() -> None:
    assert cited_ids("see ABC-007 here", _FAMILIES) == {"ABC-007"}


@pytest.mark.parametrize(
    "text",
    [
        "ABC-001..003",
        "ABC-001 to ABC-003",
        "`ABC-001`-`ABC-003`",
        "ABC-001 through 003",
        "ABC-001\u2013003",
    ],
)
def test_cited_ids_range_forms_expand(text: str) -> None:
    assert cited_ids(text, _FAMILIES) == {"ABC-001", "ABC-002", "ABC-003"}


def test_cited_ids_continuation_list() -> None:
    assert cited_ids("ABC-001..002, 005, and 010..011", _FAMILIES) == {
        "ABC-001",
        "ABC-002",
        "ABC-005",
        "ABC-010",
        "ABC-011",
    }


def test_cited_ids_backwards_range_keeps_first() -> None:
    assert cited_ids("ABC-009..003", _FAMILIES) == {"ABC-009"}


def test_cited_ids_range_span_boundary() -> None:
    at_cap = cited_ids(f"ABC-000..{MAX_RANGE_SPAN:03d}", _FAMILIES)
    over_cap = cited_ids(f"ABC-000..{MAX_RANGE_SPAN + 1:03d}", _FAMILIES)
    assert len(at_cap) == MAX_RANGE_SPAN + 1
    assert over_cap == {"ABC-000"}


def test_cited_ids_ignores_unknown_family_and_wide_numbers() -> None:
    assert cited_ids("XYZ-001, ABC-0012, ABC-01", _FAMILIES) == set()


def test_cited_ids_empty_families_raises() -> None:
    with pytest.raises(ValueError, match="at least one family"):
        cited_ids("ABC-001", frozenset())


# --- build_trace and the check gate ---------------------------------------


def test_build_trace_owned_deferred_unowned(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _state([_wave("P01-I01-W01", "ships ABC-001")]))
    rows = _rows(_write_census(repo))
    assert rows["ABC-001"].status is TraceStatus.OWNED
    assert rows["ABC-001"].owners == ("P01-I01-W01",)
    assert rows["ABC-001"].tests == ("tests/test_x.py",)
    assert rows["ABC-001"].producers == ("src/mod.py",)
    assert rows["ABC-002"].status is TraceStatus.UNOWNED
    assert rows["ABC-002"].producers == ("src/mod.py",)
    assert rows["DEF-001"].status is TraceStatus.DEFERRED
    assert rows["DEF-001"].deferral == "D01"


def test_build_trace_deferred_id_is_not_unowned_without_any_wave(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _state([]))
    catalog = _write_census(repo)
    assert _rows(catalog)["DEF-001"].status is TraceStatus.DEFERRED
    assert catalog.summary.deferred == 1
    assert catalog.summary.unowned == 2


def test_build_trace_decision_out_of_force_defers_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _state([], decision_status="superseded"))
    assert _rows(_write_census(repo))["DEF-001"].status is TraceStatus.UNOWNED


def test_build_trace_dead_wave_owns_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _state([_wave("P01-I01-W01", "ABC-001", status="abandoned")]))
    assert _rows(_write_census(repo))["ABC-001"].status is TraceStatus.UNOWNED


def test_build_trace_deferral_naming_missing_decision_raises(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _state([]))
    with pytest.raises(TraceInputError, match="D02, which state does not carry"):
        build_trace(
            titles=_TITLES,
            deferrals=(Deferral(decision="D02", release="9.9.1", ids=("DEF-001",)),),
            state=StateView.model_validate_json((repo / STATE_PATH).read_bytes()),
            repo_root=repo,
        )


def test_build_trace_deferral_naming_unknown_id_raises(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _state([]))
    with pytest.raises(TraceInputError, match="catalog lacks: DEF-009"):
        build_trace(
            titles=_TITLES,
            deferrals=(Deferral(decision="D01", release="9.9.1", ids=("DEF-009",)),),
            state=StateView.model_validate_json((repo / STATE_PATH).read_bytes()),
            repo_root=repo,
        )


def test_main_check_fresh_census_exits_zero(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _state([_wave("P01-I01-W01", "ABC-001..002")]))
    _write_census(repo)
    assert main(["--repo-root", str(repo), "check", "--require-owned"]) == 0


def test_main_check_owner_removed_reds_as_unowned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Gate-fire proof: dropping the owning wave turns the id unowned and reds."""
    repo = _repo(tmp_path, _state([_wave("P01-I01-W01", "ABC-001..002")]))
    _write_census(repo)
    (repo / STATE_PATH).write_text(
        json.dumps(_state([_wave("P01-I01-W01", "ABC-001")])), encoding="utf-8"
    )
    assert main(["--repo-root", str(repo), "check"]) == 1
    assert "1 row(s) differ: ABC-002" in capsys.readouterr().err
    assert main(["--repo-root", str(repo), "write"]) == 0
    assert main(["--repo-root", str(repo), "check"]) == 0
    assert main(["--repo-root", str(repo), "check", "--require-owned"]) == 1
    err = capsys.readouterr().err
    assert "1 unowned id(s): ABC-002" in err
    assert "DEF-001" not in err


def test_main_check_revision_move_alone_passes(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _state([_wave("P01-I01-W01", "ABC-001")]))
    catalog = _write_census(repo)
    moved = catalog.model_copy(update={"revision": "0" * 40})
    (repo / CATALOG_PATH).write_text(render(moved), encoding="utf-8")
    assert main(["--repo-root", str(repo), "check"]) == 0


def test_main_check_new_test_citation_is_stale(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _state([_wave("P01-I01-W01", "ABC-001")]))
    _write_census(repo)
    (repo / "tests" / "test_y.py").write_text("# pins ABC-002\n", encoding="utf-8")
    assert main(["--repo-root", str(repo), "check"]) == 1


def test_main_missing_catalog_exits_two(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _state([]))
    assert main(["--repo-root", str(repo), "check"]) == 2


def test_load_catalog_extra_key_raises(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _state([]))
    payload = json.loads(render(_write_census(repo)))
    payload["stray"] = 1
    (repo / CATALOG_PATH).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(TraceInputError, match="stray"):
        load_catalog(repo / CATALOG_PATH)


def test_render_round_trips(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _state([_wave("P01-I01-W01", "ABC-001")]))
    catalog = _write_census(repo)
    assert load_catalog(repo / CATALOG_PATH) == catalog


def test_stale_ids_summary_only_difference() -> None:
    catalog = Catalog.model_validate_json(
        json.dumps(
            {
                "schema_version": 1,
                "summary": {"total": 1, "owned": 0, "deferred": 0, "unowned": 1},
                "requirements": [{"id": "ABC-001", "title": "t"}],
            }
        )
    )
    tampered = catalog.model_copy(
        update={"summary": catalog.summary.model_copy(update={"unowned": 9})}
    )
    assert stale_ids(tampered, catalog) == ["summary"]
    assert stale_ids(catalog, catalog) == []


# --- scan_citations ----------------------------------------------------------


def test_scan_citations_skips_missing_root(tmp_path: Path) -> None:
    assert scan_citations(tmp_path, ("absent",), _FAMILIES) == {}


# --- extract_catalog ---------------------------------------------------------

_PACKET = """# 10 - fixture

| ID | Requirement |
|---|---|
| `ABC-001` | **Bold lead wins.** Then more prose. |
| `ABC-002` | Plain first sentence. Second sentence. |

| ID | Producer |
|---|---|
| `ABC-003` | Not a definition table. |

| Rule | Assertion |
|---|---|
| `DEF-001` | {long} |
"""


def _packet(tmp_path: Path, body: str) -> Path:
    packet = tmp_path / "packet"
    packet.mkdir()
    (packet / "10-fixture.md").write_text(body, encoding="utf-8")
    (packet / "README.md").write_text("| ID | Requirement |\n|---|---|\n| `ABC-009` | x |\n")
    return packet


def test_extract_catalog_reads_definition_rows_only(tmp_path: Path) -> None:
    titles = extract_catalog(_packet(tmp_path, _PACKET.format(long="word " * 60)))
    assert list(titles) == ["ABC-001", "ABC-002", "DEF-001"]
    assert titles["ABC-001"] == "Bold lead wins"
    assert titles["ABC-002"] == "Plain first sentence"
    assert titles["DEF-001"].endswith("...")
    assert len(titles["DEF-001"]) <= TITLE_MAX_CHARS + 3


def test_extract_catalog_duplicate_id_raises(tmp_path: Path) -> None:
    body = "| ID | Requirement |\n|---|---|\n| `ABC-001` | a |\n| `ABC-001` | b |\n"
    with pytest.raises(TraceInputError, match="ABC-001 is defined twice"):
        extract_catalog(_packet(tmp_path, body))


def test_extract_catalog_title_leak_raises(tmp_path: Path) -> None:
    body = "| ID | Requirement |\n|---|---|\n| `ABC-001` | Mail someone@example.org now |\n"
    with pytest.raises(TraceInputError, match="path or an address"):
        extract_catalog(_packet(tmp_path, body))


def test_extract_catalog_empty_directory_raises(tmp_path: Path) -> None:
    with pytest.raises(TraceInputError, match="no numbered packet files"):
        extract_catalog(tmp_path)
