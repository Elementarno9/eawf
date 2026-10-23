<!-- Generated from the eawf profile render block `release-process`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=release-process version=2.1 hash=78edea701ff2d52a -->
# `release-process`

A release-train repo tags a merged Milestone only after ``eawf release preflight`` passes, via ``eawf release pipeline --milestone``; other repos keep their own tag flow.

### Release process

Releases run through the explicit release verbs: no Milestone acceptance waits on a release, and no config leaf selects a release cadence.

In a repo that releases through an eawf release train, ``eawf release preflight <version>`` sweeps every readiness signal over the checkout and reports all of them on every run, so one pass shows the whole repair list. Among them:

- ``CHANGELOG.md`` has a new section for the release version with at least one bullet.
- The package version module (``src/<pkg>/_version.py``) advanced from the prior release.
- A migration note exists when the state ``schema_version`` changed since the last release.
- The ``provider`` signal finds a passing live dispatch smoke for every claimed os_class. Before cutting a release candidate, run the live dispatch smoke on Linux (bwrap) and on macOS (seatbelt) with ``EAWF_SMOKE_PROVIDER=claude EAWF_SMOKE_EVIDENCE_DIR=<canary evidence directory> uv run pytest -m e2e tests/e2e/test_smoke_milestone.py`` and commit its ``dispatch-smoke-receipts.json``.

A repo that releases through an eawf release train cuts each release from a merged Milestone with ``eawf release pipeline <version> --milestone <MLS-NNNN> --publish``, run from a clean checkout of the merge commit, which walks the checkpoint from tag to BAKED. It runs the readiness sweep before it tags, its push starts the publish workflows, which mark every dev and release-candidate tag as a prerelease, and it then advances the train and checks that ``main`` carries exactly the commits of each completed Batch of the Milestone, found by their ``Task:`` and ``Batch:`` trailers.

Every other repo keeps its own tag flow: the tagging it already runs after a merge stays its tagging path.
<!-- END EAWF:managed id=release-process -->
