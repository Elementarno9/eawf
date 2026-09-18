"""Planning: the PlanRevision machine and the apply that materialises it.

The package is split along the line between deciding and writing.

:mod:`~eawf.workflow.planning.revision` decides. It holds the
``DRAFT -> VALIDATED -> APPROVED -> APPLIED`` machine, the rule that binds
an approval receipt to the digest of the content it approved, the guard
that keeps a cited Campaign finding out of every Milestone count, and the
four drift predicates an apply is checked against. Nothing in it touches
a file, so the same rules answer a unit test and a live transaction.

:mod:`~eawf.workflow.planning.apply` writes. It takes the locks the plan's
own URNs name, re-reads the document under them, asks the decider, and --
only when every answer is yes -- builds the whole successor document and
hands it to one atomic replace. The Milestone, its Batches and its PLANNED
Tasks land in the same bytes, so a partial apply is not a state the tree
can be left in.
"""

from __future__ import annotations

__all__: list[str] = []
