"""Statusline module bundle for ``eawf cc statusline`` (Phase 4 W06).

Each submodule exports a single :func:`build` function that takes a Claude
JSON payload (already decoded into a ``dict``) and the resolved
``.ea/state.json`` path, and returns a
:class:`~eawf.surfaces.render.statusline.StatuslineSegment`. Modules MUST never
raise — the orchestrator tolerates exceptions but a clean degradation
(``status="missing"`` / ``status="degraded"``) is the documented contract.

Public surface: import ``build`` from each submodule directly. The
orchestrator hard-wires the call list in
:mod:`eawf.runtime.runtimes.claude.statusline`; users should not iterate over this
package's contents.
"""

from __future__ import annotations
