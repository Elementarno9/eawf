"""UI-053: every settings editor, pressed live against a real daemon, writes its value.

The console is served a tmp canary tree by a real daemon over an isolated socket, at 80
by 30. On the Settings route each of the five editors is opened, driven by its own keys
and confirmed on the consequence card, and the daemon's write is read back from the
canary's repo layer file and from the view the daemon reads back. The frame each editor
draws is held to the console grid. The repository's own ``.ea`` tree is never served
here, so nothing a key sends can reach it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf.kernel.config.layered import Layer
from eawf.kernel.projection.settings import EffectiveSettingsView
from eawf.platform.profiles import discovery
from eawf.platform.profiles.trust import profile_sha256
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.harness import capture_cells, grid_errors, settle
from eawf.surfaces.tui.console.keymap import DISMISS
from eawf.surfaces.tui.console.session import SessionSetup
from tests.tui.surfaces.tui.console.test_console_live_smoke import (
    live_console,
    walk_canary_isolated,
)
from tests.tui.surfaces.tui.console.test_settings_editors import HOUSE

#: The frame the editors are checked at: the narrowest width, with room for the readout.
LIVE = (80, 30)

#: Per editor kind: the key it edits, the keys pressed before its frame is checked, and
#: the keys that then write it. A pressed key is ``(name, shift)``.
Script = tuple[str, str, tuple[tuple[str, bool], ...], tuple[tuple[str, bool], ...]]


def _keys(*names: str, shift: bool = False) -> tuple[tuple[str, bool], ...]:
    return tuple((name, shift) for name in names)


SCRIPTS: tuple[Script, ...] = (
    (
        "check",
        "acceptance.required_before_ship",
        _keys("Enter", "ArrowRight", "ArrowRight", "ArrowRight", "ArrowRight", " "),
        _keys("Enter", "Enter"),
    ),
    (
        "order",
        "runtime.preference",
        (*_keys("Enter", "ArrowDown", " "), *_keys("ArrowUp", shift=True)),
        _keys("Enter", "Enter"),
    ),
    (
        "tiers",
        "runtime.models.codex",
        _keys("Enter", *"gpt-5.3-codex-spark", "ArrowDown", *"gpt-5.5", "ArrowDown", *"gpt-5.5"),
        _keys("Enter", "Enter"),
    ),
    (
        "rows",
        "agents.extra_tools",
        _keys("Enter", "ArrowDown", "ArrowDown", "ArrowDown", "Enter", *"LSP", "Enter"),
        _keys("w", "Enter"),
    ),
    ("pin", "profiles.trusted", _keys("Enter", "p"), _keys("w", "Enter")),
)


def _place(app: ConsoleApp, view: EffectiveSettingsView, key: str) -> None:
    """Put the route's cursor on ``key`` in the view the console holds."""
    section = view.leaf(key).drawn_in()
    assert section is not None, f"{key} is not listed"
    app.session.set_sec = view.sections().index(section)
    app.session.set_key = [leaf.key for leaf in view.keys_of(section)].index(key)


async def _press(app: ConsoleApp, pilot: Any, keys: tuple[tuple[str, bool], ...]) -> str:
    for name, shift in keys:
        app.press_key(name, shift=shift)
    text, _cycles = await settle(pilot)
    return text


