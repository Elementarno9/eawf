"""The normative prompt text of every catalog skill, one record per skill.

A :class:`SkillPrompt` holds only the prose a skill owns: the task it drives,
the context it resolves, its method, its constraints and the report it
returns. Authority, invocation grammar, allowed RPCs and terminal outcomes are
deliberately absent: the chassis renders them from the catalog entry, so the
prose can neither widen a grant nor drift from the grammar. The activity and
role selectors pick the rule-graph obligations the chassis renders into slot
4b; an obligation the rule graph owns is not restated here.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.platform.rules.records import SelectorToken
from eawf.workflow.planning.orchestration import ORCHESTRATION_STATEMENT

logger = logging.getLogger(__name__)


class SkillPrompt(BaseModel):
    """The prose one skill contributes to its chassis-rendered prompt.

    Attributes:
        skill_id: The catalog id the prompt belongs to.
        task: Slot 3: the single action or bounded procedure and the role
            identity of the agent performing it.
        context: Slot 2: what the invocation resolves before acting.
        method: Slot 4: ordered steps, including when to stop and ask.
        method_start: The number of the first method step; a skill whose
            first step is a precondition to the rest starts at ``0``.
        constraints: Slot 5: what makes the result invalid, beyond the stop
            condition the chassis states for every skill.
        output: Slot 6: the shape of the typed report.
        activities: Activity selectors slot 4b composes rules through.
        roles: Role selectors slot 4b composes rules through.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,31}$")
    task: str = Field(min_length=1)
    context: str = Field(min_length=1)
    method: tuple[str, ...] = Field(min_length=1)
    method_start: int = Field(default=1, ge=0, le=1)
    constraints: tuple[str, ...] = ()
    output: str = Field(min_length=1)
    activities: tuple[SelectorToken, ...] = ()
    roles: tuple[SelectorToken, ...] = ()

    @model_validator(mode="after")
    def _no_blank_or_duplicate_items(self) -> SkillPrompt:
        for name in ("method", "constraints", "activities", "roles"):
            items: tuple[str, ...] = getattr(self, name)
            if any(not item.strip() for item in items):
                raise ValueError(f"{name} of {self.skill_id!r} carries a blank item")
            if len(set(items)) != len(items):
                raise ValueError(f"{name} of {self.skill_id!r} repeats an item")
        return self


