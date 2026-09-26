"""Frozen :data:`SKILL_REGISTRY` data for the Eä skill surface.

``eawf plugin install claude`` emits one
``.claude/skills/<name>/SKILL.md`` per skill. The output mirrors the
hand-written placeholders that already live under ``.claude/skills/``:
YAML frontmatter (``name``/``description``/``argument-hint``/
``user-invocable``/``disable-model-invocation``) terminated by ``---``
and followed by a markdown body documenting the canonical algorithm,
the pre-flight checklist, and the output contract.

This module holds only the data: the per-skill body strings and the
frozen :data:`SKILL_REGISTRY` tuple. The typed render context / spec
dataclasses and the Jinja2-backed ``render_skill_md`` helpers live in the
sibling :mod:`eawf.surfaces.render.skills.render`; the package ``__init__``
re-exports both halves so every historical
``from eawf.surfaces.render.skills import ...`` keeps resolving unchanged.

The ``SKILL_REGISTRY`` carries only skills that are still in the closed
skill catalog (:data:`eawf.workflow.skills.catalog.SKILL_CATALOG`); a body
for a retired skill is deleted rather than kept, because a stale page
that no packager ships still reads as a live instruction to anyone who
greps for it. No packager ships this registry either: every shipped page
is rendered from the catalog through the prompt chassis in
:mod:`eawf.workflow.skills.bodies.chassis`.
"""

from __future__ import annotations

import logging

from eawf.surfaces.render.skills.render import SkillSpec

logger = logging.getLogger(__name__)


_RESEARCH_BODY = """# /research

## Canonical algorithm

1. Define the question. State the hypothesis or unknown in one sentence.
2. Survey: read source, run `git log`, fetch external refs as needed.
3. Compare alternatives — bullet list of options with pros/cons.
4. Verdict: recommend one path, or recommend "stay open" with the next
   discriminating experiment.
5. If `--final`: persist a research brief with `references` and render
   it through `eawf research show --md`.

## Output contract: `IntentBrief` + dispatch-plan

The brief body conforms to `kernel/spec/intent.IntentBrief` — typed
claims with `evidence_refs` (a brief is promotable iff every claim
has at least one resolving + entailing reference; the EviBound
rung-1 gate in `platform/artifacts/validation.validate_markdown_artifact`
enforces this at promotion time, not at ingestion). The session also
emits an optional dispatch-plan when the verdict names a follow-up
wave the brief informs, so `/prep` and `/roadmap propose` can wire
the brief into the next wave's References block automatically.

## Options

- `--depth shallow|medium|deep|exhaustive` — survey budget (file
  reads, external fetches, cross-wave grep sweeps); read from
  `ctx.args["depth"]`, then the `research.default_depth` layered-config
  leaf (reuses `StageProfile`, no new key). Default `medium`.
- `--final` — persist a research brief with `references` and render it
  through `eawf research show --md`. Default off.
- `--rounds <n>` — bound the fan-out iteration count (today the
  fan-out is depth-derived only). Default `1`.
- `--agents <n>` — fan-out width; resolves through the
  `research.agent_count` layered-config leaf. Default `4`.
- `--budget <tokens>` — recorded on the envelope; enforcement binds
  once metering rows exist. Default uncapped.

## Spike convention

A *spike* — a short read-only investigation done before claiming a
real wave — is run via `/research` and produces a brief under
`.ea/local/<YYYY-MM-DD>-<slug>.md` (or the conventional
`.ea/local/research/` sub-directory). The filename follows the
`<date>-<slug>.md` stem so it sorts chronologically and slug-matches
the wave, iter, or phase it informs. Briefs stay local-only —
`.ea/local/` is gitignored — and are promoted to `.ea/artifacts/`
only when they inform a decision recorded in `state.json` (the
artifact-chassis rule then applies). See `spike-workflow` in
AGENTS.md for the full convention.

## Pre-flight checklist

- [ ] No state mutations — read-only.
- [ ] Cite sources as dense `[N]` references backed by `Citation` rows.
- [ ] Keep promoted artifact prose scrub-clean and repo-relative.
- [ ] Distinguish "what the code does" from "what the doc claims".
- [ ] If this run is a spike, name the brief
      `<YYYY-MM-DD>-<slug>.md` and place it under `.ea/local/` (or
      `.ea/local/research/`) so the dispatch renderer can surface it
      to the next wave's executor.

## Decision surfaces

When the verdict reduces to a small set of named alternatives, surface
the choice through `AskUserQuestion` rather than free-text — the
operator can pick without retyping the option labels.

## Output contract

Eä-rendered skill envelope (`OutputEnvelope`) with `header.skill =
"/research"`. Body carries the structured findings; footer records any
persisted brief.
"""