def test_ui053_every_editor_writes_its_value_through_the_live_daemon_at_80x30(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the operator's own profile overlays would otherwise join the pin editor's rows
    monkeypatch.setattr(discovery, "user_profiles_dir", lambda: tmp_path / "no-user-profiles")
    walk, runtime_root = walk_canary_isolated(tmp_path)
    root = walk.canary.root
    profile = root / ".ea" / "profiles" / "house.yaml"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(HOUSE, encoding="utf-8")

    async def body() -> tuple[dict[str, str], EffectiveSettingsView]:
        frames: dict[str, str] = {}
        async with (
            live_console(root, runtime_root) as (app, seam),
            app.run_test(size=LIVE) as pilot,
        ):
            # a route read is what turns the link live; the settings view is not one
            for route in ("scope.home", "settings"):
                app.reset(SessionSetup(route=route))
                app.render_frame()
                first, _cycles = await settle(pilot)
            assert "● LIVE" in first.splitlines()[0]
            for kind, key, edit, write in SCRIPTS:
                view = seam.settings
                assert view is not None, "the settings route holds its view"
                _place(app, view, key)
                frames[kind] = await _press(app, pilot, edit)
                assert app.session.edit is not None, f"{kind}: the editor is open"
                errors = grid_errors(frames[kind], LIVE, capture_cells(app))
                assert not errors, f"{kind}: {'; '.join(errors)}"
                await _press(app, pilot, write)
                if app.session.overlay is not None:
                    await _press(app, pilot, _keys("Escape"))
                # the confirmation toasts would stack over the next editor's readout
                await _press(app, pilot, _keys(DISMISS))
            held = seam.settings
            assert held is not None
        return frames, held

    frames, reread = asyncio.run(body())

    assert "[×] tests" in frames["check"]  # noqa: RUF001
    assert "1 [×] codex" in frames["order"]  # noqa: RUF001
    assert "cheap   gpt-5.3-codex-spark" in frames["tiers"]
    assert "WRITES   agents.extra_tools at repo" in frames["rows"]
    assert "pinned" in frames["pin"]
    # the daemon reads the layers of the repository holding the tree, the one it writes
    assert reread.leaf("runtime.preference").source_layer is Layer.REPO
    assert reread.leaf("runtime.preference").effective.value == "[codex, claude-code]"
    written = yaml.safe_load((root / ".ea" / "config.yaml").read_text(encoding="utf-8"))
    assert written["acceptance"]["required_before_ship"] == ["state", "tests"]
    assert written["runtime"]["preference"] == ["codex", "claude-code"]
    assert written["runtime"]["models"]["codex"] == ["gpt-5.3-codex-spark", "gpt-5.5", "gpt-5.5"]
    assert written["agents"]["extra_tools"] == {"executor": ["LSP"]}
    assert written["profiles"]["trusted"] == {"house": profile_sha256(profile)}


#: The co-author identity: a pair of keys only valid together.
NAME = "vcs.coauthor.project.name"
EMAIL = "vcs.coauthor.project.email"


def test_a_coauthor_name_alone_is_refused_and_the_refusal_shows_on_the_live_card(
    tmp_path: Path,
) -> None:
    walk, runtime_root = walk_canary_isolated(tmp_path)
    root = walk.canary.root
    layer_file = root / ".ea" / "config.yaml"

    async def body() -> tuple[str, str, str]:
        async with (
            live_console(root, runtime_root) as (app, seam),
            app.run_test(size=(120, 40)) as pilot,
        ):
            for route in ("scope.home", "settings"):
                app.reset(SessionSetup(route=route))
                app.render_frame()
                await settle(pilot)
            view = seam.settings
            assert view is not None
            _place(app, view, NAME)
            await _press(app, pilot, _keys("Enter", *"Jane Doe", "Enter"))
            alone = app.session.log[0].note
            await _press(app, pilot, _keys("ArrowDown", *"jane@example.com", "Enter", "Enter"))
            await _press(app, pilot, _keys("Escape", DISMISS))
            written = layer_file.read_text(encoding="utf-8")
            view = seam.settings
            assert view is not None
            _place(app, view, EMAIL)
            refused = await _press(app, pilot, _keys("x", "Enter"))
        return alone, written, refused

    alone, written, refused = asyncio.run(body())

    # the editor sends nothing for a name without its email
    assert "email empty · the pair is written whole or removed whole" in alone
    assert yaml.safe_load(written)["vcs"]["coauthor"]["project"] == {
        "name": "Jane Doe",
        "email": "jane@example.com",
    }
    # removing the email alone would leave the name without it: the daemon refuses, and
    # the confirm card says why
    assert "config_section_invalid" in refused, refused
    assert "Field required" in refused, refused
    project = yaml.safe_load(layer_file.read_text(encoding="utf-8"))["vcs"]["coauthor"]["project"]
    assert project == {"name": "Jane Doe", "email": "jane@example.com"}
