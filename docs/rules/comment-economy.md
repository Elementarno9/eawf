<!-- Generated from the eawf profile render block `comment-economy`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=comment-economy version=1.1 hash=7e59db177a3e9d92 -->
# `comment-economy`

Comments carry why, not what: document every parameter, return and raise, but no restated signatures, change-log narration, or lifecycle ids.

### Rationale

A comment that restates the code is not neutral: it is a second copy that drifts, and every reader pays to reconcile the two. Narration of how the code came to be ("after W12 the default flipped", "the pre-fix value was...") ages into a change log nobody trusts, while the one thing a reader cannot recover from the source — why this shape was chosen over the obvious alternative — is what usually goes unwritten. A docstring is different in kind from a comment: it is the reference a caller reads instead of the body, so pruning its entries to save bytes costs the reader the API itself.


### Mechanism

Write the why, not the what — in prose. A docstring opens with one or two sentences: what the callable is for, and the non-obvious constraint or choice behind it. If the description runs past a short paragraph, cut it.

Keep the API surface documented. A caller reads the docstring instead of the body, so it carries a Google-style ``Args:`` entry for every parameter, a ``Returns:`` stating what the value means, and a ``Raises:`` block. Each entry earns its place by adding what the type alone cannot carry — unit, range, ownership, failure mode, or what a flag actually switches. Restating the type in words is the failure mode to avoid, not the presence of the entry itself.

Inline comments are the scarce resource: one per non-obvious decision, none for what the line already says, and a named constant instead of a comment explaining a literal.

Do not narrate history: no wave, iter, or phase ids, no "previously this did X", no audit or decision references (rule 25 already bars provenance from source; this extends it to bare lifecycle ids).


### Verification

Read the docstring as a caller who cannot see the body: every parameter, the return value, and each raised exception is accounted for, and each entry adds a unit, range, or meaning the signature does not already carry. Cut any prose sentence that merely restates the signature, and any inline comment narrating the line below it. Grep the diff for lifecycle ids (``P<NN>`` / ``I<NN>`` / ``W<NN>``) inside comments and docstrings; a hit is reworked into a plain statement of the constraint, or dropped.
<!-- END EAWF:managed id=comment-economy -->
