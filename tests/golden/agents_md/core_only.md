<!-- BEGIN EAWF:managed id=zone-tier0 version=1.0 hash=e0ad14f457862be5 -->
<!-- Zone 1: always-on (tier0) -->
<!-- END EAWF:managed id=zone-tier0 -->
<!-- BEGIN EAWF:managed id=non-negotiable-rules version=1.10 hash=047a6bb707dc471c -->
## Non-negotiable rules (core)

The rules below apply to every eawf-managed project. Each rule with a non-trivial body has an expansion block immediately following.

1. **CLI is dispatch; library implements.** See ``architecture-cli-dispatch``.
2. **Strict config validation.** Every YAML/JSON ingestion path uses Pydantic v2 ``BaseModel`` with ``ConfigDict(extra="forbid")``. Validation lives in the loader; downstream functions accept already-validated typed objects only.
3. **`.ea/` is committed.** See ``ea-directory-commit-policy``.
4. **Daemon is the sole canonical mutator** for ``state.json``, layered config YAML, registry JSON, event/audit stores, and telemetry DB (per Decision D-SUP-01 + the per-file authority map at ``.ea/artifacts/research/long-term/2026-05-18-authority-map.md``). Read access stays free. During v0.3-v0.5 the state CLI (``uv run eawf state ...``) remains the operator-facing surface; it proxies mutations to the daemon via JSON-RPC and falls back to direct ``portalocker`` writes only when the daemon is unavailable (CI / one-shot / recovery shell per V1). The three legacy writers (state-CLI direct, layered-config writer, registry writer) migrate into daemon internals during the v0.4 hygiene wave.
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
21. **Roadmap procedure.** See ``roadmap-procedure``.
22. **Spike workflow.** See ``spike-workflow``.
23. **Engineering principles (DRY/KISS/YAGNI).** See ``engineering-principles``.
24. **Other engineering practice.** See ``engineering-practice``.
25. **Source code stays clean of design-decision references.** No inline ``# per Q<N>``, ``# per audit XB##``, ``# per Codex``, or roundtable / operator-decision-id comments in committed source files. Decision provenance lives in the commit body and the typed Decision URN in ``state.json``; source comments are reserved for WHY-the-code-does-X explanations that aid future readers irrespective of provenance. Enforced by the ``eawf012`` lint.
26. **/prep always renders the DAG in plan mode.** See ``prep-plan-mode``.
27. **Iter and phase close timing.** See ``iter-phase-close-timing``.
28. **Rendered markdown is not manually line-wrapped.** See ``markdown-no-manual-wrap``.
29. **Release process.** See ``release-process``.
30. **Ship process.** See ``ship-process``.

<!-- END EAWF:managed id=non-negotiable-rules -->
<!-- BEGIN EAWF:managed id=state-vs-specs version=1.2 hash=b31103e9ebf5191f -->
### State vs specs

Specs describe intent; state reflects reality. The canonical writer of ``state.json`` is the daemon — see rule 4 for the full mutator-authority statement (operator-facing state CLI, JSON-RPC proxy, portalocker fallback). Do not hand-edit ``state.json`` to make it agree with a spec; drive the state mutation and let the spec follow.

<!-- END EAWF:managed id=state-vs-specs -->
<!-- BEGIN EAWF:managed id=worktree-discipline version=1.1 hash=74e41b8e1dbda664 -->
### Worktree discipline

Worktree subagents MUST branch from the current feature branch HEAD, not ``main``. Their commits are **cherry-picked** into the parent feature branch — never ``git merge``.

Cherry-pick procedure: ``git -C <main-worktree> cherry-pick <worktree-sha>`` per commit, in order. Resolve conflicts in the parent worktree. Worktree teardown only after cherry-pick lands.

Claim order (P19-W02): ``eawf wave claim`` enforces deps + W## monotonic ordering. Each claim rejects when (a) any wave in ``.deps`` is not CLOSED, or (b) a lower-numbered sibling wave under the same iter is still PENDING with its own deps already satisfied. Parallel-worktree dispatch where multiple siblings of the same dep frontier are claimed at once MUST pass ``--out-of-order`` on each claim to opt out of the gate.

