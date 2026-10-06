# Epoch-1 snapshot

A stripped copy of this repository's frozen v1 `.ea/state.json` and of the epoch-1 stores under `.ea/store/`, kept for the tests that pin their content after both left git. The layout mirrors `.ea/`: `state.json` beside a `store/` directory, so a reader that resolves stores from the document path finds them.

The document keeps only the waves, iters, phases, decisions, incidents and actuals those tests read, and validates as the v1 `State` model. Waves outside that set keep their identity, status and role with empty criteria and gates; free text is cut to 120 characters, except the one wave description a test reads in full. Each store keeps only the rows a test reads. Git history holds the full originals.
