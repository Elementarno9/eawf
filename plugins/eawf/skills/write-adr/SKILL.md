---
name: write-adr
description: "Model-only playbook for drafting an architecture decision record companion to a typed decision row."
argument-hint: ""
user-invocable: false
disable-model-invocation: false
---

# write-adr

A model-only playbook for drafting an architecture decision record. There is no slash command; the model invokes this when a design choice needs a durable, reviewable rationale. In an Eä-managed repo the decision itself is a typed row in `state.json` — the ADR markdown is the human-readable companion, never the source of truth.

## When to reach for it

- Two or more designs were weighed and one was picked.
- The choice constrains future work (a dependency, a schema shape, a protocol boundary) and a later reader will ask "why this?".
- An audit or spike produced a verdict that should outlive the session.

## Canonical structure

1. **Context** — the forces in tension, in two or three sentences. State the constraint, not the solution.
2. **Options** — each candidate as a bullet with its concrete trade-off. Name the option the way the codebase will refer to it.
3. **Decision** — the chosen option, stated as a present-tense assertion.
4. **Consequences** — what becomes easy, what becomes hard, what is now forbidden.

## Guardrails

- Reference the audit or spike that justifies the decision so the evidence chain is reconstructible (research-workflow rule).
- Keep prose scrub-clean: no machine paths, hostnames, or PII; references stay repo-relative, an external URL, or an Eä URN.
- The ADR records WHY; provenance ids from audits and review discussions live in the decision row and the commit body, not in source comments (rule 25).
