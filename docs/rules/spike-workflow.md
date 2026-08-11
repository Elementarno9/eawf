<!-- Generated from the eawf profile render block `spike-workflow`. Do not hand-edit: re-run `eawf sync`. -->

# `spike-workflow`

A spike is a time-boxed read-only investigation whose brief lands under ``.ea/local/`` and feeds the next roadmap proposal or wave claim; promote it only when it ratifies a verdict.

### Spike workflow

A *spike* is a short, time-boxed, read-only investigation run before claiming a real wave — used when the next move is unclear and the operator needs a brief or experimental verdict to write the wave's success criteria. The dedicated ``/spike`` skill (v0.4) wraps the existing ``/research`` surface with the brief-naming + dispatch-prompt conventions below; legacy use of ``/research`` for spikes stays valid and renders identically.

**When to spike.** Reach for a spike when (a) the wave's success criteria cannot yet be written without first reading code or running a probe, (b) two or more design alternatives need a verdict before ``/roadmap propose`` can commit to a DAG, or (c) an audit hypothesis needs an evidence sweep before ``set-verdict``. Skip the spike when the next move is obvious — go straight to ``/roadmap propose`` or ``/prep`` claim.

**Where the output lives.** Spike output is a research brief under ``.ea/local/<YYYY-MM-DD>-<slug>.md`` (or the conventional ``.ea/local/research/`` sub-directory). Filenames follow the ``<date>-<slug>.md`` stem so the brief sorts chronologically and slug-matches against the wave or phase it informs. Briefs stay local-only — ``.ea/local/`` is gitignored — and are promoted to ``.ea/artifacts/`` only when they inform a decision that lives in ``state.json`` (artifact-chassis rule applies on promotion).

**How the verdict feeds the workflow.** The spike's verdict is the input to the next ``/roadmap propose --phase P<NN>`` or ``/prep`` claim. Reference the brief by repo-relative path in the roadmap proposal, the wave's plan body, or the dispatch prompt — the wave dispatch renderer surfaces spike briefs whose filename matches the wave / iter / phase id under a ``## References`` section so the subagent reads them before starting work.

**Spike outputs that ratify a verdict promote on commit.** A spike brief that informs a Decision row + ``set-verdict`` MUST promote from ``.ea/local/research/<date>-<slug>.md`` to ``.ea/artifacts/research/<date>-<slug>.md`` in the same commit that lands the Decision. The promotion runs the artifact-chassis validator + scrub gate. Spikes that do NOT inform a typed verdict stay local.
