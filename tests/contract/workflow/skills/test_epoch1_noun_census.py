"""The epoch-1 noun census over shipped text: pattern, plugin bundle and help topics.

A shipped page that still teaches phase, iter or wave -- spelled bare, joined
into an identifier such as ``phase_open``, or as a ``P01-I02-W03`` id -- or
that invokes a retired skill sends a reader after an entity that no longer
exists. These gates hold the census itself and the surfaces it guards: the
packaged Claude plugin, the ``eawf help`` topics and the skill registry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

from eawf.runtime.runtimes.claude.plugin_package import package_plugin
from eawf.surfaces.cli.commands.help import _topics_dir
from eawf.surfaces.render.skills.registry import SKILL_REGISTRY
from eawf.surfaces.render.skills.render import SkillSpec
from eawf.workflow.skills.catalog import SKILL_CATALOG
from eawf.workflow.skills.census import bundle_census, epoch1_nouns

#: Topics that exist to explain the epoch-1 model to an upgrading operator.
_MIGRATION_TOPICS: Final[frozenset[str]] = frozenset({"migration.md", "upgrade-from-0.6.md"})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("call phase_open first", ("phase_open",)),
        ("then wave_close", ("wave_close",)),
        ("and iter_close", ("iter_close",)),
        ("the active_wave field", ("active_wave",)),
        ("run /prep now", ("/prep",)),
        ("(/wave-spec)", ("/wave-spec",)),
        ("all phases", ("phases",)),
        ("see P01-I02-W03", ("P01-I02-W03",)),
        ("Wave and wave", ("Wave", "wave")),
    ],
)
def test_census_catches_joined_and_retired_names(text: str, expected: tuple[str, ...]) -> None:
    assert epoch1_nouns(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Milestone",
        "Batch",
        "Task",
        "phaseless",
        "iterate over the waveform",
        "docs/prep/x.md",
        "eawf/review",
        "/prepare",
        "/plan and /verify",
    ],
)
def test_census_ignores_native_nouns_paths_and_live_skills(text: str) -> None:
    assert epoch1_nouns(text) == ()


def test_census_names_every_retired_skill_invocation() -> None:
    for row in SKILL_CATALOG.retired:
        assert epoch1_nouns(f"use /{row.skill_id}.") == (f"/{row.skill_id}",)


def test_census_reports_joined_identifier_with_its_line(tmp_path: Path) -> None:
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "x.md").write_text("ok\nemit wave_close\nrun /ship\n")
    hits = bundle_census(tmp_path)
    assert [(h.path, h.line, h.noun) for h in hits] == [
        ("agents/x.md", 2, "wave_close"),
        ("agents/x.md", 3, "/ship"),
    ]


def test_packaged_plugin_has_no_epoch1_noun(tmp_path: Path) -> None:
    target = tmp_path / "plugin"
    package_plugin(target, include_marketplace=True, include_readme=True, extra_tools={})
    shipped = {p.relative_to(target).parts[0] for p in target.rglob("*") if p.is_file()}
    assert {"agents", "skills", "hooks", "README.md"} <= shipped
    hits = [f"{hit.path}:{hit.line}: {hit.noun}" for hit in bundle_census(target)]
    assert hits == []


def test_help_topics_name_no_epoch1_model() -> None:
    topics_dir = _topics_dir()
    assert topics_dir is not None
    topics = sorted(p for p in topics_dir.glob("*.md") if p.name not in _MIGRATION_TOPICS)
    assert topics
    hits = {
        topic.name: nouns
        for topic in topics
        if (nouns := epoch1_nouns(topic.read_text(encoding="utf-8")))
    }
    assert hits == {}


@pytest.mark.parametrize("spec", SKILL_REGISTRY, ids=lambda spec: spec.skill_name)
def test_skill_registry_bodies_name_no_epoch1_model(spec: SkillSpec) -> None:
    assert epoch1_nouns("\n".join((spec.description, spec.argument_hint, spec.body))) == ()
