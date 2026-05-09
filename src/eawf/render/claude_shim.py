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
from pathlib import Path

from eawf.render._atomic import atomic_write_text
from eawf.render.agents_md import RenderResult

logger = logging.getLogger(__name__)


_CLAUDE_PAYLOAD: str = "@AGENTS.md\n"


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
    atomic_write_text(target, _CLAUDE_PAYLOAD)
    return RenderResult(
        target=target,
        regions_added=[],
        regions_updated=[],
        regions_unchanged=[],
        hand_edits_preserved=False,
    )
