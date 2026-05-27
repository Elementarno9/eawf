# TUI tour

*Open the operator surface, read the dashboard, and use the keymap without changing project state.*

The TUI is the fastest way to inspect current Eä state while a phase is in flight. It is read-oriented: the dashboard shows project, phase, iter, wave, audit, and command-palette context; lifecycle mutations still go through the CLI or slash-command workflow.

For implementation detail, see [TUI surface architecture](../architecture/tui.md). For the init path that creates the state the TUI reads, see the [`/init` pipeline](../architecture/workflow.md#init-pipeline-dag) and the [profile picker walkthrough](profile-picker.md).

## 1. Open the dashboard

From a managed repository:

```bash
eawf tui
```

In a non-TTY environment, `eawf tui` prints the deterministic status fallback instead of opening the interactive app:

```text
Eä  repo ❯ EAWF ❯ P28
  project=EAWF phases_open=1 iters_open=2 iters_closed=33 waves_pending=24 audits=41
keymap: ↑↓ move  ·  Enter open  ·  w/r/u scope  ·  F5 refresh  ·  / palette  ·  ? help  ·  q quit
```

Annotation:

- Header: scope breadcrumb, current project, and active phase.
- Summary line: compact counters from `.ea/state.json`.
- Footer: the active keymap. Arrows lead; single-key shortcuts follow.

Text screenshot: [`docs/_static/tutorial/tui-status-fallback.txt`](../_static/tutorial/tui-status-fallback.txt).

## 2. Move through scopes

The scope keys switch the dashboard lens:

| Key | Scope | Use when |
|---|---|---|
| `w` | workspace | Compare registered repos and spot stale entries. |
| `r` | repo | Inspect the current repo's phase, iter, waves, git, and backlog context. |
| `u` | user | Review the portfolio view across the user registry. |

Use `↑` / `↓` to move within the active view. Use `Enter` to open the focused item when the view supports a detail pane. Use `q` to quit.

## 3. Read the repo dashboard

The interactive repo view expands the status fallback into panels. A typical frame looks like this:

```text
┌──────────────────────────────────────────────────────────────┐
│ Eä  repo ❯ DEMO ❯ P01                                       │
├──────────────────────────────┬───────────────────────────────┤
│ roadmap                      │ status                        │
│ phase: P01 active            │ project: DEMO                 │
│ iter:  P01-I01 active        │ waves pending: 2              │
├──────────────────────────────┼───────────────────────────────┤
│ git                          │ backlog                       │
│ branch: feature/demo-v0.1    │ open: 3                       │
│ status: clean                │ ready: 1                      │
├──────────────────────────────┴───────────────────────────────┤
│ ↑↓ move · Enter open · w/r/u scope · F5 refresh · / palette   │
└──────────────────────────────────────────────────────────────┘
```

Annotation:

- `roadmap` locates the active phase / iter.
- `status` shows state counters that answer "what needs attention?".
- `git` keeps branch and cleanliness visible while waves are integrated.
- `backlog` keeps planned follow-up pressure visible without leaving the dashboard.

Text screenshot: [`docs/_static/tutorial/tui-repo-dashboard.txt`](../_static/tutorial/tui-repo-dashboard.txt).

## 4. Open the command palette

Press `/` to open the command palette. Use it when you know the verb but not the exact key path.

```text
┌─ command palette ────────────────────────────────────────────┐
│ / wave next-ready                                            │
│   roadmap show                                               │
│   doctor                                                     │
│   validate                                                   │
│   config menu                                                │
└──────────────────────────────────────────────────────────────┘
```

Annotation:

- `/` focuses command search.
- Typing filters the command list.
- `Enter` runs or opens the selected command.
- `Esc` closes the palette and returns to the previous view.

Text screenshot: [`docs/_static/tutorial/tui-command-palette.txt`](../_static/tutorial/tui-command-palette.txt).

## 5. Refresh and exit

Use `F5` after another terminal changes state. The TUI reloads the current state and redraws the active view. Use `?` for in-app help and `q` to exit.

If `eawf tui` prints the one-frame fallback instead of opening an interactive surface, check that the command is attached to a TTY and that the repository has a readable `.ea/state.json`.
