<!-- Generated from the eawf profile render block `commit-granularity`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=commit-granularity version=1.8 hash=ea9d533e5d3fc5eb -->
# `commit-granularity`

One commit per Batch, squashed before review opens; after review each fix lands as an appended Batch; each push ends with one ``state:`` commit; a squash rebinds by superseding rows.

### Rationale

Commit count is not progress. A deliverable split across a dozen commits — code here, its test there, the golden it invalidated three commits later — cannot be reviewed as a unit or reverted as one, and ``git bisect`` lands mid-change on a tree that never worked. The opposite failure, one commit for an entire Milestone, is equally unreadable. The unit that builds, passes its tests and is reviewed is the Batch. One past delivery made the cost concrete: 1,036 commits for 525 units of work, 385 of them state-only bookkeeping — a reviewer opening the PR read twice as much bookkeeping as delivery.


### Mechanism

Decision D92, which superseded D87, sets the regime. Scratch and per-Task commits are free while a Batch is in flight; before review opens, ``eawf batch squash`` writes the branch again as one commit per Batch holding its code and tests, ordered by each Batch's last commit, carrying one ``Batch:`` trailer and a ``Task:`` trailer per Task, and checks every rewritten commit builds. A large Batch may land as a few buildable commits; two Batches whose work cannot be separated share one commit, and the squash says so.

**No squash after review.** Once the Milestone PR is open for review, its commits are fixed. Fix a review finding with a new Batch appended on top, with its own commit and the push's trailing ``state:`` commit. After review opens, never squash, amend, rebase or force-push the branch. ``main`` then fast-forwards to the reviewed head, so each commit a reviewer read lands with the same SHA.

**One state commit per push.** State changes made while the Tasks ran ride a single trailing ``state:`` commit at the end of the push, touching only committed state paths. Never interleave state commits between the Batch commits of one push.

**A Batch binds when it lands.** A Batch closes as soon as every Task in it is ready and its commits are on the Milestone branch: the daemon binds the Milestone branch head and walks the Batch and its Tasks to COMPLETED. The squash replaces the commits those bindings name, so once the branch points at the squashed tip, ``eawf batch rebind`` appends a superseding row for every ledger line naming a replaced commit, bound to the commit now carrying its Batch; no row is edited. A branch the remote already has is rewritten only before review opens, only with ``--force-with-lease``, and only after an archive ref.

The one exception is a golden-only ``test:`` commit: every path it stages is a non-``.py`` file under a **managed golden surface** (the ``eawf snapshot list`` inventory). The snapshot-pairing gate requires those bytes to land under a ``test:`` subject, so a golden refresh rides its own paired ``test:`` commit immediately after the change that caused it — named for that cause, never as a bare "refresh goldens".


### Verification

Read the commit list of the Milestone PR: up to the start of review, each Batch appears as one commit, or a few, containing its tests and carrying its ``Batch:`` and ``Task:`` trailers, followed by one ``state:`` commit; after it, each review fix appears as an appended Batch commit followed by its push's ``state:`` commit, and no reviewed commit was rewritten. A ``test:`` golden refresh names the change it follows. Every commit in the range builds, so ``git bisect`` never lands on a broken intermediate, and every head a landed Batch bound is an ancestor of the pushed head.
<!-- END EAWF:managed id=commit-granularity -->
