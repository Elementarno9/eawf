# Plan: P05-I01 — Branch and merge

> Status: active · Phase: P05 (Phase Five) · Opened: 2026-05-08T00:00:00Z

## Summary
- waves: 4 (2 pending, 1 claimed, 1 closed)
- checks: 2/3 passed
- risks: 0 open
- blocked: P05-I01-W03

## DAG
```mermaid
flowchart LR
  P05_I01_W00["P05-I01-W00 closed"]:::status_closed
  P05_I01_W01["P05-I01-W01 claimed"]:::status_claimed
  P05_I01_W02["P05-I01-W02 pending"]:::status_pending
  P05_I01_W03["P05-I01-W03 pending"]:::status_pending
  P05_I01_W00 --> P05_I01_W01
  P05_I01_W00 --> P05_I01_W02
  P05_I01_W01 --> P05_I01_W03
  P05_I01_W02 --> P05_I01_W03
  classDef status_pending fill:#999,stroke:#444;
  classDef status_claimed fill:#88a,stroke:#444;
  classDef status_in_progress fill:#8a8,stroke:#444;
  classDef status_closed fill:#0a0,stroke:#040;
  classDef status_failed fill:#a00,stroke:#400;
  classDef status_abandoned fill:#666,stroke:#222;
```

## Waves
- [x] **P05-I01-W00** — Init (closed @ 1111111)
- [ ] **P05-I01-W01** — Branch A (claimed by S-A)
- [ ] **P05-I01-W02** — Branch B (pending; deps: W00)
- [ ] **P05-I01-W03** — Merge (pending; deps: W01, W02)

## Checks
- [x] **ruff_clean** (P05-I01-W01 audit AU-W01) — passed
- [ ] **tests_green** (P05-I01-W01 audit AU-W01) — failed: two failures
- [x] **ok** (P05-I01-W00 outcome) — passed

## Risks
(none)
