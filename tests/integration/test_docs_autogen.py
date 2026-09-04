"""Unit tests for the introspection-driven docs autogen + ``eawf schema dump``.

Covers the load-bearing guarantees of P27-W26:

- **Determinism** — :func:`eawf.platform.docs.autogen.all_pages` emits byte-identical
  output across calls and the committed ``docs/reference/autogen/`` tree
  matches a fresh regeneration (the drift gate behind
  ``eawf doc verify --strict``).
- **Coverage** — every canonical Pydantic model, every state enum, every
  ``ErrorCode`` member, and every exit bucket appears in its page.
- **CLI dispatch** — ``eawf schema dump`` writes the schema + reference
  pages under a ``--workspace`` root, ``--schema-only`` narrows to the
  ``.schema.json`` dumps, and a non-directory workspace exits ``USER_ERROR``.
- **Drift gate** — :func:`eawf.platform.docs.autogen.diff_against_disk` reports a
  ``missing`` row for an absent page and a ``changed`` row for a tampered
  one, then no rows once regenerated.
- **Docs front door** — ``mkdocs build --strict`` exits 0, the install /
  quickstart / first-workflow pages are reachable in the nav, and every
  internal link they carry resolves to a real file and heading.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from enum import Enum
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from eawf.kernel.state import enums as state_enums
from eawf.platform.docs import autogen
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


# --- Docs front door ---------------------------------------------------------
#
# The front door is the install -> quickstart -> first-workflow path a
# newcomer walks before they know any eawf vocabulary. Its two failure modes
# are silent: a page that ships but is unreachable in the explicit nav, and a
# cross-page link that rots when a target is renamed. Both are cheap to pin
# and expensive to notice by hand, so they are gated here alongside the
# strict site build.

_DOCS_DIR = _REPO_ROOT / "docs"
_MKDOCS_YML = _REPO_ROOT / "mkdocs.yml"
_FRONT_DOOR_PAGES = (
    "tutorial/install.md",
    "tutorial/quickstart.md",
    "tutorial/first-workflow.md",
)
# Inline markdown links only; the leading lookbehind drops image embeds.
_MD_LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
_EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "tel:")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$")


def _heading_slugs(page: Path) -> set[str]:
    """Return the anchor slugs mkdocs derives from *page*'s headings."""
    slugs: set[str] = set()
    for line in page.read_text(encoding="utf-8").splitlines():
        match = _HEADING.match(line)
        if match is None:
            continue
        text = re.sub(r"[`*_]", "", match.group(1)).strip().lower()
        slugs.add(re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", text)).strip("-"))
    return slugs


def _unresolved_links(page: Path) -> list[str]:
    """Return every internal link in *page* whose file or anchor is missing.

    External schemes and pure fragments pointing at the page's own headings
    are resolved in place; anything else is resolved relative to the page's
    directory and checked against the filesystem.

    Args:
        page: Absolute path to the markdown file to scan.

    Returns:
        The offending link targets, in source order. Empty when the page's
        every internal link resolves.
    """
    broken: list[str] = []
    for target in _MD_LINK.findall(page.read_text(encoding="utf-8")):
        if target.startswith(_EXTERNAL_SCHEMES):
            continue
        path_part, _, anchor = target.partition("#")
        resolved = page if not path_part else (page.parent / path_part).resolve()
        if not resolved.is_file():
            broken.append(target)
            continue
        if anchor and resolved.suffix == ".md" and anchor not in _heading_slugs(resolved):
            broken.append(target)
    return broken


def _nav_page_paths() -> list[str]:
    """Return every docs-relative page path declared in the mkdocs nav."""

    def walk(node: object) -> list[str]:
        if isinstance(node, str):
            return [node]
        if isinstance(node, list):
            return [p for item in node for p in walk(item)]
        if isinstance(node, dict):
            return [p for value in node.values() for p in walk(value)]
        return []

    return walk(yaml.safe_load(_MKDOCS_YML.read_text(encoding="utf-8"))["nav"])


def test_front_door_pages_resolve_every_internal_link() -> None:
    """No install / quickstart / first-workflow link points at a missing target."""
    broken = {
        relpath: _unresolved_links(_DOCS_DIR / relpath)
        for relpath in _FRONT_DOOR_PAGES
        if _unresolved_links(_DOCS_DIR / relpath)
    }
    assert broken == {}, f"front-door pages carry unresolved links: {broken}"


def test_front_door_readme_links_resolve() -> None:
    """The repo README is the outermost front door; its links resolve too."""
    assert _unresolved_links(_REPO_ROOT / "README.md") == []


def test_front_door_pages_are_reachable_in_the_nav() -> None:
    """A front-door page absent from the explicit nav is unreachable in the site."""
    navigated = set(_nav_page_paths())
    assert set(_FRONT_DOOR_PAGES) <= navigated, (
        f"mkdocs.yml nav omits {sorted(set(_FRONT_DOOR_PAGES) - navigated)}"
    )


def test_front_door_link_checker_flags_a_dangling_link(tmp_path: Path) -> None:
    """The checker reds on a real defect: a link to a file that is not there."""
    page = tmp_path / "page.md"
    page.write_text("# Title\n\nSee [gone](absent.md) and [ok](page.md).\n", encoding="utf-8")
    assert _unresolved_links(page) == ["absent.md"]


def test_front_door_link_checker_flags_a_dangling_anchor(tmp_path: Path) -> None:
    """A link to a real file but an absent heading is unresolved too."""
    page = tmp_path / "page.md"
    page.write_text("# Title\n\n[bad](page.md#nope) [good](page.md#title)\n", encoding="utf-8")
    assert _unresolved_links(page) == ["page.md#nope"]


def test_front_door_link_checker_passes_a_linkless_page(tmp_path: Path) -> None:
    """The empty boundary: a page with no links reports nothing."""
    page = tmp_path / "page.md"
    page.write_text("# Title\n\nprose only\n", encoding="utf-8")
    assert _unresolved_links(page) == []


def test_front_door_link_checker_ignores_external_urls(tmp_path: Path) -> None:
    """An external scheme is out of scope for an internal-link gate."""
    page = tmp_path / "page.md"
    page.write_text("# Title\n\n[x](https://example.invalid/a.md)\n", encoding="utf-8")
    assert _unresolved_links(page) == []


def test_front_door_mkdocs_strict_build_exits_zero(tmp_path: Path) -> None:
    """``mkdocs build --strict`` exits 0 over the committed docs tree.

    Skipped only when mkdocs is genuinely absent (the optional ``eawf[docs]``
    extra is not installed). Whenever the build runs, a non-zero exit REDS
    rather than green-skipping a real docs regression, and the build output
    is surfaced so the failing page is named.
    """
    if importlib.util.find_spec("mkdocs") is None:
        pytest.skip("mkdocs unavailable; install the eawf[docs] extra")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mkdocs",
            "build",
            "--strict",
            "--quiet",
            "-f",
            str(_MKDOCS_YML),
            "-d",
            str(tmp_path / "site"),
        ],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert completed.returncode == 0, (
        f"mkdocs build --strict exited {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    for relpath in _FRONT_DOOR_PAGES:
        built = tmp_path / "site" / relpath.removesuffix(".md") / "index.html"
        assert built.is_file(), f"strict build produced no page for {relpath}"
