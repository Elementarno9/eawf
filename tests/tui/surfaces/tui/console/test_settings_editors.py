"""Every consumed setting is edited on the Settings route, lists and mappings included.

Requirement rows proved here, by id:

- ``UI-053``: a key the catalog gives an editor is edited in the console rather than in
  its file. A fixed set is ticked from the set the reading code defines, a ranked list is
  ordered, a model ladder is three ids or none, a record keyed by role is edited a row at
  a time, and a digest ledger is pinned from digests the console computes; each writes the
  whole value, and a mapping's write is previewed as a per-member difference.
- ``CON-123``: the route offers the layers that can hold a key its lens cannot, leaves out
  the workspace layer when it is the repo's own file, words a refusal by the key's catalog
  standing, lists and removes a key nothing reads while a file still states it, and holds
  a number to its catalog range.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import pytest
import yaml

from eawf.kernel.config import layered
from eawf.kernel.config.layered import Layer
from eawf.kernel.config.schema import RuntimeAdapterId
from eawf.kernel.projection.settings import LENS_LAYERS, EffectiveSettingsView, build_settings_view
from eawf.platform.profiles.certification import profile_digest
from eawf.platform.profiles.loader import list_profiles, load_profile
from eawf.platform.profiles.trust import profile_sha256
from eawf.platform.render_block import DISPATCH_SYSTEM_PROMPT_TARGET
from eawf.runtime.daemon.methods.config import unset_layer_value
from eawf.surfaces.tui.console.app import dispatcher_key
from eawf.surfaces.tui.console.fixture import Fixture, load_fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.mutation import Card
from eawf.surfaces.tui.console.operations import SettingRequest, address_setting
from eawf.surfaces.tui.console.overlays.mutation_card import preview_frame
from eawf.surfaces.tui.console.renderers.provenance import refusal, step
from eawf.surfaces.tui.console.session import Session
from eawf.workflow.skills.ship import SHIP_GATES
from tests.tui.surfaces.tui.console import test_settings_route as sr

#: An enriched profile a repo overlays: its role-tier block reaches a dispatched agent, so
#: it is both a trust candidate (not bundled) and a certification candidate (enriched).
HOUSE = yaml.safe_dump(
    {
        "name": "house",
        "render_blocks": [
            {
                "id": "house-rule",
                "target": DISPATCH_SYSTEM_PROMPT_TARGET,
                "agent_role": "executor",
                "body_template": "Run the targeted tests before reporting.",
            }
        ],
    }
)


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a repo root whose layers the test owns, the home redirected beside it."""
    home = tmp_path / "home"
    (home / ".config" / "eawf").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    repo = tmp_path / "repo"
    (repo / ".ea" / "local").mkdir(parents=True)
    return repo


@pytest.fixture(scope="module")
def fixture() -> Fixture:
    """Return the golden fixture the console is built over."""
    return load_fixture(Path(__file__).resolve().parents[4] / "fixtures/console/golden/fixture")


def _view(tree: Path, *, workspace: Path | None = None) -> EffectiveSettingsView:
    return build_settings_view(
        workspace=workspace or tree,
        repo=tree,
        scope_id=sr.SCOPE,
        cursor=41208,
        generated_at=datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
        env={},
        branch=sr.BRANCH,
    )


def _on(view: EffectiveSettingsView, key: str, *, lens: str = "repo") -> Session:
    """Return a Settings session with the cursor on ``key``, wherever the route lists it."""
    session = sr._session()
    session.lens = lens
    section = view.leaf(key).drawn_in()
    assert section is not None, f"{key} is not listed"
    session.set_sec = view.sections().index(section)
    session.set_key = [leaf.key for leaf in view.keys_of(section)].index(key)
    return session


def _body(fixture: Fixture, view: EffectiveSettingsView, session: Session) -> str:
    return "\n".join(sr._frame(fixture, view, session, w=80, h=30))


def _card(session: Session) -> Card:
    card = session.mutation
    assert isinstance(card, Card), "a write opens the consequence card"
    return card


# ---------- UI-053: check, a fixed set ticked from the code that defines it ----------


