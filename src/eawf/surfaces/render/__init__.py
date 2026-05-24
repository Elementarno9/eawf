"""Render subsystem for eawf — managed-region markers, manifest, drift detection.

Public surface (Phase 3 W04 + P25-W09):

- :mod:`eawf.surfaces.render.regions` — marker parsing, ``replace_region``,
  ``extract_region``, ``find_regions``, ``compute_hash``,
  :class:`~eawf.surfaces.render.regions.Region`,
  :class:`~eawf.surfaces.render.regions.RegionParseError`.
- :mod:`eawf.surfaces.render.manifest` — sidecar manifest at ``.ea/indexes/generated.json``,
  :class:`~eawf.surfaces.render.manifest.Manifest`,
  :class:`~eawf.surfaces.render.manifest.ManifestEntry`,
  :func:`~eawf.surfaces.render.manifest.load`, :func:`~eawf.surfaces.render.manifest.save_atomic`.
- :mod:`eawf.surfaces.render.drift` — hash-based drift detection,
  :class:`~eawf.surfaces.render.drift.DriftReport`,
  :func:`~eawf.surfaces.render.drift.detect_drift`.
- :mod:`eawf.surfaces.render.agents_md` — :class:`~eawf.surfaces.render.agents_md.RenderResult`,
  :func:`~eawf.surfaces.render.agents_md.render_agents_md`.
- :mod:`eawf.surfaces.render.claude_shim` —
  :func:`~eawf.surfaces.render.claude_shim.render_claude_md`.
- :mod:`eawf.surfaces.render.envelope` — :class:`~eawf.surfaces.render.envelope.OutputEnvelope`,
  :class:`~eawf.surfaces.render.envelope.EnvelopeHeader`,
  :class:`~eawf.surfaces.render.envelope.EnvelopeFooter`,
  :data:`~eawf.surfaces.render.envelope.EnvelopeStatus`,
  :func:`~eawf.surfaces.render.envelope.to_markdown`,
  :func:`~eawf.surfaces.render.envelope.from_markdown`.
- :mod:`eawf.surfaces.render.brand` — Eä logotype, glyph sets, TTY/ASCII switcher
  (:func:`~eawf.surfaces.render.brand.select_glyphs`,
  :func:`~eawf.surfaces.render.brand.render_breadcrumb_head`).

The render layer is library-only in W04 — CLI surface lands in W05+ via
``eawf render`` / ``eawf sync``.
"""

from __future__ import annotations
