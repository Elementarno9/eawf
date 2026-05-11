"""Unit tests for the layered skill registry (P14-W09 / B061)."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.skills.discovery import (
    DiscoveredSkill,
    SkillFrontmatterError,
    discover_skills,
    parse_user_skill,
    user_skills_dir,
    workspace_skills_dir,
)


_VALID_BODY = """\
---
name: /demo
description: A demo user skill
runtimes: [claude, codex]
user_invocable: true
---
body line one
body line two
"""


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _write_skill(root: Path, name: str, body: str = _VALID_BODY) -> Path:
    target = root / name / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def test_discover_returns_builtin_skills_only_when_no_overlays(fake_home: Path) -> None:
    discovered = discover_skills()
    names = {d.name for d in discovered}
    assert "/research" in names
    sources = {d.source for d in discovered}
    assert sources == {"builtin"}


def test_user_overlay_overrides_builtin(fake_home: Path) -> None:
    _write_skill(user_skills_dir(), "research")
    discovered = discover_skills()
    research = next(d for d in discovered if d.name == "/research")
    assert research.source == "user"
    assert research.description == "A demo user skill"


def test_workspace_overlay_overrides_user(fake_home: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_skill(user_skills_dir(), "research")
    ws_body = _VALID_BODY.replace("A demo user skill", "Workspace override")
    _write_skill(workspace_skills_dir(workspace), "research", body=ws_body)
    discovered = discover_skills(workspace=workspace)
    research = next(d for d in discovered if d.name == "/research")
    assert research.source == "workspace"
    assert research.description == "Workspace override"


def test_user_only_skill_loads(fake_home: Path) -> None:
    _write_skill(user_skills_dir(), "demo")
    discovered = discover_skills()
    names = [d.name for d in discovered]
    assert "/demo" in names


def test_runtime_filter_drops_excluded_skills(fake_home: Path) -> None:
    _write_skill(user_skills_dir(), "demo")  # runtimes=[claude, codex]
    filtered = discover_skills(runtime="opencode")
    names = [d.name for d in filtered]
    assert "/demo" not in names
    again = discover_skills(runtime="claude")
    assert any(d.name == "/demo" for d in again)


def test_empty_runtimes_visible_to_all(fake_home: Path) -> None:
    body = """\
---
name: /global
description: Visible everywhere
---
body
"""
    _write_skill(user_skills_dir(), "global", body=body)
    rows = discover_skills(runtime="opencode")
    assert any(d.name == "/global" for d in rows)


def test_invalid_frontmatter_skipped_with_warning(fake_home: Path, caplog) -> None:
    bad = """\
not-a-real-frontmatter-block
content
"""
    _write_skill(user_skills_dir(), "bad", body=bad)
    discovered = discover_skills()
    assert all(d.name != "/bad" for d in discovered)
    assert any("skill discovery skip user" in rec.message for rec in caplog.records)


def test_parse_user_skill_missing_name_raises(tmp_path: Path) -> None:
    body = """\
---
description: no name
---
b
"""
    path = tmp_path / "SKILL.md"
    path.write_text(body)
    with pytest.raises(SkillFrontmatterError, match="'name' field is required"):
        parse_user_skill(path, source="user")


def test_parse_user_skill_missing_description_raises(tmp_path: Path) -> None:
    body = """\
---
name: /x
---
b
"""
    path = tmp_path / "SKILL.md"
    path.write_text(body)
    with pytest.raises(SkillFrontmatterError, match="'description' field is required"):
        parse_user_skill(path, source="user")


def test_parse_user_skill_runtimes_must_be_list(tmp_path: Path) -> None:
    body = """\
---
name: /x
description: y
runtimes: claude
---
"""
    path = tmp_path / "SKILL.md"
    path.write_text(body)
    with pytest.raises(SkillFrontmatterError, match="'runtimes' must be a list"):
        parse_user_skill(path, source="user")


def test_parse_user_skill_normalises_name(tmp_path: Path) -> None:
    body = """\
---
name: demo
description: x
---
b
"""
    path = tmp_path / "SKILL.md"
    path.write_text(body)
    skill = parse_user_skill(path, source="user")
    assert skill.name == "/demo"


def test_discovered_skill_carries_path(fake_home: Path) -> None:
    written = _write_skill(user_skills_dir(), "demo")
    discovered = discover_skills()
    demo = next(d for d in discovered if d.name == "/demo")
    assert demo.path == written
    assert demo.source == "user"
    assert isinstance(demo, DiscoveredSkill)
