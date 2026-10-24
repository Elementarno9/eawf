<!-- BEGIN EAWF:managed id=zone-tier0 version=1.0 hash=e0ad14f457862be5 -->
<!-- Zone 1: always-on (tier0) -->
<!-- END EAWF:managed id=zone-tier0 -->
<!-- BEGIN EAWF:managed id=project-orientation version=1.1 hash=8615530e847011f5 -->
## What this repo runs on

This repo is managed by eawf, an agent-driven workflow. Work nests as **Track** (a standing line of work) > **Milestone** (one delivery, one pull request, one release) > **Delivery Batch** (Tasks that land and are verified together) > **Task** (one agent's unit of work), and each Task executes in one **Run** (one lease, one workspace). A plan changes only through a **PlanRevision** that is submitted, approved and applied.

Reality lives in ``.ea/state.json``, not here: this file carries the rules, ``eawf status`` carries the current position.

<!-- END EAWF:managed id=project-orientation -->
<!-- BEGIN EAWF:managed id=non-negotiable-rules version=1.14 hash=14e18bdcbe922c9c -->
## Non-negotiable rules (core)

The rules below apply to every eawf-managed project. Each rule with a non-trivial body has an expansion block immediately following.

1. **CLI is dispatch; library implements.** See ``architecture-cli-dispatch``.
2. **Strict config validation.** Every YAML/JSON ingestion path uses Pydantic v2 ``BaseModel`` with ``ConfigDict(extra="forbid")``. Validation lives in the loader; downstream functions accept already-validated typed objects only.
3. **`.ea/` is committed.** See ``ea-directory-commit-policy``.
4. **Daemon is the sole canonical mutator** for ``state.json``, layered config YAML, registry JSON, event/audit stores, and telemetry DB (Decision D-SUP-01; per-file authority map at ``.ea/artifacts/research/long-term/2026-05-18-authority-map.md``). Reads are free. The eawf entity verbs (``eawf task ...``, ``eawf batch ...``) are the operator-facing surface: they send mutations to the daemon over JSON-RPC, with a fallback to direct ``portalocker`` writes only when the daemon is unavailable (CI, one-shot, recovery shell).
5. **Symbol conventions.** See ``symbol-conventions``.
6. **Deletion rule.** See ``deletion-rule``.
7. **State is in `state.json`, not in specs.** See ``state-vs-specs``.
8. **Verify before claiming.** See ``verify-before-claim``.
9. **f-strings only.** No ``%``-style or ``.format()``. See the python profile for the library-module logging form.
10. **`uv run` for all Python invocations.** See the python profile for details.
11. **Worktree discipline.** See ``worktree-discipline``.
12. **Branch currency.** See ``branch-currency``.
13. **Pre-commit before commit.** ``uv run pre-commit run --all-files`` before every ``git commit``. Hook failures are root-caused, never ``--no-verify``'d.
14. **Commit prefix.** See ``commit-prefix``.
15. **Branch naming.** See ``branch-naming``.
16. **Secrets and PII hygiene.** See ``secrets-hygiene``.
17. **Naming conventions for fields/params/log keys.** See ``naming-conventions``.
18. **Artifact chassis and citations.** See ``artifact-chassis``.
19. **Typed agent reports.** See ``agent-report-contract``.
20. **Planned-scope revisability.** See ``planned-scope-revisability``.
21. **Plan revision procedure.** See ``roadmap-procedure``.
22. **Spike workflow.** See ``spike-workflow``.
23. **Engineering principles (DRY/KISS/YAGNI).** See ``engineering-principles``.
24. **Other engineering practice.** See ``engineering-practice``.
25. **Source code stays clean of design-decision references.** No inline ``# per Q<N>``, ``# per audit XB##``, ``# per Codex``, or roundtable / operator-decision-id comments in committed source files. Decision provenance lives in the commit body and the typed Decision URN in ``state.json``; source comments are reserved for WHY-the-code-does-X explanations that aid future readers irrespective of provenance. Enforced by the ``eawf012`` lint.
26. **Plan approval always renders the plan in plan mode.** See ``prep-plan-mode``.
27. **Milestone acceptance timing.** See ``milestone-close-timing``.
28. **Rendered markdown is not manually line-wrapped.** See ``markdown-no-manual-wrap``.
29. **Release process.** See ``release-process``.
30. **Ship process.** See ``ship-process``.
31. **Comment economy.** See ``comment-economy``.
32. **Decisions are surfaced, not assumed.** See ``orchestrator-decision-surface``.
33. **A new gate proves itself and sunsets.** See ``gate-fire-proof-sunset``.
34. **One commit per Batch and one state commit per push.** See ``commit-granularity``.

<!-- END EAWF:managed id=non-negotiable-rules -->
<!-- BEGIN EAWF:managed id=state-vs-specs version=1.2 hash=b31103e9ebf5191f -->
### State vs specs

Specs describe intent; state reflects reality. The canonical writer of ``state.json`` is the daemon — see rule 4 for the full mutator-authority statement (operator-facing state CLI, JSON-RPC proxy, portalocker fallback). Do not hand-edit ``state.json`` to make it agree with a spec; drive the state mutation and let the spec follow.

<!-- END EAWF:managed id=state-vs-specs -->
<!-- BEGIN EAWF:managed id=worktree-discipline version=1.3 hash=6c2aeaba4a3a8e1c -->
### Worktree discipline

Task worktrees MUST branch from the current Milestone branch HEAD, not ``main``. Their commits are **cherry-picked** into the Milestone branch — never ``git merge``.

Cherry-pick procedure: ``git -C <main-worktree> cherry-pick <worktree-sha>`` per commit, in order. Resolve conflicts in the parent worktree. Worktree teardown only after cherry-pick lands.

Concurrency: which Tasks run side by side is derived from the Batch's Task dependency graph and write claims, never typed at dispatch. A Task is dispatched only once each Task it depends on carries the proof it requires, and two Tasks whose write claims overlap never run at once. One Task, one Run, one lease, one workspace.

<!-- END EAWF:managed id=worktree-discipline -->
<!-- BEGIN EAWF:managed id=prep-plan-mode version=2.0 hash=018e03ea08ec218d -->
### Plan approval always renders the plan in plan mode

Before a PlanRevision is approved, the agent MUST enter Claude Code plan mode (``EnterPlanMode``) with the rendered Batch and Task graph of that revision — its Batches, Tasks, dependencies and criteria, read with ``eawf plan show`` — before surfacing the approve / revise / cancel choice. File that choice with ``eawf question open-decision`` so it is a pending action with typed options, then show the bound question the answer carries: ``AskUserQuestion`` in Claude Code, the answer's numbered prompt printed verbatim in Codex. The operator answers by option, in the session or in the console, and the first answer wins; a free-text approval is not consent.

- **approve** — the operator seals the approval with ``eawf plan approve``, then ``eawf plan apply`` materialises its Batches and Tasks into the Milestone.
- **revise** — the planner proposes a child revision (``/plan propose --feedback``), and its rendered graph is shown again before the next approval.
- **cancel** — nothing is approved or applied.

The plan-mode-first invariant applies to every revision, an append to an ACTIVE Milestone included: the operator-facing surface is always the rendered graph, not raw mutator commands.

<!-- END EAWF:managed id=prep-plan-mode -->
<!-- BEGIN EAWF:managed id=milestone-close-timing version=2.0 hash=da07db2ef93cdaa5 -->
### Milestone acceptance timing

Milestone acceptance is gated on **verification + ship CI + PR review pass**. Do not open a Milestone's acceptance the moment its Batches land — run ``/verify`` in audit and review modes first, then pass the Milestone pull request's CI and review, addressing feedback by appending Batches or Tasks to the same Milestone, then accept it with ``/accept``.

**Append, don't open a second Milestone.** When verification or review surfaces follow-up work that fits the same delivery, append it to the active Milestone through a PlanRevision: an ACTIVE Milestone takes appended Batches and Tasks and stays ACTIVE. Opening another Milestone is reserved for true scope expansions, not for routine follow-ups.

**Acceptance goes in the latest state commit before merge.** Do not accept the Milestone until ship CI is green AND the review-passed branch is on the remote. The acceptance rides the push's single trailing ``state:`` commit; merging that commit ends the Milestone, and a pre-merge acceptance keeps state in sync with what reviewers approved.

<!-- END EAWF:managed id=milestone-close-timing -->
<!-- BEGIN EAWF:managed id=zone-reference version=1.0 hash=06f4f2b046f45f0f -->
<!-- Zone 2: reference (lazy) -->
<!-- END EAWF:managed id=zone-reference -->
<!-- BEGIN EAWF:managed id=architecture-cli-dispatch version=1.0 hash=74b5794d459de4b4 -->
### Architecture: CLI is dispatch; library implements

The CLI layer parses arguments and formats output. All domain logic lives in the library. CLI handlers must accept typed config / state objects, never raw ``dict``.

<!-- END EAWF:managed id=architecture-cli-dispatch -->
<!-- BEGIN EAWF:managed id=ea-directory-commit-policy version=1.0 hash=f86049892662bace -->
### `.ea/` directory: commit policy

``.ea/state.json``, ``.ea/profile.yaml`` and each generation's ``state.json`` and ledgers are committed to version control — they are the source of truth for what was released, delivered and decided. Task definitions are committed too. In-flight Task status and Run rows (each generation's ``local/`` projection), ``.ea/locks/`` and ``.ea/local/`` are gitignored.

