"""Typed fixture loaded from fixture/*.json (extracted from the packet's prototype).

Every model forbids unknown keys, so a field the extractor starts emitting cannot pass
silently. Counts are never stored: every renderer derives them from these registers.
The prototype mutates two attention fields (state, ledger) on confirm, so ``Action`` is
the one model left mutable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel

from ..goldens import FIXTURE_DIR as GOLDEN_FIXTURE_DIR

FIXTURE_DIR = GOLDEN_FIXTURE_DIR


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Milestone(_Frozen):
    id: str
    name: str
    state: str
    batches: int
    due: str


class Track(_Frozen):
    id: str
    runs: int
    needs: int
    prog: str
    as_: str = Field(alias="as")
    milestones: tuple[Milestone, ...]


class FleetRow(_Frozen):
    run: str
    task: str
    state: str
    reason: str
    as_: str = Field(alias="as")
    prov: str
    bucket: str


class Bucket(_Frozen):
    key: str
    label: str
    sub: tuple[Bucket, ...] | None = None


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    text: str
    due: str
    kind: str
    effects: str
    not_: str = Field(alias="not")
    notDeny: str
    notSnooze: str
    notResolve: str
    track: str
    bucket: str
    state: str
    ledger: list[tuple[str, str]]


class States(_Frozen):
    connection: tuple[str, ...]
    reads: dict[str, str]
    refuse: dict[str, str]
    muts: dict[str, str]
    glyph: dict[str, str]


class EntryState(_Frozen):
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
    pathsLabel: str | None = None
    pathsNote: str | None = None
    pathsOrdered: bool | None = None


class Proto(_Frozen):
    scope: str
    revision: int
    tracks: tuple[Track, ...]
    fleet: tuple[FleetRow, ...]
    buckets: tuple[Bucket, ...]
    xbuckets: tuple[Bucket, ...]
    attention: tuple[Action, ...]
    timeline: tuple[tuple[str, str, str, str], ...]
    # [key, verb, available, reason, authority, effects, non-effects, weight?, target?]
    actions: dict[str, tuple[tuple[str, ...], ...]]
    states: States
    entry: tuple[EntryState, ...]


class Detail(RootModel[dict[str, tuple[tuple[str, str], ...]]]):
    """EA_DETAIL: entity id -> ordered (label, value) rows."""


class PhaseG(_Frozen):
    """The inline fixtures proto-g.js keeps as locals; typed loosely, keys closed."""

    CHIP: dict[str, str]
    EV_RUNGS: tuple[dict[str, Any], ...]
    TR_EARLY: tuple[Any, ...]
    TR_BLOCKS: tuple[dict[str, Any], ...]
    TR_FEED: tuple[Any, ...]
    TR_GLYPH: dict[str, str | None]
    TR_CLS: dict[str, str | None]
    TR_PAL: dict[str, Any]
    RECEIPTS: dict[str, Any]
    ROUTE_OF: dict[str, str]
    ROUTE_SUBJ: dict[str, str]
    TAB_OWNER: dict[str, str]
    BACKLOG_GROUPS: tuple[str, ...]
    CAM_STEPS: tuple[Any, ...]
    CAM_STEP_DETAIL: tuple[dict[str, Any], ...]
    CAM_EVID: tuple[Any, ...]
    CAM_ART: tuple[dict[str, Any], ...]
    CAM_SECTS: tuple[str, ...]
    BL_DRAFTS: tuple[Any, ...]
    BL_DEFERRED: tuple[Any, ...]
    OVERLAY_ROUTES: tuple[str, ...]


class Settings(_Frozen):
    """EA_SETTINGS data tables; the functions (catOf, editorKind, ...) are re-implemented."""

    LAYERS: tuple[dict[str, str], ...]
    WRITABLE: tuple[str, ...]
    SECTIONS: dict[str, Any]
    SET: dict[str, Any]
    RUNTIME: dict[str, Any]
    FLOOR: dict[str, Any]
    TYPE: dict[str, Any]
    UNSET: str
    DOC: dict[str, Any]
    CHOICES: dict[str, Any]
    CATS: tuple[Any, ...]
    RAIL: tuple[Any, ...]
    SECTION_ROWS: tuple[dict[str, Any], ...]
    sectionOrder: tuple[str, ...]
    names: tuple[str, ...]


class Fixture:
    """Every register the console renders from, keyed for lookup, counts derived."""

    __slots__ = ("attention_by_id", "detail", "fleet_by_run", "g", "proto", "settings")

    def __init__(self, proto: Proto, detail: Detail, g: PhaseG, settings: Settings) -> None:
        self.proto = proto
        self.detail = detail.root
        self.g = g
        self.settings = settings
        self.fleet_by_run = {row.run: row for row in proto.fleet}
        self.attention_by_id = {row.id: row for row in proto.attention}

    @property
    def scope(self) -> str:
        return self.proto.scope

    def record(self, entity_id: str | None) -> tuple[tuple[str, str], ...] | None:
        if entity_id is None:
            return None
        return self.detail.get(entity_id)


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_fixture(fixture_dir: Path = FIXTURE_DIR) -> Fixture:
    """Load and validate the four fixture files; a fresh mutable copy on every call."""
    proto = Proto.model_validate(_read(fixture_dir / "proto.json"))
    detail = Detail.model_validate(_read(fixture_dir / "detail.json"))
    g = PhaseG.model_validate(_read(fixture_dir / "g.json"))
    settings = Settings.model_validate(_read(fixture_dir / "settings.json"))
    return Fixture(proto, detail, g, settings)
