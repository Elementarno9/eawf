<!-- Generated from the eawf profile render block `memory-hygiene`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=memory-hygiene version=1.1 hash=30c5ef6d3ef4e3e1 -->
# `memory-hygiene`

Remember only facts that stay true across sessions; status is derivable, so query it with ``eawf status`` or ``eawf memory digest`` instead of memorizing it.

### Memory hygiene: remember durable facts, query status

Curated memory holds **durable facts** — decisions that outlive a session, operator preferences, hard-won gotchas, conventions. It is the wrong place for **status**, which changes every time a wave closes: the current phase and iter, what just closed, the latest verdicts, the open backlog. Memorizing status guarantees drift, because the remembered copy goes stale the moment the real state moves.

The rule: a fact is worth remembering only when it stays true across sessions. Status is **derivable** from state, so it is queried on demand, never memorized. Run ``eawf status`` for the current pointers, recent decisions, and open backlog, and ``eawf memory digest`` for a glance-clear standup of what is in flight, what just closed, and what was recently decided. Both read straight from state, so the answer is always current and costs nothing to keep so.

Before writing a memory entry, ask whether a query already answers it. If ``eawf status`` or ``eawf memory digest`` surfaces the fact, do not duplicate it into memory — link to the query instead.
<!-- END EAWF:managed id=memory-hygiene -->