<!-- END EAWF:managed id=ea-directory-commit-policy -->
<!-- BEGIN EAWF:managed id=symbol-conventions version=1.2 hash=bc5f3f6424b12567 -->
### Symbol conventions

Project codes follow ``^[A-Z][A-Z0-9_-]{1,15}$``. Task keys: ``<PROJECT>-<NNNN>`` (e.g., ``EAWF-0042``). Milestone, Batch and PlanRevision keys: ``MLS-<NNNN>``, ``BAT-<NNNN>`` and ``PRV-<NNNN>``; Run keys: ``RUN-`` and eight digits; Track keys: ``TRK-<SYMBOL>``. Hypothesis IDs: ``H<NN>-<NN>`` (e.g., ``H03-12``). The zero-padded width is part of each key's grammar, so ``RUN-0001`` is refused.

<!-- END EAWF:managed id=symbol-conventions -->
<!-- BEGIN EAWF:managed id=naming-conventions version=1.7 hash=6744ddeadba994c3 -->
`naming-conventions` — Every cross-cutting concept has exactly one canonical name; rename an outlier to match the dominant form before merging instead of adding an adapter shim. Full text: [docs/rules/naming-conventions.md](docs/rules/naming-conventions.md)
<!-- END EAWF:managed id=naming-conventions -->
<!-- BEGIN EAWF:managed id=entity-title-naming version=1.2 hash=89c107c4724d2bae -->
`entity-title-naming` — Write every entity title as an imperative noun-phrase of at most 72 characters with no trailing period, and put the long-form purpose in the description. Full text: [docs/rules/entity-title-naming.md](docs/rules/entity-title-naming.md)
<!-- END EAWF:managed id=entity-title-naming -->
<!-- BEGIN EAWF:managed id=deletion-rule version=1.0 hash=4198ace6dc0231ae -->
### Deletion rule