def test_ui053_check_ticks_profiles_from_the_set_discovered_per_read(
    tree: Path, fixture: Fixture
) -> None:
    sr._write(tree / ".ea" / "profiles" / "house.yaml", HOUSE)
    view = _view(tree)
    leaf = view.leaf("profiles.enabled")
    assert leaf.editor == "check"
    assert leaf.allowed == list_profiles(repo=tree, workspace=tree)
    assert "house" in leaf.allowed

    link = sr._Link()
    session = _on(view, "profiles.enabled")
    sr._press(fixture, view, session, ["Enter"])
    edit = session.edit
    assert edit is not None
    assert edit["on"] == ["core"]
    body = _body(fixture, view, session)
    assert "enabled · list_str · ticked from" in body
    assert "[×] core" in body  # noqa: RUF001
    assert "[ ] house" in body

    ahead = edit["opts"].index("house") - edit["idx"]
    sr._press(fixture, view, session, ["ArrowRight"] * ahead + [" ", "Enter", "Enter"], send=link)

    assert link.sent == [
        SettingRequest(target="profiles.enabled", layer="repo", value=["core", "house"])
    ]


def test_ui053_check_arrows_move_by_the_row_of_the_grid_drawn(tree: Path, fixture: Fixture) -> None:
    view = _view(tree)
    session = _on(view, "profiles.enabled")
    sr._press(fixture, view, session, ["Enter"])
    _body(fixture, view, session)
    edit = session.edit
    assert edit is not None
    cols = edit["cols"]
    assert cols > 1

    sr._press(fixture, view, session, ["ArrowDown"])
    assert edit["idx"] == cols
    sr._press(fixture, view, session, ["ArrowUp", "ArrowUp", "ArrowLeft"])
    assert edit["idx"] == 0


def test_ui053_check_sets_are_the_code_owned_sets_not_the_design_packs(tree: Path) -> None:
    view = _view(tree)

    assert view.leaf("runtime.adapters").allowed == get_args(RuntimeAdapterId)
    assert view.leaf("acceptance.required_before_ship").allowed == SHIP_GATES
    assert SHIP_GATES == ("state", "pre-commit", "lint", "typecheck", "tests", "build")
    # the design pack offered ``local`` and ``docs``, neither of which any reader accepts
    assert "local" not in view.leaf("runtime.adapters").allowed
    assert "docs" not in SHIP_GATES


# ---------- UI-053: order, a ranked list ----------


def test_ui053_order_ticks_and_reorders_the_runtime_preference(
    tree: Path, fixture: Fixture
) -> None:
    view = _view(tree)
    link = sr._Link()
    session = _on(view, "runtime.preference")

    sr._press(fixture, view, session, ["Enter", "ArrowDown", " "])
    sr._press(fixture, view, session, ["ArrowUp"], shift=True)
    edit = session.edit
    assert edit is not None
    assert edit["opts"] == ["codex", "claude-code", "opencode"]
    body = _body(fixture, view, session)
    assert "▸ 1 [×] codex" in body  # noqa: RUF001
    assert "2 [×] claude-code" in body  # noqa: RUF001
    assert "Shift+↑↓ reorder" in body

    sr._press(fixture, view, session, ["Enter", "Enter"], send=link)
    assert link.sent == [
        SettingRequest(target="runtime.preference", layer="repo", value=["codex", "claude-code"])
    ]


# ---------- UI-053: tiers, three model ids or none ----------


def test_ui053_tiers_write_three_ids_and_refuse_a_short_ladder(
    tree: Path, fixture: Fixture
) -> None:
    view = _view(tree)
    link = sr._Link()
    session = _on(view, "runtime.models.codex")

    sr._press(fixture, view, session, ["Enter", *"spark", "ArrowDown", *"mid", "Enter"], send=link)
    assert link.sent == []
    assert "a ladder is three model ids or none · 2 of 3 filled" in session.log[0].note
    assert "mid     mid▏" in _body(fixture, view, session)

    sr._press(fixture, view, session, ["ArrowDown", *"top", "Enter", "Enter"], send=link)
    assert link.sent == [
        SettingRequest(target="runtime.models.codex", layer="repo", value=["spark", "mid", "top"])
    ]


