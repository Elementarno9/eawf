# Agent Report Architecture

Typed agent reports are append-only JSONL records that capture the final output of an agent session in a role-specific schema. They are separate from the generation document: the document tracks Runs, Tasks, Batches and Milestones; report stores keep the structured agent output.

## Store Flow

1. Runtime manifests tell every agent to emit a typed `agent_end` body.
2. Runtime hook scripts call `eawf hook run agent_end` with the `run_ref` of the native Run (a Run URN), the base id, report body, optional artifact ids, and optional blob refs.
3. The hook loader validates the body against the discriminated `AgentReportBody` union.
4. `append_report_as` files the report as the native Run the payload names, which is the authority for role and scope, then computes the next attempt for the role/base pair.
5. The report is wrapped in a store envelope and appended to the role-specific report JSONL file.

Report metadata lives in `AgentReportHeader`. Role output lives in one of the eight report bodies: researcher, planner, executor, auditor, reviewer, polisher, operator, or domain specialist.

## Store Kinds

Each role has its own `StoreKind` value and JSONL stream:

| Role | Store kind | Body model |
|---|---|---|
| researcher | `researcher_report` | `ResearcherReportBody` |
| planner | `planner_report` | `PlannerReportBody` |
| executor | `executor_report` | `ExecutorReportBody` |
| auditor | `auditor_report` | `AuditorReportBody` |
| reviewer | `reviewer_report` | `ReviewerReportBody` |
| polisher | `polisher_report` | `PolisherReportBody` |
| operator | `operator_report` | `OperatorReportBody` |
| domain-specialist | `domain_specialist_report` | `DomainSpecialistReportBody` |

Example report store URN:

```text
urn:eawf:v1:store:EAWF/executor_report/AR-executor-EAWF-0042-01
```

The `store` URN id starts with the store kind, then the report id. This keeps report rows addressable without inventing a new URN kind.

## Attempts

Reports are identified by `(role, base_id, attempt)`. The first report for a role/base pair uses attempt `1`; retries increment monotonically. This lets operators keep failed, blocked, and superseded attempts instead of rewriting history.

## Validation

Schema validation is strict:

- report headers and bodies use Pydantic models with `extra="forbid"`;
- the body role must match the header role;
- hook writes reject sensitive or machine-specific text before append;
- role-specific store kinds map one-to-one with session roles.

Cross-kind report invariants verify the report stream against state context:

- header session id exists;
- session role and scope match the report header;
- report scope resolves to a known project, Milestone or Task;
- attempts are contiguous per `(role, base_id)`;
- executor reports include the commit SHA and name a known Task in `task_key`;
- reviewer reports include coverage refs;
- auditor reports include criteria;
- operator reports list, in `completed_task_keys`, completed Tasks inside the Milestone named by `milestone_key`.

The invariant helper is not part of the state-only `ALL_INVARIANTS` tuple because report rows live in JSONL stores rather than inside the generation document.

## Rendering And Promotion

Markdown rendering is derived from the typed payload, not stored as source of truth. A report can be promoted to an `agent_report` artifact when a durable human-facing document is needed, while the store record remains the canonical machine-readable form.
