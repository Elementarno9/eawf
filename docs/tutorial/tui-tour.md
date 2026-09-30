# TUI tour

*Open the operator console, move between its routes, and read a frame in plain text.*

The console is the fastest way to watch Eä state while work is in flight. It draws the daemon's live projection of the tree: runs, attention items, tracks, milestones and releases, each on its own route.

For implementation detail, see [TUI surface architecture](../architecture/tui.md). For the init path that creates the tree the console attaches to, see the [`/init` pipeline](../architecture/workflow.md#init-pipeline-dag) and the [profile picker walkthrough](profile-picker.md).

## 1. Open the console

From a registered repository:

```bash
eawf ui
```

A folder the console cannot attach to lands in an entry state that names the next command instead. An unregistered folder, for example:

```text
eawf ui: This folder is not a registered workspace root
  eawf init
```

## 2. Move around

Every route shows its keys in the keybar on the last row, route keys first and the shared keys last. The shared keys mean the same thing on every route:

| Key | Action |
|---|---|
| `↑` / `↓` | move the row cursor |
| `PageUp` / `PageDown` | page a long list |
| `Home` / `End` | jump to the first or last row |
| `Enter` | drill into or open the row under the cursor |
| `Escape` | go back |
| `g` | go prefix: jump to another route |
| `/` | command palette |
| `.` | actions for the subject under the cursor |
| `i` | inspect drawer |
| `y` | copy |
| `?` | help |

Press `?` on any route for its help card.

## 3. Read a frame in plain text

With no interactive terminal, `eawf ui` draws no app: it writes the console's own frame in plain mode, the same rows in the ASCII glyph allocation with no colour or escape sequences. Ask for it explicitly with the global flag:

```bash
eawf --plain ui
```

A screen reader, a CI log or an export therefore reads exactly what the console shows.

If `eawf ui` prints an entry state instead of opening the console, run the command it names; a tree whose migration is owed says so and names the migration command.
