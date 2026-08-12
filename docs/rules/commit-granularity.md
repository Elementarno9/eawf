<!-- Generated from the eawf profile render block `commit-granularity`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=commit-granularity version=1.0 hash=5f72f48c4cecc2a4 -->
# `commit-granularity`

One commit per deliverable, with its tests; a golden refresh rides the change that caused it.

### Rationale

Commit count is not progress. A deliverable split across a dozen commits — code here, its test there, the golden it invalidated three commits later — cannot be reviewed as a unit or reverted as one, and ``git bisect`` lands mid-change on a tree that never worked. The opposite failure, one commit for an entire phase, is equally unreadable. The unit that works is the deliverable.


### Mechanism

Group a change and everything that follows from it into one commit: the code and its tests. Do not split one wave across several commits to show motion; each commit builds and passes its targeted tests on its own.

The one exception is a **managed golden surface** (anything under the ``eawf snapshot list`` inventory). The snapshot-pairing gate requires those bytes to land under a ``test:`` subject, so a golden refresh rides its own paired ``test:`` commit immediately after the change that caused it — named for that cause, never as a bare "refresh goldens". Unmanaged fixtures follow the general rule and ride their cause.


### Verification

Read the commit list for a delivery: each commit names one deliverable and contains its tests. A ``test:`` golden refresh names the change it follows rather than saying only "refresh goldens". Every commit in the range builds, so ``git bisect`` never lands on a broken intermediate.
<!-- END EAWF:managed id=commit-granularity -->