<!-- END EAWF:managed id=worktree-discipline -->
<!-- BEGIN EAWF:managed id=prep-plan-mode version=1.0 hash=390f282fbb12944f -->
### /prep always renders the DAG in plan mode

Both Case A and Case B of ``/prep`` MUST enter Claude Code plan mode (``EnterPlanMode``) with the rendered wave DAG of the target phase's current iter before surfacing the approve / edit / cancel ``AskUserQuestion``. Free-text approvals are forbidden per the project-wide ``AskUserQuestion``-only approval policy.

**Case A — PLANNED phase with at least one PENDING wave.**
Render the plan via ``eawf roadmap show --phase <id> --md`` → ``EnterPlanMode`` → ``AskUserQuestion`` (``use-as-is`` / ``revise`` / ``replace`` / ``cancel``). On ``revise``, hand back to ``/roadmap revise``; on ``replace``, hand back to ``/roadmap drop`` + ``/roadmap propose``.

**Case B — PLANNED phase with empty wave DAG.** Apply the planner's emitted ``eawf roadmap revise --add-wave`` commands **first** (waves land as PENDING on the still-PLANNED iter), then render the resulting DAG via ``eawf roadmap show --phase <id> --md`` → ``EnterPlanMode`` → ``AskUserQuestion`` (``approve`` / ``edit`` / ``cancel``). The operator reviews the rendered roadmap, not the planner's raw commands. Edits during plan mode are ``/roadmap revise`` calls (PLANNED scope is mutable). On ``approve``, run ``eawf phase activate <id>`` (V11 hard gate).

The plan-mode-first invariant applies to any future ``/prep`` cases (e.g. mid-flight scope expansion of an ACTIVE iter): the operator-facing surface is always the rendered DAG, not raw mutator commands.

<!-- END EAWF:managed id=prep-plan-mode -->
<!-- BEGIN EAWF:managed id=iter-phase-close-timing version=1.1 hash=67c5ac9feba039ca -->
### Iter and phase close timing

Iter close is gated on **audit + polish + ship CI + PR review pass**. Do not close an iter the moment its waves finish — run ``/audit`` and ``/polish`` first, then ``/ship`` (which runs the PR review pass + addresses feedback by appending waves to the same iter), then close.

**Append, don't open a second iter.** When ``/audit`` or ``/polish`` surfaces follow-up work that fits the same delivery, append waves to the current iter via ``eawf roadmap revise --add-wave`` (ACTIVE-phase ``add_wave_plan`` keeps the iter ACTIVE and the new waves land PENDING). Opening a second iter under the same phase is reserved for true scope expansions or repair cycles per decision D17 (iter-bump triggers), not for routine follow-ups.

**Phase close goes in the latest commit before merge.** Do not close the phase until ship CI is green AND the review-passed branch is on the remote. The phase-close mutation rides in a single ``[P<NN>] state: close iter + phase (audit=<id>)`` commit that bundles iter close + phase close. Merging that commit ends the phase; pre-merge close keeps ``state.json`` in sync with what reviewers approved.

<!-- END EAWF:managed id=iter-phase-close-timing -->
<!-- BEGIN EAWF:managed id=zone-reference version=1.0 hash=06f4f2b046f45f0f -->
<!-- Zone 2: reference (lazy) -->
<!-- END EAWF:managed id=zone-reference -->
<!-- BEGIN EAWF:managed id=architecture-cli-dispatch version=1.0 hash=74b5794d459de4b4 -->
### Architecture: CLI is dispatch; library implements

The CLI layer parses arguments and formats output. All domain logic lives in the library. CLI handlers must accept typed config / state objects, never raw ``dict``.

<!-- END EAWF:managed id=architecture-cli-dispatch -->
<!-- BEGIN EAWF:managed id=ea-directory-commit-policy version=1.0 hash=5c10976f2c665b91 -->
### `.ea/` directory: commit policy

