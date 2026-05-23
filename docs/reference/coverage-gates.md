# Coverage gates

Per-package coverage gates for the eawf load-bearing package set, plus an overall floor. Implements the C09 §5.2 nine-layer grouping (decision F-008 / G-19, Q16 "9-layer", impl deferred to C09-IMPL W02).

## How it works

pytest-cov has no native per-package threshold, so coverage enforcement is split across two CI steps in `.github/workflows/ci.yaml`:

- **Per-package ratchet** — after both pytest steps run (parallel suite + serial TUI render/timing, combined via `--cov-append` into one `coverage.xml`), an inline checker parses the Cobertura XML, aggregates line + branch coverage per gated package by filename prefix (or glob), and exits non-zero if any gated package falls below its threshold. Thresholds live in `[tool.eawf.coverage.gates]` in `pyproject.toml`.
- **Overall floor** — `uv run coverage report` enforces `[tool.coverage.report] fail_under = 60`.

Gates are a **ratchet**, not an aspirational target: each threshold is set just below the coverage measured on the current tree (with a ~1-point safety margin for cross-OS / test-selection variance), so the gate catches regressions without redding CI today. Bumping a threshold upward after a coverage improvement tightens the ratchet.

## Gate-set and measured headroom

Measured on 2026-05-22 (full suite, 5139 passed). Headroom = measured − gate.

| Package | Path | Measured line | Line gate | Headroom | Measured branch | Branch gate | Headroom |
|---|---|---:|---:|---:|---:|---:|---:|
| daemon | `daemon/` | 78.36% | 77 | +1.4 | 65.66% | 64 | +1.7 |
| telemetry | `telemetry/` | 94.01% | 93 | +1.0 | 87.91% | 86 | +1.9 |
| skills | `skills/` | 94.89% | 93 | +1.9 | 82.62% | 81 | +1.6 |
| cli | `cli/` | 76.57% | 75 | +1.6 | 66.48% | 65 | +1.5 |
| tui | `tui/` | 91.60% | waived | n/a | 78.66% | waived | n/a |
| render | `render/` | 90.56% | 89 | +1.6 | 75.61% | 74 | +1.6 |
| store | `store/` | 98.81% | 97 | +1.8 | 85.71% | 84 | +1.7 |
| state | `state/` | 99.79% | 98 | +1.8 | 96.74% | 95 | +1.7 |
| lock | `lock/` | 87.14% | 86 | +1.1 | 81.25% | 80 | +1.3 |
| plugin_install | `runtimes/*/plugin_install.py` | 96.08% | 95 | +1.1 | 89.63% | 88 | +1.6 |
| **Overall floor** | `src/eawf/` | 86% | 60 | +26 | — | — | — |

## Rationale (F-008)

**Why per-package, not per-file.** Per-file gates churn on refactors — renaming a `_helper` module trips the gate for no functional reason. Per-package gates align with the architectural boundary the project already maintains (the nine load-bearing layers).

**Why these packages.** The gated set is the load-bearing surface where a coverage regression carries real risk: the daemon (sole canonical mutator), the state writer + lock (crash-safety surface), the skill engine (AGENTS rule 19), telemetry projection, CLI verbs, render/envelope/store (serialization), and the plugin-install path across all three runtimes. Non-load-bearing helper packages are covered by the overall floor only.

**Why these thresholds.** Each threshold is the coverage measured on 2026-05-22 rounded down by ~1 point. This makes the gate a regression catcher (it fires the moment a package drops below where it is today) rather than an aspirational target that would red CI on the current tree. The spec's higher aspirational targets (e.g. daemon ≥85% line) become the direction the ratchet is tightened toward in later waves, not a gate that fails today.

**Why the overall floor stays low (60).** The TUI line-cov gate is waived because Textual widgets render asynchronously and line-cov misreports them, which drops the achievable overall by ~15 points. The per-package ratchets already enforce the layers where coverage matters; the overall floor only catches large-scale dead-code accumulation. Current overall sits at ~86%, leaving large headroom over the floor.

## Waivers

- **`tui/` line + branch — waived.** Textual widgets render asynchronously; line/branch coverage misreports them. Screen coverage is enforced by the snapshot pairing gate (C09 §5.2), not by this ratchet.

## Adding or tightening a gate

1. Run the suite with coverage: `uv run pytest -n auto --ignore=tests/snapshots/tui --ignore=tests/perf/tui --cov=eawf --cov-report=xml --cov-fail-under=0` then `uv run pytest -n0 tests/snapshots/tui tests/perf/tui --cov=eawf --cov-append --cov-report=xml --cov-fail-under=0`.
2. Read the per-package numbers (the CI `Coverage gate` step prints them; or aggregate `coverage.xml` locally).
3. Edit `[tool.eawf.coverage.gates.<name>]` in `pyproject.toml`. New gate: add a table with `path` (filename prefix relative to `src/eawf`) or `glob`, plus `line` / `branch`. To waive a dimension, set `waive_line` / `waive_branch`.
4. Keep new thresholds at or just below measured coverage to preserve the ratchet contract.
