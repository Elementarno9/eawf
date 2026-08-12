<!-- Generated from the eawf profile render block `comment-economy`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=comment-economy version=1.0 hash=649a1d1455cfe27d -->
# `comment-economy`

Comments carry why, not what: no restated signature, no change-log narration, no lifecycle ids.

### Rationale

A comment that restates the code is not neutral: it is a second copy that drifts, and every reader pays to reconcile the two. Narration of how the code came to be ("after W12 the default flipped", "the pre-fix value was...") ages into a change log nobody trusts, while the one thing a reader cannot recover from the source — why this shape was chosen over the obvious alternative — is what usually goes unwritten.


### Mechanism

Write the why, not the what. A docstring states the contract and the non-obvious constraint behind it; if deleting a sentence loses nothing a reader could not get from the signature, delete it. Do not narrate history: no wave, iter, or phase ids, no "previously this did X", no audit or decision references (rule 25 already bars provenance from source; this extends it to bare lifecycle ids). Keep a Google-style ``Raises:`` block, keep an ``Args:`` entry that carries a constraint the type does not, and drop the rest. Prefer one dense sentence to a five-line paragraph, and a named constant to a comment explaining a literal.


### Verification

Read the docstring against the signature: any sentence recoverable from the name, the types, or the arguments alone is cut before merge. Grep the diff for lifecycle ids (``P<NN>`` / ``I<NN>`` / ``W<NN>``) inside comments and docstrings; a hit is reworked into a plain statement of the constraint, or dropped.
<!-- END EAWF:managed id=comment-economy -->
