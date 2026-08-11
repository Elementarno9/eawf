<!-- Generated from the eawf profile render block `engineering-practice`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=engineering-practice version=1.1 hash=7d613bb870f944a9 -->
# `engineering-practice`

Default to fail-fast at the boundary, one reason to change per unit, parsing separate from validation separate from execution, and explicit over implicit.

### Rationale

**Other engineering practice.** Code that fails far from its cause, mixes concerns, or signals success with ``None`` is expensive to debug and easy to break: the stack trace points at a symptom, a change to one concern risks the others, and the happy path reads ambiguously. Failing fast, separating concerns, and being explicit keep behaviour where the name and the call site say it is.


### Mechanism

Default to: fail-fast (raise at the boundary, not deep in a call stack); single-responsibility (each function or class has one reason to change); principle of least surprise (behaviour matches the name); separation of concerns (parsing ≠ validation ≠ execution); pure functions where viable (no hidden state); and explicit-over-implicit (named arguments over positional when arity ≥ 3, explicit returns over ``None``-as-success).


### Verification

A reviewer reads each public function's first statements (validation precedes side effects) and each call site of arity ≥ 3 (arguments passed by keyword). A function whose name implies a value but returns ``None`` on the happy path is reworked; ``uv run mypy src/`` backs the explicit-return contract via full type hints.
<!-- END EAWF:managed id=engineering-practice -->
