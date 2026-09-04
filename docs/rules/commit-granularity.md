<!-- Generated from the eawf profile render block `commit-granularity`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=commit-granularity version=1.1 hash=5b918f609f9fb472 -->
# `commit-granularity`

One commit per wave and per deliverable; wave-close bookkeeping rides the wave commit, and a golden refresh rides its cause.

### Rationale

Commit count is not progress. A deliverable split across a dozen commits — code here, its test there, the golden it invalidated three commits later — cannot be reviewed as a unit or reverted as one, and ``git bisect`` lands mid-change on a tree that never worked. The opposite failure, one commit for an entire phase, is equally unreadable. The unit that works is the deliverable. P30 made the cost concrete: 1,036 commits for 525 waves, 385 of them state-only bookkeeping and 245 of those a per-wave close record — a reviewer opening the phase PR read twice as much bookkeeping as delivery.


### Mechanism

Group a change and everything that follows from it into one commit: the code and its tests. Do not split one wave across several commits to show motion; each commit builds and passes its targeted tests on its own.

**Wave-close bookkeeping rides the wave commit.** After ``eawf wave close``, stage ``.ea/state.json`` plus the typed stores under ``.ea/store/`` onto the cherry-picked wave commit and fold them in with ``git commit --amend`` — never as a separate state commit. Leave ``Wave.commit`` unpinned: the amend rewrites the SHA, and the drift detector finds the amended commit by its subject / ``Eawf-Wave`` trailer. The bare ``[P<NN>] state:`` commit survives only where it names no single wave: a claim batch, an iter close, a phase close.

**One commit per wave.** A wave that already has a commit does not take a second one; fold the follow-up in with ``git commit --amend``, or — when it is genuinely new work — append a reactive wave and commit under that wave's own ``W<NN>`` id. ``tools/commit_prefix_lint.py`` enforces both clauses.

The one exception is a **managed golden surface** (anything under the ``eawf snapshot list`` inventory). The snapshot-pairing gate requires those bytes to land under a ``test:`` subject, so a golden refresh rides its own paired ``test:`` commit immediately after the change that caused it — named for that cause, never as a bare "refresh goldens". Unmanaged fixtures follow the general rule and ride their cause.


### Verification

Read the commit list for a delivery: each commit names one deliverable and contains its tests, and each wave appears once outside its paired golden refresh. No ``state:`` commit closes a single named wave. A ``test:`` golden refresh names the change it follows rather than saying only "refresh goldens". Every commit in the range builds, so ``git bisect`` never lands on a broken intermediate.
<!-- END EAWF:managed id=commit-granularity -->