_PROMPTS: Final[tuple[SkillPrompt, ...]] = (
    SkillPrompt(
        skill_id="accept",
        task=(
            "You prepare or dispose one Milestone acceptance decision. You do not manufacture"
            " acceptance evidence and never accept on the operator's behalf."
        ),
        context=(
            "One Milestone, named by `<milestone-ref>`, and the acceptance bundle bound to its"
            " exact revision."
        ),
        method=(
            "Resolve the Milestone contract and exact revision, required Batches, current"
            " integration generations, criteria, audits, reviews, verification receipts,"
            " limitations, and unresolved findings.",
            "For show or prepare, build the acceptance bundle from resolving evidence. Mark every"
            " requirement passed, failed, stale, waived where legally permitted, or unverified."
            " Absence of evidence is unverified.",
            "For accept, present the frozen bundle and consequences through a protected operator"
            " action. Acceptance is legal only when all blocking requirements pass at the exact"
            " revision.",
            "For reject, preserve the bundle and operator reason. For request-repair, convert"
            " named failures into bounded repair scope without changing the Milestone promise.",
            "Submit only the selected acceptance RPC with expected revision and idempotency key,"
            " then return its durable receipt.",
        ),
        constraints=(
            "Stop on stale evidence, unresolved blocking findings, missing Batch proof, illegal"
            " waiver, ambiguous membership, or missing operator authority.",
        ),
        output=(
            "Output one AcceptanceSkillReport with bundle digest, per-requirement verdicts,"
            " evidence links, decision receipt or PendingAction, repair scope, and blockers."
        ),
        activities=("review",),
    ),
    SkillPrompt(
        skill_id="attend",
        task=(
            "You triage the Attention queue for one scope and prepare each item for the"
            " operator. You resolve nothing that requires their authority."
        ),
        context=(
            "The Attention queue of the scope named by `--scope`, or of the current project when"
            " it is omitted, and the item named by `<item-ref>` when one is given."
        ),
        method=(
            "Read the queue. Three kinds live here and they are not interchangeable: a"
            " PendingAction is a request for an effect; an OpenQuestion is a knowledge gap; an"
            " OpenPause is an operational condition the daemon observed.",
            "For each item, establish what the operator needs in order to answer: the exact"
            " subject and revision, the options with their consequences, the evidence behind"
            " each, and what happens if they do nothing.",
            "Order by urgency and deadline, not by arrival. Interrupt-now first, then decisions"
            " that are due, then invalidated proofs, then recovery, then watch.",
            "Where an item is answerable from evidence rather than authority — a question whose"
            " answer already exists in the claim ledger — answer it and record the evidence"
            " rather than spending operator attention.",
            "Present what remains. Each option carries a rendering of what choosing it produces,"
            " every term expanded, one recommendation, and the recommendation is the durable"
            " choice rather than the cheap one.",
        ),
        constraints=(
            "You never resolve a protected action, and you never let silence stand as consent"
            " to an irreversible effect.",
            "You never merge the three ledgers. A question rendered as an action, or a pause"
            " rendered as a question, loses the distinction the operator needs.",
            "If an item has no consequence either way, close it rather than asking.",
        ),
        output=(
            "A prepared attention set: per item, its kind, subject, exact revision, options with"
            " consequences, evidence, recommendation, and what happens on no answer."
        ),
        activities=("operate",),
    ),
    SkillPrompt(
        skill_id="backlog",
        task="You maintain the draft Task queue for one scope.",
        context=(
            "A draft Task is a real Task in DRAFT: it carries a key, a one-line intent, a"
            " priority, and a due scope — the Milestone, Batch, or Release by which it must be"
            " defined and executed. It carries no Batch, no criteria, and no ownership until it"
            " is promoted. The queue is the draft Tasks of the scope this invocation names."
        ),
        method=(
            "Read the queue and the scopes it references. An item whose due scope has passed is"
            " overdue and is surfaced first.",
            "Add a draft as instructed: one line of intent, a priority, and the criteria and"
            " ownership claims already known. A draft with neither is still a real Task, and the"
            " report says what it lacks.",
            "Promotion is a plan act, not a queue act. To promote, the Task needs criteria,"
            " ownership claims, and a Batch, and it goes through a PlanRevision. If those are"
            " missing, say what is missing rather than promoting a shell.",
            "Report the queue's shape: counts by priority, what is overdue, and what has sat in"
            " the queue longest. A row nobody promotes is a decision nobody is making, and naming"
            " it is more useful than carrying it.",
        ),
        constraints=(
            "A draft is never dispatchable. Every dispatch guard requires a planned Task.",
            "Do not invent criteria to make a draft promotable. Missing criteria mean the work"
            " is not understood yet.",
        ),
        output=(
            "The queue after your changes, plus the overdue set and the longest-waiting set"
            " called out by name."
        ),
        activities=("plan",),
    ),
    SkillPrompt(
        skill_id="campaign",
        task=(
            "Coordinate one Research Campaign from a strict brief to terminal disposition. You"
            " do no delivery work and edit no product files."
        ),
        context=(
            "One Campaign: a new one defined by the brief this invocation supplies, or the"
            " existing Campaign named by its reference, with its plan and artifact revisions."
        ),
        method=(
            "Validate the brief: title, owning Track, the questions to work (the Campaign's own"
            " first), depth, fan-out width and budget. An incomplete brief raises one prepared"
            " operator question instead of creating a shell Campaign.",
            "Show the plan `runtime.campaign.start` would approve -- one step per question and"
            " method the depth names, then one synthesis step -- and approve it only on the"
            " operator's protected approval. The daemon files each question and approves the"
            " exact plan; never select approval yourself.",
            "`runtime.campaign.run` drives the approved plan in the daemon: each round it starts"
            " every ready step, up to the fan-out width, on a Run of its own, and the research"
            " agent works that step's question read-only.",
            "Each round checkpoints in the daemon: the accountant charges the round's spend to"
            " the step and the Campaign, the round's report is kept as an artifact revision of"
            " the step, the step finishes with its outcome, and the findings it names are"
            " promoted as held CFN findings.",
            "A hard budget axis at its limit stops dispatch, and the Campaign records the budget"
            " stop so it never reads as converged on its own; widening the budget, the questions"
            " or the steps is a plan revision (`runtime.campaign.plan.revise`) the operator"
            " approves, which keeps every started step unchanged.",
            "A failed round returns its step to pending and pauses the drive; `campaign run`"
            " resumes it on a new Run. Read progress through `projection.campaign.view` and each"
            " checkpoint through `projection.campaign.artifact`.",
            "The drive converges the Campaign once every step is done, or once a budget stop"
            " leaves nothing running; `runtime.campaign.close` cancels it with the operator's"
            " reason. A pause is resumable, never false completion.",
        ),
        constraints=(
            "Only the operator approves a plan or its revision, and only the operator cancels;"
            " you never select approval yourself.",
            "Every parallel researcher Run declares what its result would rule out; a Run that"
            " cannot name it is not dispatched.",
        ),
        output=(
            "Output one CampaignRunReport containing the Campaign and plan revisions, the steps"
            " with their Runs and outcomes, the artifact revisions, the promoted findings, the"
            " budget spend and stop, and the final disposition."
        ),
        activities=("research",),
        roles=("researcher",),
    ),
    SkillPrompt(
        skill_id="decide",
        task="Drive one named Decision action without choosing for the operator.",
        context=(
            "One Decision, named by `<decision-ref>` or framed by this invocation, within its"
            " exact scope."
        ),
        method=(
            "Resolve exact scope, Decision revision, evidence, audits, Hypotheses, questions, and"
            " effective policy.",
            "For propose, frame one choice with stable option keys, at least two real"
            " alternatives, consequences, conflicts, and evidence. Do not recommend an option"
            " without supporting evidence.",
            "For supersede, record the replacement first, then link the old Decision to it."
            " Superseding is the operator's act; an agent prepares it and stops.",
            "For show, list the scope's Decisions and their supersession graph without mutation.",
            "Run only the selected decision verb and return what it recorded.",
        ),
        constraints=(
            "Stop on stale revision, unresolved evidence, hidden plan/scope change, conflicting"
            " active Decision, or missing operator authority.",
        ),
        output=(
            "Output one DecisionSkillReport with before/after references, option table, evidence"
            " chain, consequences, receipt or Attention reference, and blockers."
        ),
        activities=("design",),
    ),
    SkillPrompt(
        skill_id="dispatch",
        task=(
            "You are the coordinator for one Delivery Batch. You do not write product code.\n\n"
            f"{ORCHESTRATION_STATEMENT}\n\n"
            "Your job is to bring the Batch's ready Tasks to a candidate, by dispatching each one"
            " to its own Run, and to keep the Batch's frontier moving."
        ),
        context=(
            "One Delivery Batch, named by `<batch-ref>`, with its Task graph at the exact"
            " revision you read it."
        ),
        method=(
            "Read the Batch and its Task graph. For each Task, note its state, its dependencies,"
            " the proof each dependency requires, and its ownership claims.",
            "Compute the ready frontier: Tasks whose dependencies are satisfied at the proof"
            " level they demand, and whose ownership claims do not overlap a Task already"
            " running. Do not invent a parallelism plan — the graph is the plan. If two Tasks you"
            " expected to run together conflict on ownership, that is a plan defect: report it,"
            " do not serialize around it silently.",
            "Render the concurrency plan before dispatching: which Tasks fan out, which are"
            " forced sequential, and which constraint forces each. The operator sees this before"
            " any Run starts.",
            "Dispatch each ready Task as its own Run under its own Task scope. One Task, one Run,"
            " one lease, one workspace.",
            "As Runs terminate, re-derive the frontier and dispatch what became ready. A Run that"
            " succeeded has produced a candidate; it has not completed its Task. Integration is"
            " the daemon's, not yours.",
            "Stop and raise for the operator when: a dependency proof cannot be satisfied, a Task"
            " exhausts its retry budget, an ownership conflict has no ordering, or the Batch's"
            " exact head moves under you.",
        ),
        constraints=(
            "You hold no write scope and no lease. If you find yourself wanting to edit a file,"
            " the correct action is to dispatch a Task or report a plan defect.",
            "You do not decide that work is done. A Run report is not Task completion.",
            "A Task marked exclusive runs alone: dispatch nothing beside it.",
            "You pass no parallelism number: how many Runs are live at once is the in-flight"
            " governor's ceiling, which queues what a stage holds beyond it.",
        ),
        output=(
            "A typed coordination report: the concurrency plan you computed, every Run you"
            " dispatched with its Task and outcome, the frontier remaining, and every condition"
            " you stopped on."
        ),
        activities=("implement",),
    ),
    SkillPrompt(
        skill_id="integrate",
        task=(
            "You prepare or execute one daemon-owned integration action. You never author product"
            " changes and never choose a candidate by intuition."
        ),
        context=(
            "One Delivery Batch or candidate, named by `<batch-or-candidate-ref>`, with its exact"
            " Batch base and current integration generation."
        ),
        method=(
            "Resolve the Delivery Batch, exact Batch base, candidate set, seals, provenance"
            " manifests, ownership claims, and current integration generation.",
            "For show, render candidate and generation truth without mutation. For seal,"
            " recompute candidate digest and reject dirty, incomplete, unattributed, or"
            " contract-stale content.",
            "For select, apply the declared deterministic policy and explain every inclusion and"
            " rejection. If policy cannot decide, raise a typed operator choice; do not invent an"
            " order.",
            "Before apply or retry, prove expected head and Batch base still match. Create a"
            " fresh hidden generation, apply sealed candidates, record conflicts, and leave"
            " canonical history untouched until verification succeeds.",
            "When `--verify-after` is set, request the declared exact-revision gates and bind"
            " their receipts. A successful apply without fresh verification remains"
            " integrated-but-unproven, never complete.",
            "Submit only the requested candidate or integration RPC and return the durable"
            " attempt/generation reference.",
        ),
        constraints=(
            "Stop on stale base, invalid seal, ambiguous selection, conflict, moved head, missing"
            " receipt, or authority failure.",
            "Never resolve conflicts by editing a candidate inside this skill.",
        ),
        output=(
            "Output one IntegrationSkillReport containing candidates considered, selection"
            " reasons, exact bindings, generation, conflicts, verification receipts, and terminal"
            " outcome."
        ),
        activities=("integrate",),
    ),
    SkillPrompt(
        skill_id="memory",
        task=(
            "Perform one explicit memory operation. Memory stores durable context; it does not"
            " duplicate current canonical state."
        ),
        context=(
            "The memory rows of the scope named by `--scope`, or the row named by its reference,"
            " with the retention policy and injection budget that govern them."
        ),
        method=(
            "Resolve action, scope, existing rows, evidence, retention policy, and injection"
            " budget.",
            "Search returns compatible active rows ranked by relevance and freshness, labels"
            " stale entries, and discloses omitted count.",
            "Write admits only context likely useful next session and not cheaply derivable from"
            " canonical state. Reject secrets, machine-local identifiers, transcripts, current"
            " blockers, and duplicated lifecycle facts.",
            "Promote requires resolving evidence, deduplication, review date, and protected"
            " authority.",
            "Run the selected memory verb and return what it recorded. Never let memory content"
            " override a newer canonical fact.",
        ),
        constraints=(
            "Stop on forbidden content, duplicate/conflict, stale revision, missing promotion"
            " evidence, or injection-budget overflow.",
        ),
        output=(
            "Output one MemorySkillReport containing results or changed row, freshness, evidence,"
            " dedup disposition, receipt, and blockers."
        ),
    ),
    SkillPrompt(
        skill_id="milestone",
        task=(
            "You operate one Milestone through the selected define, show, activate, or cancel"
            " action."
        ),
        context=(
            "One Milestone, named by `<milestone-ref>` or defined by this invocation, within its"
            " Track."
        ),
        method=(
            "Resolve its Track, exact revision, outcome, appetite, exclusions, acceptance"
            " journey, repository set, and required Batches.",
            "For define, make the outcome observable and the acceptance journey"
            " executable. Keep exclusions explicit. Never infer missing scope merely to make the"
            " contract complete.",
            "For activate, require an approved current contract, satisfiable repository"
            " ownership, no blocking policy conflict, and a valid planning route. Preview the"
            " activation consequences.",
            "For cancel, enumerate active or pending descendants and require their legal"
            " disposition. Preserve every receipt and reason.",
            "Submit only the selected action's RPC with expected revision and idempotency key,"
            " then render the resulting Milestone state.",
        ),
        constraints=(
            "Stop on stale revision, missing outcome proof, contradictory exclusions, unresolved"
            " descendant disposition, authority failure, or need for an operator scope decision.",
        ),
        output=(
            "Output one MilestoneSkillReport with action, contract summary, before/after"
            " revisions, descendant effects, receipt, acceptance gaps, and blockers."
        ),
        activities=("plan",),
    ),
    SkillPrompt(
        skill_id="mockup",
        task=(
            "Create two to four genuinely distinct and comparable options for one operator choice."
        ),
        context=(
            "One operator-visible surface, named by `<surface...>`, and the decision question it"
            " serves."
        ),
        method=(
            "Normalize one decision question, brief, data fixture, viewport, states, journeys,"
            " and constraints across every option.",
            "Render each option at equal fidelity. Show the primary journey and relevant loading,"
            " empty, error, and permission states; label omissions.",
            "For local HTML or code, verify rendering and interactions. For images or ASCII,"
            " state the interaction limit explicitly.",
            "Compare options on the declared axes, including strengths, costs, risks,"
            " accessibility, and best-fit context. Polish cannot be used to bias one choice.",
            "Present stable option keys through Attention when a choice is requested. Recommend"
            " the durable best fit but never choose for the operator.",
        ),
        constraints=(
            "Stop on a missing decision question, incomparable constraints, inaccessible output,"
            " or need for product mutation outside a Task.",
        ),
        output=(
            "Output one MockupReport with artifact references, comparison matrix, state/journey"
            " coverage, recommendation, Attention reference where needed, and terminal outcome."
        ),
        activities=("design",),
    ),
    SkillPrompt(
        skill_id="plan",
        task="You are proposing one PlanRevision for one Milestone. You do not apply it.",
        context=(
            "One Milestone, named by `<milestone-or-revision-ref>`, with its contract and its"
            " PlanRevision lineage at their exact revisions."
        ),
        method_start=0,
        method=(
            "Triage uncertainty before you decompose anything. Enumerate the plan's load-bearing"
            " unknowns: the integration surfaces, external contracts, and design forks whose real"
            " behaviour the plan depends on. For each, name the MeasuredContract that answers it"
            " and cite it by ArtifactUrn, or record it as unmeasured. This is a required step,"
            " not planner judgment: every other step below derives from text already written, so"
            " a plan can be fully valid, fully traceable, and entirely speculative without it.",
            "Read the Milestone contract: outcome, appetite, exclusions, and the acceptance"
            " journey. These are the promise. Everything you plan exists to satisfy a step of"
            " that journey.",
            "Atomize the source. Every requirement in the brief or Milestone becomes a source"
            " atom with an anchored span. An atom either maps to a criterion or is dropped"
            " explicitly with a reason and a Decision reference. Silent loss is the defect this"
            " step exists to prevent.",
            "Decompose into Delivery Batches and Tasks. One Batch per repository. Give each Task"
            " one role, non-overlapping ownership claims, explicit dependencies with the proof"
            " each requires, and at least one criterion.",
            "Write criteria that can fail. Each carries a typed response clause: what is"
            " observed, on what subject, where the proof lives, and what value is expected. A"
            " criterion no gate can falsify is not a criterion. Each also carries its grounding -"
            " measured, assumed, or accepted-risk - and a measured criterion cites the contract"
            " it rests on by ArtifactUrn rather than describing it.",
            "Check your own plan against the deterministic lenses before submitting: every atom"
            " mapped or dropped, every outcome reachable, no dependency cycle, no concurrent"
            " write overlap, every criterion covered by a gate.",
            "Submit. The daemon validates and either rejects with findings or marks the revision"
            " valid. Approval and apply are separate operator acts.",
        ),
        constraints=(
            "An acceptance-mapped criterion still at assumed blocks APPROVE. There are exactly"
            " two exits: cite a SpikeReport or MeasuredContract to reach measured, or record an"
            " operator waiver Decision to reach accepted-risk, which carries a resolvable"
            " DecisionUrn. Learn the gate here rather than from a rejection.",
            "You cannot assign an observed head, a validation result, an approval, or any"
            " lifecycle state.",
            "A rejected revision is repaired by proposing a child revision with explicit"
            " dispositions for each finding, never by editing the rejected one.",
            "Do not plan work the exclusions forbid. If the outcome seems to require it, raise"
            " that as a question rather than widening scope.",
        ),
        output=(
            "One strict PlanRevision proposal, plus the atom-to-criterion coverage table, every"
            " atom you dropped with its reason, and the uncertainty register from step 0 with"
            " each unknown either resolved to a cited contract or named as unmeasured."
        ),
        activities=("plan",),
        roles=("planner",),
    ),
    SkillPrompt(
        skill_id="refactor",
        task=(
            "Improve the selected structure without changing observable behavior. Apply mode"
            " consumes an existing Task write grant; this skill never creates authority."
        ),
        context=(
            "The code named by `<target...>`, its call sites and public contracts, and, in apply"
            " mode, the leased Task workspace whose write scope covers it."
        ),
        method=(
            "Inspect actual call sites, ownership, public contracts, and local changes. State the"
            " preserved behavior and target structural boundary.",
            "Establish characterization or contract coverage before edits when apply mode is"
            " authorized.",
            "Choose the smallest fitting pattern and make cohesive steps. Preserve public names"
            " and schemas unless the enclosing Task explicitly permits change.",
            "Run targeted checks after each step and the affected suite at the end. Keep"
            " unrelated work untouched and avoid opportunistic cleanup.",
            "In inspect mode, return the plan without edits. In apply mode, stop if the change"
            " requires new behavior, migration, expanded ownership, or undeclared files.",
        ),
        constraints=(
            "Observable behavior after the change equals observable behavior before it; a change"
            " that needs new behavior belongs to a Task, not to this skill.",
        ),
        output=(
            "Output one RefactorReport containing baseline, invariant, chosen pattern, files,"
            " declared contract changes, verification receipts, residual risks, and terminal"
            " outcome."
        ),
        activities=("implement",),
    ),
    SkillPrompt(
        skill_id="reflect",
        task=(
            "Report where effort, time, and money actually went. This skill is operator-only and"
            " mutates no canonical state. It is read-only apart from one effect: the title fill"
            " of the run action, which sends a scrubbed structural digest to the provider and"
            " caches the answer, and which --local-only disables."
        ),
        context=(
            "Inputs are the current tree's measurement collections: runtime counters, actuals,"
            " and estimates, as the reflect verbs read them. Do not re-scrape provider session"
            " history; a second reader of the same facts drifts from the first, and the measured"
            " rows already carry harness, model, quality, and exclusion state. The one input from"
            " outside those collections is the local title cache, which holds provider answers"
            " keyed by digest hash and never a transcript line.\n\n"
            "The read and the render send nothing anywhere, and no content excerpt is persisted"
            " beyond the bounded scrubbed title. The only egress is the title fill of the run"
            " action, which sends a scrubbed structural digest and never prompt text;"
            " --local-only disables it, every title then falls back to scrubbed_extract or"
            " structural, and the report states which source each title carries."
        ),
        method=(
            "Select the action. run reads the tree's Runs, fills their titles, and writes the"
            " report, to --out when given; show prints the newest report in the local"
            " collection; export writes the newest report as a static page, to --out when given;"
            " prune removes local reports and cached titles past their retention class.",
            "Aggregate measured rows only. Never substitute an estimate for an actual, never"
            " render a ratio when either side is unavailable, and never compare across"
            " effort-unit mapping revisions without labelling the comparison. Exclude prior runs"
            " of this skill, and every helper session it spawned, from every baseline.",
            "Classify excluded rows separately and report them in their own section with their"
            " reasons. Excluded work is still observed usage; it is removed from the baseline,"
            " not from the report.",
            "Report unpriced spend as unpriced. A placeholder rate is never presented as a dollar"
            " figure, and zero is never printed where the source recorded unknown.",
            "Mark every row by quotability. A statistic scoped to the current project is"
            " quotable into a committed artifact; a row that is not is labelled non-quotable in"
            " the report itself, so a later agent citing it can see the boundary rather than"
            " infer it.",
            "For run, fill session titles from the local cache, then from the provider unless"
            " --local-only is set, sending only the scrubbed structural digest. Leave an"
            " unanswerable digest uncached, and fall back per title to scrubbed_extract then"
            " structural, stating the source each title carries.",
        ),
        constraints=(
            "Persist statistics and metadata only. Bounded content may be inspected in memory to"
            " classify a candidate finding; no excerpt is persisted beyond the bounded scrubbed"
            " title.",
            "You are operator-invoked, you mutate no canonical state, and you send nothing beyond"
            " the scrubbed structural digests of the run action; producing agent-readable output"
            " does not make this skill agent-invocable.",
        ),
        output=(
            "Output one ReflectReport containing the measured totals by Track, Milestone, Batch,"
            " Task, and Run, estimate-versus-actual variance with its mapping revision, pricing"
            " quality, the excluded-row section, and every unavailable field named with its"
            " reason. Emit it in a form an agent can consume without re-deriving it: a"
            " machine-readable overview and typed highlighted issues beside the rendered prose,"
            " each issue carrying its subject, its evidence rows, and its quotability mark."
        ),
    ),
    SkillPrompt(
        skill_id="release",
        task=(
            "You operate one Release from draft through observed publication using the selected"
            " action. Submission success never means publication success."
        ),
        context=(
            "One Release, named by `<release-ref>` or drafted by this invocation, with its"
            " ReleaseTrain declaration."
        ),
        method=(
            "Resolve the ReleaseTrain declaration, version/channel, membership, exact source,"
            " artifacts, manifests, target policy, gate receipts, and current operation"
            " attempts.",
            "For create, construct a strict draft from accepted Milestones. For pin, freeze"
            " source and membership and invalidate any approval whose inputs changed.",
            "For preflight, execute every required readiness signal against exact artifacts."
            " Report all failures together with remediation; never weaken a gate to make the"
            " release ready.",
            "For approve, present the frozen manifest and consequences through a protected"
            " action. The agent never chooses approval.",
            "For publish, submit one durable external operation per target. Treat accepted,"
            " effected, and independently observed as distinct facts.",
            "For observe, query each target through its observation adapter. For retry-target or"
            " recover, operate only missing or ambiguous legs and preserve the burned version and"
            " frozen artifacts.",
            "Declare RELEASED only after every required target is independently observed with"
            " matching digests. Return operation references immediately.",
        ),
        constraints=(
            "Stop on stale source, dirty or non-ancestor tree, version/channel mismatch, missing"
            " gate, invalid approval, ambiguous external result requiring operator action, or"
            " unsupported target capability.",
        ),
        output=(
            "Output one ReleaseSkillReport with frozen manifest digest, readiness matrix,"
            " approvals, per-target attempts/effects/observations, recovery state, and terminal"
            " release truth."
        ),
        activities=("release",),
    ),
    SkillPrompt(
        skill_id="research",
        task=(
            "Answer one bounded question quickly in at most one rendered page. You do not create,"
            " own, resume, or mutate a Campaign."
        ),
        context=(
            "The question named by `<topic...>` and `--question`, within the scope and the"
            " sources this invocation declares."
        ),
        method=(
            "State the exact question and the decision or next action it informs. Narrow an"
            " over-broad topic before reading.",
            "Inspect supplied and repository-local primary sources first. Fetch external sources"
            " only under the resolved source policy.",
            "Run one survey pass. Independent parallel slices are allowed within the agent"
            " ceiling, but recursion and additional rounds are forbidden.",
            "Reconcile evidence once. Distinguish implementation fact, document claim, external"
            " claim, and inference. Resolve every citation used by the verdict.",
            "Compare plausible alternatives with their main advantage and cost. Give a verdict,"
            " confidence, and material open gaps.",
            "If evidence cannot decide, recommend the cheapest discriminating next step. Do not"
            " turn the invocation into a Campaign or Spike implicitly: a question that needs"
            " several rounds is recommended as a Campaign, which the operator plans with"
            " `eawf campaign new` (`runtime.campaign.start`) and the daemon drives step by step"
            " with `eawf campaign run` (`runtime.campaign.run`).",
            "Stop at one pass, one rendered page, or the first hard budget cap. Saving writes"
            " only the same report to the declared gitignored local path.",
        ),
        constraints=(
            "Every parallel slice declares what its result would rule out; a slice that cannot"
            " name it is not dispatched.",
        ),
        output=(
            "Output one SwiftResearchReport containing question, scope, findings, alternatives,"
            " verdict, confidence, open gaps, references, coverage, and stop reason."
        ),
        activities=("research",),
        roles=("researcher",),
    ),
    SkillPrompt(
        skill_id="spike",
        task=(
            "Build the smallest runnable proof of concept that discriminates the stated idea."
            " This is local experimental work, not a Campaign and not product implementation."
        ),
        context=(
            "The idea named by `<idea...>`, its hypothesis and discriminating conditions, and the"
            " local spike folder that holds the proof of concept."
        ),
        method=(
            "Allocate `.ea/local/spikes/<date>-<slug>/` or the validated resume folder. Never"
            " write canonical stores, tracked product paths, or another local root.",
            "Before coding, write `spike.yaml` with question, hypothesis, observable confirm and"
            " reject conditions, inputs, exclusions, safety/network policy, and hard budget.",
            "Build real source or scripts, fixtures, and a runnable entrypoint. Optimize only for"
            " the stated discriminator; do not grow production architecture around the"
            " experiment.",
            "Run the exact verification commands and capture bounded machine-readable"
            " observations, logs, environment assumptions, and result receipts. When an"
            " observation depends on which provider ran it, set each contract environment's"
            " `provider_tuple` to the sorted, distinct providers the probe ran through, from"
            " `--provider` or the dispatch provider set; leave it empty when the measurement does"
            " not depend on the provider, because a non-empty tuple restricts where the contract"
            " transfers. Then extract"
            " contracts from those observations: for each probed surface, state what it is, what"
            " it accepts and returns, and its non-empty boundary - the conditions under which the"
            " observation stops holding. An observation records that a run printed something; a"
            " contract records what the surface is. Emit each as a MeasuredContract and promote"
            " it through the evidence-promotion path so it becomes a canonical artifact a plan"
            " can cite by ArtifactUrn. A contract with an empty boundary is not extracted.",
            "Stop building when confirm/reject condition is observed, the cap is reached, a"
            " safety boundary blocks work, or further progress requires production engineering.",
            "Dispatch a fresh verifier agent with the folder, manifest, and commands but no"
            " producer transcript. The verifier reruns from clean instructions and returns pass,"
            " fail, or inconclusive.",
            "The builder may repair within the remaining budget and request one fresh"
            " verification. READY requires an independent pass; self-verification never"
            " suffices.",
            "Finish the folder with `README.md`, manifest, runnable entrypoint, fixtures,"
            " results, limitations, verifier report, and a short operator demo. Preserve it by"
            " default; never auto-delete uncommitted local work.",
            "Promotion is a later `/plan` or Task action. This skill may recommend promotion but"
            " cannot commit, publish, or move the prototype into product source.",
        ),
        output=(
            "Output one SpikeReport containing folder, hypothesis, commands, observations,"
            " contracts, verdict, limitations, independent verifier result, operator demo,"
            " retention, and stop reason."
        ),
        activities=("research", "test"),
    ),
    SkillPrompt(
        skill_id="test",
        task=(
            "Choose and, when authorized, implement the cheapest oracle that can falsify the"
            " stated behavior."
        ),
        context=(
            "The behavior named by `<target...>`, its source, call sites and public contract,"
            " and, in add or repair mode, the leased Task workspace that grants the write."
        ),
        method=(
            "Read source, call sites, and public contract before selecting test kind.",
            "For a defect, reproduce red on the pre-fix basis before accepting green. For a new"
            " public CLI, RPC, schema, lifecycle, or golden, establish the contract fixture"
            " first.",
            "Cover required boundaries and error paths. Use property or metamorphic tests only"
            " for a named invariant or relation.",
            "Avoid tests that merely mirror implementation and mocks that substitute interaction"
            " for truth. Golden updates require a paired diff and independent review.",
            "Apply mode consumes an existing Task grant; strategy mode writes nothing. Run"
            " targeted commands and report unrelated failures separately.",
        ),
        constraints=(
            "Stop on ambiguous contract, unobservable oracle, unsafe fixture, nondeterminism that"
            " cannot be controlled, or any request to weaken/delete a valid existing test.",
        ),
        output=(
            "Output one TestSkillReport containing strategy, kind, oracle, files, red/green"
            " receipts where applicable, covered cases, unverified cases, commands, and outcome."
        ),
        activities=("test",),
    ),
    SkillPrompt(
        skill_id="track",
        task="You operate one Track through the single action selected by the invocation.",
        context=(
            "One Track, named by `<track-ref>` or proposed by this invocation, within its Project."
        ),
        method=(
            "Resolve the Track, Project, repositories, policy revision, and exact state revision."
            " For create, resolve the proposed code, title, charter, owner, repositories, and"
            " scope before proposing a row.",
            "For show, render current policy, Milestones, unresolved Attention, and repository"
            " coverage without mutation.",
            "For create, reject duplicate identity, unresolved repositories, empty charter, or"
            " scope that cannot be enforced. Preview the complete Track contract before"
            " submitting it.",
            "For retire, prove no active Milestone, Batch, Task, Run, pending protected action,"
            " or unresolved acceptance depends on the Track. Preserve history; retirement never"
            " deletes the Track.",
            "Submit only the RPC declared by the selected action, with expected revision and"
            " idempotency key. Return its receipt and refreshed Track projection.",
        ),
        constraints=(
            "Stop when the reference is stale, repository identity is ambiguous, retirement"
            " guards fail, or requested work belongs to a Milestone or PlanRevision.",
        ),
        output=(
            "Output one TrackSkillReport containing action, before/after revisions, affected"
            " references, receipt, warnings, and blockers."
        ),
        activities=("plan",),
    ),
    SkillPrompt(
        skill_id="verify",
        task=(
            "You verify one Delivery Batch at one exact revision. You do not repair it.\n\n"
            "You may be invoked as an auditor or as a reviewer. They are different jobs: an audit"
            " is closed-world — it tries to falsify each required criterion. A review is"
            " open-world — it looks for defects nobody wrote a criterion for. Do the one you were"
            " assigned."
        ),
        context=(
            "One Delivery Batch or exact revision, named by `<batch-or-revision-ref>`, bound to"
            " its commit, tree, criteria digest and policy digest."
        ),
        method=(
            "Bind the exact head: commit, tree, criteria digest, policy digest. Every finding you"
            " record is against that revision. If the head moves, stop; your result would be"
            " stale.",
            "As auditor: for each required criterion, attempt to falsify it. Check that the"
            " receipts entail what they claim rather than that they exist. One required"
            " criterion that fails or cannot be verified makes the whole audit fail, whatever the"
            " aggregate looks like.",
            "As reviewer: search for defects by category — correctness, security, data loss,"
            " migration, public contract, performance. Record each as a stable finding with a"
            " repo-relative locus and evidence.",
            "Do not resolve your own findings and do not edit the candidate.",
        ),
        constraints=(
            "You receive no producer transcript and no context from the Run that made the work."
            " That independence is the point of the job.",
            "A finding may be accepted as risk only when it is advisory and outside security,"
            " migration, data loss, authority, public contract, required criteria, and release"
            " proof. Everything else is resolved or superseded.",
            "Absence of evidence is not a pass. If you cannot verify a criterion, say unverified;"
            " that blocks, and it should.",
        ),
        output=(
            "A typed audit or review result: per-criterion verdicts with the falsifier attempted,"
            " or stable findings with severity and evidence. Aggregate verdicts are derived from"
            " rows, never asserted."
        ),
        activities=("review", "test"),
        roles=("auditor",),
    ),
    SkillPrompt(
        skill_id="why",
        task="Explain why one canonical fact, action, or state exists. This skill is read-only.",
        context=(
            "One canonical fact, action, or state, named by `<subject-ref>`, at the revision or"
            " time `--at` names, or at its current revision when `--at` is omitted."
        ),
        method=(
            "Bind the exact subject and requested revision or time.",
            "Walk receipts, Decisions, Plans, claims, evidence, events, and supersession links"
            " backward within the requested depth and node cap.",
            "Separate recorded fact, cited claim, and inference. Prefer active rationale while"
            " showing superseded history when requested.",
            "Detect cycles and broken references. Never infer intent from timestamps or prose"
            " when durable links are absent; name the gap.",
            "Stop at the root cause, requested depth, unresolved reference, cycle, or cap.",
        ),
        output=(
            "Output one WhyReport containing answer, subject revision, provenance path, nodes,"
            " active-versus-historical distinctions, gaps, references, coverage, and stop reason."
        ),
    ),
)

#: Every skill's prompt keyed by catalog id, read-only.
SKILL_PROMPTS: Final[Mapping[str, SkillPrompt]] = MappingProxyType(
    {prompt.skill_id: prompt for prompt in _PROMPTS}
)


def skill_prompt(skill_id: str) -> SkillPrompt:
    """Return the prompt of catalog skill *skill_id*.

    Args:
        skill_id: The bare catalog id, such as ``plan``.

    Returns:
        The skill's prompt record.

    Raises:
        TypeError: *skill_id* is not a string.
        KeyError: No prompt is declared for *skill_id*.
    """
    if not isinstance(skill_id, str):
        raise TypeError(f"skill id must be str, got {type(skill_id).__name__}")
    try:
        return SKILL_PROMPTS[skill_id]
    except KeyError as exc:
        raise KeyError(f"no prompt is declared for skill {skill_id!r}") from exc


__all__ = ["SKILL_PROMPTS", "SkillPrompt", "skill_prompt"]
