<!-- Generated from the eawf profile render block `release-process`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=release-process version=1.1 hash=fbd6ece946ecfd02 -->
# `release-process`

Releases are opt-in per repo via the release cadence setting; the per-phase cadence gates phase close on a changelog section, a version bump, a migration note, and the release annotation.

### Release process

Releases are opt-in per repo via ``vcs.conventions.release.cadence``. The two supported cadences:

- ``per_phase`` — agent-driven profile default; each phase PR closes with a release-readiness pre-flight gate and a post-merge auto-tag. Phase close = at least one minor version bump.
- ``manual`` — managed-repo default; releases ride a separate operator-driven tag flow.

Per-phase release pre-flight (gates ``eawf phase close``) requires:

- ``CHANGELOG.md`` has a new section for the release version with at least one bullet.
- ``__version__`` (``src/<pkg>/_version.py`` or the configured ``version_source``) advanced from the prior release.
- A migration note exists when ``state.json`` ``schema_version`` changed since the last release.
- The phase-close commit subject carries the optional ``(release=v<X.Y.Z>)`` annotation accepted by ``tools/commit_prefix_lint.py``.

Post-merge, ``.github/workflows/phase-release.yaml`` reads the annotation, tags the merge commit, and publishes release notes synthesized from the phase PR body. Repos that opt out via ``cadence: manual`` skip the gate and the workflow.
<!-- END EAWF:managed id=release-process -->
