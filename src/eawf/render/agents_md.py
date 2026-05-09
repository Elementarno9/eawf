"""Render AGENTS.md from a composed profile, preserving hand-edits in the file.

Per ``eawf-v0.1-plan.md`` §P03 W04 (line 249):

- Take a :class:`~eawf.profiles.models.ComposedProfile` whose ``render_blocks``
  list declares the regions that should appear on disk.
- Filter to ``target == "AGENTS.md"`` blocks (other targets — ``.claude/...``
  skill/agent files — are handled by sibling renderers in W05+).
- For each block: render its ``body_template`` via Jinja2 against the
  bundled ``AGENTS.md.j2`` template, then call
  :func:`~eawf.render.regions.replace_region` so an existing block with the
  same id is replaced in-place and a brand-new block is appended.
  Anything *outside* a managed region (hand-written paragraphs above, below,
  or between blocks) round-trips byte-stably — that is the contract that
  makes "re-render is safe" hold.
- Atomically write the new file content via tempfile + ``os.replace`` under a
  portalock — same discipline as :mod:`eawf.state.writer`.
- Update a :class:`~eawf.render.manifest.Manifest` so drift detection
  (:mod:`eawf.render.drift`) and ``eawf doctor`` know the renderer's view of
  what's on disk. The caller is responsible for persisting the manifest via
  :func:`~eawf.render.manifest.save_atomic` — keeping the renderer pure means
  callers can batch multiple targets into a single manifest write.

Public API::

    RenderResult                    # dataclass: target + per-region status
    render_agents_md(composed, target, manifest, *, generator) -> tuple[RenderResult, Manifest]
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from eawf.lock import portalock
from eawf.profiles.models import ComposedProfile, RenderBlock
from eawf.render import regions
from eawf.render.manifest import Manifest, ManifestEntry

logger = logging.getLogger(__name__)


_TARGET_FILENAME: str = "AGENTS.md"
_TEMPLATE_NAME: str = "AGENTS.md.j2"
_TEMPLATES_PACKAGE: str = "eawf.templates"


@dataclass(frozen=True)
class RenderResult:
    """Summary of one :func:`render_agents_md` call.

    Attributes:
        target: Path the renderer wrote (or would have written, when no work
            was needed).
        regions_added: Region ids that did not exist on disk before the call
            and were appended to the file.
        regions_updated: Region ids whose body or version changed compared to
            what was on disk.
        regions_unchanged: Region ids whose body+version exactly matched the
            on-disk content. The renderer still rewrites the file (even when
            every region is unchanged) so the manifest's ``generated_at`` stays
            in sync; this list lets callers report a no-op cleanly.
        hand_edits_preserved: ``True`` when the on-disk file had any content
            outside managed regions (hand-written sections above/below/between
            blocks). ``False`` when the file was empty or had no unmanaged
            content. Useful for tooling messages like "preserved 3 hand-edited
            paragraphs".
    """

    target: Path
    regions_added: list[str] = field(default_factory=list)
    regions_updated: list[str] = field(default_factory=list)
    regions_unchanged: list[str] = field(default_factory=list)
    hand_edits_preserved: bool = False


def _load_environment() -> Environment:
    """Load a Jinja2 environment rooted at the bundled templates directory.

    Uses :func:`importlib.resources.files` so the renderer works equally
    from a wheel install, an editable install, and the source tree (mirrors
    :mod:`eawf.profiles.loader`).
    """
    templates_dir = files(_TEMPLATES_PACKAGE)
    # Materialise to a filesystem path. importlib.resources.files() returns a
    # MultiplexedPath at module level — for a regular wheel install it is
    # already a real Path; ``str()`` is the documented way to get its location.
    templates_path = str(templates_dir)
    env = Environment(
        loader=FileSystemLoader(templates_path),
        undefined=StrictUndefined,
        keep_trailing_newline=False,
        # autoescape stays off — markdown output, not HTML; templates are trusted (bundled).
        autoescape=False,
    )
    return env


def _render_block_body(env: Environment, block: RenderBlock, composed: ComposedProfile) -> str:
    """Render one ``RenderBlock`` to its body string via the AGENTS.md template.

    The template currently emits ``block.body_template`` verbatim — see
    ``src/eawf/templates/AGENTS.md.j2`` for the substitution policy.
    """
    template = env.get_template(_TEMPLATE_NAME)
    return template.render(block=block, composed=composed)


def _extract_targeted_blocks(composed: ComposedProfile) -> list[RenderBlock]:
    """Return only render_blocks targeting the AGENTS.md file, in caller order."""
    return [b for b in composed.render_blocks if b.target == _TARGET_FILENAME]


def _has_unmanaged_content(text: str) -> bool:
    """Return ``True`` when *text* has bytes outside any managed region.

    Used to populate :attr:`RenderResult.hand_edits_preserved`. We strip the
    BEGIN…END span of every parsed region from a copy of *text* and check
    whether non-whitespace remains.
    """
    if text == "":
        return False
    parsed = regions.find_regions(text)
    if not parsed:
        return text.strip() != ""
    # Walk regions in order, accumulate unmanaged spans.
    leftover_chunks: list[str] = []
    cursor = 0
    for region in parsed:
        start, end = region.span
        leftover_chunks.append(text[cursor:start])
        cursor = end
    leftover_chunks.append(text[cursor:])
    return any(chunk.strip() != "" for chunk in leftover_chunks)


def _atomic_write_text(target: Path, payload: str) -> None:
    """Tempfile + fsync + ``os.replace`` + parent-dir fsync — text variant.

    Mirrors :func:`eawf.state.writer._write_payload` but writes UTF-8 text
    rather than orjson bytes, so callers can pass already-rendered markdown.
    Caller is expected to hold the portalock for *target* (see
    :func:`render_agents_md`).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(4)
    tmp = target.with_name(f"{target.name}.tmp.{suffix}")
    encoded = payload.encode("utf-8")
    try:
        with tmp.open("wb") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        parent_fd = os.open(target.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        logger.info(f"render.agents_md wrote {target} bytes={len(encoded)}")
    finally:
        tmp.unlink(missing_ok=True)


def render_agents_md(
    composed: ComposedProfile,
    target: Path,
    manifest: Manifest,
    *,
    generator: str = "eawf-render",
) -> tuple[RenderResult, Manifest]:
    """Render the AGENTS.md-targeted regions of *composed* into *target*.

    Workflow:

    1. Read existing *target* if present (so unmanaged content can round-trip).
    2. Filter ``composed.render_blocks`` to ``target == "AGENTS.md"``.
    3. For each block, render its body via Jinja2 + ``replace_region`` —
       insertions append, updates rewrite the BEGIN…END span, untouched
       regions are no-ops.
    4. Acquire portalock on *target*, atomically write the new text via
       tempfile + ``os.replace`` (parent-dir fsync included).
    5. Build an updated :class:`Manifest` whose entries for *target* match
       the regions just emitted; entries for *other* targets are preserved
       verbatim. Hash recorded is :func:`~eawf.render.regions.compute_hash`
       of the rendered body text — same digest the BEGIN marker carries.

    Args:
        composed: Composed profile body whose ``render_blocks`` drive the
            rendering. Blocks with ``target != "AGENTS.md"`` are skipped.
        target: Destination path. Parent directories are created on demand.
        manifest: Current manifest. NOT mutated — a new :class:`Manifest`
            value is returned. The caller is responsible for persisting it
            via :func:`~eawf.render.manifest.save_atomic`.
        generator: Recorded on each :class:`ManifestEntry`. Defaults to
            ``"eawf-render"``; init / sync may pass profile-scoped names like
            ``"profile:python"``.

    Returns:
        ``(result, updated_manifest)`` — :class:`RenderResult` summarises the
        per-region delta; *updated_manifest* is a new value with the AGENTS.md
        entries overwritten and other targets carried through.
    """
    target = Path(target)
    target_str = str(target)

    blocks = _extract_targeted_blocks(composed)

    existing_text = ""
    if target.exists():
        existing_text = target.read_text(encoding="utf-8")
    hand_edits_preserved = _has_unmanaged_content(existing_text)

    # Index existing regions for added/updated/unchanged classification.
    existing_regions = {r.id: r for r in regions.find_regions(existing_text)}

    env = _load_environment()
    new_text = existing_text
    added: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    timestamp = datetime.now(UTC).isoformat()
    rendered_bodies: dict[str, str] = {}

    for block in blocks:
        body = _render_block_body(env, block, composed)
        rendered_bodies[block.id] = body
        prev = existing_regions.get(block.id)
        if prev is None:
            added.append(block.id)
        elif prev.body == body and prev.version == block.version:
            unchanged.append(block.id)
        else:
            updated.append(block.id)
        new_text = regions.replace_region(
            new_text,
            id=block.id,
            version=block.version,
            body=body,
        )

    with portalock.acquire(target, timeout=5.0):
        _atomic_write_text(target, new_text)

    # Preserve manifest entries for OTHER targets, refresh ours from this run.
    new_generated: dict[str, ManifestEntry] = {
        key: entry for key, entry in manifest.generated.items() if entry.target != target_str
    }
    for block in blocks:
        body = rendered_bodies[block.id]
        composite_key = f"{target_str}::{block.id}"
        new_generated[composite_key] = ManifestEntry(
            target=target_str,
            region_id=block.id,
            version=block.version,
            hash=regions.compute_hash(body),
            generator=generator,
            generated_at=timestamp,
        )

    updated_manifest = Manifest(version=manifest.version, generated=new_generated)
    result = RenderResult(
        target=target,
        regions_added=added,
        regions_updated=updated,
        regions_unchanged=unchanged,
        hand_edits_preserved=hand_edits_preserved,
    )
    logger.info(
        f"render_agents_md target={target} "
        f"added={len(added)} updated={len(updated)} unchanged={len(unchanged)} "
        f"hand_edits_preserved={hand_edits_preserved}"
    )
    return result, updated_manifest
