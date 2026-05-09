"""Render subsystem for eawf — managed-region markers, manifest, drift detection.

Public surface (Phase 3 W04):

- :mod:`eawf.render.regions` — marker parsing, ``replace_region``,
  ``extract_region``, ``find_regions``, ``compute_hash``,
  :class:`~eawf.render.regions.Region`,
  :class:`~eawf.render.regions.RegionParseError`.
- :mod:`eawf.render.manifest` — sidecar manifest at ``.ea/indexes/generated.json``,
  :class:`~eawf.render.manifest.Manifest`,
  :class:`~eawf.render.manifest.ManifestEntry`,
  :func:`~eawf.render.manifest.load`, :func:`~eawf.render.manifest.save_atomic`.
- :mod:`eawf.render.drift` — hash-based drift detection,
  :class:`~eawf.render.drift.DriftReport`,
  :func:`~eawf.render.drift.detect_drift`.
- :mod:`eawf.render.agents_md` — :class:`~eawf.render.agents_md.RenderResult`,
  :func:`~eawf.render.agents_md.render_agents_md`.
- :mod:`eawf.render.claude_shim` — :func:`~eawf.render.claude_shim.render_claude_md`.

The render layer is library-only in W04 — CLI surface lands in W05+ via
``eawf render`` / ``eawf sync``.
"""

from __future__ import annotations
