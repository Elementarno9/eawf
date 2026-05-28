# TUI surface architecture

## Summary

The current Eä TUI is a Textual app under `src/eawf/surfaces/tui/`. The old Rich surface, including standalone board modules and live-layout loop, has been removed. Operators still launch the surface through `eawf tui` or the bare `eawf` command, but the runtime now resolves a scope, starts `EaApp`, and pushes one of the Textual scope screens.

This document describes the live architecture, not the historical P20 Rich implementation. Current code splits the surface into these layers:

- `app.py` — `EaApp`, scope switching, modal-depth cap, theme selection, state binding, and app-wide bindings.
- `scopes/` — repo, workspace, and user screens that compose the shared chrome and body widgets.
- `widgets/` — reusable widget catalog: header, footer, heartbeat, roadmap tree, status, git, backlog, workspace table, effort bars, and variance tiles.
- `palette/` — static `/` command palette registry and fuzzy command overlay.
- `screens/overlays/` — modal screens for config, details, events, audit state, plan preview, PR lists, reference targets, help, and needs-user pauses.
- `offline.py` — deterministic non-interactive text renderers for non-TTY, `--plain`, `--no-input`, and workspace registry status paths.
- `snapshot/` — Textual Pilot snapshot and asciinema helpers.

The app remains read-only for ordinary navigation. State enters through `StateBinding`, which loads `.ea/state.json` read-only and updates the app's reactive `state` attribute. Mutating actions, such as config saves, route through the same daemon-mediated or layered writer paths as CLI commands instead of editing state directly.

## Launch and fallback paths

`eawf.surfaces.tui.run_app(scope, state_path)` is the interactive entry point. The CLI resolves a `ScopeName` (`repo`, `workspace`, or `user`) before launch and passes the state path to `EaApp`. On mount, `EaApp` pushes the matching scope screen from `EaApp.SCREENS`.

Headless callers do not start Textual. `eawf.surfaces.tui.offline.emit_status` prints a three-line status frame for non-TTY, `--plain`, and `--no-input` paths:

```text
Eä  <breadcrumb>
  project=<code> phases_open=N iters_open=N iters_closed=N waves_pending=N audits=N
keymap: <shared hints>
```

`offline.offline_render` renders the workspace registry dashboard as plain text for `workspace registry-status` and the corresponding JSON envelope. It reads the registry, computes stale chips, and emits labelled sections without opening a Textual app.

## Shared app shell

`EaApp` owns global concerns:

- Scope screens: `repo`, `workspace`, `user`.
- Global scope-switch keys: `w`, `r`, `u`, with hidden `Ctrl-W`, `Ctrl-R`, and `Ctrl-U` aliases.
- Quit keys: `q` and `Esc`.
- Hidden vim navigation aliases: `h`, `j`, `k`, `l`.
- Reference-history keys: `Alt-Left` and `Alt-Right`.
- Modal stack cap: `MAX_MODAL_DEPTH = 3`, enforced by the app before pushing another overlay.
- Theme and glyph policy resolution, including braille-to-ASCII fallback when coverage probes fail.
- Reactive state updates from `StateBinding`.

Every scope screen inherits the shared chassis from `eawf.surfaces.tui.scopes.ScopeScreen`: header, footer, heartbeat, breadcrumb, runtime status, help, palette, and config actions. Scope screens override only the body layout and scope-specific footer hints.

## Repo scope

`eawf.surfaces.tui.scopes.repo.RepoScreen` is the repo dashboard. It composes a 2x2 Textual layout:

```text
+-------------------------------+-------------------------------+
| ROADMAP                       | STATUS                        |
| RoadmapTree                   | StatusPane                    |
+-------------------------------+-------------------------------+
| GIT                           | BACKLOG                       |
| GitPane                       | BacklogTable                  |
+-------------------------------+-------------------------------+
```

`RoadmapTree` replaces the old standalone board list. It renders the phase -> iter -> wave hierarchy with status glyphs, progress bars, and selectable wave rows. Selecting a wave posts `RoadmapTree.WaveSelected`; the shared scope shell opens `DetailModal` for typed drill-down. There is no separate board module or `b` key in the current architecture.

The repo footer hints are:

```text
↑↓ move  ←→ collapse  Enter open  w/r/u scope  c config  / palette  ? help  q quit
```

## Workspace scope

`eawf.surfaces.tui.scopes.workspace.WorkspaceScreen` provides workspace-level orientation. It keeps the shared header/footer and focuses on registered repos, stale state, and scope switching. The plain-text workspace registry dashboard is implemented separately in `offline.offline_render` so CI and scripted callers get deterministic output without Textual.

Workspace freshness comes from the platform registry helpers. Stale entries are marked when registry or repo state freshness checks fail; the TUI surfaces that as `(stale)` in the plain renderer and scope widgets.

