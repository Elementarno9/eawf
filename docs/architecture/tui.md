# TUI surface architecture

## Summary

The Eä Rich TUI is a read-only operator surface assembled out of typed `rich.live` + `rich.layout` views that share a single brand/breadcrumb chassis. Phase P20 landed five view kinds plus a tabbed config modal: the repo-scope quadrant (W02), the wave-board list+detail (W03), five detail overlays (W04), the workspace-scope strip+quadrant dashboard (W05), the user-scope portfolio table (W06), the audit-running overlay (W07), and the metadata-driven config modal (W11). Every surface reuses `eawf.tui.layout` for the `Eä` brand (outside-left of the scope breadcrumb per `feedback_tui_branding`) and the footer keymap line; per-surface footers extend the base footer rather than replacing it [1][2][3][4][5][6][7][8].

The framework is intentionally **rich** rather than **textual** (D15) — already pinned, no new runtime dep, weaker keyboard focus accepted in exchange for footprint. Production callers spin `rich.live.Live` over the surface's `Layout`; tests run the same surfaces through `offline_render` which captures the rendered frame into an in-process `io.StringIO` and asserts against a golden text fixture under `tests/golden/tui/` so a reviewer can reproduce the exact frame locally without spawning a TTY [9].

Live-loop key handling on `rich` is bare-metal: raw-mode reads from a TTY file descriptor, keys decoded against the surface-local keymap, view-state mutated through a pure `apply_key` reducer, then a single frame redrawn through `Live.update`. The reducer pattern lets tests drive each surface deterministically without a Live loop [10].

This document is a reference, not a tutorial. Each section names the producing wave, the entry-point function(s), the layout sketch, the keymap, and the golden fixture(s) reviewers should consult to reproduce the surface. Screenshots and asciinema casts are deliberately not committed — they leak machine paths and hostnames per the `secrets-hygiene` policy. The plain-text goldens are the canonical reference instead [11].

## Repo-scope quadrant (W02)

`eawf.tui.app.run_tui` (entry) + `eawf.tui.layout.build_quadrant` (composition). The view is a 2×2 quadrant covering the four panes named in `QUADRANT_PANE_NAMES`: `roadmap` (top-left), `status` (top-right), `git` (bottom-left), `backlog` (bottom-right). Builder is rejected with the wrong pane count so the layout cannot silently drift [1].

Frame:

```
+----------------------------------------------------------+
| Eä  EAWF / P20 / P20-I01                                 |  <- header
+--------------------------+-------------------------------+
| roadmap                  | status                        |
| phases (active):  1      | project: EAWF                 |
| iters  (active):  1      | phase:   P20                  |
| waves  (in-prog): 2      | iter:    P20-I01              |
+--------------------------+-------------------------------+
| git                      | backlog                       |
| branch: feature/...      | open:   2                     |
| head:   abc1234          | closed: 1                     |
| status: clean            | total:  3                     |
+--------------------------+-------------------------------+
| keymap: ...                                              |  <- footer
+----------------------------------------------------------+
```

Tick modes (W02 success criterion 4):

* **offline** — single-shot render of the four panes plus header/footer; suitable for headless `--plain` invocations and CI snapshot tests. The same code path that production uses; only the Live loop is omitted. Captured frame in `tests/golden/tui/expected.txt` [9].
* **online** — `rich.live.Live` ticking at `DEFAULT_REFRESH_HZ` Hz; blocks on raw-mode keypresses (`Esc` / `q` / `Ctrl-C` quit; `b` opens the wave-board view). Falls back to offline on non-TTY hosts so CI invocations never deadlock [10].

Keymap (`FOOTER_KEYMAP` in `eawf.tui.layout`):

```
↑↓←→ navigate  PageUp/PageDown page  Home/End jump  Enter  b board  Esc/q  (vim: h j k l g G)
```

Arrows lead, vim aliases trail; full key names spelled out per `feedback_tui_keymap_conventions` [12].

## Wave-board (W03)

`eawf.tui.wave_board.run_wave_board` (entry) + `eawf.tui.wave_board.offline_render` (test-mode render). Full-screen list-on-top, detail-on-bottom split over the current iter's wave plan. The list is the primary navigation surface; the detail pane drills into the currently selected wave [2].

