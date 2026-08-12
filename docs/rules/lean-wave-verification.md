<!-- Generated from the eawf profile render block `lean-wave-verification`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=lean-wave-verification version=1.2 hash=fb0d755afd91d07f -->
# `lean-wave-verification`

Wave success criteria name targeted tests that finish inside 60s; the full suite and one fresh-context audit run once per iter at iter close, not once per wave.

### Rationale

Running the full test suite plus a fresh-context audit on every wave costs 10-30 minutes per wave and serializes iter execution. The live close gate reserves blocking auditor spawn for the high-risk band (L/XL effort, judgment roles, and security scope). Its deterministic 1-in-4 classification for mechanical waves is advisory today: sampled mechanical waves do not trigger a blocking live auditor spawn. Iter-close audit-link integrity is the patch-release guard; proof that the audit evaluated the exact closing HEAD remains deferred to the provenance model in v0.7.


### Mechanism

Wave success criteria name targeted tests only (``uv run pytest <touched paths>``) — never the full suite — and that selection must finish within **60 seconds**. A targeted run slower than that is over-scoped: narrow it to the touched paths, or the feedback loop stops being usable and the agent starts skipping it. Iter-scoped and full-suite runs carry no such bound. A mechanical wave (S/M effort, executor role, non-security) closes on criteria + evidence alone; do not describe the 1-in-4 sampler as a blocking spawn control. The full suite (the repo's acceptance ``tests`` command) and one fresh-context audit run once per iter, at iter close, in this order: polish -> full suite -> audit over the iter diff -> close. Strict close validates that the named audit exists, belongs to the iter, is complete and accepted, and carries real evidence; it does not validate evaluated HEAD. Nonblocking audit findings become backlog items (``eawf backlog add``). One repair wave and one re-audit is operator procedure, not a production limit: reserved ``flow.max_repair_cycles`` does not enforce it. Exceptions that keep the wave-level full-tree gauntlet: persisted-schema / migration waves and security-scoped waves.


### Verification

A closed mechanical wave shows no full-suite run and no blocking sampler-spawn claim. The iter close names a real accepted audit linked to that iter; do not infer an audited HEAD SHA from the link. Audit findings resolve through operator triage, and no check claims ``flow.max_repair_cycles`` enforces a one-repair-wave ceiling.
<!-- END EAWF:managed id=lean-wave-verification -->
