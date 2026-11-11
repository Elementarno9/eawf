"""The agent-writable local folders, and the skill write scopes built from them."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from eawf.kernel.layout import (
    ONLY_EAWF_WRITES_REFUSAL,
    SLUG_MAX_LENGTH,
    LocalKind,
    LocalKindError,
    dated_local_path,
    local_root,
    local_scope,
    parse_local_kind,
    split_local_scope,
)
from eawf.observability.reflect.report import reflect_root
from eawf.workflow.skills.bodies.chassis import shipped_skill_page
from eawf.workflow.skills.catalog import SKILL_CATALOG, resolve_skill

_DAY = date(2026, 11, 10)


def test_local_kind_is_the_five_agent_folders() -> None:
    assert [kind.value for kind in LocalKind] == [
        "research",
        "spikes",
        "release",
        "reports",
        "archive",
    ]


def test_skill_write_roots() -> None:
    scopes = {
        entry.skill_id: entry.effects.local_write_scope
        for entry in SKILL_CATALOG.entries
        if entry.effects.local_write_scope is not None
    }
    assert scopes == {
        "mockup": ".ea/local/spikes/mockups",
        "reflect": ".ea/local/reports/reflect",
        "release": ".ea/local/reports/reflect",
        "research": ".ea/local/research",
        "spike": ".ea/local/spikes",
    }
    for scope in scopes.values():
        split_local_scope(scope)

    # The reflect skill's scope is where the reflect verbs write by default.
    tree = Path("repo") / ".ea"
    assert reflect_root(tree) == Path("repo") / scopes["reflect"]

    for entry in SKILL_CATALOG.entries:
        page = shipped_skill_page(entry)
        assert ".ea/local" not in page, entry.skill_id
        if entry.effects.local_write_scope is not None:
            kind, _ = split_local_scope(entry.effects.local_write_scope)
            assert f"`eawf paths local --kind {kind.value}` prints" in page, entry.skill_id

    mockup = shipped_skill_page(resolve_skill("mockup"))
    assert "Local write root: `mockups/` in the folder `eawf paths local --kind spikes`" in mockup


def test_parse_local_kind_accepts_each_kind() -> None:
    for kind in LocalKind:
        assert parse_local_kind(kind.value) is kind


@pytest.mark.parametrize("name", ["runtime", "cache"])
def test_parse_local_kind_refuses_eawf_folders(name: str) -> None:
    with pytest.raises(LocalKindError, match=f"^{ONLY_EAWF_WRITES_REFUSAL}$"):
        parse_local_kind(name)


@pytest.mark.parametrize("name", ["", "Research", "researchs", "spikes/mockups", 3])
def test_parse_local_kind_refuses_unknown_names(name: object) -> None:
    with pytest.raises(LocalKindError, match="unknown local kind"):
        parse_local_kind(name)  # type: ignore[arg-type]


def test_local_root_and_dated_path() -> None:
    tree = Path("r") / ".ea"
    assert local_root(tree, LocalKind.REPORTS) == Path("r/.ea/local/reports")
    assert dated_local_path(tree, LocalKind.RESEARCH, "x", _DAY) == Path(
        "r/.ea/local/research/2026-11-10-x.md"
    )
    longest = "a" * SLUG_MAX_LENGTH
    assert dated_local_path(tree, LocalKind.ARCHIVE, longest, _DAY).name == (
        f"2026-11-10-{longest}.md"
    )


@pytest.mark.parametrize(
    "slug",
    ["", "a" * (SLUG_MAX_LENGTH + 1), "A", "a_b", "a--b", "-a", "a-", "../a", "a b"],
    ids=["empty", "too-long", "upper", "underscore", "double", "lead", "trail", "dots", "space"],
)
def test_dated_local_path_refuses_a_malformed_slug(slug: str) -> None:
    with pytest.raises(ValueError, match="slug"):
        dated_local_path(Path(".ea"), LocalKind.RESEARCH, slug, _DAY)


def test_local_scope_round_trips() -> None:
    assert local_scope(LocalKind.RESEARCH) == ".ea/local/research"
    assert local_scope(LocalKind.SPIKES, "mockups", "x") == ".ea/local/spikes/mockups/x"
    assert split_local_scope(".ea/local/research") == (LocalKind.RESEARCH, "")
    assert split_local_scope(".ea/local/spikes/mockups/x") == (LocalKind.SPIKES, "mockups/x")


@pytest.mark.parametrize(
    "scope",
    ["", ".ea", ".ea/local", ".ea/local/runtime/log", "src/.ea/local/research", "local/research"],
)
def test_split_local_scope_refuses_a_path_outside_the_kinds(scope: str) -> None:
    with pytest.raises(ValueError, match="scope"):
        split_local_scope(scope)
