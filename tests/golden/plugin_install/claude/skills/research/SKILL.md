---
name: research
description: Read-only investigation of an open question. Produces a research brief or surfaces findings inline; no code changes, no state mutations.
argument-hint: "<topic-slug> [--final]"
user-invocable: true
disable-model-invocation: true
---

# /research

## Canonical algorithm

1. Define the question. State the hypothesis or unknown in one sentence.
2. Survey: read source, run `git log`, fetch external refs as needed.
3. Compare alternatives — bullet list of options with pros/cons.
4. Verdict: recommend one path, or recommend "stay open" with the next
   discriminating experiment.
5. If `--final`: persist a research brief via the `/research` skill body.

## Pre-flight checklist

- [ ] No state mutations — read-only.
- [ ] Cite sources by `path:line` or external URL.
- [ ] Distinguish "what the code does" from "what the doc claims".

## Output contract

Eä-rendered skill envelope (`OutputEnvelope`) with `header.skill =
"/research"`. Body carries the structured findings; footer records any
persisted brief.
