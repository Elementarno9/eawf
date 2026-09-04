# Plan: P05-I01 — Iter One

> Status: active · Phase: P05 (Phase Five) · Opened: 2026-05-08T00:00:00Z

## Summary
- waves: 2 (1 in_progress, 1 closed)
- effort: sum_wave_eu=0, critical_path_eu=0, actual_elapsed_eu=0
- checks: 1/1 passed
- risks: 0 open
- blocked: none

## DAG
```mermaid
flowchart LR
  P05_I01_W00["P05-I01-W00 closed"]:::status_closed
  P05_I01_W01["P05-I01-W01 in_progress"]:::status_in_progress
  P05_I01_W00 --> P05_I01_W01
  classDef status_pending fill:#999,stroke:#444;
  classDef status_claimed fill:#88a,stroke:#444;
  classDef status_in_progress fill:#8a8,stroke:#444;
  classDef status_closed fill:#0a0,stroke:#040;
  classDef status_failed fill:#a00,stroke:#400;
  classDef status_abandoned fill:#666,stroke:#222;
```

## Waves
| Wave | Status | Bucket | Role | Estimate EU | Success criteria | Files |
| --- | --- | --- | --- | ---: | --- | --- |
| [x] **P05-I01-W00** Bootstrap | closed | - | - | 0 | - | src/core/__init__.py |
| [ ] **P05-I01-W01** Implement core | in_progress; deps: W00 | - | - | 0 | - | src/core/run.py |

## Checks
- [x] **ok** (P05-I01-W00 outcome) — passed

## Risks
(none)
