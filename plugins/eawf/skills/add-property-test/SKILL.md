---
name: add-property-test
description: "Model-only playbook for adding a property-based test that pins a function's invariant."
argument-hint: ""
user-invocable: false
disable-model-invocation: false
---

# add-property-test

A model-only playbook for adding a property-based test. There is no slash command; the model invokes this when example-based tests leave a function's invariants under-specified.

## When to reach for it

- The function has an algebraic law (round-trip, idempotence, commutativity, monotonicity) that examples only sample.
- The input space is large and edge cases keep slipping through hand- written cases.
- A parser/serialiser pair should satisfy `decode(encode(x)) == x`.

## Canonical procedure

1. State the invariant in one sentence ("encoding then decoding returns the input").
2. Pick the narrowest strategy that generates valid inputs; constrain it so generated values are in-domain rather than filtering after the fact.
3. Assert the law inside the test body; let the framework shrink failures to a minimal counter-example.
4. Keep one or two example-based tests alongside for the named boundary cases (empty, single, max-length) the contract calls out.
5. When a property fails, treat the shrunk counter-example as a new regression fixture before fixing the code.

## Guardrails

- Property tests complement, never replace, the boundary-case AND error-path tests every public function owes (test-discipline rule).
- Use `pytest.approx` for float laws and `numpy.testing.assert_allclose` for array laws.
- Name the test `test_<func>_<invariant>` so the law is legible from the test report.
