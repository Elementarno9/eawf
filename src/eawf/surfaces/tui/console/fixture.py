"""The console fixture: every register a console frame renders from, loaded strict.

The registers are the prototype's data files (``proto.json``, ``detail.json``,
``g.json`` and ``settings.json``). Every model forbids unknown keys, so a field the
extractor starts emitting cannot pass silently, and field names are the console's own
with the files' spellings kept as aliases. Counts are never stored: every renderer
derives them from these registers at render time. Two registers stay mutable because a
confirmed verb writes them: an attention action's state and ledger, and the settings
values a lens edit stores.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel

from eawf.surfaces.tui.console.action_menu import ActionMenus, MenuVerb, VerbWeight

# The files a fixture directory holds, one per register.
FIXTURE_FILES: tuple[str, ...] = ("proto.json", "detail.json", "g.json", "settings.json")


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_by_name=True)


class Milestone(_Frozen):
    """One milestone of a track, as the scope-home tree lists it."""

    id: str
    name: str
    state: str
    batches: int
    due: str


class Track(_Frozen):
    """One track and its milestones."""

    id: str
    runs: int
    needs: int
    prog: str
    as_of: str = Field(alias="as")
    milestones: tuple[Milestone, ...]


class FleetRow(_Frozen):
    """One Run of the fleet register."""

    run: str
    task: str
    state: str
    reason: str
    as_of: str = Field(alias="as")
    prov: str
    bucket: str


class Bucket(_Frozen):
    """One exception bucket, with its sub-buckets when it has any."""

    key: str
    label: str
    sub: tuple[Bucket, ...] | None = None


class Action(BaseModel):
    """One attention action; a confirmed verb writes its state and ledger."""

    model_config = ConfigDict(extra="forbid", validate_by_name=True)

    id: str
    text: str
    due: str
    kind: str
    effects: str
    not_effects: str = Field(alias="not")
    not_deny: str = Field(alias="notDeny")
    not_snooze: str = Field(alias="notSnooze")
    not_resolve: str = Field(alias="notResolve")
    track: str
    bucket: str
    state: str
    ledger: list[tuple[str, str]]


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


class Proto(_Frozen):
    """The core registers: scope, tracks, fleet, buckets, attention and verbs."""

    scope: str
    revision: int
    tracks: tuple[Track, ...]
    fleet: tuple[FleetRow, ...]
    buckets: tuple[Bucket, ...]
    xbuckets: tuple[Bucket, ...]
    attention: tuple[Action, ...]
    timeline: tuple[tuple[str, str, str, str], ...]
    # key, verb, available, reason, authority, effects, non-effects, then weight and target
    actions: dict[str, tuple[tuple[str, ...], ...]]
    states: States
    entry: tuple[EntryState, ...]


class Detail(RootModel[dict[str, tuple[tuple[str, str], ...]]]):
    """Entity id to its ordered ``(label, value)`` record rows."""


class Registers(_Frozen):
    """The route-local registers: campaign, transcript, backlog, evidence and receipts."""

    chip: dict[str, str] = Field(alias="CHIP")
    ev_rungs: tuple[dict[str, Any], ...] = Field(alias="EV_RUNGS")
    tr_early: tuple[Any, ...] = Field(alias="TR_EARLY")
    tr_blocks: tuple[dict[str, Any], ...] = Field(alias="TR_BLOCKS")
    tr_feed: tuple[Any, ...] = Field(alias="TR_FEED")
    tr_glyph: dict[str, str | None] = Field(alias="TR_GLYPH")
    tr_cls: dict[str, str | None] = Field(alias="TR_CLS")
    tr_pal: dict[str, Any] = Field(alias="TR_PAL")
    receipts: dict[str, Any] = Field(alias="RECEIPTS")
    route_of: dict[str, str] = Field(alias="ROUTE_OF")
    route_subj: dict[str, str] = Field(alias="ROUTE_SUBJ")
    tab_owner: dict[str, str] = Field(alias="TAB_OWNER")
    backlog_groups: tuple[str, ...] = Field(alias="BACKLOG_GROUPS")
    cam_steps: tuple[Any, ...] = Field(alias="CAM_STEPS")
    cam_step_detail: tuple[dict[str, Any], ...] = Field(alias="CAM_STEP_DETAIL")
    cam_evid: tuple[Any, ...] = Field(alias="CAM_EVID")
    cam_art: tuple[dict[str, Any], ...] = Field(alias="CAM_ART")
    cam_sects: tuple[str, ...] = Field(alias="CAM_SECTS")
    bl_drafts: tuple[Any, ...] = Field(alias="BL_DRAFTS")
    bl_deferred: tuple[Any, ...] = Field(alias="BL_DEFERRED")
    overlay_routes: tuple[str, ...] = Field(alias="OVERLAY_ROUTES")


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


def menu_verb(columns: tuple[str, ...]) -> MenuVerb:
    """Return the menu verb one ``actions`` register row declares.

    Raises:
        ValueError: the row has fewer than the seven leading columns, or declares a verb
            the action menu refuses.
    """
    if len(columns) < 7:
        raise ValueError(f"action row {columns!r} has {len(columns)} columns, not at least 7")
    key, verb, available, reason, authority, effects, non_effects, *rest = columns
    return MenuVerb(
        key=key,
        verb=verb,
        available=available == "yes",
        reason=reason,
        authority=authority,
        effects=effects,
        non_effects=non_effects,
        weight=VerbWeight(rest[0]) if rest else VerbWeight.HEAVY,
        target=rest[1] if len(rest) > 1 else None,
    )


class Fixture:
    """Every register the console renders from, keyed for lookup.

    Args:
        proto: The core registers.
        detail: The entity records.
        registers: The route-local registers.
        settings: The settings catalog.

    Raises:
        ValueError: an ``actions`` row declares a verb the action menu refuses.
    """

    __slots__ = (
        "attention_by_id",
        "detail",
        "fleet_by_run",
        "menus",
        "proto",
        "registers",
        "settings",
    )

    def __init__(
        self,
        proto: Proto,
        detail: Detail,
        registers: Registers,
        settings: SettingsCatalog,
    ) -> None:
        self.proto = proto
        self.detail: Mapping[str, tuple[tuple[str, str], ...]] = detail.root
        self.registers = registers
        self.settings = settings
        self.fleet_by_run = {row.run: row for row in proto.fleet}
        self.attention_by_id = {row.id: row for row in proto.attention}
        self.menus = ActionMenus(
            {route: [menu_verb(row) for row in rows] for route, rows in proto.actions.items()}
        )

    @property
    def scope(self) -> str:
        """Return the attached scope's name."""
        return self.proto.scope

    def record(self, entity_id: str | None) -> tuple[tuple[str, str], ...] | None:
        """Return the stored record of ``entity_id``, or ``None`` when none is held."""
        if entity_id is None:
            return None
        return self.detail.get(entity_id)


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_fixture(fixture_dir: Path) -> Fixture:
    """Load and validate the four register files into a fresh, independently mutable fixture.

    Args:
        fixture_dir: The directory holding every file of :data:`FIXTURE_FILES`.

    Raises:
        FileNotFoundError: a register file is missing.
        pydantic.ValidationError: a file carries an unknown key or a malformed row.
    """
    proto_raw, detail_raw, registers_raw, settings_raw = (
        _read(fixture_dir / name) for name in FIXTURE_FILES
    )
    return Fixture(
        Proto.model_validate(proto_raw),
        Detail.model_validate(detail_raw),
        Registers.model_validate(registers_raw),
        SettingsCatalog.model_validate(settings_raw),
    )
