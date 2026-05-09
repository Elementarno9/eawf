"""Render subsystem for eawf — managed-region markers, manifest, drift detection.

Public surface (Phase 3 W03):

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

The render layer is library-only in W03 — CLI surface lands in W04+ via
``eawf render`` / ``eawf sync``.
"""

from __future__ import annotations
