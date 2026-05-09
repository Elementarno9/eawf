"""Claude Code plugin templates bundled with the eawf wheel.

The directory hosts the Jinja2 source templates the Claude runtime
adapter compiles at runtime: ``SKILL.md.j2``, ``agent.md.j2``,
``hook.sh.j2``, and the ``settings.json.fragment.j2`` patcher fragment.
Loading uses :func:`importlib.resources.files` so the templates work
from a wheel install, an editable install, and the source tree alike —
same pattern as :mod:`eawf.templates`.

This module deliberately exposes no public API; callers should reach
the templates via :func:`importlib.resources.files("eawf.templates.claude")`
directly. The renderer modules (``eawf.render.skills``,
``eawf.render.agents``, ``eawf.render.hooks``) keep that import inline
so this package stays a pure resource directory.
"""

from __future__ import annotations