Frame:

```
+----------------------------------------------------------+
| Eä  EAWF / P20 / P20-I01                                 |  <- header
+----------------------------------------------------------+
| waves (filter=all, 5 of 5)                               |
| > P20-I01-W02  in_progress  feat: quadrant TUI           |  <- list
|   P20-I01-W03  pending      feat: wave board             |
|   P20-I01-W01  closed       feat: roadmap table          |
+----------------------------------------------------------+
| wave P20-I01-W02                                         |
|   status:      in_progress                               |
|   deps:        P20-I01-W01                               |  <- detail
|   blocked_by:  -                                         |
|   tests:       -                                         |
|   budget:      4000 / 8000 (50%)                         |
|   criteria:                                              |
|     - list view sorted by status priority then wave_id   |
+----------------------------------------------------------+
| ↑↓ select  Enter open  f filter  Esc back  (vim: j k g G)|  <- footer
+----------------------------------------------------------+
```

Sort order (success criterion 1, `STATUS_PRIORITY` constant): `in_progress > claimed > pending > failed > closed > abandoned`. Within each bucket waves sort by `wave_id` ascending so the display stays stable across reloads [2].

Filter cycle (success criterion 3, `FILTER_MODES` constant; `f` advances): `all → pending → claimed_in_progress → closed → failed → all`. The current mode shows in the list-header line and in the footer hint. `next_filter_mode` is the pure reducer the tests assert against [2].

Detail view (success criterion 2) reads DAG edges through `eawf.state.wave_graph.edges_for_iter` (the typed accessor landed in W15) so the wave-board never inlines a walk over `Wave.blocks`. Tests / budget / criteria come straight off the typed `Wave` record [13].

Hotkey to enter the board from the quadrant: `b`. Hotkey to exit: `Esc`. Goldens:

* `tests/golden/tui/wave_board_default.txt` — default view, no filter, first wave selected.
* `tests/golden/tui/wave_board_failed_selected.txt` — focus on a failed wave (status priority demo).
* `tests/golden/tui/wave_board_filter_closed.txt` — `f` cycled to `closed`.
* `tests/golden/tui/wave_board_filter_pending.txt` — `f` cycled to `pending`.

## Detail overlays (W04)

`eawf.tui.overlays.open_overlay` (single dispatch entry) builds five overlay kinds backed by typed state-model records: hypothesis, decision, memory, events, dispatch. Each overlay reuses the shared header chassis (`build_brand_text` + `build_breadcrumb`) so the brand strip stays byte-identical across W02 / W03 / W04 / W05 / W06 [3].

Verb-prefixed keymap (`OVERLAY_KEYMAP` in `eawf.tui.overlays`):

| Key | Overlay | Builder |
|---|---|---|
| `oH` | hypothesis | `build_hypothesis_overlay` |
| `oD` | decision | `build_decision_overlay` |
| `oM` | memory | `build_memory_overlay` |
| `oE` | events | `build_events_overlay` |
| `oR` | dispatch (render) | `build_dispatch_overlay` |

The `o<X>` two-key sequence reads as "**o**pen **X**" so the operator can chain shortcuts without colliding with the single-letter `b` / `f` / `q` / `c` keys used by the underlying views [3].

The overlays are read-only `rich.panel.Panel`s composed into a one-frame `rich.layout.Layout`. They do not spin their own `Live` — the caller (wave-board, quadrant) composes the overlay into its tick. Goldens cover the default branch of each overlay (`tests/golden/tui/overlay_*_default.txt`) and share a single fixture state (`tests/golden/tui/overlay_state.json`) so a reviewer can reproduce each surface with a one-liner test invocation [3].

`OverlayKind` is a `Literal` over the five names; `KNOWN_OVERLAY_KINDS` is the tuple form for parametrised tests. `open_overlay` rejects an unknown kind with `ValueError`; resolution helpers (`_resolve_hypothesis`, `_resolve_decision`, `_resolve_memory`, `_resolve_wave`) raise `KeyError` when the target id is missing so the surface fails closed [3].

