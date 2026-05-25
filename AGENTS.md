<!-- BEGIN EAWF:managed id=non-negotiable-rules version=1.8 hash=e45337c787a792be -->
## Non-negotiable rules (core)

The rules below apply to every eawf-managed project. Each rule with a
non-trivial body has an expansion block immediately following.

1. **CLI is dispatch; library implements.** See ``architecture-cli-dispatch``.
2. **Strict config validation.** Every YAML/JSON ingestion path uses
   Pydantic v2 ``BaseModel`` with ``ConfigDict(extra="forbid")``. Validation
   lives in the loader; downstream functions accept already-validated typed
   objects only.
3. **`.ea/` is committed.** See ``ea-directory-commit-policy``.
4. **Daemon is the sole canonical mutator** for ``state.json``, layered
   config YAML, registry JSON, event/audit stores, and telemetry DB
   (per Decision D-SUP-01 + the per-file authority map at
   ``.ea/artifacts/research/long-term/2026-05-18-authority-map.md``).
   Read access stays free. During v0.3-v0.5 the state CLI
   (``uv run eawf state ...``) remains the operator-facing surface; it
   proxies mutations to the daemon via JSON-RPC and falls back to direct
   ``portalocker`` writes only when the daemon is unavailable
   (CI / one-shot / recovery shell per V1). The three legacy writers
   (state-CLI direct, layered-config writer, registry writer) migrate
   into daemon internals during the v0.4 hygiene wave.
5. **Symbol conventions.** See ``symbol-conventions``.
6. **Deletion rule.** See ``deletion-rule``.
7. **State is in `state.json`, not in specs.** See ``state-vs-specs``.
8. **Verify before claiming.** See ``verify-before-claim``.
9. **f-strings only.** No ``%``-style or ``.format()``. See the python
   profile for the library-module logging form.
10. **`uv run` for all Python invocations.** See the python profile for
    details.
11. **Worktree discipline.** See ``worktree-discipline``.
12. **Branch currency.** See ``branch-currency``.
13. **Pre-commit before commit.** ``uv run pre-commit run --all-files``
    before every ``git commit``. Hook failures are root-caused, never
    ``--no-verify``'d.
14. **Commit prefix.** See ``commit-prefix``.
15. **Branch naming.** See ``branch-naming``.
16. **Secrets and PII hygiene.** See ``secrets-hygiene``.
17. **Naming conventions for fields/params/log keys.** See
    ``naming-conventions``.
18. **Artifact chassis and citations.** See ``artifact-chassis``.
19. **Typed agent reports.** See ``agent-report-contract``.
20. **Planned-scope revisability.** See ``planned-scope-revisability``.
21. **Roadmap procedure.** See ``roadmap-procedure``.
22. **Spike workflow.** See ``spike-workflow``.
23. **Engineering principles (DRY/KISS/YAGNI).** See
    ``engineering-principles``.
24. **Other engineering practice.** See ``engineering-practice``.
25. **Source code stays clean of design-decision references.** No
    inline ``# per Q<N>``, ``# per audit XB##``, ``# per Codex``, or
    roundtable / operator-decision-id comments in committed source
    files. Decision provenance lives in the commit body and the typed
    Decision URN in ``state.json``; source comments are reserved for
    WHY-the-code-does-X explanations that aid future readers
    irrespective of provenance.
26. **/prep always renders the DAG in plan mode.** See
    ``prep-plan-mode``.
27. **Iter and phase close timing.** See
    ``iter-phase-close-timing``.
28. **Rendered markdown is not manually line-wrapped.** See
    ``markdown-no-manual-wrap``.

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
<!-- BEGIN EAWF:managed id=naming-conventions version=1.3 hash=80afcb466abe1f57 -->
### Naming conventions

To prevent drift across state models, envelopes, parameters, and
log keys, every cross-cutting concept has exactly one canonical
name. Outliers MUST be renamed to match the dominant form before
merging, not papered over with adapter shims.

**State scope identifier** — ``scope_id`` (never bare ``scope``).
Applies to Pydantic field names on ``State`` models
(e.g. ``PluginInstall``), :class:`~eawf.surfaces.render.envelope.EnvelopeHeader`,
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

