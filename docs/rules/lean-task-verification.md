<!-- Generated from the eawf profile render block `lean-task-verification`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=lean-task-verification version=2.0 hash=d347a17da5683389 -->
# `lean-task-verification`

Task success criteria name targeted tests that finish inside 60s; the full suite and one fresh-context audit run once per Batch, before it lands, not once per Task.

### Rationale

Running the full test suite plus a fresh-context audit on every Task costs 10-30 minutes per Task and serializes the Batch. The Batch is the unit that lands and is verified as one change — ``/verify`` binds one Batch at one exact revision — so it is where the whole change is judged once, and every Task criterion is proved again at the Batch head before the Batch closes.


### Mechanism

Task success criteria name targeted tests only (``uv run pytest <touched paths>``) — never the full suite — and that selection must finish within **60 seconds**. A targeted run slower than that is over-scoped: narrow it to the touched paths, or the feedback loop stops being usable and the agent starts skipping it. Batch-scoped and full-suite runs carry no such bound. The full suite (the repo's acceptance ``tests`` command) and one fresh-context audit (``/verify --mode audit``) run once per Batch, before it lands, in this order: polish -> full suite -> audit over the Batch diff -> land. Nonblocking audit findings become backlog Tasks (``eawf task create``). One repair Task and one re-audit is operator procedure, not a production limit. Exceptions that keep the Task-level full-tree gauntlet: persisted-schema / migration Tasks and security-scoped Tasks.


### Verification

A Task whose criteria name only targeted tests shows no full-suite run of its own. The Batch's verification names a real accepted audit bound to that Batch at its exact revision. Audit findings resolve through operator triage, and no check claims a production limit on repair Tasks.
<!-- END EAWF:managed id=lean-task-verification -->