## Workspace-scope dashboard (W05)

`eawf.tui.workspace.run_workspace` (entry) + `eawf.tui.workspace.offline_render` (test-mode). A horizontally-striped top strip enumerates every repo registered in the user-scope registry; the W02 repo-scope quadrant renders for the *active* repo below it. The dashboard is strictly read-only — per `feedback_explicit_registry_only` it never grows the registry [4].

Frame:

```
+----------------------------------------------------------+
| Eä  EAWF / P20 / P20-I01                                 |  <- header
+----------------------------------------------------------+
| < [EAWF] (active)   DEMO (stale)   OTHER >               |  <- strip
+-------------------------------+--------------------------+
| roadmap                       | status                   |
+-------------------------------+--------------------------+   <- W02
| git                           | backlog                  |     quadrant
+-------------------------------+--------------------------+      (active repo)
| ↑↓ select repo  Enter focus  Esc back  ...               |  <- footer
+----------------------------------------------------------+
```

Stale chips (success criterion 3) fire on three signals from `eawf.registry.is_stale`: registry mtime older than `STALE_AFTER`, repo state mtime older than the same threshold, or repo state unreadable. Stale entries carry an inline `(stale)` chip in `STALE_CHIP_STYLE` (dim) so the operator can spot them without drilling in [4].

Keymap (`WORKSPACE_FOOTER_KEYMAP`): `←/→ strip  Enter focus  Esc back  b board  q quit  (vim: h l)`. Goldens cover the populated, empty, and stale-chip branches (`workspace_default.txt`, `workspace_empty.txt`, `workspace_stale.txt`) [4].

The workspace dashboard reuses `build_quadrant` + `repo_quadrant_panes` from `eawf.tui.layout` so it cannot drift from the W02 quadrant on layout or pane order. Registry reads go through `eawf.registry.read_registry` (the W05 helper) which returns a typed `Registry` model or raises `RegistryReadError`; the surface bails to an empty-strip placeholder when the registry is missing or malformed rather than crashing the frame [4].

## User-scope portfolio (W06)

`eawf.tui.portfolio.offline_render` (test-mode entry; the live-loop wiring is intentionally deferred — the surface ships as the headless renderer first). A `rich.table.Table` summary of every repo in the user-scope registry with one row per repo: code, title, active phase, open iter, ready-wave count, stale flag, active flag [5].

Frame:

```
+-------------------------------------------------------------------+
| Eä  portfolio (3 repos)                                           |  <- header
+-------------------------------------------------------------------+
| code      title       phase       iter        ready  stale active |
| EAWF      Eä          P20 active  P20-I01     2      no    yes    |
| DEMO      Demo        P03 active  P03-I02     0      no    no     |
| OTHER     Other       (none)      (none)      0      yes   no     |
+-------------------------------------------------------------------+
| ↑↓ navigate  Enter open  Esc back  q quit                         |  <- footer
+-------------------------------------------------------------------+
```

Like W05 the portfolio is strictly read-only on the registry; the explicit registry mutators land in `eawf repo {add,remove,prune}` (not scan/walk/import-from-scan) per `feedback_explicit_registry_only`. The same `read_registry` / `read_repo_state` / `is_stale` helpers back both surfaces so the staleness signal is consistent between the W05 workspace strip and the W06 portfolio table [5].

`PORTFOLIO_FOOTER_KEYMAP`: `↑↓ navigate  Enter open  Esc back  q quit  (vim: j k)`. Goldens: `portfolio_default.txt`, `portfolio_empty.txt`, `portfolio_unavailable.txt` (registry file missing) [5].

## Audit-running overlay (W07)

`eawf.tui.audit_overlay.open_audit_overlay` builds a one-frame overlay surfaced when an audit is mid-run or has recently failed. The overlay aggregates the three pieces of context an operator wants when a ship-gate, review, or evaluation is in flight: audit summary, attached-runtime block, remediation hints, action menu [6].

Frame:

```
+----------------------------------------------------------+
| Eä  EAWF / P20 / P20-I01  | overlay: audit A21-P16       |  <- header
+----------------------------------------------------------+
| id:        A21-P16                                       |
| scope:     P20                                           |  <- summary
| kind:      ship-gate                                     |
| status:    failed                                        |
| verdict:   major                                         |
+----------------------------------------------------------+
| attachment:                                              |
|   pid:       12345                                       |  <- runtime
|   adapter:   claude-code                                 |
|   session:   SES-001                                     |
+----------------------------------------------------------+
| remediation hints:                                       |
|   - rerun the failing checks once root-cause is logged   |  <- hints
|   - audit verdict major: block ship until downgraded     |
+----------------------------------------------------------+
| actions (v0.4):                                          |
|   r  retry          (v0.4)                               |  <- actions
|   b  mark-blocked   (v0.4)                               |
|   e  escalate       (v0.4)                               |
+----------------------------------------------------------+
```

`AuditAttachment` is a typed `BaseModel` (`extra="forbid"`) the caller threads in from a hook payload or dispatch envelope. The overlay never spawns a process and never kills one; the PID + adapter id are rendered for operator situational-awareness only. The harness adapter literal mirrors D12 (v0.3 scope: `claude-code`, `codex`, `opencode`, plus the explicit `unknown` fallback) [6][14].

Action menu (`ACTION_KEYMAP`): `r` retry, `b` mark-blocked, `e` escalate. All three carry the muted `ACTION_DEFERRED_MARKER` `(v0.4)` so the operator knows the verbs are deferred; `handle_action_key` returns the footer toast `action deferred to v0.4` (`ACTION_DEFERRED_TOAST`) without mutating state. The mutating verbs land in v0.4 [6].

Goldens cover the three audit branches: `audit_overlay_pass.txt`, `audit_overlay_running.txt`, `audit_overlay_failed.txt`, all keyed off `audit_overlay_state.json` [6].

## Weekly burn line (W09)

`eawf.tui.layout.build_weekly_burn_line` reads `state.project.weekly_eu_target`; when set, the footer renders a `weekly burn: <consumed> / <target>` line via `eawf.estimation.metrics.compute_weekly_burn`. When the field is unset (default `None`) the line is omitted entirely — success criterion 3 of W09. The metrics-module import is deferred so the offline fast-path stays cheap when the operator has not opted into the weekly cadence [15].

The divisor surfaces only in surfaces that render the footer chassis (quadrant, workspace, wave-board); the overlays use a different one-line footer and intentionally do not show the burn line so the overlay frame stays focused on the open record.

## Config modal (W11)

`eawf.tui.config_modal.run_config_modal` (entry) + `eawf.tui.config_modal.apply_key` (pure reducer). A tabbed full-screen `rich.layout.Layout` that replaces the W02 quadrant when the operator presses `c`. Tabs and keys are sourced from `eawf.config.registry` (W10) so the modal and the `eawf config menu` CLI cannot drift on metadata. Save uses the same layered-YAML writer as `eawf config set` — namely `eawf.cli.commands.config._save_value_to_layer` — so the modal never touches `state.json` directly and the layered-config CLI stays the single writer of YAML layers [7][8].

Frame:

```
+----------------------------------------------------------------+
| Eä  EAWF / P20 / P20-I01  >> config                            |  <- header
+----------------------------------------------------------------+
| tabs: [audit] estimation planning research runtime ship ui ... |
+----------------------------------------------------------------+
| audit.fix_safe           [bool]    False                       |
| > audit.flaky_retry_count [int]    1                           |  <- form
+----------------------------------------------------------------+
| edit > new value: _                                            |  <- input
+----------------------------------------------------------------+
| ↑↓ field  Tab tabs  Enter edit  s save  q/Esc back             |  <- footer
+----------------------------------------------------------------+
```

Keymap (per `feedback_tui_keymap_conventions` — arrows primary, vim aliases secondary; full key names lead):

| Key | Action |
|---|---|
| ↑ / ↓ | move the field cursor inside the active tab |
| Tab / Shift-Tab | cycle tabs left / right (arrows ← / → reserved for editing) |
| Enter | open the per-type inline editor for the selected field |
| Esc | at the modal root, exit without saving; while editing, cancel |
| `s` | flush every dirty field through the W10 layered writer + toast `saved N keys` |
| `q` | synonym for Esc at the modal root |
| `c` | (parent surface only) open the config modal |