**Mutator-path precision in wave success criteria** — when a
wave's success criterion text references a "save through" or
"persist via" path, name the **canonical writer** (the daemon,
per rule 4) rather than the generic phrase ``state-CLI``. The
authority map
``.ea/artifacts/research/long-term/2026-05-18-authority-map.md``
names the canonical writer per file. Conflating the
operator-facing surface (``uv run eawf state ...``) with the
daemon-internal subsystem in criterion prose makes audits flag
false positives.

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
<!-- BEGIN EAWF:managed id=state-vs-specs version=1.2 hash=7cc9c321140e8332 -->
### State vs specs

Specs describe intent; state reflects reality. The canonical writer of
``state.json`` is the daemon — see rule 4 for the full mutator-authority
statement (operator-facing state CLI, JSON-RPC proxy, portalocker
fallback). Do not hand-edit ``state.json`` to make it agree with a spec;
drive the state mutation and let the spec follow.

<!-- END EAWF:managed id=state-vs-specs -->
<!-- BEGIN EAWF:managed id=verify-before-claim version=1.1 hash=da4d1a7a1791bb85 -->
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

Wave commit SHA (P19-W04): ``Wave.commit`` no longer exists on the
state model. Quote the SHA via
``eawf wave show --commit <wave-id>`` (which walks ``git log
--grep '[P##-W##]'``) instead of reading the dropped field.

<!-- END EAWF:managed id=verify-before-claim -->
<!-- BEGIN EAWF:managed id=worktree-discipline version=1.1 hash=36802bc751ac9da7 -->
### Worktree discipline

Worktree subagents MUST branch from the current feature branch HEAD,
not ``main``. Their commits are **cherry-picked** into the parent
feature branch — never ``git merge``.

Cherry-pick procedure: ``git -C <main-worktree> cherry-pick
<worktree-sha>`` per commit, in order. Resolve conflicts in the
parent worktree. Worktree teardown only after cherry-pick lands.

Claim order (P19-W02): ``eawf wave claim`` enforces deps + W##
monotonic ordering. Each claim rejects when (a) any wave in
``.deps`` is not CLOSED, or (b) a lower-numbered sibling wave
under the same iter is still PENDING with its own deps already
satisfied. Parallel-worktree dispatch where multiple siblings of
the same dep frontier are claimed at once MUST pass
``--out-of-order`` on each claim to opt out of the gate.

<!-- END EAWF:managed id=worktree-discipline -->
<!-- BEGIN EAWF:managed id=branch-currency version=1.0 hash=3af31aa0e925c2fb -->
### Branch currency

Before opening or resuming a phase, iter, or wave, verify the current
branch is based on the intended source branch (normally the repo default
branch or the configured phase base). Fetch first, inspect divergence,
and rebase or fast-forward the long-running feature branch when it is
stale.

If the working tree is dirty, preserve the dirty/untracked work before
rebasing. If the branch intentionally remains behind or forked, record
the reason in the plan or handoff before dispatching worktrees or
starting new commits.

<!-- END EAWF:managed id=branch-currency -->
<!-- BEGIN EAWF:managed id=commit-prefix version=1.3 hash=f9f7015dfda2b8b2 -->
### Commit prefix

``[P<NN>(-I<NN>)?(-W<NN>|-CORE)?] <type>: <summary>`` — types:
``feat``, ``fix``, ``chore``, ``docs``, ``refactor``, ``test``,
``build``, ``perf``, ``ci``, ``revert``, ``state``.

Subject grammar (post-P26-W23):

- **Planned wave deliverable** — ``[P<NN>-W<NN>] <type>:`` (or
  ``[P<NN>-I<NN>-W<NN>] <type>:`` when iter ≥ I02). The
  ``-W<NN>`` suffix declares the wave the commit advances.
