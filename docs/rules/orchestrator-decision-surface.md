<!-- Generated from the eawf profile render block `orchestrator-decision-surface`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=orchestrator-decision-surface version=1.1 hash=b85bfb9d6dc40f78 -->
# `orchestrator-decision-surface`

Surface every consequential choice as an explicit question with visual option previews, never a silent default.

### Rationale

An orchestrating agent that picks silently spends the operator's budget on a decision the operator never saw, and a choice offered as prose alone is answered on vibes. The cost of a wrong pick is a wave of rework; the cost of asking is one round trip. Options compared side by side, with the trade-off drawn rather than described, are the difference between an informed answer and an agreeable one.


### Mechanism

When two paths would produce materially different work, stop and ask. File the choice with ``eawf question open-decision`` so it is a pending action with typed options, one per path, each carrying its consequence, its cost and a preview: an ASCII sketch of the before / after, the measured cost, and what is given up. Show the bound question the answer carries and stop: ``AskUserQuestion`` in Claude Code, the answer's numbered prompt printed verbatim in Codex, with the reply relayed through ``eawf question answer``. The operator may answer in the session or in the console, and the first answer wins. Expand every abbreviation, mark the option that is best for the long term as recommended, and state the cost of each option rather than only its benefit. Do not ask a question whose answer changes nothing — make the call, state the assumption, and continue. Free-text approval is not a decision record: the choice must be one of the offered options.


### Verification

A dispatched decision shows an option set with previews, not a paragraph of prose. Every abbreviation in the question is expanded on first use, one option is marked recommended, and each option names what it costs. A choice made silently that later needed rework is a rule violation, not bad luck.
<!-- END EAWF:managed id=orchestrator-decision-surface -->