Per-type inline editor (W11 success criterion 2 — sized per type):

* `bool` — confirm widget (`y` / `n` / space toggles; Enter commits).
* `int` / `float` / `str` — text input; backspace deletes; Enter commits, Esc cancels.
* `choice` — narrow select list (↑ ↓ moves; Enter commits).
* `multichoice` — checklist (space toggles; Enter commits).

The view-state is a frozen `pydantic.BaseModel` with `extra="forbid"`, matching the W03 wave-board view-state convention. `apply_key` is a pure function — it returns the next view-state without touching disk — so the dispatch surface is easy to drive from tests [7].

## Keymap reference

Cross-surface key conventions, by tier:

**Navigation (always primary, never vim):**

| Key | Action |
|---|---|
| ↑ / ↓ | row / field cursor |
| ← / → | strip cursor (workspace) / form cursor (config edit-mode) |
| PageUp / PageDown | page through a long list |
| Home / End | jump to first / last row |
| Enter | open / focus the selection |
| Esc | back / cancel |

**Cross-surface single-letter verbs:**

| Key | Action | Producing wave |
|---|---|---|
| `b` | open wave-board (from quadrant / workspace) | W03 |
| `f` | cycle filter (wave-board only) | W03 |
| `c` | open config modal (from quadrant) | W11 |
| `q` | quit | W02 |

**Two-key overlay verbs (`o<X>` = "open X"):**

| Sequence | Overlay |
|---|---|
| `oH` | hypothesis |
| `oD` | decision |
| `oM` | memory |
| `oE` | events |
| `oR` | dispatch (render) |

**Audit-running overlay action menu (deferred to v0.4 — surfaced but inert):**

| Key | Action |
|---|---|
| `r` | retry `(v0.4)` |
| `b` | mark-blocked `(v0.4)` |
| `e` | escalate `(v0.4)` |

**Vim aliases (secondary; per `feedback_tui_keymap_conventions`):**

| Vim key | Alias for |
|---|---|
| `h` | ← |
| `j` | ↓ |
| `k` | ↑ |
| `l` | → |
| `g` | Home |
| `G` | End |

Vim aliases trail in every footer hint (rendered as `(vim: h j k l g G)` after the primary keymap). The hint convention is anchored in `feedback_tui_keymap_conventions` so a reader who has never seen the framework can still discover the primary surface from the footer alone [12].

## References

[1] `src/eawf/tui/layout.py`
[2] `src/eawf/tui/wave_board.py`
[3] `src/eawf/tui/overlays.py`
[4] `src/eawf/tui/workspace.py`
[5] `src/eawf/tui/portfolio.py`
[6] `src/eawf/tui/audit_overlay.py`
[7] `src/eawf/tui/config_modal.py`
[8] `src/eawf/cli/commands/config.py`
[9] `tests/golden/tui/`
[10] `src/eawf/tui/app.py`
[11] `AGENTS.md` (rules 16, 18)
[12] `feedback_tui_keymap_conventions` (operator memory note)
[13] `src/eawf/state/wave_graph.py`
[14] `D12: v0.3 harness scope: Claude + Codex + OpenCode only`
[15] `src/eawf/estimation/metrics.py`

## Provenance

Document assembled on 2026-05-15 from the P20 wave outcomes recorded in `.ea/state.json` (W02..W11) plus a direct read of `src/eawf/tui/*.py` on `feature/eawf-v0.3-p20` HEAD at the time of the W13 inline run. Frame sketches were lifted from the module docstrings of the source files themselves so the rendered ASCII tracks the implementation exactly. Goldens cited under `tests/golden/tui/` are the same fixtures the per-wave pytest snapshots assert against — a reviewer reproduces any frame by running the corresponding `pytest tests/ -k <surface>` invocation locally.

## Scrub

- status: clean
- notes: repo-relative paths only; no absolute paths, no host-local URLs, no PII; no screenshot or asciinema cast files committed (text goldens substitute per AGENTS rule 16).
