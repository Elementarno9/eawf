"""Unit tests for the introspection-driven docs autogen + ``eawf schema dump``.

Covers the load-bearing guarantees of P27-W26:

- **Determinism** — :func:`eawf.docs.autogen.all_pages` emits byte-identical
  output across calls and the committed ``docs/reference/autogen/`` tree
  matches a fresh regeneration (the drift gate behind
  ``eawf doc verify --strict``).
- **Coverage** — every canonical Pydantic model, every state enum, every
  ``ErrorCode`` member, and every exit bucket appears in its page.
- **CLI dispatch** — ``eawf schema dump`` writes the schema + reference
  pages under a ``--workspace`` root, ``--schema-only`` narrows to the
  ``.schema.json`` dumps, and a non-directory workspace exits ``USER_ERROR``.
- **Drift gate** — :func:`eawf.docs.autogen.diff_against_disk` reports a
  ``missing`` row for an absent page and a ``changed`` row for a tampered
  one, then no rows once regenerated.
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path

from typer.testing import CliRunner

from eawf.docs import autogen
from eawf.kernel.state import enums as state_enums
from eawf.surfaces.cli import error_codes as error_codes_mod
from eawf.surfaces.cli import exit_codes as exit_codes_mod
from eawf.surfaces.cli.app import app

runner = CliRunner()

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _page(pages: list[autogen.GeneratedPage], suffix: str) -> autogen.GeneratedPage:
    """Return the single generated page whose relpath ends with *suffix*."""
    matches = [p for p in pages if p.relpath.endswith(suffix)]
    assert len(matches) == 1, f"expected one page ending {suffix!r}, got {len(matches)}"
    return matches[0]


# --- Determinism -------------------------------------------------------------


def test_all_pages_are_deterministic() -> None:
    """Two regenerations emit byte-identical bodies in the same order."""
    first = autogen.all_pages()
    second = autogen.all_pages()
    assert [(p.relpath, p.body) for p in first] == [(p.relpath, p.body) for p in second]


def test_committed_autogen_tree_matches_regeneration() -> None:
    """The committed pages match a fresh in-memory regeneration (drift gate)."""
    drift = autogen.diff_against_disk(_REPO_ROOT)
    assert drift == [], f"committed autogen tree drifted: {drift}"


def test_every_page_ends_with_single_trailing_newline() -> None:
    """Every generated artifact is newline-terminated exactly once."""
    for page in autogen.all_pages():
        assert page.body.endswith("\n")
        assert not page.body.endswith("\n\n")


# --- Coverage ----------------------------------------------------------------


def test_schema_pages_cover_every_canonical_model() -> None:
    """Each canonical model dumps a valid JSON Schema with its title."""
    pages = autogen.schema_pages()
    assert len(pages) == len(autogen.SCHEMA_MODELS)
    for stem, model in autogen.SCHEMA_MODELS:
        page = _page(pages, f"{stem}.schema.json")
        schema = json.loads(page.body)
        assert schema["title"] == model.__name__


def test_enums_page_lists_every_state_enum() -> None:
    """Every StrEnum declared in eawf.kernel.state.enums appears in enums.md."""
    body = autogen.enums_page().body
    enum_names = [
        attr
        for attr in dir(state_enums)
        if isinstance(getattr(state_enums, attr), type)
        and issubclass(getattr(state_enums, attr), Enum)
        and getattr(state_enums, attr) is not Enum
        and getattr(state_enums, attr).__module__ == state_enums.__name__
    ]
    assert enum_names, "no state enums discovered"
    for name in enum_names:
        assert f"`{name}`" in body, f"enum {name} missing from enums.md"


def test_error_codes_page_covers_every_member() -> None:
    """Every ErrorCode member appears with its folded exit bucket."""
    body = autogen.error_codes_page().body
    for member in error_codes_mod.ErrorCode:
        assert f"`{member.value}`" in body
        bucket = exit_codes_mod.name_for(member.exit_code)
        assert f"`{bucket}`" in body


def test_exit_codes_page_lists_the_five_bucket_surface() -> None:
    """The exit-code page lists all six canonical codes (OK + five buckets)."""
    body = autogen.exit_codes_page().body
    for code, name in (
        (0, "OK"),
        (1, "USER_ERROR"),
        (2, "VALIDATION_ERROR"),
        (3, "STATE_CONFLICT"),
        (4, "DAEMON_UNREACHABLE"),
        (5, "INTERNAL_ERROR"),
    ):
        assert f"| {code} | `{name}` |" in body


def test_cli_page_lists_a_known_command_group() -> None:
    """The CLI inventory surfaces a registered group + verb."""
    body = autogen.cli_page().body
    assert "### `eawf wave`" in body
    assert "| `claim` |" in body


def test_skills_page_lists_registry_entries() -> None:
    """Every skill in the registry appears as a slash-command row."""
    from eawf.surfaces.render.skills import SKILL_REGISTRY

    body = autogen.skills_page().body
    for spec in SKILL_REGISTRY:
        assert f"`/{spec.skill_name}`" in body


# --- CLI dispatch ------------------------------------------------------------


def test_schema_dump_writes_full_tree(tmp_path: Path) -> None:
    """``eawf schema dump`` writes schema + reference pages under --workspace."""
    result = runner.invoke(app, ["-w", str(tmp_path), "schema", "dump"])
    assert result.exit_code == 0, result.output
    autogen_dir = tmp_path / autogen.AUTOGEN_RELDIR
    assert (autogen_dir / "state.schema.json").is_file()
    assert (autogen_dir / "cli.md").is_file()
    assert (autogen_dir / "enums.md").is_file()
    # A fresh dump leaves the tmp tree drift-free.
    assert autogen.diff_against_disk(tmp_path) == []


def test_schema_dump_schema_only_writes_just_json(tmp_path: Path) -> None:
    """``--schema-only`` writes the .schema.json dumps and no markdown."""
    result = runner.invoke(app, ["--json", "-w", str(tmp_path), "schema", "dump", "--schema-only"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["schema_only"] is True
    assert payload["count"] == len(autogen.SCHEMA_MODELS)
    assert all(p.endswith(".schema.json") for p in payload["written"])
    autogen_dir = tmp_path / autogen.AUTOGEN_RELDIR
    assert not (autogen_dir / "cli.md").exists()


def test_schema_dump_rejects_non_directory_workspace(tmp_path: Path) -> None:
    """A workspace path that is not a directory exits USER_ERROR (1)."""
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x", encoding="utf-8")
    result = runner.invoke(app, ["-w", str(not_a_dir), "schema", "dump"])
    assert result.exit_code == exit_codes_mod.USER_ERROR, result.output


# --- Drift gate --------------------------------------------------------------


def test_diff_reports_missing_then_changed_then_clean(tmp_path: Path) -> None:
    """diff_against_disk flags missing + changed pages, then clears once written."""
    # Nothing written yet -> every page is missing.
    missing = autogen.diff_against_disk(tmp_path)
    assert missing, "expected missing rows for an empty tree"
    assert all(d.reason == "missing" for d in missing)

    autogen.generate_all(tmp_path)
    assert autogen.diff_against_disk(tmp_path) == []

    # Tamper one page -> exactly one "changed" row.
    tampered = tmp_path / autogen.AUTOGEN_RELDIR / "exit-codes.md"
    tampered.write_text(tampered.read_text(encoding="utf-8") + "\nx\n", encoding="utf-8")
    changed = autogen.diff_against_disk(tmp_path)
    assert [(d.relpath, d.reason) for d in changed] == [
        (f"{autogen.AUTOGEN_RELDIR}/exit-codes.md", "changed")
    ]
