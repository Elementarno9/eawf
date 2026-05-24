"""Jinja2 template payloads bundled with the eawf wheel.

The directory hosts the source templates the renderer compiles at runtime
(``AGENTS.md.j2``, ``CLAUDE.md.j2``, future ``.claude/*.md.j2``). Loading is
done via :func:`importlib.resources.files` so the templates work from a wheel
install, an editable install, and the source tree alike — same pattern used by
:mod:`eawf.platform.profiles.loader` for ``data/<id>.yaml``.

This module deliberately exposes no public API: callers should use
:func:`importlib.resources.files("eawf.platform.templates")` directly. The renderer
modules (e.g. :mod:`eawf.surfaces.render.agents_md`) keep that import inline so
``templates`` stays a pure resource directory.
"""

from __future__ import annotations
