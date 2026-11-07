"""The rebuilt Claude preamble: version matching, memory files and reconciliation."""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import pytest

from eawf.observability.reflect.preamble import (
    PREAMBLE_TABLE,
    RECONSTRUCTED_ORIGINS,
    TOOL_SCHEMA_TOTAL,
    InstructionOrigin,
    PreambleVersion,
    ReconstructedBlock,
    ReconstructionBasis,
    match_version,
    read_memory_files,
    reconcile_preamble,
    reconstruct_claude_preamble,
)

_TOTAL_ONLY = MappingProxyType(
    {
        "2.1.220": PreambleVersion(
            version="2.1.220",
            system_prompt_tokens=4000,
            tool_schema_tokens={TOOL_SCHEMA_TOTAL: 20000},
            measured_at="2026-08-01",
        ),
        "2.1.235": PreambleVersion(
            version="2.1.235",
            system_prompt_tokens=4300,
            tool_schema_tokens={TOOL_SCHEMA_TOTAL: 24500},
            measured_at="2026-08-20",
        ),
    }
)

_ITEMISED = MappingProxyType(
    {
        "2.1.235": PreambleVersion(
            version="2.1.235",
            system_prompt_tokens=4300,
            tool_schema_tokens={"Bash": 900, "Read": 700},
            measured_at="2026-08-20",
        ),
    }
)


def _kinds(blocks: tuple[ReconstructedBlock, ...]) -> list[str]:
    return [block.kind for block in blocks]


# ---- version matching -----------------------------------------------------------------


def test_match_version_returns_the_exact_row_when_measured() -> None:
    match = match_version("2.1.235", table=_TOTAL_ONLY)

    assert match.matched == "2.1.235"
    assert match.distance == 0
    assert match.exact is True


def test_match_version_falls_back_to_the_nearest_row_below() -> None:
    match = match_version("2.1.233", table=_TOTAL_ONLY)

    assert match.matched == "2.1.220"
    assert match.distance == 13
    assert match.exact is False


def test_match_version_never_borrows_a_later_row() -> None:
    match = match_version("2.1.219", table=_TOTAL_ONLY)

    assert match.matched is None
    assert match.entry is None


@pytest.mark.parametrize("version", ["2.2.240", "3.1.235", "1.1.235"])
def test_match_version_refuses_a_different_release_series(version: str) -> None:
    assert match_version(version, table=_TOTAL_ONLY).matched is None


@pytest.mark.parametrize("version", ["2.1.235", "2.1.300", "2.1.221"])
def test_match_version_always_pairs_a_match_with_a_distance(version: str) -> None:
    match = match_version(version, table=_TOTAL_ONLY)

    assert (match.matched is None) == (match.distance is None)


@pytest.mark.parametrize("version", [None, "", "2", "2.1", "2.1.x", "v2.1.235", "2.1.235.1"])
def test_match_version_refuses_an_unusable_version(version: str | None) -> None:
    match = match_version(version, table=_TOTAL_ONLY)

    assert match.matched is None
    assert match.requested == version


def test_match_version_on_an_empty_table_matches_nothing() -> None:
    assert match_version("2.1.235", table=MappingProxyType({})).matched is None


def test_the_shipped_table_is_keyed_by_its_own_version() -> None:
    for version, entry in PREAMBLE_TABLE.items():
        assert entry.version == version
        assert match_version(version).exact is True


# ---- memory files ---------------------------------------------------------------------