``.ea/state.json`` and ``.ea/profile.yaml`` are committed to version control — they are the source of truth for project state. ``.ea/locks/`` and ``.ea/local/`` are gitignored.

<!-- END EAWF:managed id=ea-directory-commit-policy -->
<!-- BEGIN EAWF:managed id=symbol-conventions version=1.1 hash=aa4eba138c4bd39e -->
### Symbol conventions

Project codes / phase IDs follow ``^[A-Z][A-Z0-9_-]{1,15}$``. Hypothesis IDs: ``H<NN>-<NN>`` (e.g., ``H03-12``). Phase IDs in commits: ``P<NN+>`` (zero-padded, two-or-more digits, e.g., ``P00``, ``P03``, ``P100``). Iter IDs: ``I<NN+>`` and wave IDs: ``W<NN+>`` likewise. The ``\d{2,}`` width matches ``tools/commit_prefix_lint.py`` so 3-digit ids land cleanly once the queue grows past ``P99`` / ``I99`` / ``W99``.

<!-- END EAWF:managed id=symbol-conventions -->
<!-- BEGIN EAWF:managed id=naming-conventions version=1.5 hash=6744ddeadba994c3 -->
`naming-conventions` — Every cross-cutting concept has exactly one canonical name; rename an outlier to match the dominant form before merging instead of adding an adapter shim. Full text: [docs/rules/naming-conventions.md](docs/rules/naming-conventions.md)
<!-- END EAWF:managed id=naming-conventions -->
<!-- BEGIN EAWF:managed id=entity-title-naming version=1.1 hash=89c107c4724d2bae -->
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
<!-- BEGIN EAWF:managed id=verify-before-claim version=1.1 hash=50c978e307bc060d -->
### Verify before claiming

Quantitative or behavioural claims about command I/O, schema fields, exit codes, or rendering output MUST be verified against the actual code path before assertion. The verification ladder, in order:

(a) Read the source file.
(b) ``grep`` for actual call sites in the active branch.
(c) Inspect golden fixtures or snapshot tests.
(d) Only then quote the behaviour.

Design-intent docs (command matrix, schema inventory, ADRs) are the *design intent*; the source tree is the *implementation* — when they drift, quote the implementation. Treat doc/memory citations as a hypothesis to verify, not as ground truth.

Wave commit SHA: ``Wave.commit`` is an optional ``ShaStr`` field on the state model — set by ``eawf wave close --commit <ref>`` when the operator pins a SHA at close time. Quote the SHA via ``eawf wave show --commit <wave-id>``, which prefers the pinned ``Wave.commit`` and falls back to deriving via ``git log --grep '[P##-W##]'`` so cherry-picked or unpinned waves still resolve.

<!-- END EAWF:managed id=verify-before-claim -->
<!-- BEGIN EAWF:managed id=branch-currency version=1.0 hash=f38baee54406c2d2 -->
### Branch currency

Before opening or resuming a phase, iter, or wave, verify the current branch is based on the intended source branch (normally the repo default branch or the configured phase base). Fetch first, inspect divergence, and rebase or fast-forward the long-running feature branch when it is stale.

If the working tree is dirty, preserve the dirty/untracked work before rebasing. If the branch intentionally remains behind or forked, record the reason in the plan or handoff before dispatching worktrees or starting new commits.

<!-- END EAWF:managed id=branch-currency -->
<!-- BEGIN EAWF:managed id=commit-prefix version=1.6 hash=57a081c4d4260873 -->
`commit-prefix` — Subjects are ``[P<NN>(-I<NN>)?(-W<NN>)?] <type>: <summary>`` with type from feat|fix|chore|docs|refactor|test|build|perf|ci|revert|state, a 3-6 bullet body, and a bracket-free bare subject only while no phase is ACTIVE. Full text: [docs/rules/commit-prefix.md](docs/rules/commit-prefix.md)
<!-- END EAWF:managed id=commit-prefix -->
<!-- BEGIN EAWF:managed id=branch-naming version=1.0 hash=8251a99a4f2ce095 -->
### Branch naming

