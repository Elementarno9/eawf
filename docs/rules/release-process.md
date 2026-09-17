<!-- Generated from the eawf profile render block `release-process`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=release-process version=1.2 hash=67e000e7b42e0811 -->
# `release-process`

Releases are opt-in per repo via the release cadence setting; the per-phase cadence gates phase close on a changelog section, a version bump, a migration note, and the release annotation.

### Release process

Releases are opt-in per repo via ``vcs.conventions.release.cadence``. The two supported cadences:

- ``manual`` — the default for every repo; releases ride a separate operator-driven tag flow.
- ``per-phase`` — opt-in, set explicitly in the repo config; each phase PR closes with a release-readiness pre-flight gate and a post-merge release tag. Phase close = at least one minor version bump.

Under ``per-phase``, ``eawf phase close`` refuses until the phase-close audit carries a passing ``release-preflight`` check. That check covers:

- ``CHANGELOG.md`` has a new section for the release version with at least one bullet.
- The package version module (``src/<pkg>/_version.py``) advanced from the prior release.
- A migration note exists when ``state.json`` ``schema_version`` changed since the last release.
- The phase-close commit subject carries the optional ``(release=v<X.Y.Z>)`` annotation accepted by ``tools/commit_prefix_lint.py``.

Post-merge, ``.github/workflows/phase-release.yaml`` reads the annotation, checks it against the package version, and prints the tag command. It pushes no tag and creates no release object: a tag pushed with a workflow's default token starts no other workflow, so nothing would publish. Who pushes the tag depends on how the repo releases:

- A repo that releases through an eawf release train tags the merge commit with ``eawf release tag --push`` from a clean checkout of it. The verb runs the readiness sweep before it tags, and its push starts the publish workflows, which mark every dev and release-candidate tag as a prerelease.
- Every other repo keeps its own tag flow: the tagging it already runs after a merge stays its tagging path.

Repos on the default ``manual`` cadence skip the gate and the workflow.
<!-- END EAWF:managed id=release-process -->