Agents MAY delete code, configs, or docs IF AND ONLY IF:

(a) the content has been committed at least once on the current branch's ancestry (recoverable via ``git log -- <path>``); AND

(b) the deletion is motivated by an explicit verdict (rejected hypothesis, superseded design, deprecated module) recorded in ``state.json``; AND

(c) the deletion is enumerated in the commit body or PR description (``Removed: <path1> (reason); ...``) so the reviewer can object before merge.

Agents MUST NOT delete: schema files, golden fixtures, MIT ``LICENSE``, ``CHANGELOG.md``, or any uncommitted file. When in doubt, propose the list and wait for explicit confirmation.

<!-- END EAWF:managed id=deletion-rule -->
<!-- BEGIN EAWF:managed id=verify-before-claim version=1.4 hash=f6f2a8d7c4aa1436 -->
### Verify before claiming

Quantitative or behavioural claims about command I/O, schema fields, exit codes, or rendering output MUST be verified against the actual code path before assertion. The verification ladder, in order:

(a) Resolve the symbol with the symbol tools (``mcp__serena__find_symbol``, ``mcp__serena__find_referencing_symbols``, ``mcp__serena__get_symbols_overview``) — they resolve bindings, so they answer "where is X defined" and "who calls X" without the false hits and missed aliases text search returns.
(b) Read the source file.
(c) ``grep`` for actual call sites in the active branch — the fallback when the target is a string, a config value, or a name you do not yet know.
(d) Inspect golden fixtures or snapshot tests.
(e) Only then quote the behaviour.