Long-running phase-bundled branch: ``feature/<symbol>-v<X.Y>``. Per-wave worktree branches: ``feature/<symbol>-v<X.Y>-pNN-wMM`` — cherry-picked back into the long-running branch then deleted.

PRs: one per phase (typical) — phase always ends with a PR.

<!-- END EAWF:managed id=branch-naming -->
<!-- BEGIN EAWF:managed id=secrets-hygiene version=1.0 hash=786969acaab1614a -->
### Secrets and PII hygiene

Never commit local paths, machine-specific identifiers, or sensitive info. Do **not** stage commits, code, docs, or config containing: machine-specific paths (``/Users/<name>/...``, ``~/Workspace/...``, ``C:\Users\...``), hostnames, IPs, employer / customer names, credentials, API keys, tokens, SSH keys, ``.env`` contents, internal URLs, real email addresses other than a canonical author block, or PII.

Companion-doc references in rendered docs / commit messages / PR bodies / docstrings MUST stay repo-relative or generic ("external companion docs", ``docs/...``).

Forward-fix only — once a leak lands in a published commit, history rewrite is the *last* resort because the blast radius (force-push, SHA churn, broken PR refs) is much larger than the prevention cost. Scrub locally before ``git add``; let ``pre-commit`` (``detect-secrets`` + custom path checks) catch the rest.

<!-- END EAWF:managed id=secrets-hygiene -->
<!-- BEGIN EAWF:managed id=artifact-chassis version=1.2 hash=44b3e142a036a29a -->
`artifact-chassis` — Durable research, plan, audit, decision, hypothesis, and incident markdown uses the renderer-owned Summary / References / Provenance / Scrub chassis, with dense citations backed by typed rows and no absolute local paths. Full text: [docs/rules/artifact-chassis.md](docs/rules/artifact-chassis.md)
<!-- END EAWF:managed id=artifact-chassis -->
<!-- BEGIN EAWF:managed id=planned-scope-revisability version=1.1 hash=d2ec84e7cc93e285 -->
`planned-scope-revisability` — Scope mutability is status-tiered: PLANNED scope is freely editable, ACTIVE scope is append-only with PENDING-only wave edits, and CLOSED scope changes only via a reopen. Full text: [docs/rules/planned-scope-revisability.md](docs/rules/planned-scope-revisability.md)
<!-- END EAWF:managed id=planned-scope-revisability -->
<!-- BEGIN EAWF:managed id=roadmap-procedure version=1.0 hash=a44fa58c7863325d -->
### Roadmap procedure

The canonical flow for altering the roadmap (one phase at a time):

::

  1. /research <topic> [--final]
  2. /research <topic2> --final            # optional more briefs
  3. /roadmap propose --phase P<NN> [--from-briefs RES-...]
                                            # status=needs_user envelope
                                            # Claude: plan-mode + AUQ
                                            # Codex: text-prompt + y/N
  4. /roadmap revise P<NN> --add-wave ...   # add waves until DAG fits
  5. /roadmap revise P<NN> --set-deps ...   # iterate on deps
  6. /roadmap apply P<NN>                   # confirm PLANNED scope
  7. /prep P<NN>                            # activate_phase
                                            # runs V11 hard gate
                                            # dispatches waves per DAG

Bulk propose (``--bulk --from-briefs RES-12,RES-13,...``) is deferred to a follow-up phase; P19 ships phase-at-a-time only. ``/roadmap reorder`` is also deferred — operator drops + re- proposes to swap order.

