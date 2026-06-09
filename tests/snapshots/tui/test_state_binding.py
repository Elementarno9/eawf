"""Staleness-banner snapshot + pure-helper coverage for the TUI bind (P29-I13-W01).

On launch the TUI binds ``state.json`` read-only. When the on-disk state
was written under an OLDER schema than the live daemon ``State`` model
loads -- the file has not been migrated up yet -- the bound view trails the
live schema. This wave surfaces that drift two ways:

* the bound state is migrated IN MEMORY up to the live schema version
  (:func:`~eawf.surfaces.tui.state_binding.migrate_bound_state`) so every
  pane renders against the current shape; and
* a staleness banner
  (:func:`~eawf.surfaces.tui.app.stale_schema_banner_message`) surfaces
  whenever the bound state's ``schema_version`` trailed the live daemon
  schema, naming both versions.

The measurable signal is the snapshot assertion: the banner shows when the
bound state schema version trails the live daemon schema.

This module pins:

* the pure staleness predicate + in-memory migration helpers (no Textual
  mount); and
* the Pilot-driven banner -- a stale fixture surfaces the banner; a fixture
  already at the live schema hides it.

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_state_binding.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
import pytest
from textual.widgets import Static

from eawf.kernel.state.models import State
from eawf.surfaces.tui.app import (
    STALE_SCHEMA_BANNER_HIDDEN_CLASS,
    STALE_SCHEMA_BANNER_ID,
    EaApp,
    stale_schema_banner_message,
)
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.state_binding import (
    is_state_schema_stale,
    live_schema_version,
    load_state,
    migrate_bound_state,
)
from eawf.surfaces.tui.widgets.git_pane import GitFields
from eawf.surfaces.tui.widgets.sigils import Sigil, chrome, glyph

_SIZE = (120, 40)
_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "states" / "valid"
#: A repo fixture written at schema 1.0 -- older than the live daemon max,
#: so it is stale and migrates up on bind.
_STALE_STATE = _FIXTURES / "03-phase-iter-wave-active.json"

assert _STALE_STATE.is_file(), f"missing snapshot fixture: {_STALE_STATE}"

_GOLDEN = Path(__file__).resolve().parent / "golden"


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every git probe so the rendered chrome is deterministic.

    The workspace-table probe is stubbed to a clean tree; the repo GIT pane
    shells out via ``git_pane._git_run`` (a different probe), so it is stubbed
    to ``None`` -- every GIT field then resolves to the dash placeholder
    rather than the live, drifting working-tree status.
    """
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )
    monkeypatch.setattr("eawf.surfaces.tui.widgets.git_pane._git_run", lambda *a, **k: None)


def _write_current_state(tmp_path: Path) -> Path:
    """Write the stale fixture bumped to the live schema version, return its path.

    Loads the stale fixture, re-stamps its ``schema_version`` to the live
    daemon max, and writes it to a tmp ``state.json`` -- the not-stale
    control for the banner-hidden assertion.
    """
    payload = orjson.loads(_STALE_STATE.read_bytes())
    payload["schema_version"] = live_schema_version()
    state = State.model_validate(payload)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))
    return state_path


# --------------------------------------------------------------------------
# Pure helpers -- no Textual mount
# --------------------------------------------------------------------------


def test_live_schema_version_is_a_dotted_numeric() -> None:
    """The live schema version is a dotted-numeric string (e.g. 1.8)."""
    version = live_schema_version()
    parts = version.split(".")
    assert len(parts) >= 2
    assert all(part.isdigit() for part in parts)


def test_is_state_schema_stale_true_when_bound_trails_live() -> None:
    """A state written under an older schema is stale vs the live version."""
    state = load_state(_STALE_STATE)
    assert state is not None
    assert state.schema_version == "1.0"
    assert is_state_schema_stale(state, live_version="1.8") is True


def test_is_state_schema_stale_false_when_bound_equals_live() -> None:
    """A state at the live schema version is not stale."""
    state = load_state(_STALE_STATE)
    assert state is not None
    assert is_state_schema_stale(state, live_version="1.0") is False


