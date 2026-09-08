"""The frozen pack-to-port normalisation map, loaded strict.

The rev H design pack is the reference contract, and the port compares its render
against it only through this map: each entry is one admitted transformation, naming what
the pack renders, what the port renders, the golden ids it touches and the rulings that
admit it. A difference outside the map fails the replay.

Two entries also carry a classification correction the tracked route registry applies at
import, so the map -- not a second hand-maintained table -- is the one place a corrected
route key or family is written.
"""

from __future__ import annotations

import fnmatch
import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

from .goldens import NORMALISATION_MAP


class NormalisationEntry(BaseModel):
    """One admitted transformation between a pack frame and the port's render.

    Attributes:
        entry: The transformation's name, unique across the map.
        pack: What the design pack renders.
        port: What the port renders instead.
        golden_ids: ``fnmatch`` patterns over frame ids and journey ids. Every pattern
            must select at least one record of the tracked contract.
        rulings: The ruling ids that admit the transformation. Never empty.
        route_id: The pack route id this entry reclassifies, when it carries a
            classification correction.
        route_key: The port's canonical key for ``route_id``.
        route_family: The port's family for ``route_id``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    entry: str
    pack: str
    port: str
    golden_ids: tuple[str, ...]
    rulings: tuple[str, ...]
    route_id: str | None = None
    route_key: str | None = None
    route_family: str | None = None

    @model_validator(mode="after")
    def _check(self) -> NormalisationEntry:
        """Reject an empty entry or a half-written classification correction.

        Raises:
            ValueError: the entry names no golden id or no ruling, or names a route
                correction without both the corrected key and the corrected family.
        """
        if not self.golden_ids:
            raise ValueError(f"entry {self.entry!r} names no golden id")
        if not self.rulings:
            raise ValueError(f"entry {self.entry!r} names no ruling")
        corrections = (self.route_id, self.route_key, self.route_family)
        if any(part is not None for part in corrections) and not all(
            part is not None for part in corrections
        ):
            raise ValueError(
                f"entry {self.entry!r} is a half-written route correction: "
                "route_id, route_key and route_family stand or fall together"
            )
        return self

    def selects(self, contract_id: str) -> bool:
        """Return whether ``contract_id`` is touched by this entry."""
        return any(fnmatch.fnmatchcase(contract_id, pattern) for pattern in self.golden_ids)


class RouteCorrection(BaseModel):
    """The port's key and family for one pack route id."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    family: str


class NormalisationMap(BaseModel):
    """The whole map: the contract it implements, its provenance and its entries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    spec: str
    source: str
    entries: tuple[NormalisationEntry, ...]

    @model_validator(mode="after")
    def _check(self) -> NormalisationMap:
        """Reject an empty map or a duplicated entry name.

        Raises:
            ValueError: the map carries no entry, or two entries share a name.
        """
        if not self.entries:
            raise ValueError("the normalisation map carries no entry")
        names = [entry.entry for entry in self.entries]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate normalisation entries: {', '.join(duplicates)}")
        return self

    def route_corrections(self) -> dict[str, RouteCorrection]:
        """Return the classification correction per pack route id."""
        return {
            entry.route_id: RouteCorrection(key=entry.route_key, family=entry.route_family)
            for entry in self.entries
            if entry.route_id is not None
            and entry.route_key is not None
            and entry.route_family is not None
        }


@lru_cache(maxsize=1)
def load_map(path: Path = NORMALISATION_MAP) -> NormalisationMap:
    """Load and validate the tracked map.

    Args:
        path: The map file. Defaults to the tracked one.

    Raises:
        FileNotFoundError: ``path`` does not exist.
        pydantic.ValidationError: the file carries an unknown key or a malformed entry.
    """
    return NormalisationMap.model_validate(json.loads(path.read_text(encoding="utf-8")))
