<!-- Generated from the eawf profile render block `agent-driven-milestone-equals-release`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=agent-driven-milestone-equals-release version=1.2 hash=bb7d8fe23b67307d -->
# `agent-driven-milestone-equals-release`

Every accepted Milestone ships as at least a minor release: its acceptance commit bumps the package version module and the release cut from its merged PR tags it.

### Rationale

In a single-operator, agent-driven repo a Milestone is the unit of shipped value, not an internal checkpoint — agent throughput collapses many human-weeks of work into one reviewable delivery. Treating each Milestone as at least a minor release keeps the published version honest about what landed and gives every Milestone a tag to bisect releases against. Drip-releasing per Task would churn tags faster than the value is observable.


### Mechanism

Every accepted Milestone ships as at least a minor release. The commit that records the Milestone's acceptance bumps the package version in ``src/eawf/_version.py`` (e.g. ``0.3.0`` -> ``0.3.1``, or ``-> 0.4.0`` / ``-> 1.0.0`` when the Milestone warrants it), and the release cut from the merged Milestone PR tags it. ``pyproject.toml`` declares the version dynamically; it has no static ``project.version`` field to edit. The version bump rides the same trailing ``state:`` commit that records the acceptance, so the release marker and the accepted state land together. Do not accept a Milestone without bumping the version.


### Verification

Read the Milestone's acceptance commit: it advances ``src/eawf/_version.py`` while ``pyproject.toml`` remains dynamically versioned, and the release tag matching the new version points at the merge. ``git tag --points-at <merge-sha>`` lists the release tag; a Milestone accepted without a version bump is reworked before the PR merges.
<!-- END EAWF:managed id=agent-driven-milestone-equals-release -->
