# Concepts

*Glossary for Eä lifecycle, state, runtime, and evidence terms.*

Eä is an agent-driven development framework. The CLI and runtime adapters are operator surfaces; durable truth lives in the typed generation document, its append-only ledgers, the JSONL stores, and committed artifacts.

## Lifecycle

The containment tree is Workspace, Project, Track, Milestone, Batch, Task. A Run is one agent's execution episode against one of those entities, and a Release packages accepted Milestones across Tracks beside that chain rather than inside it.

**Workspace**
: The machine-registered group of projects a set of URNs is minted under. A workspace names its home project and its member projects in the machine registry.

**Project**
: The top-level repository or product tracked by Eä. A project carries a short code that prefixes its Task keys.

**Repository**
: The integration target a Batch lands its Tasks into. A repository row records the head it was admitted at; a plan binds that head and an apply refuses once it has moved.

**Track**
: A long-lived line of work that owns Milestones. A Track is either `ACTIVE` or `RETIRED`, and its policy carries the domain variation a profile needs.

**Milestone**
: The unit an operator accepts. A Milestone states its outcome, its explicit exclusions, the acceptance journey that proves the outcome and the Batches that must complete first. It moves through `PLANNED`, `ACTIVE`, `ACCEPTANCE_REVIEW` and `COMPLETED`, or ends `CANCELLED`, and it ships through one pull request.

**Batch**
: A delivery Batch: the only thing that merges, into exactly one repository. A Batch binds the exact head it claims to be mergeable at, so a stale proof cannot pass as a fresh one.

**Task**
: The smallest planned execution unit. A Task carries its intent, priority, success criteria, write claims and Batch. A backlog row is a `DRAFT` Task, so one key survives from first idea to completed delivery; a Task is dispatchable only from `PLANNED`.

**Run**
: One execution episode of one agent. A Run addresses one scope (a Task, a Batch, a Milestone, a Release and so on), and only a Task-scoped Run may write. A suspended Run names the reason whose clearing resumes it.

**PlanRevision**
: The immutable plan a Milestone is materialised from: one Milestone, its Batches and their Tasks, bound to the state revision, repository heads and policy it was written over and to the digest of its own content. Plans go through `eawf plan submit`, `approve` and `apply`; an operator approves a digest, never "the plan".

**Release**
: The packaging of accepted Milestones into one published version, proved, approved and published through `/release`.

**Campaign**
: A research plan of bounded steps, each a Run over one question, with the artifact revisions those steps wrote.

## State And Stores

**`.ea/`**
: The project-local Eä directory. Committed files hold reproducible state and policy; local-only cache, scratch, and secret directories stay ignored.

**Generation**
: One immutable epoch of the tree's state under `.ea/generations/<id>/`. The selected generation holds the generation document, its ledgers and its history; in-flight status lives in the generation's machine-local `local/` projection.

**Generation document**
: The generation's `state.json`: one object of rows per collection, keyed by each row's public key. A record leaves the document for its collection's ledger once it reaches a terminal status. The bundled `state.schema.json` describes the document.

**JSONL store**
: Append-only event or evidence file under `.ea/store/`. Store entries preserve history for audit, research, decision, memory, incident, and runtime events.

**Artifact**
: Durable evidence or generated output referenced by state. Artifacts use repo-relative paths, Eä URNs, or external URLs; they must avoid local machine paths and sensitive data.

**URN**
: Stable identifier. The qualified `eawf://` URN addresses the lifecycle entities; the `urn:eawf:v1:` URN addresses store records, artifacts, blobs and version-control objects. URNs let state records, stores, artifacts, and rendered docs link to the same entity without relying on local paths.

**Layered config**
: Built-in, global, workspace, repo, and local config combined by strict schema rules. Downstream code consumes validated typed objects, not raw dictionaries.

## Runtime And Surfaces

**CLI**
: The `eawf` command. It parses input, formats output, and dispatches to library code. Domain behavior belongs in the library.

**Daemon**
: The canonical mutator for the generation document, layered config, registry data, event and audit stores, and telemetry. CLI mutation paths proxy to it when available.

**Runtime adapter**
: Integration layer for tools such as Claude Code, Codex, or OpenCode. Adapters render skills, agents, hooks, and settings without becoming workflow source of truth.

**Skill**
: Operator-facing workflow command such as `/plan`, `/dispatch`, `/verify`, `/integrate`, `/accept`, `/release`, `/research` or `/attend`. The generated [skill catalog](reference/autogen/skills.md) lists every shipped skill. Custom skill overlays (workspace `.ea/skills/` and user `~/.eawf/skills/`) reach `eawf skill run`, not the Claude slash surface.

**Agent role**
: Typed role assigned to an agent or Run. The canonical field name is `agent_role`; the bare `role` is reserved for already-namespaced role specs.

**Worktree**
: Per-Task Git worktree used to isolate parallel writers. Worktree changes are cherry-picked back to the Batch branch, not merged.

**Attention queue**
: The pending actions, open questions and open pauses waiting on an operator, worked through `/attend`. A pending action is a decision the machine deliberately did not take; only a human principal can resolve it.

## Evidence And Quality

**Research brief**
: Typed investigation output that captures claims, evidence references, options, tradeoffs, risks, and a recommendation.

**Decision**
: State-resident record of a chosen path and its rationale. Decisions link to evidence so future agents can reconstruct why a route was taken.

**Hypothesis**
: Testable claim with a measurable verdict. Hypotheses use `H<NN>-<NN>` IDs and are resolved only from audit-backed evidence.

**Verification**
: A pass over one Batch at one exact revision, as auditor or as reviewer, through `/verify`. It runs the configured gates, collects evidence, classifies findings and records a verdict.

**Acceptance**
: The operator's decision on a Milestone's acceptance bundle through `/accept`: accept, reject or request repair.

**Envelope**
: Structured skill or CLI output that separates metadata from body content. Envelopes make automation parseable while keeping human output readable.

## Naming

**Eä**
: Human prose name for the framework.

**Ea**
: ASCII spelling for machine-readable text such as logs, commit messages, generated contracts, JSON, and PR titles.

**`eawf`**
: Canonical command, package, import, and URN namespace spelling.

## Cross-references

- [Architecture overview](architecture/overview.md) for repository and `.ea/` layout.
- [State model](architecture/state-model.md) for record fields and validation invariants.
- [Workflow](architecture/workflow.md) for the lifecycle algorithms and Task discipline.
- [URN namespace](reference/urn-namespace.md) for identifier grammar.
