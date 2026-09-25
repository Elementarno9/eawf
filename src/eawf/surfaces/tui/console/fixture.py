"""The console fixture: every register a console frame renders from, loaded strict.

The registers are the prototype's data files (``proto.json``, ``detail.json``,
``g.json`` and ``settings.json``). Every model forbids unknown keys, so a field the
extractor starts emitting cannot pass silently, and field names are the console's own
with the files' spellings kept as aliases. Counts are never stored: every renderer
derives them from these registers at render time. Two registers stay mutable because a
confirmed verb writes them: an attention action's state and ledger, and the settings
values a lens edit stores.

The files carry the chrome tables too (buckets, menus, connection states, entry states
and the settings catalog). A fixture keeps its own copy of them, so the golden contract
replays exactly what the prototype drew; a console built from the packaged chrome alone
holds a fixture with no prototype rows at all (:meth:`Fixture.from_chrome`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel

from eawf.surfaces.tui.console.action_menu import ActionMenus, MenuVerb, VerbWeight
from eawf.surfaces.tui.console.chrome import (
    Bucket,
    ConsoleChrome,
    EntryState,
    SettingsCatalog,
    States,
)
from eawf.surfaces.tui.console.tokens import TRUTH

# The files a fixture directory holds, one per register.
FIXTURE_FILES: tuple[str, ...] = ("proto.json", "detail.json", "g.json", "settings.json")

#: The scope a console holding no prototype rows names until a projection states one.
UNKNOWN_SCOPE = TRUTH["unknown"].unicode


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
        prototype: Whether the registers hold the prototype's rows. A fixture built from
            the packaged chrome alone holds none, and the console then draws the unknown
            token wherever no read model is held rather than an empty prototype frame.

    Raises:
        ValueError: an ``actions`` row declares a verb the action menu refuses.
    """

    __slots__ = (
        "attention_by_id",
        "chrome",
        "detail",
        "fleet_by_run",
        "menus",
        "proto",
        "prototype",
        "registers",
        "settings",
    )

    def __init__(
        self,
        proto: Proto,
        detail: Detail,
        registers: Registers,
        settings: SettingsCatalog,
        *,
        prototype: bool = True,
    ) -> None:
        self.proto = proto
        self.detail: Mapping[str, tuple[tuple[str, str], ...]] = detail.root
        self.registers = registers
        self.settings = settings
        self.prototype = prototype
        self.chrome = ConsoleChrome(
            buckets=proto.buckets,
            xbuckets=proto.xbuckets,
            actions=proto.actions,
            states=proto.states,
            entry=proto.entry,
            settings=settings,
        )
        self.fleet_by_run = {row.run: row for row in proto.fleet}
        self.attention_by_id = {row.id: row for row in proto.attention}
        self.menus = ActionMenus(
            {route: [menu_verb(row) for row in rows] for route, rows in proto.actions.items()}
        )

    @classmethod
    def from_chrome(cls, chrome: ConsoleChrome) -> Fixture:
        """Return a fixture that holds ``chrome`` and not one prototype row.

        The scope is the unknown token until a projection names one, every record
        register is empty, and :attr:`prototype` is false.
        """
        proto = Proto(
            scope=UNKNOWN_SCOPE,
            revision=0,
            tracks=(),
            fleet=(),
            buckets=chrome.buckets,
            xbuckets=chrome.xbuckets,
            attention=(),
            timeline=(),
            actions=chrome.actions,
            states=chrome.states,
            entry=chrome.entry,
        )
        return cls(proto, Detail({}), _EMPTY_REGISTERS, chrome.settings, prototype=False)

    @property
    def scope(self) -> str:
        """Return the attached scope's name."""
        return self.proto.scope

    def record(self, entity_id: str | None) -> tuple[tuple[str, str], ...] | None:
        """Return the stored record of ``entity_id``, or ``None`` when none is held."""
        if entity_id is None:
            return None
        return self.detail.get(entity_id)


_EMPTY_REGISTERS = Registers.model_validate(
    {
        "CHIP": {},
        "EV_RUNGS": (),
        "TR_EARLY": (),
        "TR_BLOCKS": (),
        "TR_FEED": (),
        "TR_GLYPH": {},
        "TR_CLS": {},
        "TR_PAL": {},
        "RECEIPTS": {},
        "ROUTE_OF": {},
        "ROUTE_SUBJ": {},
        "TAB_OWNER": {},
        "BACKLOG_GROUPS": (),
        "CAM_STEPS": (),
        "CAM_STEP_DETAIL": (),
        "CAM_EVID": (),
        "CAM_ART": (),
        "CAM_SECTS": (),
        "BL_DRAFTS": (),
        "BL_DEFERRED": (),
        "OVERLAY_ROUTES": (),
    }
)


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