_MOCKUP_BODY = """# /mockup

## Canonical algorithm

1. Take the UI surface under design and the operator's intent (the
   fields to show, the terminal width budget, the priority elements).
2. Author 2-4 concrete mockup variants as ASCII layouts. Use plain
   ASCII box characters (`+`, `-`, `|`) so the render stays lint-clean;
   do not use Unicode box-drawing glyphs.
3. Surface the variants as `UserQuestionOption.preview` strings on a
   single `UserQuestion` so the operator compares the layouts
   side-by-side and picks one. Each option's `preview` carries that
   variant's full multi-line layout; the `label` is the variant name.
4. Stop at the mockup. `/mockup` is advisory: it produces layouts and
   the comparison prompt, it does not write files or mutate state.

## Pre-flight checklist

- [ ] Read-only / advisory -- no state mutations, no file writes.
- [ ] 2-4 variants only (the `AskUserQuestion` cap; single-select).
- [ ] ASCII-only box art so the preview renders cleanly everywhere.

## Decision surfaces

The variant choice is the one operator-facing decision. Surface it
through `AskUserQuestion`, one option per variant, with the layout in
each option's `preview` box -- never bounce the operator to free-text.

## Output contract

Skill envelope whose body conforms to `MockupBody`: the authored
`variants` plus the optional `UserQuestion` whose options carry the
side-by-side `preview` layouts.
"""