<!-- END EAWF:managed id=roadmap-procedure -->
<!-- BEGIN EAWF:managed id=spike-workflow version=1.3 hash=9fe63d701295e8dc -->
`spike-workflow` — A spike is a time-boxed read-only investigation whose brief lands under ``.ea/local/`` and feeds the next roadmap proposal or wave claim; promote it only when it ratifies a verdict. Full text: [docs/rules/spike-workflow.md](docs/rules/spike-workflow.md)
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
<!-- BEGIN EAWF:managed id=release-process version=1.1 hash=e9197d40232446a3 -->
`release-process` — Releases are opt-in per repo via the release cadence setting; the per-phase cadence gates phase close on a changelog section, a version bump, a migration note, and the release annotation. Full text: [docs/rules/release-process.md](docs/rules/release-process.md)
<!-- END EAWF:managed id=release-process -->
<!-- BEGIN EAWF:managed id=ship-process version=1.1 hash=79ff416238f54342 -->
`ship-process` — Ship rides the phase-co-closing iter: open the one phase PR, pass CI, address review by appending waves to that same iter, then close and merge with rebase. Full text: [docs/rules/ship-process.md](docs/rules/ship-process.md)
<!-- END EAWF:managed id=ship-process -->
<!-- BEGIN EAWF:managed id=agent-report-contract version=1.0 hash=600b85c26e27f28b -->
### Agent report contract

Every agent session that reaches a terminal handoff MUST emit a typed ``agent_end`` report body accepted by ``AgentReportBody``. Runtime hooks own ``AgentReportHeader`` fields (session, role, scope, runtime, attempt); agents provide the role-specific body only.

Reports are append-only. Never overwrite or "fix" an earlier report attempt; retry by appending the next attempt for the same ``(role, base_id)`` pair.

Verdicts MUST use ``AgentReportVerdict`` exactly: ``pass``, ``pass-with-followups``, ``fail``, or ``blocked``. Report store URNs use the role-specific ``StoreKind`` such as ``executor_report`` or ``reviewer_report``.

<!-- END EAWF:managed id=agent-report-contract -->
<!-- BEGIN EAWF:managed id=workflow-lifecycle version=1.1 hash=71c70a630057d4b3 -->
`workflow-lifecycle` — The lifecycle runs research, plan, execute waves, cherry-pick, ship phase, with a branch-currency check before opening or resuming any scope. Full text: [docs/rules/workflow-lifecycle.md](docs/rules/workflow-lifecycle.md)
<!-- END EAWF:managed id=workflow-lifecycle -->
<!-- BEGIN EAWF:managed id=pr-template version=1.0 hash=d92fc15954e9e6e0 -->
### PR template

``## Summary`` (3-6 bullets) + ``## Test plan`` (markdown checklist). Phase PRs include a ``## Phase deliverables`` section linking back to the per-phase plan.

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
<!-- BEGIN EAWF:managed id=memory-hygiene version=1.1 hash=0228d6e82ab5bd61 -->
`memory-hygiene` — Remember only facts that stay true across sessions; status is derivable, so query it with ``eawf status`` or ``eawf memory digest`` instead of memorizing it. Full text: [docs/rules/memory-hygiene.md](docs/rules/memory-hygiene.md)
<!-- END EAWF:managed id=memory-hygiene -->
<!-- BEGIN EAWF:managed id=anti-patterns version=1.2 hash=d92fc4c83b8d1338 -->
## Anti-patterns

- Mutating ``state.json`` outside the daemon (or its state-CLI proxy / portalocker direct-write fallback) — see rule 4.
- Skipping ``extra="forbid"`` on a Pydantic model "just for now".
- Merging worktree branches instead of cherry-picking.
- ``--no-verify`` on a failing pre-commit hook.
- Quoting design-intent docs as authoritative for current behaviour (verify against the source tree).
- Starting a phase, iter, or wave from a stale feature branch.
- Adding a runtime dep without checking it's not already pulled in transitively.
- Using ``Read`` to verify a file you just wrote with ``Write``/``Edit``.
- Carrying audit citations or decision-round IDs into source comments (rule 25 violation; provenance lives in commit body
  + ``state.json`` Decision URN).

<!-- END EAWF:managed id=anti-patterns -->
