<!-- Generated from the eawf profile render block `code-craft-explicit-over-implicit`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=code-craft-explicit-over-implicit version=1.1 hash=123f2bd2c6ad3917 -->
# `code-craft-explicit-over-implicit`

Pass arguments by keyword at arity three or more, return explicit values rather than ``None``-as-success, and keep behaviour matching the name.

### Rationale

Implicit behaviour surprises the next reader: positional arguments at high arity transpose silently, and ``None``-as-success hides the real outcome. Explicit code matches the principle of least surprise — the behaviour reads off the call site without chasing the definition.


### Mechanism

Use named arguments over positional when a function takes three or more parameters. Return explicit values rather than ``None``-as-success. Prefer pure functions with no hidden state. Keep behaviour matching the name so a reader trusts the signature.


### Verification

Read each call site of a function with arity of three or more: arguments are passed by keyword. A function whose name implies a value but returns ``None`` on the happy path is reworked. mypy (``uv run mypy src/``) backs the explicit-return contract via full type hints.
<!-- END EAWF:managed id=code-craft-explicit-over-implicit -->