_SPIKE_BODY = """# /spike

## Purpose

A *spike* is a time-boxed read-only investigation that produces
**direction** — picks across many design axes — to unblock the next
planning skill. Direction-only means no wave plan, no success
criteria, and no EU estimates fall out of a spike; those belong to
`/design` and `/roadmap propose`. Spike writes only under
`.ea/local/`.

Three failure modes it prevents:

1. Single-verdict context loss. `/research` answers one question; a
   rebuild or phase-opening decision has many entangled axes. Spike
   runs them as multi-round AUQ batches in one session with a rolling
   matrix.
2. Silent scope creep. Mid-session picks can quietly expand or reduce
   surface vs prior briefs. Spike surfaces scope deltas as a mandatory
   section when prior briefs exist.
3. Postmortem-without-next-plan. Spike couples a postmortem (gap
   matrix + root causes + salvage matrix) to direction picks in the
   same brief so the rebuild plan inherits the lessons.

## When to invoke

Reach for `/spike` when multiple decisions block `/roadmap propose`
and must be picked together to stay coherent, when a prior attempt
shipped but missed its briefs and the next phase is a rebuild, or when
direction picks span scope-expansions and -reductions that need
explicit acknowledgement. Pick a neighbour instead when one unknown
blocks progress and a single verdict lands it (`/research`), or when
direction is locked and an interactive surface needs a full statechart +
matrix + journey design (`/design`).

## Canonical algorithm

1. Resolve the slug from the argument or AUQ. Filename stem:
   `<YYYY-MM-DD>-<slug>.md`.
2. Frame the multi-axis unknown in one paragraph; list the prior
   briefs feeding the spike (`--from-briefs`). On
   `--postmortem <phase-id>`, declare the phase under postmortem.
3. Survey: read the cited prior briefs, read source on the verdict
   ladder (verify-before-claim), run `git log` for shipped-vs-spec
   drift. Optionally dispatch worktree subagents for independent
   investigative arms; the parent compiles the chunks.
4. Multi-round AUQ picks. Each round = 3-6 axes batched into one
   `AskUserQuestion` call. Decisions accumulate into the rolling
   matrix; round-close is gated on all axes in the batch answered.
5. Scope-delta surfacing. When picks diverge from prior briefs, record
   explicit expansion and reduction tables, each row citing the
   original brief line and the new pick.
6. Critical-contracts capture. When picks ripple into process changes
   (commit-prefix lint, success-criteria shape, schema migration,
   audit kind), surface them with enforcement + effect named.
7. Open follow-ups. Enumerate explicitly — "none" is rare. Label each
   with a next-action (next-spike, next-research, hypothesis-open,
   blocked-on-EU, blocked-on-demo).
8. Hand-off declaration. The Summary closes with a literal `next:`
   line naming the unblocked skill and its args.
9. Self-lint, then write (on `--final`)
   `.ea/local/research/<YYYY-MM-DD>-<slug>.md` with the sentinel
   `<!-- eawf-template: spike-brief -->` on line 1. Return the output
   envelope.

## Options

Spike is model-driven, so these are prose parameters the session
honours, not engine-parsed flags:

- `--rounds <n>` — number of multi-axis AUQ rounds; parameterizes the
  default "3-6 axes per round" shape. Default is model-judged.
- `--axes-per-round <m>` — axes batched into each `AskUserQuestion`
  round. Default `3-6`.
- `--worktree` — make the execution-spike isolation branch explicit
  and mandatory: when passed, code that imports `eawf.*` runs in a
  dedicated worktree branch rather than the local PoC scope.
- `--final` / `--from-briefs <paths>` / `--postmortem <phase-id>` — see
  the canonical algorithm above.

## PoC allowance + execution-spike isolation

A spike MAY produce throwaway runnable artifacts to ground direction
picks — smoke demos, probe scripts, config experiments — under
`.ea/local/` only (`.ea/local/smoke/<slug>/`, `.ea/local/poc/<slug>/`,
or `.ea/local/research/notes/<slug>/`), gitignored, with a manifest
table + a "How to run the PoCs" section when >=1 is built.

A *direction spike* is read-only. An *execution spike* writes code to
ground a verdict against a running artifact, so it needs isolation the
read-only flow does not. Zero-internal-dep throwaway scripts stay in
the local gitignored PoC scope. Code that imports `eawf.*` runs in a
dedicated worktree branch off the current feature-branch HEAD
(`feature/<symbol>-vX.Y-spike-<slug>`); on a green verdict the commits
are cherry-picked into the feature branch (never merged), and on a
rejected verdict the worktree is torn down. Do NOT commit spike code
straight onto the shared feature branch — a pre-verdict commit
interleaves with concurrent sessions and bakes unratified experiment
into history.

## Brief chassis

Required sections (the writer rejects on missing): frontmatter
(scope URN, `status=local-draft`, created date, agent); the sentinel
on line 1; Summary (verdict rollup + direction bullets + the `next:`
line); a decision matrix (>=1 round table when picks were made, else a
Findings section); Open follow-ups (labelled); References (dense `[N]`
rows, repo-relative + external URLs + Eä URNs); Provenance
(`store_record=none (local-only spike)`, starting commit SHA, session
slug); and Scrub (repo-relative paths only, no PII, placeholder names
when project codes appear). Conditionally required: the PoC manifest +
run guide when >=1 PoC was built, the postmortem arc when
`--postmortem` is passed, and the scope-delta tables when
`--from-briefs` cites prior briefs.

## Pre-flight checklist

- [ ] No state mutations; `state.json` untouched. PoCs under
      `.ea/local/{smoke,poc,research/notes}/` only (gitignored).
- [ ] If an execution spike that landed runnable code — code is
      isolated (worktree branch for internal deps, local PoC scope for
      zero deps) and ships a `.ea/local/` user test guide.
- [ ] Multi-round picks via `AskUserQuestion`, never free-text;
      recommended option first, labelled.
- [ ] Every direction pick carries a one-line rationale (`[N]` cite or
      "because X").
- [ ] Scope deltas surfaced when `--from-briefs` cited; critical
      contracts surfaced when picks ripple into process change.
- [ ] Open follow-ups enumerated with next-action labels; `next:` line
      present in the Summary.
- [ ] References dense `[N]`, repo-relative only; Provenance records
      the starting commit SHA + session slug; Scrub confirms no PII.
- [ ] Brief filename `<YYYY-MM-DD>-<slug>.md` under
      `.ea/local/research/`; sentinel `<!-- eawf-template: spike-brief -->`
      on line 1.

## Decision surfaces

Round axis picks, scope-delta acknowledgement, and the hand-off
declaration are all surfaced through `AskUserQuestion` batches — the
hand-off AUQ options name the unblocked skill (`/roadmap propose`,
another `/spike`, `/research --final`, or `/design`).

## Output contract

Eä-rendered envelope (`OutputEnvelope`) with
`header.skill = "/spike"`. Body carries the Summary + `next:` line,
the round tables (decision matrix), the postmortem arc when
`--postmortem`, the scope deltas when `--from-briefs` cited, the
critical contracts when applicable, the labelled open follow-ups, and
the References + Provenance + Scrub chassis tail. The footer records
the persisted brief path + sentinel when `--final` was passed.
"""


