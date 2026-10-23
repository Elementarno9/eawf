<!-- Generated from the eawf profile render block `ship-process`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=ship-process version=1.9 hash=098163d330c02405 -->
# `ship-process`

Ship a Milestone through one PR: pass CI, address review by appending Batches, then merge so each Batch commit survives; main and tags are never rewritten.

### Ship process

Ship is the Milestone's terminal pass, under decision D87, which superseded D85. The ordered steps from landed Batches to a merged, tagged Milestone:

1. **Open the Milestone PR.** One PR per Milestone, body per the ``pr-template``. The PR head is the long-running feature branch, squashed before each push to one commit per Batch (a few for a large Batch) carrying its ``Batch:`` and ``Task:`` trailers, plus one trailing ``state:`` commit per push. Each Batch already closed when it landed: the daemon bound the Milestone branch head and walked the Batch and its Tasks to COMPLETED.
2. **Pass CI gates.** Beyond the per-commit lint + test gauntlet, two gates fire only on the Milestone PR and so are easy to miss locally: the per-package **coverage gate** (CI parses ``coverage.xml`` against the ``[tool.eawf.coverage]`` thresholds) and the **snapshot-pairing gate** (``tools/snapshot_pairing_gate.py`` over the PR base..head range, which rejects a managed golden-surface mutation whose commit names no Batch or Task of the generation). Run both locally before pushing the PR so they do not surface late.
3. **Pass the review.** ``/verify --mode review`` runs the review pass over each Batch. Address feedback by appending Batches to the Milestone, not by opening a second PR.
4. **Rewrite only under lease.** ``eawf batch squash`` writes the per-Batch history on a new branch and proves its tree equals the source tip's; once the branch points at it, ``eawf batch rebind`` appends a superseding row for every binding that named a source commit. A pushed branch is rewritten only with ``--force-with-lease``, after writing an archive ref of the old head. Ledgers stay append-only: a correction is an appended superseding row.
5. **Merge.** Once CI is green on the pushed head and review passes, merge so each Batch commit survives: fast-forward ``main`` to the reviewed head, never folding Batches together. main and tags are never rewritten, and old history is not rewritten.
6. **Release at merge.** The Milestone is release-ready at merge: the release verbs check it before anything is tagged (see ``release-process``), and nothing after the merge is Milestone work. A repo that releases through an eawf release train then runs ``eawf release pipeline <version> --milestone <MLS-NNNN> --publish`` from a clean checkout of the merge commit: one journaled, resumable walk that tags and pushes only with ``--publish``, waits for the publish runs, pins, proves, approves and observes the checkpoint to BAKED, advances the train, checks each completed Batch's commits on ``main`` by their ``Task:`` and ``Batch:`` trailers, and stages the evidence for one ``state:`` commit. A refused step names its reason and a rerun resumes there, so the next Milestone carries no release Tasks; every other repo keeps its own tag flow. Nothing publishes until the tag is pushed.
<!-- END EAWF:managed id=ship-process -->
