---
name: roadmap
description: Surface open hypotheses, audits, and decisions; cluster by phase/wave and recommend next moves.
argument-hint: ""
user-invocable: true
disable-model-invocation: true
---

# /roadmap

## Canonical algorithm

1. Read open hypotheses, audits, and decisions from `state.json`.
2. Cluster by phase / wave; surface stale items.
3. Recommend a next move per cluster.

## Pre-flight checklist

- [ ] Read-only — no state mutations.

## Output contract

Skill envelope with a structured roadmap body (clusters, stale items,
next-move recommendations).
