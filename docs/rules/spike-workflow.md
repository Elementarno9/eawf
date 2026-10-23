<!-- Generated from the eawf profile render block `spike-workflow`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=spike-workflow version=1.6 hash=869c77d08d80fa8f -->
# `spike-workflow`

A spike is a time-boxed read-only investigation; its text brief stays local under ``.ea/local/research/``, cited by sha256 digest, and heavy artefacts go under ``local.scratch_root``.

### Spike workflow

A *spike* is a short, time-boxed, read-only investigation run before a Task is planned — used when the next move is unclear and the operator needs a brief or experimental verdict to write the Task's success criteria. ``/research`` answers one question with a one-page brief, and ``/spike`` builds and independently verifies a local proof of concept; both follow the brief-naming conventions below.

**When to spike.** Reach for a spike when (a) a Task's success criteria cannot yet be written without first reading code or running a probe, (b) two or more design alternatives need a verdict before ``/plan propose`` can commit to a Batch and Task graph, or (c) an audit hypothesis needs an evidence sweep before ``set-verdict``. Skip the spike when the next move is obvious — go straight to ``/plan propose`` or ``/dispatch``.

**Where the output lives.** A brief is ``.ea/local/research/<YYYY-MM-DD>-<slug>.md``, so it sorts by date and slug-matches the Task or Milestone it informs. ``.ea/local/research/`` holds only text evidence: briefs, notes, logs and small outputs. Heavy artefacts (virtual environments, models, installs, archives) go outside the repository under the scratch root the ``local.scratch_root`` config leaf names, each in its own directory with a ``.retention`` file holding its expiry date as ``YYYY-MM-DD``. A directory under ``.ea/local/`` may carry a ``.retention`` too: ``eawf local gc`` removes each one whose date has passed (``--dry-run`` lists them first), and ``eawf doctor`` warns once ``.ea/local/`` grows past ``local.size_warn_mb``.

**How the verdict feeds the workflow.** The spike's verdict is the input to the next ``/plan propose``. Reference the brief by repo-relative path in the PlanRevision, the Task's contract, or the dispatch prompt, so the subagent reads it before starting work.

**No promotion by default.** Briefs stay local (``.ea/local/`` is gitignored), and a Decision they inform cites them by sha256 digest. Promotion to ``.ea/artifacts/research/`` is an explicit operator request, and it runs the artifact-chassis validator and scrub gate.
<!-- END EAWF:managed id=spike-workflow -->