def test_read_memory_files_returns_basenames_and_token_estimates(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("a" * 400, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("b" * 40, encoding="utf-8")

    files, limitations = read_memory_files(tmp_path)

    assert files == [("AGENTS.md", 10), ("CLAUDE.md", 100)]
    assert "preamble_memory_files_read_at_report_time" in limitations


def test_read_memory_files_lets_no_path_out(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text("x" * 8, encoding="utf-8")

    files, _ = read_memory_files(tmp_path)

    assert files == [("MEMORY.md", 2)]
    assert str(tmp_path) not in json.dumps(files)


def test_read_memory_files_reports_a_directory_that_is_gone(tmp_path: Path) -> None:
    files, limitations = read_memory_files(tmp_path / "deleted-worktree")

    assert files == []
    assert "preamble_memory_files_unreadable" in limitations


def test_read_memory_files_skips_an_empty_file(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("", encoding="utf-8")

    files, limitations = read_memory_files(tmp_path)

    assert files == []
    assert "preamble_memory_files_read_at_report_time" not in limitations


def test_read_memory_files_ignores_a_directory_named_like_a_memory_file(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").mkdir()

    assert read_memory_files(tmp_path)[0] == []


def test_read_memory_files_handles_an_unrecorded_directory() -> None:
    files, limitations = read_memory_files(None)

    assert files == []
    assert "preamble_memory_files_unreadable" in limitations


def _store(tmp_path: Path, *, root_text: str = "@NOTES.md\n") -> Path:
    """Return a harness store laid out the way the harness lays out its own."""
    user_root = tmp_path / "harness-home"
    store = user_root / "projects" / "-synthetic-project"
    (store / "memory").mkdir(parents=True)
    (user_root / "CLAUDE.md").write_text(root_text, encoding="utf-8")
    (user_root / "NOTES.md").write_text("r" * 240, encoding="utf-8")
    (store / "memory" / "MEMORY.md").write_text("m" * 2800, encoding="utf-8")
    return store


def test_read_memory_files_reads_the_user_level_set(tmp_path: Path) -> None:
    files, limitations = read_memory_files(tmp_path / "gone", harness_store=_store(tmp_path))

    assert files == [("CLAUDE.md (user)", 3), ("NOTES.md (user)", 60), ("MEMORY.md (project)", 700)]
    assert "preamble_memory_files_read_at_report_time" in limitations
    assert "preamble_memory_files_unreadable" in limitations


def test_read_memory_files_reconstructs_more_than_the_working_directory_alone(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENTS.md").write_text("a" * 4000, encoding="utf-8")

    cwd_only, _ = read_memory_files(project)
    with_user, _ = read_memory_files(project, harness_store=store)

    assert sum(t for _, t in cwd_only) == 1000
    assert sum(t for _, t in with_user) == 1000 + 3 + 60 + 700


def test_read_memory_files_follows_one_level_of_at_import(tmp_path: Path) -> None:
    files, _ = read_memory_files(
        None, harness_store=_store(tmp_path, root_text="\n".join(["text", "@NOTES.md", ""]))
    )

    assert ("NOTES.md (user)", 60) in files


def test_read_memory_files_ignores_an_unimported_sibling(tmp_path: Path) -> None:
    files, _ = read_memory_files(None, harness_store=_store(tmp_path, root_text="no imports\n"))

    assert [name for name, _ in files] == ["CLAUDE.md (user)", "MEMORY.md (project)"]


@pytest.mark.parametrize("target", ["../escape.md", "/etc/hosts", "sub/nested.md"])
def test_read_memory_files_refuses_an_import_outside_the_memory_root(
    tmp_path: Path, target: str
) -> None:
    store = _store(tmp_path, root_text=f"@{target}\n")
    (tmp_path / "escape.md").write_text("e" * 400, encoding="utf-8")

    files, _ = read_memory_files(None, harness_store=store)

    assert [name for name, _ in files] == ["CLAUDE.md (user)", "MEMORY.md (project)"]


def test_read_memory_files_survives_a_store_that_is_not_a_harness_store(tmp_path: Path) -> None:
    files, limitations = read_memory_files(None, harness_store=tmp_path / "nowhere")

    assert files == []
    assert "preamble_memory_files_read_at_report_time" not in limitations


def test_read_memory_files_skips_a_user_root_that_is_a_directory(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("x", encoding="utf-8")
    store = tmp_path / "harness-home" / "projects" / "p"
    store.mkdir(parents=True)
    (tmp_path / "harness-home" / "CLAUDE.md").mkdir()
    (tmp_path / "harness-home" / "CLAUDE.md" / "inner").write_text("x", encoding="utf-8")

    files, _ = read_memory_files(tmp_path, harness_store=store)

    assert files == [("CLAUDE.md", 1)]


# ---- reconstruction -------------------------------------------------------------------


def test_reconstruct_claude_preamble_marks_version_table_basis() -> None:
    blocks, _ = reconstruct_claude_preamble(
        version="2.1.235", tool_names=(), memory_files=(), table=_TOTAL_ONLY
    )
    by_kind = {block.kind: block for block in blocks}

    assert by_kind["system"].basis is ReconstructionBasis.VERSION_TABLE
    assert by_kind["system"].token_estimate == 4300
    assert by_kind["system"].origin is InstructionOrigin.BUILTIN_SYSTEM_PROMPT
    assert by_kind["tool_definition"].basis is ReconstructionBasis.VERSION_TABLE
    assert by_kind["tool_definition"].token_estimate == 24500
    assert by_kind["tool_definition"].origin is InstructionOrigin.BUILTIN_TOOL_SCHEMA


def test_reconstruct_claude_preamble_gives_memory_files_their_own_origin() -> None:
    blocks, _ = reconstruct_claude_preamble(
        version="2.1.235",
        tool_names=(),
        memory_files=(("AGENTS.md", 7767), ("CLAUDE.md", 3)),
        table=_TOTAL_ONLY,
    )
    (memory,) = [block for block in blocks if block.kind == "project_instruction"]

    assert memory.origin is InstructionOrigin.MEMORY_FILE
    assert memory.basis is ReconstructionBasis.READ_FROM_DISK_NOW
    assert memory.token_estimate == 7770
    assert memory.label == "AGENTS.md, CLAUDE.md"


def test_reconstruct_claude_preamble_reports_unmeasured_version() -> None:
    blocks, limitations = reconstruct_claude_preamble(
        version="2.1.100", tool_names=(), memory_files=(("AGENTS.md", 100),), table=_TOTAL_ONLY
    )

    assert "preamble_version_unmeasured" in limitations
    assert _kinds(blocks) == ["project_instruction"]


def test_reconstruct_claude_preamble_reports_an_approximated_version() -> None:
    _, limitations = reconstruct_claude_preamble(
        version="2.1.233", tool_names=(), memory_files=(), table=_TOTAL_ONLY
    )

    assert "preamble_version_approximated" in limitations
    assert "preamble_version_unmeasured" not in limitations


def test_reconstruct_claude_preamble_names_unmeasured_tool() -> None:
    blocks, limitations = reconstruct_claude_preamble(
        version="2.1.235", tool_names=("Bash", "Read", "Artifact"), memory_files=(), table=_ITEMISED
    )
    (schemas,) = [block for block in blocks if block.kind == "tool_definition"]

    assert "preamble_tool_schema_unmeasured" in limitations
    assert schemas.token_estimate == 1600


def test_reconstruct_claude_preamble_sums_a_split_table_into_one_block() -> None:
    blocks, limitations = reconstruct_claude_preamble(
        version="2.1.235", tool_names=("Bash", "Read"), memory_files=(), table=_ITEMISED
    )

    assert [b.token_estimate for b in blocks if b.kind == "tool_definition"] == [1600]
    assert "preamble_tool_schema_unmeasured" not in limitations
    assert "preamble_tool_schema_not_itemised" not in limitations


def test_reconstruct_claude_preamble_says_a_total_is_not_itemised() -> None:
    _, limitations = reconstruct_claude_preamble(
        version="2.1.235", tool_names=("Bash",), memory_files=(), table=_TOTAL_ONLY
    )

    assert "preamble_tool_schema_not_itemised" in limitations


def test_reconstruct_claude_preamble_emits_nothing_it_cannot_source() -> None:
    blocks, limitations = reconstruct_claude_preamble(
        version=None, tool_names=(), memory_files=(), table=_TOTAL_ONLY
    )

    assert blocks == ()
    assert {"preamble_version_unmeasured", "preamble_listings_not_rebuilt"} <= limitations


def test_reconstruct_claude_preamble_gives_every_block_its_own_origin() -> None:
    blocks, _ = reconstruct_claude_preamble(
        version="2.1.235",
        tool_names=("Bash", "Read"),
        memory_files=(("AGENTS.md", 100), ("CLAUDE.md", 3)),
        table=_TOTAL_ONLY,
    )
    origins = [block.origin for block in blocks]

    assert len(set(origins)) == len(origins)
    assert set(origins) == RECONSTRUCTED_ORIGINS


def test_reconstruct_claude_preamble_orders_the_preamble_as_the_harness_builds_it() -> None:
    blocks, _ = reconstruct_claude_preamble(
        version="2.1.235", tool_names=(), memory_files=(("AGENTS.md", 100),), table=_TOTAL_ONLY
    )

    assert _kinds(blocks) == ["system", "tool_definition", "project_instruction"]


@pytest.mark.parametrize("tokens", [-1, -4300])
def test_reconstruct_claude_preamble_rejects_a_negative_memory_estimate(tokens: int) -> None:
    with pytest.raises(ValueError, match="token estimate"):
        reconstruct_claude_preamble(
            version="2.1.235",
            tool_names=(),
            memory_files=(("AGENTS.md", tokens),),
            table=_TOTAL_ONLY,
        )


def test_reconstruct_claude_preamble_drops_a_zero_token_memory_file() -> None:
    blocks, _ = reconstruct_claude_preamble(
        version="2.1.235", tool_names=(), memory_files=(("AGENTS.md", 0),), table=_TOTAL_ONLY
    )

    assert "project_instruction" not in _kinds(blocks)


def test_reconstructed_block_refuses_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        ReconstructedBlock.model_validate(
            {
                "kind": "skill_instruction",
                "origin": "memory_file",
                "basis": "read_from_disk_now",
                "token_estimate": 1,
                "label": "x",
            }
        )


# ---- reconciliation -------------------------------------------------------------------


def test_reconcile_preamble_agrees_within_tolerance() -> None:
    result = reconcile_preamble(
        reconstructed_tokens=31_000, first_request_input=40_000, blocks_before_first_request=8_000
    )

    assert result.residual_tokens == 32_000
    assert result.delta_tokens == -1_000
    assert result.status == "agrees"


def test_reconcile_preamble_flags_drift() -> None:
    result = reconcile_preamble(
        reconstructed_tokens=28_800, first_request_input=60_000, blocks_before_first_request=4_333
    )

    assert result.residual_tokens == 55_667
    assert result.status == "drifted"


def test_reconcile_preamble_uses_a_floor_on_a_small_residual() -> None:
    def status(reconstructed: int) -> str:
        return reconcile_preamble(
            reconstructed_tokens=reconstructed,
            first_request_input=1_000,
            blocks_before_first_request=0,
        ).status

    assert status(1_500) == "agrees"
    assert status(1_501) == "drifted"


def test_reconcile_preamble_flags_an_unusable_residual() -> None:
    result = reconcile_preamble(
        reconstructed_tokens=28_800,
        first_request_input=120_000,
        blocks_before_first_request=293_103,
    )

    assert result.residual_tokens == -173_103
    assert result.status == "residual_implausible"


def test_reconcile_preamble_at_a_zero_residual_is_implausible() -> None:
    result = reconcile_preamble(
        reconstructed_tokens=0, first_request_input=10, blocks_before_first_request=10
    )

    assert result.status == "residual_implausible"


def test_reconcile_preamble_never_reports_the_residual_as_content() -> None:
    result = reconcile_preamble(
        reconstructed_tokens=10, first_request_input=50_000, blocks_before_first_request=0
    )

    assert set(result.model_dump()) == {
        "reconstructed_tokens",
        "residual_tokens",
        "delta_tokens",
        "status",
    }


@pytest.mark.parametrize(
    ("reconstructed", "first", "before"), [(-1, 10, 0), (0, -1, 0), (0, 10, -1)]
)
def test_reconcile_preamble_rejects_a_negative_input(
    reconstructed: int, first: int, before: int
) -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        reconcile_preamble(
            reconstructed_tokens=reconstructed,
            first_request_input=first,
            blocks_before_first_request=before,
        )
