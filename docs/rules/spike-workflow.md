<!-- Generated from the eawf profile render block `spike-workflow`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=spike-workflow version=1.4 hash=7d8b64d769591176 -->
# `spike-workflow`

A spike is a time-boxed read-only investigation whose brief lands under ``.ea/local/`` and feeds the next plan; the brief stays local, cited by sha256 digest, unless the operator asks to promote it.

### Spike workflow

A *spike* is a short, time-boxed, read-only investigation run before claiming a real wave — used when the next move is unclear and the operator needs a brief or experimental verdict to write the wave's success criteria. The dedicated ``/spike`` skill (v0.4) wraps the existing ``/research`` surface with the brief-naming + dispatch-prompt conventions below; legacy use of ``/research`` for spikes stays valid and renders identically.

**When to spike.** Reach for a spike when (a) the wave's success criteria cannot yet be written without first reading code or running a probe, (b) two or more design alternatives need a verdict before ``/roadmap propose`` can commit to a DAG, or (c) an audit hypothesis needs an evidence sweep before ``set-verdict``. Skip the spike when the next move is obvious — go straight to ``/roadmap propose`` or ``/prep`` claim.

**Where the output lives.** Spike output is a research brief under ``.ea/local/<YYYY-MM-DD>-<slug>.md`` (or the conventional ``.ea/local/research/`` sub-directory). Filenames follow the ``<date>-<slug>.md`` stem so the brief sorts chronologically and slug-matches against the wave or phase it informs. Briefs stay local-only — ``.ea/local/`` is gitignored — and a decision they inform cites them by sha256 digest. A brief is promoted to ``.ea/artifacts/`` only when the operator asks (the artifact-chassis rule applies on promotion).

**How the verdict feeds the workflow.** The spike's verdict is the input to the next ``/roadmap propose --phase P<NN>`` or ``/prep`` claim. Reference the brief by repo-relative path in the roadmap proposal, the wave's plan body, or the dispatch prompt — the wave dispatch renderer surfaces spike briefs whose filename matches the wave / iter / phase id under a ``## References`` section so the subagent reads them before starting work.

**No promotion by default.** A spike brief that informs a Decision row stays at ``.ea/local/research/<date>-<slug>.md``; the Decision cites its sha256 digest. Promotion to ``.ea/artifacts/research/`` is an explicit operator request, and it runs the artifact-chassis validator + scrub gate.
<!-- END EAWF:managed id=spike-workflow -->
