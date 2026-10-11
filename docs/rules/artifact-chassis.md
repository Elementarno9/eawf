<!-- Generated from the eawf profile render block `artifact-chassis`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=artifact-chassis version=1.3 hash=3f8d2ca00dd7f297 -->
# `artifact-chassis`

Markdown promoted out of ``.ea/local/`` uses the renderer-owned Summary / References / Provenance / Scrub chassis, with typed citations and no absolute paths.

### Artifact chassis and citations

Artifacts stay in ``.ea/local/`` unless promoted, and a local artifact is cited from state by its sha256 digest. The chassis applies only to promoted artifacts: research, plan, audit, decision, hypothesis, and incident markdown promoted to ``.ea/artifacts/`` uses renderer-owned chassis sections: ``Summary``, ``References``, ``Provenance``, and ``Scrub``. Local drafts under ``.ea/local/`` carry an ``eawf-template`` sentinel; promoted artifacts do not.

Citations use dense ``[N]`` markers backed by typed ``Citation`` rows. References stay repo-relative, external URL, or Eawf URN. Absolute local paths, host-local URLs, and PII must fail validation before promotion or PR text ships.

**IntentBrief + NarrativeBundle.** ``/research`` outputs a typed :class:`~eawf.kernel.spec.intent.IntentBrief` whose claims carry ``evidence_refs``; a brief is promotable iff every claim has at least one resolving + entailing reference (the ``evidence_refs`` invariant). The promoted artifact wraps the brief in a :class:`~eawf.surfaces.render.narrative.NarrativeBundle` that fixes provenance to the originating session and the ``IntentBrief`` URN — researcher prose and the typed claim graph stay in lockstep through promotion.
<!-- END EAWF:managed id=artifact-chassis -->
