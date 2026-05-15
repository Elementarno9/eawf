# Co-author trailer architecture

*How `eawf coauthor resolve` derives the `Co-Authored-By` trailer from
layered VCS config, and how to verify the trailer survives an end-to-end
commit.*

The eawf framework attaches a `Co-Authored-By:` trailer to every commit
it authors. The trailer is derived deterministically from the merged
`.ea/config.yaml` (layered global → workspace → repo → local overlays),
not from the runtime's environment alone. This keeps the trailer
reproducible across machines and runtimes, and lets the project pin a
project-owned identity when desired.

## Configuration surface

The `vcs.coauthor` config block (validated by
`eawf.vcs.coauthor.CoauthorConfig`) carries four knobs:

| Key | Type | Default | Notes |
|---|---|---|---|
| `mode` | `runtime` / `project` / `disabled` | `runtime` | Resolution strategy. |
| `default_runtime` | string | `claude` | Used when `mode=runtime` and no override is supplied. |
| `project` | `CoauthorIdentity` or `null` | `null` | Required when `mode=project`. |
| `trailers` | mapping of runtime id → identity | `{claude, codex}` | Registered runtime identities. |
| `require_trailer` | bool | `true` | If true, missing runtime identity raises `CoauthorPolicyError`. |

The runtime registry maps a small set of aliases to canonical runtime
ids (`anthropic` → `claude`, `claude-code` → `claude`, `codex-cli` →
`codex`, `openai` → `codex`). Keys in `trailers` are normalised against
the same alias table so config can use the human-readable spelling.

## Resolution algorithm

`resolve_coauthor_trailer` (`src/eawf/vcs/coauthor.py`) executes the
following ladder:

1. **`mode=disabled`** — return `None`. If `message_text` is supplied
   and contains any `Co-Authored-By:` line, raise `CoauthorPolicyError`
   so a stray trailer cannot slip through a disabled-mode commit.
2. **`mode=project`** — return `config.project.trailer()`.
3. **`mode=runtime`** — pick the runtime id by priority:
   - explicit `--runtime` flag,
   - `EAWF_COAUTHOR_RUNTIME` / `EAWF_COAUTHOR_HARNESS` env override,
   - environment heuristic (any `CLAUDE*` env var → `claude`,
     any `CODEX*` env var → `codex`),
   - `config.default_runtime`.

   Look up the canonical runtime in `config.trailers`. If present,
   return `identity.trailer()`. If absent and `require_trailer` is
   `true`, raise `CoauthorPolicyError`; otherwise return `None`.

For the default config and runtime `claude`, the trailer is:

```
Co-Authored-By: Claude <noreply@anthropic.com>
```

For runtime `codex` it is `Co-Authored-By: Codex <noreply@openai.com>`.

## CLI surface

```bash
eawf coauthor resolve                       # uses default_runtime
eawf coauthor resolve --runtime codex       # explicit runtime override
eawf coauthor resolve --message-file PATH   # disabled-mode policy check
eawf --json coauthor resolve                # envelope mode for hook callers
```

The `--json` envelope body is:

```json
{
  "mode": "runtime",
  "runtime": "claude",
  "trailer": "Co-Authored-By: Claude <noreply@anthropic.com>",
  "required": true
}
```

Non-zero exit codes:

- `4` — disabled mode rejected a message that already carries a trailer,
  or runtime mode could not resolve an identity with `require_trailer`.

## Claude Code settings interaction

Claude Code does not own the coauthor trailer — eawf does. Claude Code
settings live in `~/.claude/settings.json` (user scope) and
`<repo>/.claude/settings.json` (repo scope, managed by
`eawf plugin install claude`). The repo-scope `settings.json` is patched
with an `__eawf_managed` namespace; hand-edits inside the managed region
trigger `IntegrityViolation` on `eawf plugin update claude --check`.

The trailer is wired in by the runtime hook router, which calls
`eawf coauthor resolve --json` and appends the returned `trailer` to
the commit message that Claude Code is about to author. The trailer
itself is invariant to which Claude Code settings the user has chosen,
which is why the manual verification step below uses a benign settings
edit as the trigger.

## Manual verification procedure

The integration test
`tests/integration/test_coauthor_verification.py` covers the runtime
path in a tmp workspace. The manual verification step exercises a real
commit on a real branch:

1. **Make a benign Claude Code settings tweak.** Open
   `~/.claude/settings.json` (or the repo-scope `.claude/settings.json`
   if you are verifying the repo path) and toggle a harmless field —
   for example, flip a `theme` value or add a comment-only entry.
   Avoid touching anything inside an `__eawf_managed` block; that
   region is render-owned and a hand-edit will surface as a drift
   error on the next `eawf plugin update claude --check`.
2. **Stage and commit.** From the repo root, run:

   ```bash
   git add <the-settings-file>
   git commit -m "[P##-W##] chore: settings tweak for coauthor check"
   ```

   The runtime hook calls `eawf coauthor resolve` while the commit
   message is being composed, so the trailer is appended automatically.
3. **Confirm the trailer.** Inspect the new commit:

   ```bash
   git log -1 --format=%B
   ```

   The bottom of the message should include the line

   ```
   Co-Authored-By: Claude <noreply@anthropic.com>
   ```

   (or the codex equivalent, depending on the active runtime).

4. **Revert.** Drop the benign settings tweak (`git revert` or simply
   reset the working tree if the commit was a throwaway probe). The
   only artefact of interest is the captured trailer line — there is
   no state mutation to roll back.

If the trailer is missing, the most likely causes are: (a) the runtime
hook is not registered (`eawf plugin install claude` was not run for
this repo), (b) `vcs.coauthor.mode` was overridden to `disabled` in a
higher-priority config layer, or (c) the runtime registry has been
edited so the active runtime has no configured identity. The
deterministic check is:

```bash
eawf --json coauthor resolve --runtime claude
```

If that command emits the canonical trailer, the policy layer is
healthy and the missing-trailer symptom lives in the hook wiring, not
in the resolver.

## Cross-references

- CLI surface table — `docs/architecture/cli-surface.md`.
- Layered config loader — `src/eawf/config/layered.py`.
- Plugin install / `__eawf_managed` namespace — `docs/architecture/plugins.md`.
- Verification ladder convention — `AGENTS.md` (verify-before-claiming).
