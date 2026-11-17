<!-- Generated from the eawf profile render block `agent-driven-milestone-equals-release`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=agent-driven-milestone-equals-release version=1.4 hash=5ae9a6459ce277b5 -->
# `agent-driven-milestone-equals-release`

A single-Milestone release ships as at least a minor release; a Milestone of a declared multi-Milestone release ships as a dev pre-release; stable fast-forwards main to a reviewed dev head.

### Rationale

In a single-operator, agent-driven repo a Milestone is the unit of shipped value, not an internal checkpoint — agent throughput collapses many human-weeks of work into one reviewable delivery. Treating each Milestone as at least a minor release keeps the published version honest about what landed and gives every Milestone a tag to bisect releases against. Drip-releasing per Task would churn tags faster than the value is observable.


### Mechanism

The accepted Milestone of a single-Milestone release ships as at least a minor release. The commit that records the Milestone's acceptance bumps the package version in ``src/eawf/_version.py`` (e.g. ``0.3.0`` -> ``0.3.1``, or ``-> 0.4.0`` / ``-> 1.0.0`` when the Milestone warrants it). ``pyproject.toml`` declares the version dynamically; it has no static ``project.version`` field to edit. The version bump rides the acceptance record's ``state:`` commit, so the release marker and the accepted state land together. Do not accept such a Milestone without bumping the version. Each Milestone of a declared multi-Milestone release instead ships as a ``{version}-dev.{n}`` pre-release cut on ``dev``, and the stable release follows its last Milestone.

The Milestone PR targets ``dev``, the integration branch, and ``dev`` fast-forwards to the reviewed head. Pre-release tags ``{version}-dev.{n}`` are cut on ``dev``. A stable release fast-forwards ``main`` to a reviewed ``dev`` head and is tagged there, so ``main`` carries only stable releases and the fixes that ship from it.


### Verification

Read the Milestone's acceptance commit: it advances ``src/eawf/_version.py`` while ``pyproject.toml`` remains dynamically versioned. ``git tag --points-at <dev-head>`` lists the ``{version}-dev.{n}`` pre-release cut from the merged PR, and the stable tag points at a ``main`` commit that is also a reviewed ``dev`` head. A single-Milestone release accepted without a version bump is reworked before the PR merges.
<!-- END EAWF:managed id=agent-driven-milestone-equals-release -->
