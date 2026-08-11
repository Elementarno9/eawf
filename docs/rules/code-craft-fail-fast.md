<!-- Generated from the eawf profile render block `code-craft-fail-fast`. Do not hand-edit: re-run `eawf sync`. -->

# `code-craft-fail-fast`

Validate inputs at the boundary and raise there, so downstream functions accept already-validated typed objects and never re-check.

### Rationale

An error raised deep in a call stack surfaces far from its cause, so the stack trace points at the symptom rather than the bad input. Fail-fast raises at the boundary where invalid data enters, keeping the trace short and the blame obvious.


### Mechanism

Validate inputs at the boundary (loader, CLI parse, public function entry) and raise immediately on bad data. Downstream functions accept already-validated typed objects and never re-check. Error messages start lowercase, carry no trailing period, and interpolate user input with ``!r`` so the offending value is quoted.


### Verification

Read the function's first statements: argument validation precedes any side effect. Each public function has an error-path test asserting the exception type (``TypeError`` / ``ValueError`` / ``KeyError`` / ``ValidationError``) and, when the message is part of the contract, the message substring via ``pytest.raises``.
