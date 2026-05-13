<!-- BEGIN EAWF:managed id=non-negotiable-rules version=1.1 hash=88bf8d6c09ce59b4 -->
## Non-negotiable rules (core)

The rules below apply to every eawf-managed project. Each rule with a
non-trivial body has an expansion block immediately following.

1. **CLI is dispatch; library implements.** See ``architecture-cli-dispatch``.
2. **Strict config validation.** Every YAML/JSON ingestion path uses
   Pydantic v2 ``BaseModel`` with ``ConfigDict(extra="forbid")``. Validation
   lives in the loader; downstream functions accept already-validated typed
   objects only.
3. **`.ea/` is committed.** See ``ea-directory-commit-policy``.
4. **State CLI is the only mutator of `state.json`.** Read access is free;
   mutations go through the state CLI which uses ``portalocker``-backed
   file locking. No direct file writes from skills, agents, or ad-hoc
   scripts.
5. **Symbol conventions.** See ``symbol-conventions``.
6. **Deletion rule.** See ``deletion-rule``.
7. **State is in `state.json`, not in specs.** See ``state-vs-specs``.
8. **Verify before claiming.** See ``verify-before-claim``.
9. **f-strings only.** No ``%``-style or ``.format()``. Library modules use
   ``logger = logging.getLogger(__name__)``.
10. **`uv run` for all Python invocations.** See the python profile for
    details.
11. **Worktree discipline.** See ``worktree-discipline``.
12. **Pre-commit before commit.** ``uv run pre-commit run --all-files``
    before every ``git commit``. Hook failures are root-caused, never
    ``--no-verify``'d.
13. **Commit prefix.** See ``commit-prefix``.
14. **Branch naming.** See ``branch-naming``.
15. **Secrets and PII hygiene.** See ``secrets-hygiene``.
16. **Naming conventions for fields/params/log keys.** See
    ``naming-conventions``.

<!-- END EAWF:managed id=non-negotiable-rules -->
<!-- BEGIN EAWF:managed id=architecture-cli-dispatch version=1.0 hash=7c8769d23177628b -->
### Architecture: CLI is dispatch; library implements

The CLI layer parses arguments and formats output. All domain logic
lives in the library. CLI handlers must accept typed config / state
objects, never raw ``dict``.

<!-- END EAWF:managed id=architecture-cli-dispatch -->
<!-- BEGIN EAWF:managed id=ea-directory-commit-policy version=1.0 hash=bf2799d5f9d4c758 -->
### `.ea/` directory: commit policy

``.ea/state.json`` and ``.ea/profile.yaml`` are committed to version
control — they are the source of truth for project state.
``.ea/locks/`` and ``.ea/local/`` are gitignored.

<!-- END EAWF:managed id=ea-directory-commit-policy -->
<!-- BEGIN EAWF:managed id=symbol-conventions version=1.0 hash=6e661ec30d84006b -->
### Symbol conventions

Project codes / phase IDs follow ``^[A-Z][A-Z0-9_-]{1,15}$``. Hypothesis
IDs: ``H<NN>-<NN>`` (e.g., ``H03-12``). Phase IDs in commits: ``P<NN>``
(zero-padded, e.g., ``P00``, ``P03``). Wave IDs: ``W<NN>`` likewise.

<!-- END EAWF:managed id=symbol-conventions -->
<!-- BEGIN EAWF:managed id=naming-conventions version=1.0 hash=684e66ab6089ae5c -->
### Naming conventions

To prevent drift across state models, envelopes, parameters, and
log keys, every cross-cutting concept has exactly one canonical
name. Outliers MUST be renamed to match the dominant form before
merging, not papered over with adapter shims.

**State scope identifier** — ``scope_id`` (never bare ``scope``).
Applies to Pydantic field names on ``State`` models
(e.g. ``PluginInstall``), :class:`~eawf.render.envelope.EnvelopeHeader`,
function kwargs (e.g. ``add_artifact(scope_id=...)``,
``artifact_urn(scope_id, ...)``), JSON keys on the wire, and
``state.json`` field names. Bare ``scope`` is reserved for CLI
argument names (``--scope``) and skill-context attributes
(``SkillContext.scope``) where the caller maps onto the URN.

**Output directory parameter** — ``output_dir`` (never ``out_dir``
or ``target_dir``). Applies to schema dumpers, plugin installers,
and any helper that takes a write destination directory.