## User scope

`eawf.surfaces.tui.scopes.user.UserScreen` is the user-scope portfolio. It summarizes known repos through `WorkspaceTable`-style rows and shares the same app chrome, palette, help, config, and scope-switch affordances as the repo and workspace scopes.

## Widget catalog

Reusable widgets live under `src/eawf/surfaces/tui/widgets/`:

- `Header` — brand, breadcrumb, runtime cell, and clock.
- `Footer` and `Heartbeat` — key hints and liveness/degraded signal.
- `RoadmapTree` — phase / iter / wave tree and wave selection events.
- `StatusPane` — lifecycle status summary.
- `GitPane` — branch, status, and ahead/behind context.
- `BacklogTable` — backlog rows with filtering/sorting support.
- `WorkspaceTable` — registered repo table.
- `EUBar` and `VarianceTile` — effort and estimate-actual visualizations.

Widgets bind to the app's reactive read-only state. Screens compose widgets; they do not duplicate data loading.

## Command palette and overlays

The `/` key opens `CommandPalette`. Palette verbs are statically declared in `eawf.surfaces.tui.palette.verbs`, filtered by scope, and ranked by the palette widget. The old `:` alias is gone; `/` is the canonical palette prefix.

Common overlays live under `src/eawf/surfaces/tui/screens/overlays/`:

- `config_modal.py` + `config_modal_logic.py` — registry-driven config editor opened by `c` or `/config`.
- `detail.py` — typed detail card for selected roadmap references, including waves selected from `RoadmapTree`.
- `events.py` — event ring-buffer view.
- `audit_running.py` and `audit_failed.py` — audit progress/failure overlays.
- `plan_preview.py` — rendered plan preview surface for needs-user review flows.
- `needs_user.py` — daemon-push pause overlay.
- `pr_list.py` — pull request list overlay.
- `help.py` and `reference.py` — keymap/help and reference-target display.

`ConfigModal` uses strict `ConfigModalState` models and routes saves through the layered config writer path used by the CLI. It edits `.ea/config.yaml` layers, not `.ea/state.json`.

## Snapshot and docs references

Current visual tests use Textual Pilot helpers in `src/eawf/surfaces/tui/snapshot/`. The old `tests/golden/tui/` Rich-layout frame set is no longer the canonical architecture reference. Snapshot helpers normalize Textual screen output so tests can assert stable frames without committing host-specific screenshots.

Screenshots and asciinema casts remain non-canonical for docs because they can embed machine-local details. Text docs should cite repo-relative paths and Textual snapshot fixtures only.

## Keymap reference

Global keys:

| Key | Action |
|---|---|
| `w` | switch to workspace scope |
| `r` | switch to repo scope |
| `u` | switch to user scope |
| `/` | open command palette |
| `?` | open help |
| `c` | open config overlay where available |
| `Enter` | open focused row/detail |
| `q` | quit |
| `Esc` | quit or dismiss active overlay |
| `Alt-Left` / `Alt-Right` | reference history back / forward |

Navigation keys:

| Key | Action |
|---|---|
| `↑` / `↓` | move row or field cursor |
| `←` / `→` | collapse/expand tree rows or move within focused controls |
| `PageUp` / `PageDown` | page long lists where supported |
| `Home` / `End` | jump to first / last row where supported |
| `h` / `j` / `k` / `l` | hidden vim aliases for arrow navigation |

## References

| Ref | Path |
|---|---|
| [1] | `src/eawf/surfaces/tui/__init__.py` |
| [2] | `src/eawf/surfaces/tui/app.py` |
| [3] | `src/eawf/surfaces/tui/offline.py` |
| [4] | `src/eawf/surfaces/tui/scopes/repo.py` |
| [5] | `src/eawf/surfaces/tui/scopes/workspace.py` |
| [6] | `src/eawf/surfaces/tui/scopes/user.py` |
| [7] | `src/eawf/surfaces/tui/widgets/__init__.py` |
| [8] | `src/eawf/surfaces/tui/widgets/roadmap_tree.py` |
| [9] | `src/eawf/surfaces/tui/screens/overlays/detail.py` |
| [10] | `src/eawf/surfaces/tui/screens/overlays/config_modal.py` |
| [11] | `src/eawf/surfaces/tui/palette/verbs.py` |
| [12] | `src/eawf/surfaces/tui/snapshot/__init__.py` |

## Provenance

Refreshed for P28-I03-W47 from current `src/eawf/surfaces/tui/` source. This supersedes the historical P20 Rich TUI reference that cited removed modules and standalone board frames.

## Scrub

- status: clean
- notes: repo-relative paths only; no absolute paths, host-local URLs, real emails, or PII.
