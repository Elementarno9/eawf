<!-- Generated from the eawf profile render block `code-craft-dry`. Do not hand-edit: re-run `eawf sync`. -->

# `code-craft-dry`

Extract shared logic into one named home only once a third use site appears; until then tolerate the duplication.

### Rationale

Repeated logic drifts: a fix lands in one copy and the others rot. DRY (don't repeat yourself) keeps one canonical home per behaviour so a change has one place to land. The balance is KISS — three similar lines are cheaper to read than a half-fitted helper, so do not abstract until a third caller actually appears.


### Mechanism

When a third use site of the same logic appears, extract it into one named function or constant and route every caller through it. Until then, tolerate the duplication. Never introduce a parameterised helper for two call sites or for a use site that does not yet exist (YAGNI).


### Verification

grep for the literal or near-literal logic across the changed module; a reviewer confirms either a single shared definition or fewer than three copies. A new helper with only one caller is a YAGNI violation and the reviewer rejects it.
