"""CLAUDE.md shim renderer — emits the literal ``@AGENTS.md\\n`` import.

Per ``ea-proposal.md`` and the v0.1 plan: CLAUDE.md is a one-line file whose
sole purpose is the Claude Code ``@AGENTS.md`` import directive. There are no
managed regions; the entire file is generated content. Re-rendering is
therefore idempotent — same one-byte difference between presence and absence,
nothing else.

The shim still uses tempfile + ``os.replace`` so a process crash mid-write
cannot leave a half-written file in place. We deliberately do NOT load
``CLAUDE.md.j2`` through Jinja2 here: the payload is constant, parsing a
template just to re-emit a fixed string adds latency and a failure surface
without buying anything. The ``.j2`` source file is kept on disk (and
bundled with the wheel) so a future change — e.g. a CLAUDE.md frontmatter
header — can swap to template rendering without breaking the public shape
of :class:`~eawf.render.agents_md.RenderResult`.

Public API::

    render_claude_md(target) -> RenderResult
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from eawf.lock import portalock
from eawf.render.agents_md import RenderResult

logger = logging.getLogger(__name__)


_CLAUDE_PAYLOAD: str = "@AGENTS.md\n"


def _atomic_write_text(target: Path, payload: str) -> None:
    """Tempfile + fsync + ``os.replace`` + parent-dir fsync.

    Local copy of :func:`eawf.render.agents_md._atomic_write_text` to keep the
    shim independent — :mod:`eawf.render.agents_md` is otherwise unrelated to
    the CLAUDE.md shim path, and importing a private helper from there would
    create a ``claude_shim → agents_md`` dep that's purely circumstantial.
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
        logger.info(f"render.claude_shim wrote {target} bytes={len(encoded)}")
    finally:
        tmp.unlink(missing_ok=True)


def render_claude_md(target: Path) -> RenderResult:
    """Atomically write the CLAUDE.md shim (``@AGENTS.md\\n``) to *target*.

    Idempotent — the payload is fixed, so two consecutive calls produce
    byte-identical files. There are no managed regions, so the returned
    :class:`RenderResult` always reports empty add/update/unchanged lists and
    ``hand_edits_preserved=False``. Callers wanting "did anything change?"
    should compare ``target.read_bytes()`` before and after.

    Args:
        target: Destination path. Parent directories are created on demand.

    Returns:
        :class:`RenderResult` describing this call. Region lists are empty by
        construction (CLAUDE.md has no managed regions in v0.1).
    """
    target = Path(target)
    with portalock.acquire(target, timeout=5.0):
        _atomic_write_text(target, _CLAUDE_PAYLOAD)
    return RenderResult(
        target=target,
        regions_added=[],
        regions_updated=[],
        regions_unchanged=[],
        hand_edits_preserved=False,
    )