_MEMORY_BODY = """# /memory

## Cross-links

Each memory mutation carries a typed `MutationKind` —
`create | update | refresh | demote | archive` — so the audit trail
distinguishes a freshly captured fact from a content-preserving
re-touch. `find_stale` is recommender-only (it surfaces candidates
ranked by age and use-count) and never flips `status=STALE`
unilaterally; the explicit `demote` mutation does that. Promoted
memories carry a `Project` row reference so cross-repo recall stays
scoped.

## Canonical algorithm

1. Resolve the verb (`save` default / `list` / `forget`) and the target
   tier (`working` default / `archival` / `retrieval`).
2. A named verb (`save` / `forget`) without a `name` degrades to
   `status=needs_user`.
3. Append a single append-only `EVENT` describing the operation intent;
   the daemon is the sole canonical writer of the memory JSONL store, so
   the skill routes the operator to the `eawf memory` writer via
   `next_valid_actions` rather than mutating the store itself.

## Pre-flight checklist

- [ ] The skill records intent only — the daemon owns the store write.
- [ ] `save` / `forget` carry a memory entry name.

## Decision surfaces

A named verb (`save` / `forget`) without a `name` degrades to
`status=needs_user`, which routes the operator to an `AskUserQuestion`
prompt for the missing entry name rather than inventing one.

## Output contract

Skill envelope with `header.skill = "/memory"`. Body carries verb, name,
and tier (or a reason on the needs_user path).
"""


# ---------------------------------------------------------------------------
# Model-only code-quality skills. These carry no executable skill body in
# `eawf.workflow.skills.registry`; they are prompt-only playbooks the model invokes
# while editing source. Each renders with `user-invocable: false` so it is
# hidden from the slash menu, and `disable-model-invocation: false` so the
# model may still reach for it. Bodies document the canonical refactoring
# procedure rather than a CLI output contract.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The three lifecycle bodies. Unlike the bodies above they are authored one
# line per paragraph rather than hard-wrapped: the Claude renderer unwraps
# its input, but the opencode command renderer emits the body verbatim, so a
# wrapped literal would ship a manually wrapped artifact on that surface.
# Implicit concatenation keeps the source inside the line-length budget while
# the emitted line stays whole.
# ---------------------------------------------------------------------------

_DISPATCH_BODY = (
    "# /dispatch\n"
    "\n"
    "## Canonical algorithm\n"
    "\n"
    "1. Bind the Batch and its Task graph at one exact read cursor. Every Task named later"
    " carries the key it was read under, so a head that moves during the pass is detectable"
    " rather than silent.\n"
    "2. Compute the candidate frontier: the Tasks standing at PLANNED. The read models carry"
    " each record's lifecycle status and no dependency edge and no ownership claim, so"
    " promoting that candidate set to a ready frontier needs facts this surface cannot see."
    " Do not invent a parallelism plan: the graph is the plan, and two Tasks you expected to"
    " run together that conflict on ownership are a plan defect to report rather than to"
    " silently serialize around.\n"
    "3. Render the concurrency plan before dispatching anything: which Tasks fan out, which"
    " are forced sequential, and which constraint forces each. The operator sees this before"
    " any Run starts.\n"
    "4. Dispatch each ready Task as its own Run under its own Task scope: one Task, one Run,"
    " one lease, one workspace. Opening a Run needs a compiled run specification, and no"
    " surface this grammar reaches produces one, so the pass stops with"
    " `run_request_uncompilable` rather than sending a request whose halves it invented.\n"
    "5. Resume a Run with `--resume`. That request names the Run and nothing else, so it"
    " completes, and the daemon's answer is reported as it came back.\n"
    "6. Stop and raise for the operator when a dependency proof cannot be satisfied, a Task"
    " exhausts its retry budget, an ownership conflict has no ordering, or the Batch's exact"
    " head moves under the pass.\n"
    "\n"
    "## Invocation\n"
    "\n"
    "```text\n"
    "/dispatch <batch-ref> [--task <ref>...] [--until <frontier-empty|candidate-ready|"
    "attention>] [--max-parallel <N>] [--provider <id>] [--resume <run-ref>] [--budget <spec>]"
    " [--dry-run] [--idempotency-key <key>] [--output <human|json|markdown>]\n"
    "```\n"
    "\n"
    "An option the grammar does not declare fails before anything is dispatched.\n"
    "\n"
    "## Effects boundary\n"
    "\n"
    "Coordinator read models plus the Run dispatch and retry verbs, and nothing else: a call"
    " outside that allowlist is refused before the transport is touched. The coordinator holds"
    " no write scope and no lease, edits no repository, and records no Task completion. A Run"
    " that succeeded has produced a candidate; it has not completed its Task, and integration"
    " is the daemon's job rather than the coordinator's.\n"
    "\n"
    "## Pre-flight checklist\n"
    "\n"
    "- [ ] The Batch reference names a Batch in a native tree; a tree without that authority"
    " refuses at the first read.\n"
    "- [ ] `--max-parallel` matches the resolved ceiling. The ceiling is policy, not"
    " preference: do not raise it, and do not lower it to be safe.\n"
    "- [ ] A Task marked exclusive runs alone, so nothing is dispatched beside it.\n"
    "\n"
    "## Decision surfaces\n"
    "\n"
    "A frontier the read models cannot settle stops the pass at `needs_operator` with a"
    " two-option question: name the ready Tasks with `--task`, or stop and resolve the plan"
    " defect. Stopping is a valid outcome rather than a failure.\n"
    "\n"
    "## Output contract\n"
    "\n"
    'Skill envelope with `header.skill = "/dispatch"`. The body is the `coordination_report`:'
    " the concurrency plan computed before anything moved, every Run addressed with the method"
    " called for it, the frontier remaining, and every condition the pass stopped on."
    " Terminal outcomes are `candidate_ready`, `frontier_empty`, `needs_operator`,"
    " `budget_exhausted`, `blocked` and `cancelled`.\n"
)