**Wave / iter / phase keys in logs and dict payloads** —
``wave=<id>``, ``iter=<id>``, ``phase=<id>``. Bare keys only,
never ``wave_id=<id>`` in log lines (the trailing ``_id`` is
reserved for typed-model field names where the type system
benefits from explicit suffix). Inside structured envelopes
(``EventPayload``, ``state.json``) keep the ``_id`` suffix so the
schema is unambiguous.

**Log format inside library modules** — ``<funcname> key=value
key=value`` form, space-separated, no leading ``:`` after the
function name. f-strings only (project-wide rule 9). Example:
``logger.info(f"create_worktree wave={wave_id} branch={name!r}")``.

**Error message phrasing** — lowercase leading word, no trailing
period, no class-name prefix. Use ``!r`` when interpolating user
input so quoting is visible. Example:
``raise ValueError(f"unknown wave: {wave_id!r}")``.

**Docstring ``Raises:`` block** — Google-style ``Raises:`` block
with one ``ExceptionType: explanation`` line per case. Do NOT
use inline prose like ``Raises ValueError if ...`` in the
summary; reserve the ``Raises:`` block for that.

<!-- END EAWF:managed id=naming-conventions -->
<!-- BEGIN EAWF:managed id=deletion-rule version=1.0 hash=bd0cf5251c37fe42 -->
### Deletion rule

Agents MAY delete code, configs, or docs IF AND ONLY IF:

(a) the content has been committed at least once on the current
    branch's ancestry (recoverable via ``git log -- <path>``); AND
(b) the deletion is motivated by an explicit verdict (rejected
    hypothesis, superseded design, deprecated module) recorded in
    ``state.json``; AND
(c) the deletion is enumerated in the commit body or PR description
    (``Removed: <path1> (reason); ...``) so the reviewer can object
    before merge.

Agents MUST NOT delete: schema files, golden fixtures, MIT
``LICENSE``, ``CHANGELOG.md``, or any uncommitted file. When in doubt,
propose the list and wait for explicit confirmation.

<!-- END EAWF:managed id=deletion-rule -->
<!-- BEGIN EAWF:managed id=state-vs-specs version=1.0 hash=d693976daac13b8a -->
### State vs specs

Specs describe intent; state reflects reality. ``uv run eawf state ...``
is the only writer of ``state.json``.

<!-- END EAWF:managed id=state-vs-specs -->
<!-- BEGIN EAWF:managed id=verify-before-claim version=1.0 hash=654293f625317c78 -->
### Verify before claiming

Quantitative or behavioural claims about command I/O, schema fields,
exit codes, or rendering output MUST be verified against the actual
code path before assertion. The verification ladder, in order:

(a) Read the source file.
(b) ``grep`` for actual call sites in the active branch.
(c) Inspect golden fixtures or snapshot tests.
(d) Only then quote the behaviour.

Design-intent docs (command matrix, schema inventory, ADRs) are the
*design intent*; the source tree is the *implementation* — when they
drift, quote the implementation. Treat doc/memory citations as a
hypothesis to verify, not as ground truth.

<!-- END EAWF:managed id=verify-before-claim -->
<!-- BEGIN EAWF:managed id=worktree-discipline version=1.0 hash=ce39ac42bf65075d -->
### Worktree discipline

Worktree subagents MUST branch from the current feature branch HEAD,
not ``main``. Their commits are **cherry-picked** into the parent
feature branch — never ``git merge``.

Cherry-pick procedure: ``git -C <main-worktree> cherry-pick
<worktree-sha>`` per commit, in order. Resolve conflicts in the
parent worktree. Worktree teardown only after cherry-pick lands.

<!-- END EAWF:managed id=worktree-discipline -->
<!-- BEGIN EAWF:managed id=commit-prefix version=1.0 hash=8d880a9c124b84bf -->
### Commit prefix

``[P<NN>[-W<NN>]] <type>: <summary>`` — types: ``feat``, ``fix``,
``chore``, ``docs``, ``refactor``, ``test``, ``build``, ``perf``,
``ci``, ``revert``. Use ``[CORE]`` for cross-phase work. ``-W<NN>``
is mandatory when the commit is a planned wave deliverable.

