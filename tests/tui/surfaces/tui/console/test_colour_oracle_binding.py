"""The shipped themes equal the tracked packet colour oracle, bar one recorded divergence.

``tests/fixtures/console/colour-oracle.json`` holds the solid colour variables of the
final packet stylesheet, the map binding each theme variable to one of them, and the one
divergence the product keeps on purpose. These tests hold the palette to that file in both
directions: every packet variable is bound or explained, every theme variable is bound or
explained, and the only theme value that differs from its packet value is the recorded
dark status green. The colour-blind theme has no packet counterpart, so its chrome is held
to the product-authored rows in the same file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from textual.theme import Theme

from eawf.surfaces.tui.chassis.theme import EA_CB, EA_DARK, EA_LIGHT, EA_THEMES
from eawf.surfaces.tui.console.token_map import TOKEN_MAP

TESTS_ROOT = Path(__file__).resolve().parents[4]
ORACLE_PATH = TESTS_ROOT / "fixtures" / "console" / "colour-oracle.json"
ORACLE: dict[str, Any] = json.loads(ORACLE_PATH.read_text(encoding="utf-8"))

#: The packet themes and the registered theme each one is bound into.
PACKET_THEMES: dict[str, Theme] = {"dark": EA_DARK, "light": EA_LIGHT}

#: Theme variable -> packet variable, chrome and semantic together.
BINDINGS: dict[str, str] = {**ORACLE["bindings"]["chrome"], **ORACLE["bindings"]["semantic"]}

#: The chrome rows Textual itself derives surfaces from, beyond the variables map.
TEXTUAL_CHROME: tuple[str, ...] = ("surface", "panel", "foreground", "border")


def _packet_vars(packet_theme: str) -> dict[str, str]:
    return dict(ORACLE["oracle"][packet_theme]["vars"])


def _resolved(theme: Theme) -> dict[str, str]:
    """Resolve a theme's CSS variables the way ``App.get_css_variables`` does."""
    return {**theme.to_color_system().generate(), **theme.variables}


def _differing_oracle_rows() -> dict[tuple[str, str], set[str]]:
    """Map each packet row some theme variable disagrees with to the values shipped for it."""
    rows: dict[tuple[str, str], set[str]] = {}
    for packet_theme, theme in PACKET_THEMES.items():
        packet = _packet_vars(packet_theme)
        for theme_var, packet_var in BINDINGS.items():
            shipped = theme.variables[theme_var]
            if shipped != packet[packet_var]:
                rows.setdefault((packet_theme, packet_var), set()).add(shipped)
    return rows


def test_colour_oracle_source_is_the_repo_relative_packet_stylesheet() -> None:
    source = ORACLE["source"]
    assert source == ".ea/local/packet/handoff-v0.7-packet/stages/stage.css"
    assert not Path(source).is_absolute()


def test_colour_oracle_values_are_lowercase_six_digit_hex() -> None:
    values = [value for block in ORACLE["oracle"].values() for value in block["vars"].values()]
    values += list(ORACLE["product_authored"]["cb"]["chrome"].values())
    for entry in ORACLE["divergences"]:
        values += [entry["oracle"], entry["shipped"]]
    for value in values:
        assert len(value) == 7 and value.startswith("#") and value == value.lower(), value
        int(value[1:], 16)


@pytest.mark.parametrize("packet_theme", sorted(PACKET_THEMES))
def test_colour_oracle_every_packet_var_is_bound_or_explained(packet_theme: str) -> None:
    packet = set(_packet_vars(packet_theme))
    bound = set(BINDINGS.values())
    unbound = set(ORACLE["unbound_oracle_vars"])
    assert not bound & unbound
    assert packet == bound | unbound
    assert not packet & set(ORACLE["non_solid"])