_INTEGRATE_BODY = (
    "# /integrate\n"
    "\n"
    "## Canonical algorithm\n"
    "\n"
    "1. Resolve the Delivery Batch, its exact base, its candidate set, its conflict frames and"
    " its current integration generation. Never author a product change, and never choose a"
    " candidate by intuition.\n"
    "2. For `show`, render Batch and conflict truth at the read cursor and mutate nothing."
    " This is the branch that completes today.\n"
    "3. For `select`, apply the declared deterministic policy and explain every inclusion and"
    " every rejection. No read model renders a Batch's sealed candidate set, so the policy has"
    " nothing to order and the branch stops with `candidate_set_unreadable` instead of"
    " inventing one.\n"
    "4. For `seal`, bind the named Run's accepted report to the candidate and attempt the"
    " seal. The request names the Run, the candidate, and the accepted report's schema,"
    " digest and verdict; presented in full, the branch sends exactly that request and"
    " returns the sealed candidate or the checks that still leave it standing. Presented in"
    " part, it stops with `candidate_report_unbound` and names the missing fields.\n"
    "5. For `apply` and `retry`, prove the expected head and the Batch base still match, then"
    " create a fresh hidden generation and leave canonical history untouched until"
    " verification succeeds. The delivery request names the exact base binding, the branch,"
    " one commit subject per sealed candidate, a typed exit per conflict kind and a diagnostic"
    " reference; no surface this invocation reaches resolves them, so the branch stops with"
    " `integration_request_unnamed` rather than fabricating a revision binding.\n"
    "6. Never resolve a conflict by editing a candidate inside this skill.\n"
    "\n"
    "## Invocation\n"
    "\n"
    "```text\n"
    "/integrate <seal|select|apply|retry|show> <batch-or-candidate-ref> [--candidate <ref>...]"
    " [--strategy <declared-strategy>] [--expected-head <sha>] [--verify-after] [--reason"
    " <text>] [--base <revision-binding>] [--exit <kind>=<ref>...] [--diagnostic"
    " <evidence-ref>] [--dry-run] [--run <run-ref>] [--report-schema-ref <ref>]"
    " [--report-digest <digest>] [--verdict <verdict>] [--resulting-tree-digest <digest>]"
    " [--expected-revision <N>] [--idempotency-key <key>] [--output"
    " <human|json|markdown>]\n"
    "```\n"
    "\n"
    "Options irrelevant to the selected branch reject rather than being ignored.\n"
    "\n"
    "## Effects boundary\n"
    "\n"
    "The Batch and conflict read models plus the candidate-report and delivery-integration"
    " verbs, and nothing else: a call outside that allowlist is refused before the transport"
    " is touched. The skill authors no product change, edits no candidate and resolves no"
    " conflict itself. An apply that lands without fresh verification is"
    " integrated-but-unproven, never complete.\n"
    "\n"
    "## Pre-flight checklist\n"
    "\n"
    "- [ ] `--expected-head` names the head the caller actually read, so a moved head reads as"
    " stale rather than as a result.\n"
    "- [ ] The selection policy is declared, not improvised.\n"
    "- [ ] `--verify-after` is set whenever the delivery is meant to be complete rather than"
    " merely applied.\n"
    "\n"
    "## Decision surfaces\n"
    "\n"
    "An ambiguous selection is a typed operator choice, never an invented order. A stale base,"
    " an invalid seal, a conflict, a moved head, a missing receipt or an authority failure all"
    " stop the action with the code that says which rule refused it.\n"
    "\n"
    "## Output contract\n"
    "\n"
    'Skill envelope with `header.skill = "/integrate"`. The body is the'
    " `integration_skill_report`: the candidates considered with the reason for each"
    " disposition, the generations selected, the conflict frames recorded, the verification"
    " receipts bound, and the request fields the branch could not resolve. Terminal outcomes"
    " are `shown`, `sealed`, `selected`, `integrated`, `conflicted`, `stale` and `blocked`.\n"
)


