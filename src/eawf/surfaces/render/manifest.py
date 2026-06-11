"""Sidecar manifest at ``.ea/indexes/generated.json``.

The manifest records, for every managed region the renderer has emitted, the
declared hash + version + generator + ISO 8601 timestamp. It is the source of
truth for drift detection (compared against on-disk regions by
:mod:`eawf.surfaces.render.drift`) and for the "what does eawf currently own?" answer
that ``eawf doctor`` / ``eawf sync`` will surface in W04+.

Disk layout::

    {
      "version": 1,
      "generated": {
        "<target>::<region_id>": {
          "target": "<target>",
          "region_id": "<region_id>",
          "version": "1.0",
          "hash": "<16-hex>",
          "generator": "profile:python",
          "generated_at": "2026-05-09T12:34:56+00:00"
        },
        ...
      }
    }

Composite key ``"<target>::<region_id>"`` makes the same region id usable on
multiple target files (e.g. the ``rules`` region in both ``AGENTS.md`` and
``.opencode/AGENTS.md``).

Public API:

    Manifest, ManifestEntry           # Pydantic models with extra="forbid"
    load(path) -> Manifest            # absent → empty Manifest
    save_atomic(path, manifest)       # portalock + tempfile + os.replace
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from eawf.runtime.lock import portalock

logger = logging.getLogger(__name__)


class ManifestEntry(BaseModel):
    """One generated-region row in the manifest.

    Attributes:
        target: Repo-relative POSIX path of the rendered file (or
            absolute path for user-scope installs).
        region_id: Logical region identifier; pairs with *target* to
            form the composite manifest key ``"<target>::<region_id>"``.
        version: Generator-declared version (``"1.0"`` for v0.1 callers).
        hash: 16-hex blake2b-64 of the rendered body bytes — drift
            detection compares the live file body against this value.
        generator: Symbolic id of the generator that produced the row
            (``"profile:python"``, ``"eawf-plugin-codex"``, ...).
        generated_at: ISO 8601 UTC timestamp at render time.
        scope: Optional install scope (``"project"`` or ``"user"``) for
            entries emitted by the runtime plugin installers. ``None``
            for back-compat with pre-P28 entries (every renderer that
            owns a sidecar prior to P28 wrote into a single scope, so
            the field was implicit). The plugin-cross-scope-dup detector
            (``eawf doctor`` / ``eawf plugin doctor``) keys off this
            field to flag the same region_id installed at both scopes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str
    region_id: str
    version: str
    hash: str
    generator: str
    generated_at: str
    scope: str | None = None


class Manifest(BaseModel):
    """Top-level manifest body — schema version + region table."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    generated: dict[str, ManifestEntry] = {}


def _empty() -> Manifest:
    return Manifest(version=1, generated={})


def load(path: Path) -> Manifest:
    """Load the manifest at *path*. Return an empty :class:`Manifest` if absent.

    Raises:
        ValueError: The file exists but contains invalid JSON.
        pydantic.ValidationError: The file's JSON does not match the schema.
    """
    path = Path(path)
    if not path.exists():
        return _empty()
    raw = path.read_text(encoding="utf-8")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifest at {path} is not valid JSON: {exc}") from exc
    return Manifest.model_validate(body)


def _serialise(manifest: Manifest) -> bytes:
    """Render *manifest* as deterministic, byte-stable JSON."""
    payload = manifest.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, indent=2).encode("utf-8")


def save_atomic(path: Path, manifest: Manifest) -> None:
    """Persist *manifest* atomically to *path*.

    Procedure (mirrors :func:`eawf.kernel.state.writer.atomic_write_json`):

    1. Acquire ``portalock.acquire(path)`` (sibling lock, default 5 s timeout).
    2. Serialise to a sibling tempfile ``<path>.tmp.<hex4>``.
    3. ``flush()`` + ``os.fsync(fileno)`` so bytes hit the platter.
    4. ``os.replace(tmp, path)`` — atomic POSIX/Windows rename.
    5. ``os.fsync`` the parent directory so the rename is durable.
    6. Release the lock; clean up the tempfile on any failure.

    The serialised bytes are deterministic (sorted keys, fixed indent) so two
    saves of the same manifest produce identical content — handy for drift
    detection on the manifest *itself*.
    """
    path = Path(path)
    payload = _serialise(manifest)
    path.parent.mkdir(parents=True, exist_ok=True)

    with portalock.acquire(path, timeout=5.0):
        suffix = secrets.token_hex(4)
        tmp = path.with_name(f"{path.name}.tmp.{suffix}")
        try:
            with tmp.open("wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            if hasattr(os, "O_DIRECTORY"):  # parent-dir fsync is POSIX-only (no-op on Windows)
                parent_fd = os.open(path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            logger.info(f"render_manifest path={path} bytes={len(payload)}")
        finally:
            tmp.unlink(missing_ok=True)
