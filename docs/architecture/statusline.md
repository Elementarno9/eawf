# Statusline integration

*Powerline rows of live session, limit, scope and budget state for Claude Code, rendered on every redraw.*

The Claude Code status line runs `eawf cc statusline` on every prompt redraw, feeding it the host's JSON payload on stdin and printing the rows it writes to stdout.
The command always exits 0, in any directory, managed or not: a segment that cannot be sourced is left out of the rows, never drawn as a crash or a guessed value.

## Install

No plugin can supply a status line, because Claude Code keeps only `agent` and `subagentStatusLine` from a plugin's settings, and a project's settings file holds it for one checkout only. The line therefore lives once, at user level, in `~/.claude/settings.json`:

```json
"statusLine": {"type": "command", "command": "eawf cc statusline", "refreshInterval": 30}
```

`eawf statusline install` writes that key (`src/eawf/runtime/runtimes/claude/statusline_install.py`). `--scope user` is the default; `--scope project` and `--scope local` target the repository's `.claude/settings.json` and `.claude/settings.local.json`. It sets only `statusLine`, keeps every other key in its order, the file's indentation and its mode, and renames a temporary file into place. The file is re-serialised from its parsed content, so inline arrays and objects come back one item per line.

- The same entry already present: exit 0. Another entry: it is printed and the command exits 1, unless `--yes` replaces it.
- `--print` prints the line and writes nothing.
- A file it cannot write: the line goes to stdout, the path and the reason to stderr, and the command exits 6 so the operator adds the line by hand.

`eawf plugin install claude` and `eawf init --quick` no longer write `statusLine`; when the user layer names eawf, they remove a project entry an earlier install left behind. `eawf cc statusline install` remains a hidden alias for one release.

`eawf statusline doctor [--json]` (and the `claude.statusline` doctor check) reports which settings layer wins (`managed`, `local`, `project`, `user` or `none`), its command, what shadows eawf's line, where the command resolves, its version, one measured render against the 300 ms budget, the refresh interval, `disableAllHooks`, and a verdict of `ok`, `missing`, `shadowed` or `broken` with its fix. It exits 1 only for `broken`.

`refreshInterval` 30 redraws an idle session every 30 seconds, so the limit rows and their countdowns move without a prompt.

## Segments

Line 1 is a row of filled powerline segments, built left to right by the modules under `src/eawf/runtime/runtimes/claude/statusline_modules/`, each taking `(claude_payload, state_path)`.
Each segment degrades on its own: an exception inside a module becomes a `failed` segment, and a segment without a sourced value keeps its `<label>:n/a(<reason>)` marker for audit but is omitted from the row.

