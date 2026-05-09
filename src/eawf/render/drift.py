"""Hash-based drift detection for managed regions.

Drift kinds:

- ``"ok"``      — on-disk region body hashes to the manifest's recorded hash.
- ``"hand-edited"`` — the region exists on disk but its body hash differs
  from the manifest's hash. (We compute the hash from the *body text*, not
  the BEGIN marker's ``hash=`` attribute, so a hand-edit that leaves the
  marker untouched is still caught.)
- ``"missing"`` — the manifest expects a region with this id on this target,
  but the file (or the marker block) is gone.

Public API::

    DriftReport
    detect_drift(target_path, manifest) -> list[DriftReport]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from eawf.render import regions
from eawf.render.manifest import Manifest

logger = logging.getLogger(__name__)


DriftKind = Literal["hand-edited", "missing", "ok"]


@dataclass(frozen=True)
class DriftReport:
    """One row of drift output.

    Attributes:
        target: The file inspected.
        id: The region id from the manifest entry.
        kind: ``"ok"`` / ``"hand-edited"`` / ``"missing"``.
        on_disk_hash: Recomputed hash of the on-disk body, or ``None`` when
            the region is missing entirely.
        manifest_hash: Hash recorded in the manifest entry.
    """

    target: Path
    id: str
    kind: DriftKind
    on_disk_hash: str | None
    manifest_hash: str


def detect_drift(target_path: Path, manifest: Manifest) -> list[DriftReport]:
    """Compare the manifest entries for *target_path* against the on-disk file.

    Only manifest entries whose ``target`` field equals
    ``Path(target_path).as_posix()`` are considered — entries pointing at
    other targets are silently ignored so callers can pass the full project
    manifest. The POSIX form ensures the comparison stays platform-stable
    (a manifest written on Linux and inspected on Windows still matches).

    File missing → every entry for that target reports ``"missing"``. File
    present but parse-malformed → :exc:`~eawf.render.regions.RegionParseError`
    propagates (do not silently swallow corruption).
    """
    target_path = Path(target_path)
    target_key = target_path.as_posix()
    relevant = [e for e in manifest.generated.values() if e.target == target_key]
    if not relevant:
        return []

    if not target_path.exists():
        return [
            DriftReport(
                target=target_path,
                id=entry.region_id,
                kind="missing",
                on_disk_hash=None,
                manifest_hash=entry.hash,
            )
            for entry in relevant
        ]

    text = target_path.read_text(encoding="utf-8")
    on_disk_by_id = {r.id: r for r in regions.find_regions(text)}

    reports: list[DriftReport] = []
    for entry in relevant:
        region = on_disk_by_id.get(entry.region_id)
        if region is None:
            reports.append(
                DriftReport(
                    target=target_path,
                    id=entry.region_id,
                    kind="missing",
                    on_disk_hash=None,
                    manifest_hash=entry.hash,
                )
            )
            continue
        live_hash = regions.compute_hash(region.body)
        kind: DriftKind = "ok" if live_hash == entry.hash else "hand-edited"
        reports.append(
            DriftReport(
                target=target_path,
                id=entry.region_id,
                kind=kind,
                on_disk_hash=live_hash,
                manifest_hash=entry.hash,
            )
        )
    return reports