def test_ui053_tiers_emptied_unset_the_ladder_the_lens_layer_states(
    tree: Path, fixture: Fixture
) -> None:
    sr._write(tree / ".ea" / "config.yaml", "runtime:\n  models:\n    codex: [a, b, c]\n")
    view = _view(tree)
    link = sr._Link()
    session = _on(view, "runtime.models.codex")

    keys = ["Enter", "Backspace", "ArrowDown", "Backspace", "ArrowDown", "Backspace"]
    sr._press(fixture, view, session, keys)
    assert "AFTER    no layer states it afterwards" in _body(fixture, view, session)
    sr._press(fixture, view, session, ["Enter"], send=link)
    assert _card(session).items[0].changes == ("cheap  a → –", "mid  b → –", "top  c → –")  # noqa: RUF001
    sr._press(fixture, view, session, ["Enter"], send=link)

    assert link.sent == [SettingRequest(target="runtime.models.codex", layer="repo", unset=True)]


# ---------- UI-053: rows, a record keyed by role, and the diff card (D2) ----------


def test_ui053_rows_edit_a_roles_tools_and_the_card_previews_the_difference(
    tree: Path, fixture: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_config = tree.parent / "home" / ".config" / "eawf" / "config.yaml"
    # the session isolates the merge's global layer; point it at this test's own file
    monkeypatch.setattr(layered, "global_config_path", lambda: home_config)
    sr._write(home_config, "agents:\n  extra_tools:\n    executor: [A, B]\n    '*': [LSP]\n")
    view = _view(tree)
    link = sr._Link()
    session = _on(view, "agents.extra_tools", lens="global")

    sr._press(fixture, view, session, ["Enter"])
    edit = session.edit
    assert edit is not None
    assert edit["roles"][0] == "*"
    # the role column is as wide as its longest role, domain-specialist, and a gap
    assert f"executor{' ' * 12}A +1" in _body(fixture, view, session)

    to_executor = ["ArrowDown"] * edit["roles"].index("executor")
    retype = ["Enter", *(["Backspace"] * len("A, B")), "C", "Enter"]
    to_operator = ["ArrowDown"] * (
        edit["roles"].index("operator") - edit["roles"].index("executor")
    )
    sr._press(
        fixture,
        view,
        session,
        [*to_executor, *retype, *to_operator, "Enter", *"LSP", "Enter", "w"],
        send=link,
    )

    card = _card(session)
    assert card.items[0].changes == (
        "executor  − A",  # noqa: RUF001
        "executor  − B",  # noqa: RUF001
        "executor  + C",
        "operator  + LSP",
        "1 role unchanged",
    )
    frame = "\n".join(
        preview_frame(View(session=session, fixture=fixture, w=80, h=30, settings=view), card)
    )
    # the pane wraps each change as prose, so the column gap closes to one space
    assert "CHANGES   executor − A" in frame  # noqa: RUF001
    assert "operator + LSP" in frame
    assert "agents.extra_tools is written whole to the global layer file" in frame

    sr._press(fixture, view, session, ["Enter"], send=link)
    assert link.sent == [
        SettingRequest(
            target="agents.extra_tools",
            layer="global",
            value={"*": ["LSP"], "executor": ["C"], "operator": ["LSP"]},
        )
    ]


def test_ui053_rows_escape_leaves_the_tools_before_the_editor(tree: Path, fixture: Fixture) -> None:
    view = _view(tree)
    session = _on(view, "agents.extra_tools")

    sr._press(fixture, view, session, ["Enter", "Enter", "x", "Escape"])
    edit = session.edit
    assert edit is not None
    assert edit["typing"] is None
    assert edit["tools"] == {}
    sr._press(fixture, view, session, ["Escape"])
    assert session.edit is None


# ---------- UI-053: pin, digests the console computes ----------


def test_ui053_pin_records_the_trust_digest_the_console_computes(
    tree: Path, fixture: Fixture
) -> None:
    path = tree / ".ea" / "profiles" / "house.yaml"
    sr._write(path, HOUSE)
    view = _view(tree)
    digest = profile_sha256(path)
    # a bundled profile is trusted by the package, so only the overlay has a pin to take
    assert view.leaf("profiles.trusted").candidates == (("house", digest),)
    enriched = load_profile("house", repo=tree, workspace=tree)
    assert view.leaf("profiles.certified").candidates == (("house", profile_digest(enriched)),)

    link = sr._Link()
    session = _on(view, "profiles.trusted")
    sr._press(fixture, view, session, ["Enter"])
    body = _body(fixture, view, session)
    assert f"▸ house    –          {digest[:8]}   unpinned" in body  # noqa: RUF001
    assert "p pin" in body

    sr._press(fixture, view, session, ["p"])
    assert "pinned" in _body(fixture, view, session)
    sr._press(fixture, view, session, ["w"], send=link)
    assert _card(session).items[0].changes == (f"house  + {digest[:8]}",)
    sr._press(fixture, view, session, ["Enter"], send=link)

    assert link.sent == [
        SettingRequest(target="profiles.trusted", layer="repo", value={"house": digest})
    ]


def test_ui053_pin_drops_a_stale_pin_the_file_still_holds(tree: Path, fixture: Fixture) -> None:
    sr._write(tree / ".ea" / "config.yaml", "profiles:\n  certified:\n    gone: sha256:abc\n")
    view = _view(tree)
    link = sr._Link()
    session = _on(view, "profiles.certified")

    sr._press(fixture, view, session, ["Enter", "p"])
    assert "gone has no digest to pin" in session.log[0].note
    sr._press(fixture, view, session, ["x", "w"], send=link)
    assert _card(session).items[0].changes == ("gone  − abc",)  # noqa: RUF001
    sr._press(fixture, view, session, ["Enter"], send=link)

    assert link.sent == [SettingRequest(target="profiles.certified", layer="repo", value={})]


# ---------- CON-123: the layer offer (F) ----------


def test_con123_enter_offers_the_layers_that_hold_the_key_and_writes_there(
    tree: Path, fixture: Fixture
) -> None:
    view = _view(tree)
    link = sr._Link()
    session = _on(view, "telemetry.db_kind")

    sr._press(fixture, view, session, ["Enter"])
    body = _body(fixture, view, session)
    assert "repo cannot hold db_kind · only global can" in body
    assert "▸ write it at global · ~/.config/eawf/config.yaml" in body
    assert "Esc keeps the lens at repo and writes nothing" in body
    assert "Enter edit there" in body

    sr._press(fixture, view, session, ["Enter"])
    edit = session.edit
    assert edit is not None
    assert edit["at"] == "global"
    target = edit["vals"].index("duckdb")
    sr._press(fixture, view, session, ["ArrowDown"] * ((target - edit["idx"]) % 2))
    assert "WRITES   telemetry.db_kind at global" in _body(fixture, view, session)
    sr._press(fixture, view, session, ["Enter", "Enter"], send=link)

    assert link.sent == [SettingRequest(target="telemetry.db_kind", layer="global", value="duckdb")]
    assert session.lens == "repo"


def test_con123_escape_on_the_offer_writes_nothing_and_keeps_the_lens(
    tree: Path, fixture: Fixture
) -> None:
    view = _view(tree)
    session = _on(view, "telemetry.db_kind")

    sr._press(fixture, view, session, ["Enter", "Escape"])

    assert session.edit is None
    assert session.lens == "repo"
    assert "the lens stays at repo" in session.log[0].note


# ---------- CON-123: the workspace layer that is the repo file ----------


def test_con123_the_lens_skips_the_workspace_only_when_it_is_the_repo_file(
    tree: Path, tmp_path: Path
) -> None:
    same = _view(tree)
    elsewhere = tmp_path / "workspace"
    (elsewhere / ".ea").mkdir(parents=True)

    assert same.lens_layers == (Layer.GLOBAL, Layer.REPO, Layer.BRANCH, Layer.LOCAL)
    assert _view(tree, workspace=elsewhere).lens_layers == LENS_LAYERS


# ---------- CON-123: the refusal says why, by catalog standing ----------


@pytest.mark.parametrize(
    ("kind", "words"),
    [
        ("engine", "schema_version is locked · it is set by code"),
        ("deprecated", "schema_version is deprecated · no longer read · x removes it"),
        ("reserved", "schema_version is reserved · not read yet · kept for a planned feature"),
        (None, "schema_version is off the catalog · no eawf code reads it · x removes it"),
    ],
)
def test_con123_a_refusal_is_worded_by_the_keys_catalog_standing(
    tree: Path, kind: str | None, words: str
) -> None:
    leaf = _view(tree).leaf("schema_version").model_copy(update={"consumer_kind": kind})
    assert words in refusal(leaf, Layer.REPO)


# ---------- CON-123: a key nothing reads, listed while a file states it (G) ----------


def test_con123_a_key_nothing_reads_is_listed_only_while_a_file_states_it(tree: Path) -> None:
    quiet = _view(tree)
    assert quiet.leaf("audit.fix_safe").drawn_in() is None
    assert "audit.fix_safe" not in [leaf.key for leaf in quiet.keys_of("audit")]

    sr._write(tree / ".ea" / "config.yaml", "stale:\n  flag: []\naudit:\n  fix_safe: true\n")
    stated = _view(tree)

    assert stated.leaf("audit.fix_safe").drawn_in() == "audit"
    assert stated.leaf("stale.flag").consumer_kind is None
    assert stated.leaf("stale.flag").drawn_in() == "stale"
    assert stated.category_of("stale") == "system"
    assert [c.name for c in stated.rail] == [c.name for c in quiet.rail]


def test_con123_x_removes_a_key_nothing_reads_and_enter_says_why_it_is_not_edited(
    tree: Path, fixture: Fixture
) -> None:
    sr._write(tree / ".ea" / "config.yaml", "stale:\n  flag: []\nresearch:\n  auto_save: true\n")
    view = _view(tree)
    link = sr._Link()
    session = _on(view, "stale.flag")

    body = _body(fixture, view, session)
    # CON-165: the four glyphs keep their meaning for it; the FROM cell says unread
    row = next(line for line in body.splitlines() if "▸ = flag" in line)
    assert row.rstrip().endswith(" unread")
    assert "flag · outside the catalog" in body

    sr._press(fixture, view, session, ["Enter"])
    assert session.edit is None
    assert "stale.flag is off the catalog · no eawf code reads it" in session.log[0].note

    sr._press(fixture, view, session, ["x", "Enter"], send=link)
    assert link.sent == [SettingRequest(target="stale.flag", layer="repo", unset=True)]

    daemon = sr._daemon_ctx(tree)
    params = dict(address_setting(link.sent[0]).params)
    removed = asyncio.run(unset_layer_value(daemon, params))
    assert removed["removed"] is True
    assert yaml.safe_load((tree / ".ea" / "config.yaml").read_text()) == {
        "research": {"auto_save": True}
    }


def test_con123_x_removes_a_deprecated_leaf_from_the_lens_layer(
    tree: Path, fixture: Fixture
) -> None:
    sr._write(tree / ".ea" / "config.yaml", "audit:\n  fix_safe: true\n")
    view = _view(tree)
    link = sr._Link()
    session = _on(view, "audit.fix_safe")

    sr._press(fixture, view, session, ["Enter"])
    assert "audit.fix_safe is deprecated · no longer read" in session.log[0].note
    sr._press(fixture, view, session, ["x", "Enter"], send=link)

    assert link.sent == [SettingRequest(target="audit.fix_safe", layer="repo", unset=True)]


# ---------- CON-123: a number is held to its catalog range ----------


def test_con123_arrows_stop_at_the_catalog_range_the_readout_states(
    tree: Path, fixture: Fixture
) -> None:
    sr._write(tree / ".ea" / "config.yaml", "planning:\n  max_parallel_waves: 16\n")
    view = _view(tree)
    leaf = view.leaf(sr.RANGED_KEY)
    session = _on(view, sr.RANGED_KEY)

    assert "max_parallel_waves · int · 1 to 16" in _body(fixture, view, session)
    sr._press(fixture, view, session, ["Enter", "ArrowUp", "ArrowUp"])
    edit = session.edit
    assert edit is not None
    assert edit["text"] == "16"
    assert step(leaf, "1", False) == "1"
    assert step(leaf, "40", False) == "16"
    assert step(leaf.model_copy(update={"value_range": (1.0, None)}), "40", True) == "41"


# ---------- UI-053: the keys an editor needs reach it from a terminal ----------


def test_ui053_shift_with_an_arrow_reaches_the_order_editor_from_the_toolkit() -> None:
    assert dispatcher_key("shift+up", None) == ("ArrowUp", True)
    assert dispatcher_key("shift+down", None) == ("ArrowDown", True)
    assert dispatcher_key("up", None) == ("ArrowUp", False)


def test_ui053_a_typed_j_or_k_is_text_in_an_editor_field_never_a_motion(
    tree: Path, fixture: Fixture
) -> None:
    view = _view(tree)
    session = _on(view, "vcs.pr_merge_method")

    sr._press(fixture, view, session, ["Enter", "Backspace", *"jk"])
    edit = session.edit
    assert edit is not None
    assert edit["text"].endswith("jk")

    sr._press(fixture, view, session, ["Escape", "\\", *"jk"])
    assert session.set_filter == "jk"