- **State-bookkeeping** — ``[P<NN>] state:`` (or
  ``[P<NN>-I<NN>] state:`` when iter ≥ I02). The ``state``
  conventional-commit type IS the semantic signal for phase-
  scope bookkeeping; no suffix needed. Allowed paths:
  ``.ea/state.json``, ``.ea/store/event.jsonl``,
  ``.ea/store/audit.jsonl``, ``.secrets.baseline``, and
  ``.ea/specs/**``.
- **Legacy ``-CORE`` alias** — ``[P<NN>-CORE] state:`` (or the
  iter variant) remains valid for back-compat with pre-P26-W23
  commits. New bookkeeping commits MAY drop the ``-CORE``
  suffix; the lint accepts both forms identically.
- **Phase/iter-scoped artifact docs** — ``[P<NN>] docs:`` (or
  ``[P<NN>-I<NN>] docs:``) for documentation artifacts no single
  wave owns (closure audits, promoted research / decision /
  incident briefs). Restricted to ``.ea/artifacts/**``;
  wave-produced docs use the ``[P<NN>-W<NN>] docs:`` form.

The path whitelist for state-bookkeeping commits triggers on
``type == 'state'`` (the canonical semantic signal). The
legacy ``-CORE`` suffix is also treated as a whitelist
trigger so pre-P26-W23 commits continue to validate.

Bare ``[P<NN>]`` is accepted for ``type == 'state'`` (any
state-bookkeeping path) and ``type == 'docs'`` (restricted to
``.ea/artifacts/**``); for every other type the ``-W<NN>`` or
``-CORE`` suffix remains mandatory.

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
<!-- BEGIN EAWF:managed id=artifact-chassis version=1.0 hash=a5a6f9421f5f5d23 -->
### Artifact chassis and citations

Durable research, plan, audit, decision, hypothesis, and incident
markdown uses renderer-owned chassis sections: ``Summary``,
``References``, ``Provenance``, and ``Scrub``. Local drafts under
``.ea/local/`` carry an ``eawf-template`` sentinel; promoted artifacts
under ``.ea/artifacts/`` do not.

Citations use dense ``[N]`` markers backed by typed ``Citation`` rows.
References stay repo-relative, external URL, or Eawf URN. Absolute
local paths, host-local URLs, and PII must fail validation before
promotion or PR text ships.

<!-- END EAWF:managed id=artifact-chassis -->
<!-- BEGIN EAWF:managed id=workflow-lifecycle version=1.0 hash=e1e63ab1ed813a44 -->
## Workflow lifecycle

Agent-driven lifecycle:

```
research → plan → execute waves → cherry-pick → ship phase
```

- **Research** is unstructured exploration of the proposal/plan.
- **Branch currency gate** = fetch and compare the current branch to the
  intended source branch before opening or resuming a phase, iter, or
  wave; rebase or fast-forward first when stale.
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
<!-- BEGIN EAWF:managed id=anti-patterns version=1.2 hash=e7dec10ef5d0076a -->
## Anti-patterns

- Mutating ``state.json`` outside the daemon (or its state-CLI
  proxy / portalocker direct-write fallback) — see rule 4.
- Skipping ``extra="forbid"`` on a Pydantic model "just for now".
- Merging worktree branches instead of cherry-picking.
- ``--no-verify`` on a failing pre-commit hook.
- Quoting design-intent docs as authoritative for current behaviour
  (verify against the source tree).
- Starting a phase, iter, or wave from a stale feature branch.
- Adding a runtime dep without checking it's not already pulled in
  transitively.
- Using ``Read`` to verify a file you just wrote with
  ``Write``/``Edit``.
- Carrying audit citations or decision-round IDs into source
  comments (rule 25 violation; provenance lives in commit body
  + ``state.json`` Decision URN).

<!-- END EAWF:managed id=anti-patterns -->
<!-- BEGIN EAWF:managed id=python-style version=1.1 hash=ab17daa64d2cfa01 -->
## Python style (python profile)

- f-strings only; no ``%``-style or ``.format()``.
- Full type hints; ``from __future__ import annotations`` at the top of
  every module.
- Library modules use ``logger = logging.getLogger(__name__)``.
- ``uv run`` for all Python invocations — never ``.venv/bin/python``
  or bare ``python``.
- Pre-commit before commit (``uv run pre-commit run --all-files``).
- Before adding a runtime dependency, verify it's not already pulled
  in transitively.

<!-- END EAWF:managed id=python-style -->
<!-- BEGIN EAWF:managed id=test-discipline version=1.0 hash=82d92549434f3d71 -->
## Test discipline (python profile)

- ``pytest.approx`` for any float comparison.
- ``numpy.testing.assert_allclose`` for arrays (when numpy is in use).
- Public functions MUST have boundary-case AND error-path tests.
  Boundary: empty, single, off-by-one, max-length. Error: invalid
  type (``TypeError``), out-of-range (``ValueError``), missing key
  (``KeyError``), schema mismatch (``ValidationError``).
- Test names: ``test_<func>_<scenario>`` — no
  ``test_compute_iv_solver_1``.
- ``pytest.raises`` for error paths; assert message substring when
  the message is part of the API contract.

<!-- END EAWF:managed id=test-discipline -->
<!-- BEGIN EAWF:managed id=research-workflow version=1.0 hash=d1726bdcdd49c5d8 -->
## Research workflow (research profile)

Hypotheses, audits, and decisions are first-class state-resident entities.
Every claim about behaviour, performance, or correctness MUST be backed
by an audit-recorded artifact (notebook, log, dataset). Hypotheses use
the ``H<NN>-<NN>`` symbol; audits link to the hypothesis, the claim, and
the supporting artifact id. Decisions reference the audit that justifies
them so the evidence chain is reconstructible from ``state.json`` alone.

<!-- END EAWF:managed id=research-workflow -->
<!-- BEGIN EAWF:managed id=agent-report-contract version=1.0 hash=4af66a5ed7687989 -->
### Agent report contract

Every agent session that reaches a terminal handoff MUST emit a typed
``agent_end`` report body accepted by ``AgentReportBody``. Runtime hooks
own ``AgentReportHeader`` fields (session, role, scope, runtime, attempt);
agents provide the role-specific body only.

Reports are append-only. Never overwrite or "fix" an earlier report
attempt; retry by appending the next attempt for the same
``(role, base_id)`` pair.

Verdicts MUST use ``AgentReportVerdict`` exactly: ``pass``,
``pass-with-followups``, ``fail``, or ``blocked``. Report store URNs use
the role-specific ``StoreKind`` such as ``executor_report`` or
``reviewer_report``.

<!-- END EAWF:managed id=agent-report-contract -->
<!-- BEGIN EAWF:managed id=planned-scope-revisability version=1.0 hash=228755425c2e6cc1 -->
### Planned-scope revisability

Phases and iters are first-class state records that move through
``PLANNED -> ACTIVE -> CLOSED`` (waves move through ``PENDING ->
CLAIMED -> IN_PROGRESS -> CLOSED``). Mutability is status-tiered:

- **PLANNED** scope is freely mutable. ``eawf roadmap revise
  <phase-id> --add-wave / --remove-wave / --set-deps / --retitle``
  edits the phase before it activates.
- **ACTIVE** scope is append-only at the phase level — only
  PENDING waves under it may still be mutated. The W01
  ``edit_wave_plan`` / ``remove_wave_plan`` / ``set_wave_deps``
  transitions enforce the PENDING-only invariant on their own.
- **CLOSED** scope is immutable except via ``eawf phase reopen``
  (which flips CLOSED back to ACTIVE; audit linkage is preserved
  for traceability).

Mid-flight reshapes go through ``eawf roadmap revise <active-phase>``
too; the same PENDING-only invariant applies. Drop-and-redo
(``eawf roadmap drop`` + ``eawf roadmap propose``) is the escape
hatch when more than half the waves need to change.

<!-- END EAWF:managed id=planned-scope-revisability -->
<!-- BEGIN EAWF:managed id=roadmap-procedure version=1.0 hash=d1ab6d337cfe314f -->
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

Bulk propose (``--bulk --from-briefs RES-12,RES-13,...``) is
deferred to a follow-up phase; P19 ships phase-at-a-time only.
``/roadmap reorder`` is also deferred — operator drops + re-
proposes to swap order.

<!-- END EAWF:managed id=roadmap-procedure -->
<!-- BEGIN EAWF:managed id=spike-workflow version=1.1 hash=2e32fb783dd93583 -->
### Spike workflow

A *spike* is a short, time-boxed, read-only investigation run
before claiming a real wave — used when the next move is unclear
and the operator needs a brief or experimental verdict to write
the wave's success criteria. Spikes are convention-only in v0.3
(no dedicated CLI verb); they ride the existing ``/research``
surface and produce a brief artifact, not a state mutation.

**When to spike.** Reach for a spike when (a) the wave's success
criteria cannot yet be written without first reading code or
running a probe, (b) two or more design alternatives need a
verdict before ``/roadmap propose`` can commit to a DAG, or
(c) an audit hypothesis needs an evidence sweep before
``set-verdict``. Skip the spike when the next move is obvious —
go straight to ``/roadmap propose`` or ``/prep`` claim.

**Where the output lives.** Spike output is a research brief
under ``.ea/local/<YYYY-MM-DD>-<slug>.md`` (or the conventional
``.ea/local/research/`` sub-directory). Filenames follow the
``<date>-<slug>.md`` stem so the brief sorts chronologically and
slug-matches against the wave or phase it informs. Briefs stay
local-only — ``.ea/local/`` is gitignored — and are promoted to
``.ea/artifacts/`` only when they inform a decision that lives
in ``state.json`` (artifact-chassis rule applies on promotion).

**How the verdict feeds the workflow.** The spike's verdict is
the input to the next ``/roadmap propose --phase P<NN>`` or
``/prep`` claim. Reference the brief by repo-relative path in
the roadmap proposal, the wave's plan body, or the dispatch
prompt — the wave dispatch renderer surfaces spike briefs whose
filename matches the wave / iter / phase id under a
``## References`` section so the subagent reads them before
starting work.

**Spike outputs that ratify a verdict promote on commit.** A
spike brief that informs a Decision row + ``set-verdict`` MUST
promote from ``.ea/local/research/<date>-<slug>.md`` to
``.ea/artifacts/research/<date>-<slug>.md`` in the same commit
that lands the Decision. The promotion runs the
artifact-chassis validator + scrub gate. Spikes that do NOT
inform a typed verdict stay local.

<!-- END EAWF:managed id=spike-workflow -->
<!-- BEGIN EAWF:managed id=prep-plan-mode version=1.0 hash=1a9d4b39246035f4 -->
### /prep always renders the DAG in plan mode

Both Case A and Case B of ``/prep`` MUST enter Claude Code
plan mode (``EnterPlanMode``) with the rendered wave DAG of
the target phase's current iter before surfacing the
approve / edit / cancel ``AskUserQuestion``. Free-text
approvals are forbidden per the project-wide
``AskUserQuestion``-only approval policy.

**Case A — PLANNED phase with at least one PENDING wave.**
Render the plan via ``eawf roadmap show --phase <id> --md``
→ ``EnterPlanMode`` → ``AskUserQuestion``
(``use-as-is`` / ``revise`` / ``replace`` / ``cancel``). On
``revise``, hand back to ``/roadmap revise``; on ``replace``,
hand back to ``/roadmap drop`` + ``/roadmap propose``.

**Case B — PLANNED phase with empty wave DAG.** Apply the
planner's emitted ``eawf roadmap revise --add-wave`` commands
**first** (waves land as PENDING on the still-PLANNED iter),
then render the resulting DAG via ``eawf roadmap show
--phase <id> --md`` → ``EnterPlanMode`` → ``AskUserQuestion``
(``approve`` / ``edit`` / ``cancel``). The operator reviews
the rendered roadmap, not the planner's raw commands. Edits
during plan mode are ``/roadmap revise`` calls (PLANNED
scope is mutable). On ``approve``, run
``eawf phase activate <id>`` (V11 hard gate).

The plan-mode-first invariant applies to any future ``/prep``
cases (e.g. mid-flight scope expansion of an ACTIVE iter):
the operator-facing surface is always the rendered DAG, not
raw mutator commands.

<!-- END EAWF:managed id=prep-plan-mode -->
<!-- BEGIN EAWF:managed id=iter-phase-close-timing version=1.1 hash=8440075478e00422 -->
### Iter and phase close timing

Iter close is gated on **audit + polish + ship CI + PR
review pass**. Do not close an iter the moment its waves
finish — run ``/audit`` and ``/polish`` first, then
``/ship`` (which runs the PR review pass + addresses feedback
by appending waves to the same iter), then close.

**Append, don't open a second iter.** When ``/audit`` or
``/polish`` surfaces follow-up work that fits the same
delivery, append waves to the current iter via
``eawf roadmap revise --add-wave`` (ACTIVE-phase
``add_wave_plan`` keeps the iter ACTIVE and the new waves
land PENDING). Opening a second iter under the same phase
is reserved for true scope expansions or repair cycles per
decision D17 (iter-bump triggers), not for routine
follow-ups.

**Phase close goes in the latest commit before merge.** Do
not close the phase until ship CI is green AND the
review-passed branch is on the remote. The phase-close
mutation rides in a single ``[P<NN>] state: close iter +
phase (audit=<id>)`` commit that bundles iter close + phase
close (the legacy ``[P<NN>-CORE] state: ...`` form remains
valid per the ``commit-prefix`` block). Merging that commit
ends the phase; pre-merge close keeps ``state.json`` in sync
with what reviewers approved.

<!-- END EAWF:managed id=iter-phase-close-timing -->
<!-- BEGIN EAWF:managed id=entity-title-naming version=1.0 hash=a9fd52b122658082 -->
### Rationale

**Entity-title naming.** Every lifecycle and research entity
(``Phase`` / ``Iter`` / ``Wave`` / ``Decision`` / ``Hypothesis`` /
``BacklogItem`` / ``Incident``) carries a bounded ``title`` and an
optional long-form ``description``. The bound exists so titles stay
scannable in dense renders — the roadmap tree, plan-view table, and
dispatch header all lay titles out in a single fixed-width row, and an
unbounded sentence either truncates with an ellipsis (losing the tail)
or wraps and breaks the column. A trailing period reads as the end of a
sentence, but a title is a label, not prose; the period is visual noise
that the description, which IS prose, should carry instead.


### Mechanism

Write ``title`` as an imperative noun-phrase of at most 72 characters
with no trailing period — e.g. ``Add bounded title to entities`` or
``Enforce sandbox deny-list at dispatch``, never
``Adds a bounded title to every entity.`` (over-cap once the clause
grows, and the period is sentence noise). Put the why / the long-form
purpose in ``description`` (bounded at 500 characters); the renderers
surface it as a detail block under the bounded title, so the two fields
split the label from the explanation rather than competing for one line.


### Verification

The model enforces the hard bound: ``title`` is
``Annotated[str, Field(min_length=1, max_length=72)]`` on every entity,
so an over-72 title fails :class:`pydantic.ValidationError` at the
ingestion boundary. The style backstop is
:func:`eawf.surfaces.render.agents_md.lint_entity_title`, which a reviewer (or a
future authoring command) runs over a candidate title to flag an
over-cap or a trailing-period title before it reaches the model — the
same two failure modes the bound and this rule describe.

<!-- END EAWF:managed id=entity-title-naming -->
<!-- BEGIN EAWF:managed id=engineering-principles version=1.0 hash=befb20ec78f50d19 -->
### Rationale

**Engineering principles (DRY/KISS/YAGNI).** Speculative flexibility is
the dominant source of accidental complexity: an abstraction added for a
caller that never arrives costs reading effort on every later edit while
paying back nothing. DRY (don't repeat yourself) keeps one canonical home
per behaviour; KISS (keep it simple, stupid) keeps the design no larger
than the immediate need; YAGNI (you aren't gonna need it) defers anything
the current change does not require.


### Mechanism

Reach for the simplest design that solves the immediate need. Three
similar lines are better than a half-fitted helper — do not extract until
a third caller actually appears. Do not add error handling, fallbacks, or
validation for scenarios that cannot happen on the real call paths.


### Verification

A reviewer checks that each new helper, parameter, or config knob has a
present-day caller; a helper introduced for one or two call sites, or for
a use site that does not yet exist, is rejected. Defensive branches for
impossible states are removed before merge.

<!-- END EAWF:managed id=engineering-principles -->
<!-- BEGIN EAWF:managed id=engineering-practice version=1.0 hash=89789e32b7eb3b87 -->
### Rationale

**Other engineering practice.** Code that fails far from its cause, mixes
concerns, or signals success with ``None`` is expensive to debug and easy
to break: the stack trace points at a symptom, a change to one concern
risks the others, and the happy path reads ambiguously. Failing fast,
separating concerns, and being explicit keep behaviour where the name and
the call site say it is.


### Mechanism

Default to: fail-fast (raise at the boundary, not deep in a call stack);
single-responsibility (each function or class has one reason to change);
principle of least surprise (behaviour matches the name); separation of
concerns (parsing ≠ validation ≠ execution); pure functions where viable
(no hidden state); and explicit-over-implicit (named arguments over
positional when arity ≥ 3, explicit returns over ``None``-as-success).


### Verification

A reviewer reads each public function's first statements (validation
precedes side effects) and each call site of arity ≥ 3 (arguments passed
by keyword). A function whose name implies a value but returns ``None`` on
the happy path is reworked; ``uv run mypy src/`` backs the explicit-return
contract via full type hints.

<!-- END EAWF:managed id=engineering-practice -->
<!-- BEGIN EAWF:managed id=markdown-no-manual-wrap version=1.0 hash=f2e90c93b197633d -->
### Rendered markdown is not manually line-wrapped

Rendered and authored markdown — PR bodies, issue/review comments,
audit / research / decision artifacts, READMEs, mkdocs pages, and
skill output envelopes — is written one line per paragraph. Do NOT
hard-wrap prose at ~72 (or any) columns; let the renderer / viewer
soft-wrap. Manual wrapping fights diffs (a one-word edit reflows a
whole block), breaks tables and list continuations, and corrupts
copy-paste.

The ~72-column wrap convention is reserved for **commit messages**
(subject + body), where tooling and ``git log`` assume it. Fenced
code blocks keep their own formatting. Skill output contracts inherit
this rule: a skill that emits markdown emits unwrapped paragraphs.

<!-- END EAWF:managed id=markdown-no-manual-wrap -->