Body: 3-6 bullets on what changed and why. Trailer: a recognized
Claude or Codex ``Co-Authored-By`` trailer.

<!-- END EAWF:managed id=commit-prefix -->
<!-- BEGIN EAWF:managed id=branch-naming version=1.0 hash=cbc58632710aa3c0 -->
### Branch naming

Long-running phase-bundled branch: ``feature/<symbol>-v<X.Y>``.
Per-wave worktree branches: ``feature/<symbol>-v<X.Y>-pNN-wMM`` —
cherry-picked back into the long-running branch then deleted.

PRs: one per phase (typical) — phase always ends with a PR.

<!-- END EAWF:managed id=branch-naming -->
<!-- BEGIN EAWF:managed id=secrets-hygiene version=1.0 hash=1e2d9173c5e3b5a3 -->
### Secrets and PII hygiene

Never commit local paths, machine-specific identifiers, or sensitive
info. Do **not** stage commits, code, docs, or config containing:
machine-specific paths (``/Users/<name>/...``, ``~/Workspace/...``,
``C:\Users\...``), hostnames, IPs, employer / customer names,
credentials, API keys, tokens, SSH keys, ``.env`` contents, internal
URLs, real email addresses other than a canonical author block, or
PII.

Companion-doc references in rendered docs / commit messages /
PR bodies / docstrings MUST stay repo-relative or generic
("external companion docs", ``docs/...``).

Forward-fix only — once a leak lands in a published commit, history
rewrite is the *last* resort because the blast radius (force-push,
SHA churn, broken PR refs) is much larger than the prevention cost.
Scrub locally before ``git add``; let ``pre-commit``
(``detect-secrets`` + custom path checks) catch the rest.

<!-- END EAWF:managed id=secrets-hygiene -->
<!-- BEGIN EAWF:managed id=workflow-lifecycle version=1.0 hash=6bdda55c129b2402 -->
## Workflow lifecycle

Agent-driven lifecycle:

```
research → plan → execute waves → cherry-pick → ship phase
```

- **Research** is unstructured exploration of the proposal/plan.
- **Plan** = open the next phase, enumerate waves, write per-wave
  success criteria.
- **Execute** = dispatch waves. Independent waves go in parallel via
  worktree-isolated subagents. Sequential waves run inline.
- **Cherry-pick** = bring worktree commits into the long-running
  feature branch. Never merge.
- **Ship** = open the phase PR, run CI, address review, merge.

<!-- END EAWF:managed id=workflow-lifecycle -->
<!-- BEGIN EAWF:managed id=pr-template version=1.0 hash=737c0c4f7165c3b9 -->
### PR template

``## Summary`` (3-6 bullets) + ``## Test plan`` (markdown checklist).
Phase PRs include a ``## Phase deliverables`` section linking back to
the per-phase plan.

<!-- END EAWF:managed id=pr-template -->
<!-- BEGIN EAWF:managed id=agent-tool-discipline version=1.0 hash=1da57c2a9fd16da0 -->
### Agent tool discipline

- Subagents do **not** see the parent conversation. Dispatch prompts
  must be self-contained — paths, line numbers, success criteria.
  "Based on your findings, fix the bug" pushes synthesis onto the
  agent; that work belongs in the parent.
- Do not use ``Read`` to verify a file you just wrote with
  ``Write``/``Edit``: the edit tool errors on conflict, the harness
  tracks file state, and re-reads waste tokens.
- Prefer dedicated tools over ``Bash`` when one fits (``Read``,
  ``Edit``, ``Write``); reserve ``Bash`` for shell-only operations.

<!-- END EAWF:managed id=agent-tool-discipline -->
<!-- BEGIN EAWF:managed id=anti-patterns version=1.0 hash=62752fcd572bcbcf -->
## Anti-patterns

- Mutating ``state.json`` outside the state CLI.
- Skipping ``extra="forbid"`` on a Pydantic model "just for now".
- Merging worktree branches instead of cherry-picking.
- ``--no-verify`` on a failing pre-commit hook.
- Quoting design-intent docs as authoritative for current behaviour
  (verify against the source tree).
- Adding a runtime dep without checking it's not already pulled in
  transitively.
- Using ``Read`` to verify a file you just wrote with
  ``Write``/``Edit``.

<!-- END EAWF:managed id=anti-patterns -->
