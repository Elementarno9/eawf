"""CLAUDE.md shim renderer — emits the literal ``@AGENTS.md\\n`` import.

Per ``docs/policy/agents-claude-md.md``: CLAUDE.md is a one-line file whose
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
of :class:`~eawf.surfaces.render.agents_md.RenderResult`.

A repository that authors ``.ea/rules.yaml`` gets a different shim: the rule
render transaction writes it through :func:`render_import_shim`, and its whole
content imports the generated policy projection, because Claude discovers
policy only through ``CLAUDE.md`` and would otherwise see the committed card
without the obligations the policy projection carries. The shim's path is a
host fact of the runtime, recorded in
:mod:`eawf.platform.rules.host_facts`, not a constant of this module.

Public API::

    render_claude_md(target) -> RenderResult
    render_import_shim(imports) -> str
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from eawf.platform.rules.views import refuse_view_imports
from eawf.surfaces.render._atomic import atomic_write_text
from eawf.surfaces.render.agents_md import RenderResult

logger = logging.getLogger(__name__)


_CLAUDE_PAYLOAD: str = "@AGENTS.md\n"


def render_import_shim(imports: Sequence[str]) -> str:
    """Render a shim whose entire content imports ``imports``, one per line.

    The shim carries no policy text of its own, so the runtime reading it
    receives byte-identical policy to a runtime reading the imported file.

    Args:
        imports: Repository-relative POSIX paths to import, in order.

    Returns:
        One ``@<path>`` line per import.

    Raises:
        ValueError: When ``imports`` is empty or a path is absolute, climbs
            out of the repository, or contains whitespace.
        RuleViewStartupImportError: When a path is a module view, which is
            read on demand and never loaded at session start.
    """
    if not imports:
        raise ValueError("an import shim must import at least one file")
    for path in imports:
        pure = PurePosixPath(path)
        if not path or pure.is_absolute() or ".." in pure.parts or any(c.isspace() for c in path):
            raise ValueError(f"import shim path {path!r} must be a plain repository-relative path")
    refuse_view_imports(imports)
    return "".join(f"@{path}\n" for path in imports)


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