Design-intent docs (command matrix, schema inventory, ADRs) are the *design intent*; the source tree is the *implementation* — when they drift, quote the implementation. Treat doc/memory citations as a hypothesis to verify, not as ground truth.

Task commit SHA: a Task's work is the commit carrying its ``Task: <KEY>`` trailer, found with ``git log --grep 'Task: <KEY>'`` so a cherry-picked or squashed commit still resolves, and a landed Batch pins the branch head its Tasks completed on. Quote those, not a remembered SHA.

<!-- END EAWF:managed id=verify-before-claim -->
<!-- BEGIN EAWF:managed id=branch-currency version=1.1 hash=5bbd558e9ba53000 -->
### Branch currency

Before opening or resuming a Milestone, Batch, or Task, verify the current branch is based on the intended source branch (normally the repo default branch or the configured Milestone base). Fetch first, inspect divergence, and rebase or fast-forward the long-running feature branch when it is stale.

If the working tree is dirty, preserve the dirty/untracked work before rebasing. If the branch intentionally remains behind or forked, record the reason in the plan or handoff before dispatching worktrees or starting new commits.

<!-- END EAWF:managed id=branch-currency -->
<!-- BEGIN EAWF:managed id=commit-prefix version=2.4 hash=227fbceccefc9007 -->
`commit-prefix` — A Batch commit is ``<type>: <summary>`` with one ``Batch:`` trailer and a ``Task:`` trailer per Task it delivers; each push ends with one ``state:`` commit. Full text: [docs/rules/commit-prefix.md](docs/rules/commit-prefix.md)
<!-- END EAWF:managed id=commit-prefix -->
<!-- BEGIN EAWF:managed id=branch-naming version=1.1 hash=9848cd8623da04b5 -->
### Branch naming

Long-running Milestone branch: ``feature/<symbol>-v<X.Y>``. Per-Task worktree branches: ``feature/<symbol>-v<X.Y>-<task-key>``, the Task key in lower case — cherry-picked back into the long-running branch then deleted.

PRs: one per Milestone (typical) — a Milestone always ends with a PR.

<!-- END EAWF:managed id=branch-naming -->
<!-- BEGIN EAWF:managed id=secrets-hygiene version=1.0 hash=786969acaab1614a -->
### Secrets and PII hygiene

Never commit local paths, machine-specific identifiers, or sensitive info. Do **not** stage commits, code, docs, or config containing: machine-specific paths (``/Users/<name>/...``, ``~/Workspace/...``, ``C:\Users\...``), hostnames, IPs, employer / customer names, credentials, API keys, tokens, SSH keys, ``.env`` contents, internal URLs, real email addresses other than a canonical author block, or PII.