_VERIFY_BODY = (
    "# /verify\n"
    "\n"
    "## Canonical algorithm\n"
    "\n"
    "You verify one Delivery Batch at one exact revision and you do not repair it. You may be"
    " invoked as an auditor or as a reviewer, and they are different jobs: an audit is"
    " closed-world and tries to falsify each required criterion, a review is open-world and"
    " looks for defects nobody wrote a criterion for. Do the one you were assigned.\n"
    "\n"
    "1. Bind the exact head the Batch delivers. Every finding is recorded against that"
    " revision, and a head that moved makes the result stale rather than negative.\n"
    "2. As auditor, attempt to falsify each required criterion and check that the receipts"
    " entail what they claim rather than merely that they exist. One required criterion that"
    " fails or cannot be verified fails the whole audit, whatever the aggregate looks like.\n"
    "3. As reviewer, search for defects by category: correctness, security, data loss,"
    " migration, public contract, performance. Record each as a stable finding with a"
    " repo-relative locus and its evidence.\n"
    "4. The `audit`, `review` and `all` modes walk the Batch verification cycle, which needs"
    " the Batch reference and the judgment criteria the caller names and nothing else, so they"
    " complete and report the daemon's answer.\n"
    "5. The `gates` mode judges one Task's completion, which needs the exact base binding, the"
    " report verdict, the gate specifications its criteria reference and the runtime facts its"
    " proofs ran under. No surface this invocation reaches resolves them, so the mode reports"
    " `unverified` with `proof_receipts_unpresented` and names those fields instead of"
    " presenting invented ones.\n"
    "6. When the invocation names a Milestone and the Batch clears, open the protected"
    " approval the acceptance is taken on, presenting what the acceptance journey showed and"
    " the exact tree it was shown on. The daemon files that journey as the Milestone's"
    " acceptance bundle and binds the question to its digest; report both and never answer"
    " the question, which is a person's to seal.\n"
    "7. Do not resolve your own findings and do not edit the candidate.\n"
    "\n"
    "## Invocation\n"
    "\n"
    "```text\n"
    "/verify <batch-or-revision-ref> [--mode <gates|audit|review|security|all>] [--gate"
    " <id>...] [--severity-floor <P0|P1|P2|P3>] [--agents <1..8>] [--budget <spec>]"
    " [--milestone <ref>] [--journey <step>...] [--accepted-binding <binding>]"
    " [--requested-by <principal>] [--no-cache] [--idempotency-key <key>] [--output"
    " <human|json|markdown>]\n"
    "```\n"
    "\n"
    "## Effects boundary\n"
    "\n"
    "The Batch and evidence read models plus the Batch verification and Task completion verbs,"
    " which file verification receipts, and the verb that opens a Milestone's acceptance"
    " question, and nothing else: a call outside that allowlist is refused before the"
    " transport is touched. The pass receives no producer transcript and no context from the"
    " Run that made the work, resolves no finding, edits no candidate, and answers no"
    " question -- that independence is the point of the job.\n"
    "\n"
    "## Pre-flight checklist\n"
    "\n"
    "- [ ] The subject names one exact revision, not a moving branch.\n"
    "- [ ] Every judgment criterion the audit must cover is named with `--gate`.\n"
    "- [ ] The reviewer did not produce the work being judged, and could not have changed it.\n"
    "\n"
    "## Decision surfaces\n"
    "\n"
    "Absence of evidence is not a pass. A criterion the pass cannot settle reads as"
    " `unverified`, which blocks, and that is the correct outcome. A finding is accepted as"
    " risk only when it is advisory and outside security, migration, data loss, authority,"
    " public contract, required criteria and release proof; everything else is resolved or"
    " superseded.\n"
    "\n"
    "## Output contract\n"
    "\n"
    'Skill envelope with `header.skill = "/verify"`. The body is the `verification_report`:'
    " the head the pass was taken on, the stage the cycle now stands at, the blocking and"
    " settled criteria, and the per-criterion rows the aggregate verdict is derived from, plus"
    " the acceptance approval reference and bundle digest once a named Milestone's Batch"
    " clears. Aggregate verdicts are derived from rows, never asserted. Terminal outcomes are"
    " `passed`, `failed`, `unverified`, `stale` and `blocked`.\n"
)


