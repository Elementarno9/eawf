# VIS-1 mockup-image-diff gate spec

The `mockup_golden_diff` image mode rasterizes a reference MOCKUP frame and a screenshot of the LIVE TUI as PNG images and scores their divergence with a rubric that weights LAYOUT SHAPE above token fidelity. It is the falsifier that catches the failure mode the ASCII-text mode is blind to: a screen that diffs clean as text yet renders square where the mockup asked for round, or one column where the mockup asked for two.

## What the rubric weights

The divergence score is dominated by two layout-shape features; a single mismatch in either fails the gate on its own, while per-glyph (token) pixel noise is averaged at a much smaller weight and cannot, by itself, fail.

- **Border-corner shape** — round vs square. A round border clips the arc inward and leaves the extreme corner cell empty; a square border runs straight into the corner and fills it. The four-corner fill signature is the dominant signal.
- **Body column count** — one vs two. The body's vertical ink profile is scanned for an empty interior gutter splitting two content bands; one contiguous run is a single column, an interior gutter is two.

## CSS-to-Textual mapping table (pinned)

A mockup authored in CSS is realised on the Textual surface through this fixed vocabulary. An "un-renderable" claim — a CSS property the candidate screen does not realise — MUST cite the row that governs it; the table is the canonical reference so the gate spec and a reviewer share one source of truth.

| CSS source | Textual realisation |
| --- | --- |
| `border-radius: <r>` | `border: round` (round corners; a square `border: solid` is the divergence this gate catches) |
| `border: 1px solid` | `border: solid` (square corners) |
| `display: flex; flex-direction: row` (2 cols) | `Grid` / `Horizontal` with two child containers (a single child is the one-vs-two-column divergence) |
| `display: flex; flex-direction: column` | `Vertical` container |
| `display: grid; grid-template-columns` | `Grid` with `grid-size-columns` |
| `padding` | `padding: <cells>` |
| `background-color` | `background: <color>` |
| `color` | `color: <color>` |
| `font-weight: bold` | `text-style: bold` |

The same table is pinned in code at `eawf.workflow.audit_dsl.kinds.mockup_image_diff.CSS_TO_TEXTUAL_MAPPING` so the spec prose and the runtime constant cannot drift.

## Committed reference frames

The reference mockup frame and the TUI-render frames are committed as pre-rendered PNGs in this directory (`tests/fixtures/mockup_image_diff/`). They are CI-readable, unlike the gitignored brand corpus, so CI runs the real comparison. The frames are small schematic cards rendered deterministically via resvg.

- `mockup_round_1col.png` — the reference mockup (round border, one column).
- `tui_round_1col_faithful.png` — a faithful TUI render; the gate PASSES.
- `tui_square_1col_divergent.png` — square border where the mockup is round; the gate FAILS on border shape.
- `tui_round_2col_divergent.png` — two columns where the mockup is one; the gate FAILS on column count.
