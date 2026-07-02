# Concepts

*Glossary for Eä lifecycle, state, runtime, and evidence terms.*

Eä is an agent-driven development framework. The CLI and runtime adapters are operator surfaces; durable truth lives in typed state, JSONL stores, and committed artifacts.

## Lifecycle

**Project**
: The top-level repository or product tracked by Eä. Project records carry a short code, title, goals, and active lifecycle pointers.

**Subproject**
: Optional subdivision inside a project. Use subprojects when one repository has separate workstreams that need separate goals or phase queues.

**Phase**
: A bounded delivery slice with a reviewable outcome. A typical phase maps to one long-running feature branch and one pull request.

**Iter**
: The active work cycle inside a phase. An iter groups waves plus the audit, polish, and ship pass that prove the work is ready.

**Wave**
: The smallest planned execution unit. Waves have success criteria, file scopes, dependencies, an `agent_role`, and an `effort_bucket`.

**Roadmap**
: The planned queue of phases, iters, and waves. Roadmap changes go through `eawf roadmap propose`, `revise`, `apply`, and `drop`.

**DAG**
: The dependency graph that controls which waves can run in parallel. Waves with disjoint scopes and satisfied dependencies can be claimed independently.

## State And Stores

**`.ea/`**
: The project-local Eä directory. Committed files hold reproducible state and policy; local-only cache, scratch, and secret directories stay ignored.

**`state.json`**
: The canonical project ledger. It records lifecycle entities, active pointers, outcomes, hypotheses, decisions, and related metadata.

**JSONL store**
: Append-only event or evidence file under `.ea/store/`. Store entries preserve history for audit, research, decision, memory, incident, and runtime events.

**Artifact**
: Durable evidence or generated output referenced by state. Artifacts use repo-relative paths, Eä URNs, or external URLs; they must avoid local machine paths and sensitive data.

**URN**
: Stable identifier in the `urn:eawf:v1:*` namespace. URNs let state records, stores, artifacts, and rendered docs link to the same entity without relying on local paths.

**Layered config**
: Built-in, global, workspace, repo, and local config combined by strict schema rules. Downstream code consumes validated typed objects, not raw dictionaries.

## Runtime And Surfaces

**CLI**
: The `eawf` command. It parses input, formats output, and dispatches to library code. Domain behavior belongs in the library.

**Daemon**
: The canonical mutator for state, layered config, registry data, event and audit stores, and telemetry. CLI mutation paths proxy to it when available.

**Runtime adapter**
: Integration layer for tools such as Claude Code, Codex, or OpenCode. Adapters render skills, agents, hooks, and settings without becoming workflow source of truth.

**Skill**
: Operator-facing workflow command such as `/research`, `/prep`, `/audit`, `/polish`, `/ship`, `/review`, `/roadmap`, or `/flow`. Custom skill overlays (workspace `.ea/skills/` and user `~/.eawf/skills/`) reach `eawf skill run`, not the Claude slash surface — the plugin installer renders builtins only until the P31 overlay merge.

**Agent role**
: Typed role assigned to an agent or wave. The canonical field name is `agent_role`; the bare `role` is reserved for already-namespaced role specs.

**Worktree**
: Per-wave Git worktree used to isolate parallel writers. Worktree changes are cherry-picked back to the parent feature branch, not merged.

## Evidence And Quality

**Research brief**
: Typed investigation output that captures claims, evidence references, options, tradeoffs, risks, and a recommendation.

**Decision**
: State-resident record of a chosen path and its rationale. Decisions link to evidence so future agents can reconstruct why a route was taken.

**Hypothesis**
: Testable claim with a measurable verdict. Hypotheses use `H<NN>-<NN>` IDs and are resolved only from audit-backed evidence.

**Audit**
: Verification pass over an iter, wave, phase, or PR. Audits run configured checks, collect evidence, classify findings, and record a verdict.

**Polish**
: Repo-wide consistency sweep after implementation and before shipping. Polish aligns naming, docs, state, generated files, and follow-up hygiene.

**Ship**
: Closeout controller for commits, PR body rendering, final checks, and state bookkeeping. `/ship` does not replace audit; it consumes audit evidence.

**Envelope**
: Structured skill or CLI output that separates metadata from body content.
  Envelopes make automation parseable while keeping human output readable.

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
- [Workflow](architecture/workflow.md) for lifecycle algorithms and wave discipline.
- [URN namespace](reference/urn-namespace.md) for identifier grammar.
