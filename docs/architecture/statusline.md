# Statusline integration

*Cross-platform Claude Code statusline; portable Python renderer; opt-in by default.*

`eawf global install` offers the Claude Code statusline as an
**opt-in, pre-checked** prompt. The user can decline. There is no
forced install.

## Required design

Eä statusline is cross-platform from v1, implemented in Python:

```text
src/eawf/runtimes/claude/statusline.py    # `eawf cc statusline` entrypoint (renders from stdin JSON)
src/eawf/render/statusline.py             # portable renderer logic
```

Claude settings point at the rendered command using
`cli.preferred_command`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "<preferred_command> cc statusline"
  }
}
```

`SessionStart` hook prewarms the cache asynchronously:

```json
{
  "type": "command",
  "command": "<preferred_command> cc statusline prewarm",
  "timeout": 10,
  "async": true
}
```

If `ea` collides or the user declines the alias, rendered Claude
settings use `eawf cc statusline` and no `ea` alias is required.

## Statusline content

Minimum modules:

- Eä active state scope: workspace / repo, project, subproject, phase,
  iter, active waves.
- Git branch and dirty / staged indicator.
- Model / session / cwd.
- Context / token pressure if available from payload / transcript.
- MCP health count if cheap / cached.
- Hook / plugin status warnings.
- Memory freshness indicator.
- Caveman / RTK / token-saving status if installed.

## Fonts and glyphs

The statusline must not require Nerd Fonts.

```yaml
statusline:
  glyph_mode: auto | ascii | unicode | powerline
  color_mode: auto | none | ansi16 | ansi256 | truecolor
  width: auto
  rows: compact | normal | rich
```

Default: `auto` with safe fallback.

- If terminal / font supports Powerline, use separators.
- If not, use ASCII separators: ` | `, ` > `.
- If color is disabled or the renderer is non-TTY, emit plain text.
- If Windows terminal detection is uncertain, default to ANSI16 or no
  color.
- Strip control chars from any file-derived content.
- Configurable max width and row count to avoid terminal wrapping.

## Performance requirements

- **Cold first paint: <200ms** (Python interpreter startup + first
  render). The cold-paint budget is 200ms because cold-importing
  `typer` + `rich` + Eä's state reader on a typical Mac / Linux box
  takes ~120-180ms; budgeting <50ms for the cold path is unrealistic
  without an AOT binary.
- **Cached refresh: <50ms** for warm renders.
- Cache key: runtime + session ID + cwd + active state version.
- **Stale-while-revalidate**: serve cached on cache hit; refresh
  asynchronously in the background. Never block the terminal on `git`,
  `mcp`, or transcript scan.
- Optional `pyinstaller` / `nuitka` binary for users requiring
  sub-100ms cold paint (deferred beyond v0.1).
- Statusline failures must fail open and render minimal fallback.

## Global install behavior

`eawf global install` prompts the user with a **pre-checked, opt-in**
statusline question, then asks each module individually:

```text
Install Claude Code statusline? [Y/n] (recommended)
Style: compact / normal / rich
Glyphs: auto / ascii / unicode / powerline
Colors: auto / none / ansi16 / ansi256 / truecolor
Enable state scope module? [Y/n]
Enable git module? [Y/n]
Enable model/session/cwd module? [Y/n]
Enable context/token module? [Y/n]
Enable MCP health module? [Y/n]
Enable hook/plugin warning module? [Y/n]
Enable memory freshness module? [Y/n]
Enable token-saving module? [Y/n]
```

A `--no-statusline` flag on `eawf global install` skips the question
and persists `cli.statusline.enabled: false` in `~/.ea/config.yaml`.
When the prompt is accepted, the same config records
`cli.statusline.enabled: true` plus per-module choices. There are no
universal module defaults beyond safe fallback rendering; the install
wizard records user choices.

## Cross-references

- Plugin install / update lifecycle — `docs/architecture/plugins.md`.
- Memory module data source — `docs/architecture/memory.md`.
- Hook events that prewarm the cache — `docs/reference/hook-events.md`.
- `/init` and `/global install` flows — `docs/architecture/installation.md`.