Companion-doc references in rendered docs / commit messages / PR bodies / docstrings MUST stay repo-relative or generic ("external companion docs", ``docs/...``).

Forward-fix only — once a leak lands in a published commit, history rewrite is the *last* resort because the blast radius (force-push, SHA churn, broken PR refs) is much larger than the prevention cost. Scrub locally before ``git add``; let ``pre-commit`` (``detect-secrets`` + custom path checks) catch the rest.

<!-- END EAWF:managed id=secrets-hygiene -->
<!-- BEGIN EAWF:managed id=artifact-chassis version=1.4 hash=31aec4fd9ed14687 -->
`artifact-chassis` — Markdown promoted out of ``.ea/local/`` uses the renderer-owned Summary / References / Provenance / Scrub chassis, with typed citations and no absolute paths. Full text: [docs/rules/artifact-chassis.md](docs/rules/artifact-chassis.md)
<!-- END EAWF:managed id=artifact-chassis -->
<!-- BEGIN EAWF:managed id=planned-scope-revisability version=1.3 hash=ab0445b7964e122b -->
`planned-scope-revisability` — Scope mutability is status-tiered: a PLANNED Milestone is freely re-planned, an ACTIVE Milestone only takes appended Batches and Tasks, and started or completed work is never rewritten. Full text: [docs/rules/planned-scope-revisability.md](docs/rules/planned-scope-revisability.md)
<!-- END EAWF:managed id=planned-scope-revisability -->
<!-- BEGIN EAWF:managed id=roadmap-procedure version=2.0 hash=8ea04e366358ecde -->
### Plan revision procedure

The canonical flow for changing a Milestone's plan (one PlanRevision at a time):

::

  1. /research <topic>                    # one-page brief under .ea/local/
  2. /plan propose <milestone-ref>        # eawf plan submit: VALIDATED,
                                          # or rejected with findings
  3. /plan propose <milestone-ref> --feedback <ref>
                                          # child revision until the
                                          # Batch and Task graph fits
  4. /plan approve <revision-ref>         # operator only
  5. /plan apply <revision-ref>           # operator only: materialise
                                          # Batches and Tasks
  6. eawf milestone activate <urn>        # PLANNED -> ACTIVE
  7. /dispatch <batch-ref>                # each ready Task in its own Run

Approval and apply are separate operator acts. An agent that reaches one files it with ``eawf question open-decision`` and stops; it never chooses the recommended option itself.

<!-- END EAWF:managed id=roadmap-procedure -->
<!-- BEGIN EAWF:managed id=spike-workflow version=1.6 hash=456fd615dccb63f3 -->
`spike-workflow` — A spike is a time-boxed read-only investigation; its text brief stays local under ``.ea/local/research/``, cited by sha256 digest, and heavy artefacts go under ``local.scratch_root``. Full text: [docs/rules/spike-workflow.md](docs/rules/spike-workflow.md)
<!-- END EAWF:managed id=spike-workflow -->
<!-- BEGIN EAWF:managed id=engineering-principles version=1.1 hash=c6bb250bd2ed16c5 -->
`engineering-principles` — Reach for the simplest design that solves the immediate need: no helper, parameter, or config knob without a present-day caller, and no handling for states that cannot happen. Full text: [docs/rules/engineering-principles.md](docs/rules/engineering-principles.md)
<!-- END EAWF:managed id=engineering-principles -->
<!-- BEGIN EAWF:managed id=engineering-practice version=1.1 hash=8b2b9d6762b50c71 -->
`engineering-practice` — Default to fail-fast at the boundary, one reason to change per unit, parsing separate from validation separate from execution, and explicit over implicit. Full text: [docs/rules/engineering-practice.md](docs/rules/engineering-practice.md)
<!-- END EAWF:managed id=engineering-practice -->
<!-- BEGIN EAWF:managed id=markdown-no-manual-wrap version=1.0 hash=36955a02ca956432 -->
### Rendered markdown is not manually line-wrapped