def test_is_state_schema_stale_false_when_bound_ahead_of_live() -> None:
    """A state ahead of the live version is not flagged stale."""
    state = load_state(_STALE_STATE)
    assert state is not None
    assert is_state_schema_stale(state, live_version="0.9") is False


def test_is_state_schema_stale_against_real_live_version() -> None:
    """The 1.0 fixture trails the real live daemon max (1.x > 1.0)."""
    state = load_state(_STALE_STATE)
    assert state is not None
    assert is_state_schema_stale(state) is True


def test_migrate_bound_state_lifts_to_live_schema() -> None:
    """A stale bound state migrates up to the live schema version in memory."""
    state = load_state(_STALE_STATE)
    assert state is not None
    migrated = migrate_bound_state(state)
    assert migrated.schema_version == live_schema_version()
    # Identity facts survive the migration (additive edges only).
    assert migrated.urn == state.urn


def test_migrate_bound_state_noop_when_already_current() -> None:
    """A state already at the live version is returned unchanged."""
    state = load_state(_STALE_STATE)
    assert state is not None
    current = migrate_bound_state(state, live_version="1.0")
    assert current is state


def test_stale_schema_banner_message_names_both_versions() -> None:
    """The banner text names the bound version and the live daemon version."""
    text = stale_schema_banner_message(bound_version="1.0", live_version="1.8")
    assert "1.0" in text
    assert "1.8" in text


def test_stale_schema_banner_message_leads_with_warn_sigil() -> None:
    """The banner LEADS with the WARN (attention-triangle) sigil, per column."""
    unicode_text = stale_schema_banner_message(
        bound_version="1.0", live_version="1.8", mode="unicode"
    )
    assert unicode_text.startswith(chrome("attention", mode="unicode"))
    ascii_text = stale_schema_banner_message(bound_version="1.0", live_version="1.8", mode="ascii")
    assert ascii_text.startswith(chrome("attention", mode="ascii"))


# --------------------------------------------------------------------------
# Pilot-driven banner -- stale shows it, current hides it
# --------------------------------------------------------------------------


def test_stale_schema_banner_shows_when_bound_trails_live() -> None:
    """The banner surfaces when the bound state schema trails the live daemon."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_STALE_STATE)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            assert app.stale_schema is True
            banner = app.screen.query_one(f"#{STALE_SCHEMA_BANNER_ID}", Static)
            assert not banner.has_class(STALE_SCHEMA_BANNER_HIDDEN_CLASS)
            # Belt-and-braces over the banner widget: the WARN sigil leads (NOT
            # the FAIL cross) + the version delta names both versions, pinned
            # independently of the coupled full-app golden below (which sibling
            # reskin waves also drift).
            rendered = str(banner.render())
            assert glyph(Sigil.FAILED, mode="unicode") not in rendered
            assert chrome("attention", mode="unicode") in rendered
            assert "1.0" in rendered
            assert live_schema_version() in rendered
            assert "trails live daemon schema" in rendered
            frame = normalize_snapshot(capture_screen_text(app))
            assert "1.0" in frame
            assert live_schema_version() in frame
            assert "trails live daemon schema" in frame
            assert_screen_snapshot(app, _GOLDEN / "stale_schema_banner.txt")

    asyncio.run(body())


def test_stale_schema_banner_hidden_when_bound_is_current(tmp_path: Path) -> None:
    """The banner stays hidden when the bound state is at the live schema."""

    async def body() -> None:
        state_path = _write_current_state(tmp_path)
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            assert app.stale_schema is False
            banners = app.screen.query(f"#{STALE_SCHEMA_BANNER_ID}")
            # The banner widget mounts lazily; when present it must be hidden.
            if banners:
                assert banners.first(Static).has_class(STALE_SCHEMA_BANNER_HIDDEN_CLASS)
            frame = normalize_snapshot(capture_screen_text(app))
            assert "trails live daemon schema" not in frame

    asyncio.run(body())
