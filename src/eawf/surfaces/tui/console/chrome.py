"""The console chrome: the static tables every console frame draws around its rows.

Chrome is what the console knows before it has read a single record: the exception
bucket labels, the verbs each route's action menu offers, what each connection value
reads and refuses, the entry layer's pre-session states, and the settings catalog's
schema. It ships inside the package as ``data/chrome.json``, so a console built from it
needs nothing from the test tree. The prototype registers (the invented fleet, tracks,
attention actions and records the golden contract replays) are not chrome and never
ship here.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: The packaged chrome file, beside this module under ``data/``.
CHROME_RESOURCE = "chrome.json"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_name=True)


class Bucket(_Frozen):
    """One exception bucket, with its sub-buckets when it has any."""

    key: str
    label: str
    sub: tuple[Bucket, ...] | None = None


class States(_Frozen):
    """The connection values and what each one reads, refuses and marks."""

    connection: tuple[str, ...]
    reads: dict[str, str]
    refuse: dict[str, str]
    muts: dict[str, str]
    glyph: dict[str, str]


class EntryState(_Frozen):
    """One pre-session state of the entry layer."""

    id: str
    exit: str
    state: str
    glyph: str
    title: str
    keys: tuple[tuple[str, str], ...]
    paths: tuple[tuple[str, str], ...]
    tail: tuple[str, ...]
    panes: tuple[tuple[str, str], ...] | None = None
    cols: tuple[tuple[str, int], ...] | None = None
    rows: tuple[tuple[str, ...], ...] | None = None
    paths_label: str | None = Field(default=None, alias="pathsLabel")
    paths_note: str | None = Field(default=None, alias="pathsNote")
    paths_ordered: bool | None = Field(default=None, alias="pathsOrdered")


class SettingsCatalog(_Frozen):
    """The settings catalog: layers, sections, stored values and their documentation.

    The stored values (``stored``) are the one mutable table: a lens edit writes into it.
    """

    layers: tuple[dict[str, str], ...] = Field(alias="LAYERS")
    writable: tuple[str, ...] = Field(alias="WRITABLE")
    sections: dict[str, Any] = Field(alias="SECTIONS")
    stored: dict[str, Any] = Field(alias="SET")
    runtime: dict[str, Any] = Field(alias="RUNTIME")
    floor: dict[str, Any] = Field(alias="FLOOR")
    types: dict[str, Any] = Field(alias="TYPE")
    unset: str = Field(alias="UNSET")
    doc: dict[str, Any] = Field(alias="DOC")
    choices: dict[str, Any] = Field(alias="CHOICES")
    cats: tuple[Any, ...] = Field(alias="CATS")
    rail: tuple[Any, ...] = Field(alias="RAIL")
    section_rows: tuple[dict[str, Any], ...] = Field(alias="SECTION_ROWS")
    section_order: tuple[str, ...] = Field(alias="sectionOrder")
    names: tuple[str, ...]


class ConsoleChrome(_Frozen):
    """Every static table the console draws, independent of any record.

    Attributes:
        buckets: The Activity exception buckets.
        xbuckets: The Attention exception buckets.
        actions: Route id to its action-menu rows: key, verb, available, reason,
            authority, effects, non-effects, then optionally weight and target.
        states: The connection values and what each one reads and refuses.
        entry: The entry layer's pre-session states, in the packet's order.
        settings: The settings catalog.
    """

    buckets: tuple[Bucket, ...]
    xbuckets: tuple[Bucket, ...]
    actions: dict[str, tuple[tuple[str, ...], ...]]
    states: States
    entry: tuple[EntryState, ...]
    settings: SettingsCatalog


def load_chrome() -> ConsoleChrome:
    """Load the packaged chrome, fresh on every call so a lens edit stays local.

    Raises:
        FileNotFoundError: the package was built without ``data/chrome.json``.
        pydantic.ValidationError: the file carries an unknown key or a malformed row.
    """
    resource = files(__package__).joinpath("data", CHROME_RESOURCE)
    return ConsoleChrome.model_validate(json.loads(resource.read_text(encoding="utf-8")))
