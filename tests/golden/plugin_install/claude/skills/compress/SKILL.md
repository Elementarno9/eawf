---
name: compress
description: Compress the session conversation when context approaches the limit.
argument-hint: "[--tokens-before=<n>] [--tokens-after=<n>] [--runtime=<id>]"
user-invocable: true
disable-model-invocation: false
---

# /compress

## Canonical algorithm

1. Read `tokens_before` (required; missing/zero degrades to
   `status=needs_user`) and `tokens_after` (defaults to a no-op when
   omitted; clamped so a pass can only shrink the context).
2. Build the per-runtime compression directive (cache-control wiring) for
   the target `runtime` (defaults to `claude-code`, the only runtime with
   a caller-side cache-control marker). An unknown runtime degrades to
   `status=needs_user`.
3. Append the canonical `compression_emitted` event carrying the token
   deltas and the realised ratio so the telemetry projector can chart
   context pressure over a session.

The model summarisation fan-out lives behind the runtime adapter's
cache-control hook; the skill records the requested compression.

## Pre-flight checklist

- [ ] `tokens_before` is present and > 0.
- [ ] The target runtime is a known runtime id.

## Output contract

Skill envelope with `header.skill = "/compress"`. Body carries
tokens_before, tokens_after, ratio, runtime, and cache_control_applied.