| Segment | Renders | Source |
| --- | --- | --- |
| model | the model's display name | the host payload |
| project | basename of the workspace project directory | the host payload |
| session | `<wall> · <share>% active`: wall-clock time since the session started, and the share of it spent in API calls from the first minute on | the host payload's cost block |
| cost | `$<d.dd> API-equivalent`, or `API-eq` when narrow: what the session's tokens would cost at API prices | the host payload |
| MCP | `MCP: <installed>/<registered>`, filled warning or alert when some are not installed | the Model Context Protocol (MCP) registry rows |
| hooks | `hooks!<n>` (`hooks!99+` from 100), absent at 0; see [Hook failures](#hook-failures) | the hook health record |
| scope | the key of the record the session's Run is scoped to | the Run bound to the host session |
| budget | `budget: <spent>/<cap>`, plus `!limit` at the cap | the same Run's sealed token cap and its usage readings |

Outside a managed repository only model, project, session, cost and the meter rows draw; no daemon is started or called.
The first MCP figure counts installed servers, not connected ones: Claude Code's status line input carries no MCP connection state, and learning it means probing each server, which a redraw cannot afford.

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

## Meter rows and grid

Under line 1 come up to three meter rows, `ctx`, `5h` and `7d`, read from the payload alone; a row without data is not printed. One blank row separates line 1 from the first meter row, and none is printed when no meter row is; in colour the row holds a lone SGR reset (`\x1b[0m`) and nothing visible, because Claude Code drops an empty status line row, while a line drawn without colour keeps it empty.
Every row lays out the same five columns, one space apart, so the columns line up down the rows:

| Column | Width | Align | `ctx` | `5h`, `7d` |
| --- | --- | --- | --- | --- |
| label | 3 | left | `ctx` | `5h` |
| bar | W | none | used share | used share, pace marker |
| percent | 4 | right | `  7%` | ` 31%` |
| pace or cache | 5 | right | ` 92%c` | ` -27%` |
| detail | rest | left | `68K/1.0M` | `2h05m` |

The detail starts at offset W + 16 on every row: offset 36 with the default 20-cell bar. A blank inner field is padded with spaces; a blank trailing field is not emitted.

- `ctx` fills with the three input classes of the last API call over the context window, capped at 100 %. Its cache column, such as `92%c`, is the cache-read share of those tokens; with no tokens it is blank.
- `5h` and `7d` fill with the window's used share and end on the time to reset, such as `2h05m` or `2d4h`.
- The bar is W cells (`statusline.bar_width`, 10 to 40, default 20): full cells, one eighth-cell edge, then the track. The pace marker `│` sits at the cell an even spend would have reached, drawn white or black, whichever stands out on that cell: in `ansi` white (97) on the danger fill 41 and on the track, black (30) on 42, 43 and 46.

### Pace

Pace compares a limit window's used share with an even spend across it. With `L` the window's length (18,000 s for `five_hour`, 604,800 s for `seven_day`), `expected = 100 * clamp(L - (resets_at - now), 0, L) / L` and `pace = round_half_up(used - expected)`.
Above +3 it prints `+N%`, below -3 `-N%`, otherwise `~`; for example, 31 % used with 2h05m left is 58.3 % expected, so `-27%`.
In the first 2 % of a window, or with a reset further out than `L`, pace prints `~`; a reset already passed prints `~` and `0m`.
`resets_at` is read once into Unix seconds: a number from 1e11 up is milliseconds, a smaller one seconds, a string ISO-8601 (naive means UTC). A reset that cannot be read blanks only that row's pace, marker and countdown.

## Hook failures

The `hooks` segment counts the rows of the hook health record (`.ea/local/hooks/health.jsonl` in the main checkout) written in the last 24 hours and after the last acknowledgement in `ack.json` beside it (`src/eawf/runtime/hooks/health.py`).
The record is read backward from its end, stopping at the first older row or after 100 rows, so a long record costs a redraw no more than the rows it counts.
`eawf hook health ack` writes the acknowledgement, prints how many failures it acknowledged, and deletes every session's cached line, so no redraw shows the acknowledged failures again.

## Theme

The default theme, `ansi`, paints with the terminal's 16 ANSI colours, so the line follows whatever palette the terminal is set to (`src/eawf/platform/templates/themes.yaml`).
Segment fills are SGR backgrounds: model 45, project 44, session 46, cost 42, MCP 47, scope 105, budget 106, a warning 43, an alert 41. Segment text takes `statusline.ink`: `auto` by default, white (97) on 41, 44 and 45 and black (30) on 42, 43, 46, 47, 105 and 106; `30` or `97` forces one ink on every segment.
Gaps, meter-row text and the cell after the last separator stay on the terminal's own background (SGR 49); only the bar paints its cells, on a track of 100.
Each separator takes the colour of the segment before it on the fill of the segment after it, so the segments chain without gaps.
The four Catppuccin flavours, `catppuccin-latte`, `catppuccin-frappe`, `catppuccin-macchiato` and `catppuccin-mocha`, reuse the same roles in 24-bit hex; `dark` and `light` name Mocha and Latte. `none` emits no escape at all and joins segments with ` | `.

The theme resolves in this order (`src/eawf/runtime/runtimes/claude/statusline_theme.py`): `--theme`, then `NO_COLOR` set to anything non-empty (`none`), then `EAWF_STATUSLINE_THEME`, then `statusline.theme`, then `ansi`.
Glyphs (`statusline.glyphs`) are `nerd` (the U+E0B0 arrow, default), `unicode` (U+258C) or `ascii`; `TERM=linux` or a non-UTF-8 locale forces `ascii`, then `EAWF_STATUSLINE_GLYPHS` wins over the config.
The flag and environment choices key the cached line, so a fallback never shows a line drawn in another style.

## Fitting the terminal

When the `COLUMNS` environment variable names a positive width, no row is wider than it and none wraps.
Line 1 first narrows the cost label to `API-eq`, then drops MCP, then session, then project, then hooks, budget and scope, keeping model and cost, and clips the model with an ellipsis last.
A meter row shrinks its bar to fit the widest detail, down to ten cells; then the detail goes on every row, then the pace column; a row that still does not fit is left out.
Width is measured in terminal cells on the visible text, so colour escapes never count and wide glyphs take two cells.
Without a usable `COLUMNS` the rows render whole.

## Freshness

Every redraw renders synchronously, so a line is never older than the payload it answers (`serve_line` in `src/eawf/runtime/runtimes/claude/statusline.py`).
The render runs on a daemon thread with a 250 ms budget, under the host's 300 ms redraw debounce:

- A render that finishes in time is printed and written to the session's cache entry; the older cached line is never printed.
- A render that raises, or overruns while the session has a cached line, prints that line and spawns a detached `eawf cc statusline prewarm` in its own session, fed the payload through an unlinked temporary file, with no window and its output discarded. The prewarm renders and rewrites the entry for the next redraw.
- With no cached line, an overrun is waited out, because nothing else describes the session; a render that raises with none prints the model name alone.
- A payload with no session id has no cache entry and renders on the redraw's time.

The cache (`src/eawf/runtime/statusline_cache.py`) keeps one entry per Claude session id at `~/.claude/statusline-cache/<session-id>.json`, holding the line, its write time and the style key it was drawn under. The session id is sanitised so a hostile payload cannot escape the directory, and `EAWF_STATUSLINE_CACHE` redirects the directory. Writes go to a sibling temporary file renamed over the entry, so a reader never sees a torn write; a failed write is swallowed, and a missing, corrupt or ill-shaped entry reads as none.
Every render, the redraw's own or a prewarm, also writes the runtime counter sidecar beside the cache entry.

`src/eawf/__main__.py`, the console entry point, serves the plain invocation, `cc statusline` with at most `--theme`, with piped stdin on a non-Windows host, without importing the Typer app. When it printed the cached line for an overrun it exits at once rather than wait for the abandoned render. A terminal on stdin and Windows consoles go to the full command.

## Measured budgets

Budgets are 95th percentiles (p95): a cold redraw at most 300 ms, and the cache fallback itself at most 100 ms. `tests/perf/runtime/runtimes/claude/test_statusline_budget.py` asserts the fallback and reports the cold p95.

| Path | Measured on 2026-11-10 | How |
| --- | --- | --- |
| Cold redraw, no managed repository | p95 340 ms; median 383 ms | perf gate, 10 fresh interpreters; median of 10 by hand |
| Cold redraw, this repository | median 550 ms | 10 fresh interpreters, empty cache |
| Redraw with a cached line, this repository | median 370 ms; 7 of 10 printed the cached line | 10 fresh interpreters |
| Redraw with a cached line, no managed repository | median 354 ms; 3 of 10 printed the cached line | 10 fresh interpreters |

All were taken on a shared machine under a load average of 6 to 9, so they overstate an idle one.
In a fresh interpreter, imports cost about 290 ms of the cold path (`python -X importtime`): the runtime counter sidecar with Pydantic about 76 ms, the renderer about 47 ms, the state-path resolver with Typer about 35 ms, the layered config loader about 34 ms, the scrubbed log sink about 27 ms. In this repository the render then spends about 100 ms more, most of it decoding the generation document for the MCP segment.
Figures are re-measured when the import graph changes.

## Failure behaviour

- The command exits 0 on every input; an unsourced value is left out of the rows, and its segment carries `n/a(<reason>)`, never a bare `?` or `-`.
- A healthy call writes nothing to stderr.

## Second runtime

Codex has no command-backed statusline, so no Eä renderer is built for it.
It is served by its native `[tui].status_line` preset, an ordered list of fixed items, applied and accepted on 2026-07-20 (decision SURF-130).
The disposition is revisited only if that vendor ships a command-backed statusline.

## Cross-references

- Plugin install and update lifecycle — `docs/architecture/plugins.md`.
- Memory module data source — `docs/architecture/memory.md`.
- Hook events — `docs/reference/hook-events.md`.
- Install flows — `docs/architecture/installation.md`.