@pytest.mark.parametrize("theme", EA_THEMES, ids=lambda theme: theme.name)
def test_colour_oracle_every_theme_var_is_bound_or_explained(theme: Theme) -> None:
    unbound = set(ORACLE["unbound_theme_vars"])
    assert not set(BINDINGS) & unbound
    assert set(theme.variables) == set(BINDINGS) | unbound


def test_theme_variables_equal_oracle_with_one_admitted_divergence() -> None:
    """Only the dark st-closed row differs, and only to the recorded shipped value."""
    (divergence,) = ORACLE["divergences"]
    differing = _differing_oracle_rows()
    assert differing == {(divergence["theme"], divergence["oracle_var"]): {divergence["shipped"]}}


@pytest.mark.parametrize("packet_theme", sorted(PACKET_THEMES))
def test_theme_variables_equal_oracle_after_the_divergence(packet_theme: str) -> None:
    packet = _packet_vars(packet_theme)
    for entry in ORACLE["divergences"]:
        if entry["theme"] == packet_theme:
            assert packet[entry["oracle_var"]] == entry["oracle"]
            packet[entry["oracle_var"]] = entry["shipped"]
    expected = {theme_var: packet[packet_var] for theme_var, packet_var in BINDINGS.items()}
    shipped = {name: PACKET_THEMES[packet_theme].variables[name] for name in BINDINGS}
    assert shipped == expected


def test_admitted_divergence_is_the_dark_status_closed_green() -> None:
    (divergence,) = ORACLE["divergences"]
    dark = _packet_vars("dark")
    assert (divergence["theme"], divergence["oracle_var"]) == ("dark", "st-closed")
    assert divergence["shipped"] == "#009e73"
    # The packet collapses the status green onto the accent; the product keeps them apart
    # by shipping a green the packet itself declares.
    assert divergence["oracle"] == dark["accent"]
    assert divergence["shipped"] == dark[divergence["shipped_as"]]
    assert EA_DARK.variables["status-closed"] == divergence["shipped"]
    assert EA_DARK.variables["status-closed"] != EA_DARK.variables["accent"]
    assert divergence["rationale"]


def test_ea_cb_chrome_equals_product_authored_rows() -> None:
    rows = ORACLE["product_authored"]["cb"]["chrome"]
    assert set(rows) == set(ORACLE["bindings"]["chrome"])
    assert {name: EA_CB.variables[name] for name in rows} == rows


def test_ea_cb_chrome_rows_are_marked_product_authored() -> None:
    assert "cb" not in ORACLE["oracle"]
    assert ORACLE["product_authored"]["cb"]["basis"]


@pytest.mark.parametrize("theme", EA_THEMES, ids=lambda theme: theme.name)
def test_textual_derives_from_the_bound_chrome(theme: Theme) -> None:
    """The ctor mirrors the chrome, so Textual's own derived colours start from the oracle.

    Textual copies ``primary`` into ``$border`` unless the variables override it; the
    neutral packet border must win.
    """
    generated = theme.to_color_system().generate()
    for name in TEXTUAL_CHROME:
        assert generated[name].lower() == theme.variables[name], name
    assert generated["border"].lower() != theme.variables["primary"]


@pytest.mark.parametrize("theme", EA_THEMES, ids=lambda theme: theme.name)
def test_token_map_tokens_resolve_on_every_theme(theme: Theme) -> None:
    resolved = _resolved(theme)
    missing = sorted({row.token for row in TOKEN_MAP} - set(resolved))
    assert not missing, f"{theme.name} leaves {missing} unresolved"


def test_differing_oracle_rows_reds_on_a_drifted_theme(monkeypatch: pytest.MonkeyPatch) -> None:
    """A light-theme near miss is reported as a second differing row, so the gate can red."""
    drifted = dict(EA_LIGHT.variables, warn="#a35b00")
    monkeypatch.setitem(
        PACKET_THEMES, "light", Theme(name="drifted", primary="#000000", variables=drifted)
    )
    assert ("light", "warn") in _differing_oracle_rows()