SKILL_REGISTRY: tuple[SkillSpec, ...] = (
    SkillSpec(
        skill_name="research",
        description=(
            "Read-only investigation of an open question. Produces a research"
            " brief or surfaces findings inline; no code changes, no state"
            " mutations."
        ),
        argument_hint=(
            "<topic-slug> [--depth=shallow|medium|deep|exhaustive] [--final]"
            " [--rounds=<n>] [--agents=<n>] [--budget=<tokens>]"
        ),
        user_invocable=True,
        disable_model_invocation=False,
        body=_RESEARCH_BODY,
    ),
    SkillSpec(
        skill_name="mockup",
        description=(
            "Author 2-4 UI mockups as ASCII layouts and surface them as"
            " side-by-side AskUserQuestion option previews to compare."
        ),
        argument_hint="<surface-slug>",
        user_invocable=True,
        disable_model_invocation=False,
        body=_MOCKUP_BODY,
    ),
    SkillSpec(
        skill_name="spike",
        description=(
            "Read-only multi-axis direction investigation that unblocks"
            " /roadmap propose or /design: N rounds x M axis picks, optional"
            " postmortem + scope deltas. No state mutations."
        ),
        argument_hint=(
            "<spike-slug> [--final] [--from-briefs <path1,path2,...>]"
            " [--postmortem <phase-id>] [--rounds=<n>] [--axes-per-round=<m>] [--worktree]"
        ),
        user_invocable=True,
        disable_model_invocation=False,
        body=_SPIKE_BODY,
    ),
    SkillSpec(
        skill_name="memory",
        description="Save, list, or forget curated durable memory entries.",
        argument_hint="save|list|forget [<name>] [--tier=working|archival|retrieval]",
        user_invocable=True,
        disable_model_invocation=True,
        body=_MEMORY_BODY,
    ),
    SkillSpec(
        skill_name="dispatch",
        description=("Coordinate one Delivery Batch: bring its ready Tasks to a candidate."),
        argument_hint=(
            "<batch-ref> [--task=<ref>] [--max-parallel=<n>] [--resume=<run-ref>] [--dry-run]"
        ),
        user_invocable=True,
        disable_model_invocation=True,
        body=_DISPATCH_BODY,
    ),
    SkillSpec(
        skill_name="integrate",
        description=("Prepare or execute one daemon-owned integration action on a Delivery Batch."),
        argument_hint=(
            "<seal|select|apply|retry|show> <batch-or-candidate-ref>"
            " [--candidate=<ref>] [--expected-head=<sha>] [--verify-after]"
        ),
        user_invocable=True,
        disable_model_invocation=True,
        body=_INTEGRATE_BODY,
    ),
    SkillSpec(
        skill_name="verify",
        description=("Verify one Delivery Batch at one exact revision, as auditor or as reviewer."),
        argument_hint=(
            "<batch-or-revision-ref> [--mode=gates|audit|review|security|all]"
            " [--gate=<id>] [--severity-floor=<P0|P1|P2|P3>] [--no-cache]"
        ),
        user_invocable=True,
        disable_model_invocation=True,
        body=_VERIFY_BODY,
    ),
)


__all__ = [
    "SKILL_REGISTRY",
    "SkillSpec",
]
