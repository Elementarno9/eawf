# TUI surface architecture

## Summary

The Eä TUI is the operator console, a Textual app under `src/eawf/surfaces/tui/console/` [2]. `eawf ui` opens it and nothing else [1]: the launcher resolves the tree's authority, attaches the console to the live daemon projection when the tree is epoch 2, and otherwise lands in the entry state that names the next command. The epoch-1 app that preceded it, with its scope screens, modes, widgets and overlays, has been deleted.

The surface splits into three parts:

- `launch.py` — `launch_tui`, the one entry point the CLI calls [1].
- `console/` — the console: the app that composes one frame, the route registry, the session model, the key tables, the renderers and overlays each route draws through, the daemon seam, the plain-mode renderer and the golden replay harness [2].
- `chassis/` — modules the console draws on that are not console surfaces: the daemon transport, the themes, the glyph resolver and its status colours, the workspace registry dashboard, the plain-text screen capture, the cast recorder and the colour-vision-deficiency simulation [3].

## Launch and plain mode

`eawf.surfaces.tui.launch.launch_tui` resolves the tree's authority and opens `ConsoleApp` on it [1]. A tree the console can attach to opens on the live daemon projection over one seam; a launch that cannot simply attach (no registered root, an ambiguous one, an epoch-1 tree whose migration is owed, a schema the console cannot read) lands in the entry state the attach path names.

A launch with no interactive terminal (`--plain`, `--no-input` or a stdout that is not a TTY) draws no app: it writes the console's own frame in plain mode [4], the offline snapshot for an attached tree or the entry state for one it could not attach to. Plain mode is the same frame in the ASCII glyph allocation, one row per line, with no escape sequences. The `ui.glyphs` setting picks the allocation the interactive console draws in: `unicode`, `ascii`, or `auto`, which draws ASCII when stdout is not UTF-8 [1].

`eawf workspace registry-status` renders the workspace registry dashboard as plain text through `eawf.surfaces.tui.chassis.offline.offline_render` [5]. It reads the registry, marks stale repos and folds every repo's active-phase waves and effort units into one totals line, without opening a Textual app.

## The console

`ConsoleApp` is one screen of three row widgets, one key dispatcher and one clock [2]. `compose_frame` renders the frame: an overlay replaces it, an open drawer or an armed go prefix keeps the route frame and replaces its tail, otherwise the route's own frame is shown. The app reads its own size at render time, so a terminal resize re-lays the frame on the next render.

Everything the console draws arrives through `ProjectionSeam` [6], which holds the chassis `StateBinding` [7] rather than opening a second connection: the binding's socket push, its always-on poll backstop and its resume cursor are the console's one answer to "am I live".

The route registry declares every destination once, grouped into route groups, and each route names the read model it binds [8]. The key tables give every route its ordered keys, route verbs first and the shared global keys last [9]. A key the global grammar reserves means the same thing on every route.

## Keymap

| Key | Action |
|---|---|
| `↑` / `↓` | move the row cursor |
| `PageUp` / `PageDown` | page a long list |
| `Home` / `End` | jump to the first or last row |
| `Enter` | drill into or open the row under the cursor |
| `Tab` | switch buckets or sections where a route has them |
| `Escape` | go back |
| `g` | go prefix: jump to another route |
| `/` | command palette |
| `.` | actions for the subject under the cursor |
| `i` | inspect drawer |
| `y` | copy |
| `?` | help |

## Verification

The console's golden contract lives in `tests/fixtures/console/golden/`: the frames and journeys recorded from the design prototype, the pack-to-port normalisation map and the styled captures [10]. The replay harness renders every recorded frame and drives every journey against the console, and the tests under `tests/snapshots/tui/console/` hold it to the contract. The coverage gate's TUI floor counts those recorded frames and journeys, so deleting one without a replacement fails the gate [11].

## References

| Ref | Path |
|---|---|
| [1] | `src/eawf/surfaces/tui/launch.py` |
| [2] | `src/eawf/surfaces/tui/console/app.py` |
| [3] | `src/eawf/surfaces/tui/chassis/__init__.py` |
| [4] | `src/eawf/surfaces/tui/console/plain.py` |
| [5] | `src/eawf/surfaces/tui/chassis/offline.py` |
| [6] | `src/eawf/surfaces/tui/console/seam.py` |
| [7] | `src/eawf/surfaces/tui/chassis/state_binding.py` |
| [8] | `src/eawf/surfaces/tui/console/registry.py` |
| [9] | `src/eawf/surfaces/tui/console/keybar.py` |
| [10] | `src/eawf/surfaces/tui/console/harness.py` |
| [11] | `tools/coverage_gate.py` |

## Provenance

Rewritten from the current `src/eawf/surfaces/tui/` source when the epoch-1 app was deleted. This supersedes the reference that described the epoch-1 scope screens, modes, widgets and overlays.

## Scrub

- status: clean
- notes: repo-relative paths only; no absolute paths, host-local URLs, real emails, or PII.
