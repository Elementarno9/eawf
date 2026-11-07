# Statusline integration

*Powerline rows of live session, scope and budget state for Claude Code; answered from a cache in tens of milliseconds.*

The Claude Code statusline runs `eawf cc statusline` on every prompt redraw, feeding it the host's JSON payload on stdin and printing the rows it writes to stdout.
The command always exits 0: a segment that cannot be sourced is left out of the rows, never drawn as a crash or a guessed value.

## Wiring

`eawf cc statusline install` (or the project plugin install) writes this entry into Claude's `settings.json`, unless the operator already set a statusline of their own:

```json
{
  "statusLine": {
    "type": "command",
    "command": "eawf cc statusline"
  }
}
```

The command is the bare name `eawf`, which resolves on `PATH` to the installed tool; no alias and no configured command prefix is involved.
The wizard is idempotent (a second run reports the no-op and writes nothing) and `--yes` skips its confirmation.
No hook installs the cache warm-up: a stale cache hit starts it itself, as described below.

## Segments

Line 1 is a row of arrow-ended powerline segments, built left to right by the modules under `src/eawf/runtime/runtimes/claude/statusline_modules/`, each taking `(claude_payload, state_path)`.
Each segment degrades on its own: an exception inside a module becomes a `failed` segment, and a segment without a sourced value keeps its `<label>:n/a(<reason>)` marker for audit but is omitted from the row.

| Segment | Renders | Source |
| --- | --- | --- |
| model | the model's display name | the host payload |
| project | basename of the workspace project directory | the host payload |
| active time | `<m>m · <p>% active`, API minutes and their share of the session | the host payload's cost block |
| cost | `$<d.dd>` session cost | the host payload |
| MCP | `MCP: <installed>/<registered>`, filled warning or alert when some are not installed | the Model Context Protocol (MCP) registry rows |
| hooks | `hook-errors:<n>`, shown only when hooks have failed | the hook health record |
| scope | the key of the record the session's Run is scoped to | the Run bound to the host session |
| budget | `budget: <spent>/<cap>`, plus `!limit` at the cap | the same Run's sealed token cap and its usage readings |

The first MCP figure counts installed servers, not connected ones: Claude Code's status line input carries no MCP connection state, and learning it means probing each server, which a redraw cannot afford.

Under line 1 come up to three bar rows, each a bar, the used percentage, the change in points since the session's previous render (`↑+n` or `↓-n`) and a detail:

| Row | Detail | Source |
| --- | --- | --- |
| `ctx` | used over limit tokens, such as `64K/1.0M` | the host payload's `context_window` |
| `5h` | time to reset, such as `2h05m` | the host payload's `rate_limits.five_hour` |
| `7d` | time to reset, such as `6d14h` | the host payload's `rate_limits.seven_day` |

A window the payload omits has no row, and a reset time that cannot be read drops only that row's countdown.

### Scope and budget

The tree's authority lives in the selected epoch-2 generation, and `scope` and `budget` read it through the Run bound to the host's session.
The host's `session_id` is hashed the way a Run stores its `vendor_session`; of the Runs matching that digest, the most recently updated one wins (`statusline_modules/_spine.py`, `session_run`).
`scope` shows the key of the record that Run is scoped to, such as a Task, Batch or Milestone.
`budget` takes its cap from the token ceiling the Run's dispatch binding sealed into its capsule, and its spend from the usage readings on the run ledger, folded so a running total is never counted twice.
The kernel models behind these reads are imported inside the functions that need them, so a redraw that finds no Run does not pay for them.

Every way the read can fail comes back as one reason that the segment renders as `<label>:n/a(<reason>)`:

| Reason | Meaning |
| --- | --- |
| `no-workspace` | no workspace, and so no tree, resolved |
| `epoch1-<gap>` | the tree still answers in epoch 1; the gap names why |
| `no-session` | the host payload carried no session id |
| `generation-unreadable` | the generation document could not be read |
| `no-run-for-session` | no Run is bound to this session |
| `run-invalid` | a matching Run row failed validation |

`budget` adds its own reasons for a Run sealed without a token cap or with no usage reading yet.

## Fitting the terminal

When the `COLUMNS` environment variable names a positive width, no row is wider than it and none wraps.
Line 1 drops MCP, then active time, then project, then hooks, budget and scope, keeping model and cost, and clips the model with an ellipsis last.
A bar row shrinks its bar from ten cells to five, then drops the trend, then the detail; a row that still does not fit is left out.
Width is measured in terminal cells on the visible text, so colour escapes never count and wide glyphs take two cells.
Without a usable `COLUMNS` the rows render whole.

