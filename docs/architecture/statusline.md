# Statusline integration

*One line of live scope, budget and session state for Claude Code; answered from a cache in tens of milliseconds.*

The Claude Code statusline runs `eawf cc statusline` on every prompt redraw, feeding it the host's JSON payload on stdin and printing the single line it writes to stdout.
The command always exits 0: a segment that cannot be sourced renders an explicit marker, never a crash and never a guessed value.

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

Each segment is one module under `src/eawf/runtime/runtimes/claude/statusline_modules/` exposing `build(claude_payload, state_path)`, and each degrades on its own; an exception inside a module becomes a `failed` segment, not a missing line.
The modules render left to right in this fixed order:

| Module | Renders | Source |
| --- | --- | --- |
| `scope` | `scope:<key>`, the record the session's Run is scoped to | the Run bound to the host session |
| `budget` | `budget:<spent>/<cap>`, plus `!limit` at the cap | the same Run's sealed token cap and its usage readings |
| `git` | branch and dirty or staged state | the workspace repository |
| `model_session_cwd` | model, session and working directory | the host payload |
| `context_tokens` | context and token pressure | the host payload and transcript counters |
| `rate_window` | used fraction and reset time per rate-limit window | the host payload |
| `cost` | session cost | the host payload |
| `mcp_health` | Model Context Protocol (MCP) registry health count | the MCP registry rows |
| `hooks_plugins` | `hooks:<n> plugins:<m>` | files under `.claude/hooks/` and the host's plugin record |
| `memory` | memory freshness | the memory ledger |
| `token_saving` | `save:<pct>%`, the prompt-cache hit share of the context | the host payload |

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

When the `COLUMNS` environment variable names a positive width, segments claim it in a fixed keep order and each that no longer fits is dropped:

1. `scope`
2. `budget`
3. `context_tokens`
4. `model_session_cwd`
5. `git`
6. `rate_window`
7. `cost`
8. `mcp_health`
9. `hooks_plugins`
10. `memory`
11. `token_saving`

What the operator steers by outlives the ambient counts, and a smaller low-ranked segment may still fill room a larger one could not.
Width is measured in terminal cells on the visible text, so colour escapes never count and wide glyphs take two cells.
When not even the first segment fits it is kept alone and clipped with an ellipsis.
Without a usable `COLUMNS` the line renders whole.

## Themes and colour

A theme (`default`, `powerline` or `ascii-fallback`, from `src/eawf/platform/templates/themes.yaml`) sets the separator, a glyph per module, whether failed segments are skipped, and a colour per segment status.
The `--theme` flag wins over the `EAWF_STATUSLINE_THEME` environment variable; an unknown name falls back to `default`.
The `ascii-fallback` theme emits no colour bytes, carries no glyphs and drops failed segments, so it suits terminals that cannot show colour.

| Status | Colour in `default` | Used for |
| --- | --- | --- |
| `ok` | green | a value that makes a health claim and passes it |
| `info` | none | a value shown with no health claim either way, such as a hook count |
| `warn` | yellow | a value worth attention |
| `missing` | grey | a value with no producer, such as an absent plugin record |
| `degraded` | red | a real fault, such as a plugin record that exists but cannot be read |
| `failed` | none | a module that raised |

The hook count is informational: it counts files on disk and never reads an exit code, so it renders `info` and turns red only when the plugin record is unreadable.

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

- The command exits 0 on every input; an unsourced value renders `n/a(<reason>)`, never a bare `?` or `-`.
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