Rendered and authored markdown — PR bodies, issue/review comments, audit / research / decision artifacts, READMEs, mkdocs pages, and skill output envelopes — is written one line per paragraph. Do NOT hard-wrap prose at ~72 (or any) columns; let the renderer / viewer soft-wrap. Manual wrapping fights diffs (a one-word edit reflows a whole block), breaks tables and list continuations, and corrupts copy-paste.

The ~72-column wrap convention is reserved for **commit messages** (subject + body), where tooling and ``git log`` assume it. Fenced code blocks keep their own formatting. Skill output contracts inherit this rule: a skill that emits markdown emits unwrapped paragraphs.

<!-- END EAWF:managed id=markdown-no-manual-wrap -->
<!-- BEGIN EAWF:managed id=release-process version=2.1 hash=e8a3b534cfd81643 -->
`release-process` — A release-train repo tags a merged Milestone only after ``eawf release preflight`` passes, via ``eawf release pipeline --milestone``; other repos keep their own tag flow. Full text: [docs/rules/release-process.md](docs/rules/release-process.md)
<!-- END EAWF:managed id=release-process -->
<!-- BEGIN EAWF:managed id=ship-process version=1.9 hash=37dd04d0ac8a855f -->
`ship-process` — Ship a Milestone through one PR: pass CI, address review by appending Batches, then merge so each Batch commit survives; main and tags are never rewritten. Full text: [docs/rules/ship-process.md](docs/rules/ship-process.md)
<!-- END EAWF:managed id=ship-process -->
<!-- BEGIN EAWF:managed id=agent-report-contract version=1.0 hash=600b85c26e27f28b -->
### Agent report contract

Every agent session that reaches a terminal handoff MUST emit a typed ``agent_end`` report body accepted by ``AgentReportBody``. Runtime hooks own ``AgentReportHeader`` fields (session, role, scope, runtime, attempt); agents provide the role-specific body only.

Reports are append-only. Never overwrite or "fix" an earlier report attempt; retry by appending the next attempt for the same ``(role, base_id)`` pair.

Verdicts MUST use ``AgentReportVerdict`` exactly: ``pass``, ``pass-with-followups``, ``fail``, or ``blocked``. Report store URNs use the role-specific ``StoreKind`` such as ``executor_report`` or ``reviewer_report``.

<!-- END EAWF:managed id=agent-report-contract -->
<!-- BEGIN EAWF:managed id=workflow-lifecycle version=1.2 hash=9be4d100ccbb9db2 -->
`workflow-lifecycle` — The lifecycle runs research, plan, dispatch Tasks, cherry-pick, ship the Milestone, with a branch-currency check before opening or resuming any scope. Full text: [docs/rules/workflow-lifecycle.md](docs/rules/workflow-lifecycle.md)
<!-- END EAWF:managed id=workflow-lifecycle -->
<!-- BEGIN EAWF:managed id=pr-template version=1.1 hash=53ffbc1a5e53e4d1 -->
### PR template

``## Summary`` (3-6 bullets) + ``## Test plan`` (markdown checklist). Milestone PRs include a ``## Milestone deliverables`` section linking back to the Milestone's applied PlanRevision.

<!-- END EAWF:managed id=pr-template -->
<!-- BEGIN EAWF:managed id=clarity-contract version=1.1 hash=1be368cf10281612 -->
`clarity-contract` — Every newcomer-facing artifact must be understandable without opening ``state.json``: right audience, jargon glossed on first use, motivation stated, scannable, references tabulated. Full text: [docs/rules/clarity-contract.md](docs/rules/clarity-contract.md)
<!-- END EAWF:managed id=clarity-contract -->
<!-- BEGIN EAWF:managed id=agent-tool-discipline version=1.0 hash=6602dedd476abc7a -->
### Agent tool discipline