## Palettes

Every row is drawn in a light or a dark 24-bit palette from `src/eawf/platform/templates/themes.yaml`, and every foreground and background pair keeps a contrast ratio of at least 4.5:1.
The palette resolves in this order (`src/eawf/runtime/runtimes/claude/statusline_theme.py`):

1. `--theme light|dark`, then the `EAWF_STATUSLINE_THEME` environment variable set to `light` or `dark`; any other value falls through.
2. Claude Code's `theme` setting, from the project's `.claude/settings.local.json`, its `.claude/settings.json`, the user's `~/.claude/settings.json`, then the user's `~/.claude.json`; a value starting `light` or `dark` picks that palette.
3. When the setting is `auto` or absent, `COLORFGBG`: a last field of 7 or 15 picks light, any other integer dark.
4. Otherwise dark.

The resolved palette keys the cached rows, so a theme switch shows on the next redraw.
There is no colour-free palette: the rows always carry 24-bit colour escapes.

## Cache and refresh

`StatuslineCache` (`src/eawf/runtime/statusline_cache.py`) is the only statusline cache, and it is stale-while-revalidate: a cached line is served at once, even past its time-to-live (TTL), and a background refresh replaces it for the next redraw.

- One entry per Claude session id, at `~/.claude/statusline-cache/<session-id>.json`, holding the line and its write time. The session id is sanitised so a hostile payload cannot escape the directory, and `EAWF_STATUSLINE_CACHE` redirects the directory.
- The TTL is 5 seconds. An entry whose age equals the TTL is still fresh.
- Writes go to a sibling temporary file and are renamed over the entry, so a concurrent reader sees the old line or the new one, never a torn write. A failed write is swallowed: a read-only cache directory degrades to an always-cold render.
- A missing, empty, corrupt or ill-shaped entry is a miss, not an error.
- A stale hit re-stamps the entry, so redraws arriving during the refresh read a fresh entry, then spawns a detached `eawf cc statusline prewarm` in its own session, fed the payload through an unlinked temporary file. If the refresh dies, the entry goes stale again after one TTL and the next redraw retries.
- A miss renders synchronously, writes the entry and returns the line. A payload with no session id renders uncached.

Every render, whether a miss or a refresh, also writes the runtime counter sidecar beside the cache entry, so the sidecar trails the live payload by at most one TTL.

### Entry point fast path

Importing the full command line costs several hundred milliseconds, so `src/eawf/__main__.py`, the console entry point, answers a cache hit before the Typer app is imported.
It matches only the plain invocation, `cc statusline` with at most `--theme`, and only with piped stdin on a non-Windows host.
On a hit it prints the cached line and returns, starting the refresh if the entry is stale.
On a miss it puts the payload it read back on stdin and falls through to the full command, which renders and writes the entry.
A terminal on stdin and Windows consoles go straight to the full command, which owns the stdin hint and console encoding setup.

## Measured budgets

| Path | Measured | How |
| --- | --- | --- |
| Warm redraw (cache hit) | p50 35 ms through the installed script, 70 ms through `uv run` | 10 runs |
| Cold redraw (cache miss) | p50 about 880 ms through `uv run` | 10 runs; the command line import (about 350 ms) and the kernel state models behind `budget` (about 250 ms) dominate |

The warm path meets its 50 ms target when run through the installed script.
The cold path misses its former 200 ms target; it happens once per session, then once per refresh in the background, so it never blocks a redraw once a session has an entry.
Figures are re-measured when the import graph changes, not asserted.

## Failure behaviour

- The command exits 0 on every input; an unsourced value is left out of the rows, and its segment carries `n/a(<reason>)`, never a bare `?` or `-`.
- Logging is imported only on first use, because a cache hit logs nothing and the import would cost a third of the redraw. A healthy call writes nothing to stderr.

## Second runtime

Codex has no command-backed statusline, so no Eä renderer is built for it.
It is served by its native `[tui].status_line` preset, an ordered list of fixed items, applied and accepted on 2026-07-20 (decision SURF-130).
The disposition is revisited only if that vendor ships a command-backed statusline.

## Cross-references

- Plugin install and update lifecycle — `docs/architecture/plugins.md`.
- Memory module data source — `docs/architecture/memory.md`.
- Hook events — `docs/reference/hook-events.md`.
- Install flows — `docs/architecture/installation.md`.
