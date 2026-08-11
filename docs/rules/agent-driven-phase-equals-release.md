<!-- Generated from the eawf profile render block `agent-driven-phase-equals-release`. Do not hand-edit: re-run `eawf sync`. -->

# `agent-driven-phase-equals-release`

Every closed phase ships as at least a minor release: the phase-close commit bumps the package version module and the PR merge tags that release.

### Rationale

In a single-operator, agent-driven repo a phase is the unit of shipped value, not an internal checkpoint — agent throughput collapses many human-weeks of work into one reviewable delivery. Treating each phase as at least a minor release keeps the published version honest about what landed and gives every phase a tag to bisect releases against. Drip- releasing per wave would churn tags faster than the value is observable.


### Mechanism

Every closed phase ships as at least a minor release. The phase-close commit bumps the package version in ``src/eawf/_version.py`` (e.g. ``0.3.0`` -> ``0.3.1``, or ``-> 0.4.0`` / ``-> 1.0.0`` when the phase warrants it) and the phase PR merge tags that release. ``pyproject.toml`` declares the version dynamically; it has no static ``project.version`` field to edit. The version bump and tag ride the same ``[P<NN>] state: close iter + phase`` commit that ends the phase, so the release marker and the closed state land together. Do not close a phase without bumping the version.


### Verification

Read the phase-close commit: it advances ``src/eawf/_version.py`` while ``pyproject.toml`` remains dynamically versioned, and the merge creates a release tag matching the new version. ``git tag --points-at <phase-close-sha>`` lists the release tag; a phase that closed without a version bump or a tag is reworked before the PR merges.