- Subagents do **not** see the parent conversation. Dispatch prompts must be self-contained — paths, line numbers, success criteria. "Based on your findings, fix the bug" pushes synthesis onto the agent; that work belongs in the parent.
- Do not use ``Read`` to verify a file you just wrote with ``Write``/``Edit``: the edit tool errors on conflict, the harness tracks file state, and re-reads waste tokens.
- Prefer dedicated tools over ``Bash`` when one fits (``Read``, ``Edit``, ``Write``); reserve ``Bash`` for shell-only operations.

<!-- END EAWF:managed id=agent-tool-discipline -->
<!-- BEGIN EAWF:managed id=memory-hygiene version=1.2 hash=0228d6e82ab5bd61 -->
`memory-hygiene` — Remember only facts that stay true across sessions; status is derivable, so query it with ``eawf status`` or ``eawf memory digest`` instead of memorizing it. Full text: [docs/rules/memory-hygiene.md](docs/rules/memory-hygiene.md)
<!-- END EAWF:managed id=memory-hygiene -->
<!-- BEGIN EAWF:managed id=comment-economy version=1.2 hash=d094cbd2800f756a -->
`comment-economy` — Comments carry why, not what: document every parameter, return and raise, but no restated signatures, change-log narration, or lifecycle ids. Full text: [docs/rules/comment-economy.md](docs/rules/comment-economy.md)
<!-- END EAWF:managed id=comment-economy -->
<!-- BEGIN EAWF:managed id=orchestrator-decision-surface version=1.2 hash=7a31e6bd6d6c497f -->
`orchestrator-decision-surface` — Surface every consequential choice as an explicit question with visual option previews, never a silent default. Full text: [docs/rules/orchestrator-decision-surface.md](docs/rules/orchestrator-decision-surface.md)
<!-- END EAWF:managed id=orchestrator-decision-surface -->
<!-- BEGIN EAWF:managed id=gate-fire-proof-sunset version=1.1 hash=81a182623bee3388 -->
`gate-fire-proof-sunset` — A new gate ships with a test proving it reds on a real defect, and sunsets at Milestone acceptance if it never fired. Full text: [docs/rules/gate-fire-proof-sunset.md](docs/rules/gate-fire-proof-sunset.md)
<!-- END EAWF:managed id=gate-fire-proof-sunset -->
<!-- BEGIN EAWF:managed id=commit-granularity version=1.7 hash=0b34ce900665e070 -->
`commit-granularity` — One commit per Batch, or a few for a large Batch, squashed before the push; each push ends with one ``state:`` commit, and the bindings a squash replaced are rebound by superseding rows. Full text: [docs/rules/commit-granularity.md](docs/rules/commit-granularity.md)
<!-- END EAWF:managed id=commit-granularity -->
<!-- BEGIN EAWF:managed id=anti-patterns version=1.3 hash=08565b9ac6767889 -->
## Anti-patterns

- Mutating ``state.json`` outside the daemon (or its entity-verb proxy / portalocker direct-write fallback) — see rule 4.
- Skipping ``extra="forbid"`` on a Pydantic model "just for now".
- Merging worktree branches instead of cherry-picking.
- ``--no-verify`` on a failing pre-commit hook.
- Quoting design-intent docs as authoritative for current behaviour (verify against the source tree).
- Starting a Milestone, Batch, or Task from a stale feature branch.
- Adding a runtime dep without checking it's not already pulled in transitively.
- Using ``Read`` to verify a file you just wrote with ``Write``/``Edit``.
- Carrying audit citations or decision-round IDs into source comments (rule 25 violation; provenance lives in commit body
  + ``state.json`` Decision URN).

<!-- END EAWF:managed id=anti-patterns -->
