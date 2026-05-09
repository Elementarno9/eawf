"""Inspect the Claude Code plugin tree for drift / missing files.

``eawf plugin doctor claude`` does a hash-equality sweep over every
file the installer claims ownership of:

- The file is missing → :attr:`DoctorReport.missing` grows.
- The file's bytes hash differs from the expected (registry-rendered)
  bytes → :attr:`DoctorReport.drifted` grows.
- The file matches expectation → :attr:`DoctorReport.ok` grows.

Exit code mapping (the CLI surface in
:mod:`eawf.cli.commands.plugin`):

- Clean (no drift, no missing) → exit 0.
- Any drift or missing entry → exit 8 (``INTEGRITY_VIOLATION``).

Public API::

    DoctorReport                          # dataclass with summary lists
    DoctorEntry                           # one line of the sweep
    doctor_plugin(target_dir) -> DoctorReport
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from eawf.render.manifest import Manifest, ManifestEntry
from eawf.runtimes.claude.plugin_install import (
    _event_type_for,
    _expected_bytes_for,
    expected_paths,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DoctorEntry:
    """One row of the doctor sweep.

    Attributes:
        region_id: The plugin region identifier (e.g.
            ``"plugin.claude.skill.research"``).
        path: Disk path that was inspected.
        kind: One of ``"ok"``, ``"missing"``, ``"drifted"``.
        on_disk_hash: Recomputed hash of the on-disk bytes; ``None``
            when the file is missing.
        expected_hash: Hash of the bytes the installer would emit for
            this region.
    """

    region_id: str
    path: Path
    kind: str  # Literal["ok", "missing", "drifted"]
    on_disk_hash: str | None
    expected_hash: str


@dataclass(frozen=True)
class DoctorReport:
    """Aggregate result of one :func:`doctor_plugin` call.

    Attributes:
        target_dir: Workspace root that was inspected.
        ok: Entries whose on-disk bytes match the expected bytes.
        drifted: Entries whose on-disk bytes have been hand-edited.
        missing: Entries whose file is gone.
        clean: Property derived from the lists.
    """

    target_dir: Path
    ok: list[DoctorEntry] = field(default_factory=list)
    drifted: list[DoctorEntry] = field(default_factory=list)
    missing: list[DoctorEntry] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """``True`` when no entry is drifted or missing."""
        return not self.drifted and not self.missing


def _hash_bytes(payload: bytes) -> str:
    """Return the canonical 16-hex blake2b digest used across the renderer."""
    return hashlib.blake2b(payload, digest_size=8).hexdigest()


def _load_manifest(target_dir: Path) -> Manifest:
    """Load ``.ea/indexes/generated.json`` under *target_dir* (empty when absent)."""
    manifest_path = target_dir / ".ea" / "indexes" / "generated.json"
    if not manifest_path.exists():
        return Manifest()
    raw = manifest_path.read_text(encoding="utf-8")
    if not raw.strip():
        return Manifest()
    body = json.loads(raw)
    return Manifest.model_validate(body)


def _settings_managed_hash_from_manifest(manifest: Manifest, settings_path: Path) -> str | None:
    """Look up the recorded hash for ``settings.json`` from *manifest*.

    Returns the recorded hash for the ``plugin.claude.settings`` entry
    keyed at *settings_path*, or ``None`` when the manifest has no
    record of the file (i.e. install never ran).
    """
    key = f"{settings_path.as_posix()}::plugin.claude.settings"
    entry: ManifestEntry | None = manifest.generated.get(key)
    if entry is None:
        return None
    return entry.hash


def doctor_plugin(target_dir: Path) -> DoctorReport:
    """Inspect the Claude plugin tree under *target_dir*.

    Args:
        target_dir: Workspace root that hosts ``.claude/``.

    Returns:
        :class:`DoctorReport` summarising every file Eä installs.

    The check is byte-equality on the rendered files (skills/agents/
    hooks). For ``settings.json`` we compare the on-disk bytes against
    the manifest's recorded hash, because the user owns most of the
    file and may have legitimately changed unrelated keys; only a hand
    edit *to the rendered bytes themselves* counts as drift. When the
    manifest is absent, ``settings.json`` is reported as ``"missing"``
    so the operator gets a single coherent "rerun install" recipe.
    """
    target_dir = Path(target_dir).resolve()
    manifest = _load_manifest(target_dir)
    paths, settings_path = expected_paths(target_dir)

    ok: list[DoctorEntry] = []
    drifted: list[DoctorEntry] = []
    missing: list[DoctorEntry] = []

    for region_id, path in paths.items():
        expected = _expected_bytes_for_region(region_id)
        expected_hash = _hash_bytes(expected)
        if not path.exists():
            missing.append(
                DoctorEntry(
                    region_id=region_id,
                    path=path,
                    kind="missing",
                    on_disk_hash=None,
                    expected_hash=expected_hash,
                )
            )
            continue
        live = path.read_bytes()
        live_hash = _hash_bytes(live)
        if live_hash == expected_hash:
            ok.append(
                DoctorEntry(
                    region_id=region_id,
                    path=path,
                    kind="ok",
                    on_disk_hash=live_hash,
                    expected_hash=expected_hash,
                )
            )
        else:
            drifted.append(
                DoctorEntry(
                    region_id=region_id,
                    path=path,
                    kind="drifted",
                    on_disk_hash=live_hash,
                    expected_hash=expected_hash,
                )
            )

    # settings.json compared against the manifest-recorded hash.
    expected_settings_hash = _settings_managed_hash_from_manifest(manifest, settings_path)
    if expected_settings_hash is None:
        missing.append(
            DoctorEntry(
                region_id="plugin.claude.settings",
                path=settings_path,
                kind="missing",
                on_disk_hash=None,
                expected_hash="",
            )
        )
    else:
        if not settings_path.exists():
            missing.append(
                DoctorEntry(
                    region_id="plugin.claude.settings",
                    path=settings_path,
                    kind="missing",
                    on_disk_hash=None,
                    expected_hash=expected_settings_hash,
                )
            )
        else:
            live = settings_path.read_bytes()
            live_hash = _hash_bytes(live)
            kind = "ok" if live_hash == expected_settings_hash else "drifted"
            entry = DoctorEntry(
                region_id="plugin.claude.settings",
                path=settings_path,
                kind=kind,
                on_disk_hash=live_hash,
                expected_hash=expected_settings_hash,
            )
            (ok if kind == "ok" else drifted).append(entry)

    logger.info(
        f"doctor_plugin target={target_dir} ok={len(ok)} drifted={len(drifted)} "
        f"missing={len(missing)}"
    )
    return DoctorReport(target_dir=target_dir, ok=ok, drifted=drifted, missing=missing)


def _expected_bytes_for_region(region_id: str) -> bytes:
    """Wrapper around :func:`plugin_install._expected_bytes_for` (re-exported)."""
    # Indirection so tests can monkeypatch a single resolution point.
    if region_id.startswith("plugin.claude.hook."):
        # Resolve via the shared helper which knows the HookEventType lookup.
        _event_type_for(region_id.removeprefix("plugin.claude.hook."))
    return _expected_bytes_for(region_id)


__all__ = [
    "DoctorEntry",
    "DoctorReport",
    "doctor_plugin",
]
