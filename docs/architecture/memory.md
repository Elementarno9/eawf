# Memory model

*Authoritative memory lives on the selected generation's memory ledger; markdown views are generated.*

Eä memory is durable, synchronized across terminals, informative, and token-efficient.

## Memory storage

```text
Project/repo memory      .ea/generations/<generation>/ledger/memory.jsonl
Workspace memory         <workspace>/.ea/store/memory.jsonl
User/global memory       ~/.ea/store/memory.jsonl
Generated markdown views .ea/artifacts/rendered/memory/*.md
Session scratch          .ea/local/sessions/* (gitignored)
```

## Source of truth

Memory is not random chat history. Memory is **curated facts** promoted by explicit commands or hooks.

Rules:

- The generation's memory ledger is the authoritative memory source of truth. On a tree that has not cut over, `.ea/store/memory.jsonl` still is, and it is read-only.
- Markdown memory files are generated / curated views only, never lifecycle authority.
- User preferences live in global `~/.ea/store/memory.jsonl`.
- Session scratch is local and expires.
- State remains authoritative for lifecycle; memory explains recurring context / gotchas.

## Memory commands

```bash
eawf memory add --scope project --title "Use uv run" --body "All Python commands use uv run."
# Add curated memory entry. Purpose: preserve recurring project rule / gotcha.

eawf memory promote --from-session SES-... --scope urn:eawf:v1:state:QR/COLLAR
# Promote selected session findings into durable memory. Purpose: avoid chat-only knowledge.

eawf memory list --scope current
# Show memory entries relevant to active project / subproject / profile.

eawf memory compact
# Rewrite memory into concise canonical form with review diff. Purpose: keep memory small and useful.

eawf memory stale
# List memory entries not reviewed recently or contradicted by state / docs.

eawf memory render-context --budget 2000
# Produce token-budgeted context block for statusline / hooks / agents.
```

`eawf memory add`, `promote`, `prune`, `gc` and `tier` send the daemon's native `memory.*` verbs, and the daemon is the only writer of the memory ledger. It reads the standing notes under the tree's locks and commits each revision as one ledger line that supersedes the note's previous line, so a note is never edited in place and each revision emits one firehose row.

`promote --to artifact` retires a note into a decision the decision ledger already holds, named by `--artifact-id`; the decision itself is filed with `eawf record append`. The reads (`list`, `view`, `stale`, `render-context`, the `--dry-run` of `prune` and `gc`) read the ledger directly and need no daemon.

The cutover imports every `memory_index` row and, from `.ea/store/memory.jsonl`, the latest envelope of every note, which carries its body and creation time. The two id sets are reconciled as a union: the index rows plus the store-only ids, and one ledger line per index row plus one per store id.

## Sync with terminals

- Claude / OpenCode plugins inject only a compact memory summary, not full memory files.
- Statusline shows memory freshness and active scope, not content.
- `SessionStart` hook calls `eawf memory render-context --budget N` and injects state + memory summary.
- `PreCompact` hook saves unresolved facts to session scratch and asks user / agent to promote later.
- Memory writes are atomic and append / edit through `eawf memory`, not direct agent edits.

## Memory entry schema

```yaml
id: MEM-20260505-001
scope: urn:eawf:v1:state:QR/COLLAR
applies_to:
  - urn:eawf:v1:state:QR/COLLAR
  - urn:eawf:v1:repo:QR
summary: One sentence, <= 200 chars
body: Details, <= 1000 chars unless artifact-linked
source: urn:eawf:v1:artifact:QR/ART-20260505-source
confidence: high | medium | low
status: active | stale | superseded | pruned
review_due: 2026-08-01
superseded_by: null
```

## Efficiency rules

- Hard-cap injected memory by token budget.
- Prefer current subproject / profile memories.
- Exclude stale / superseded by default.
- Summarize repeated items.
- Link long memory to artifacts.
- `eawf doctor` warns when memory grows too large or `review_due` passes.

## Cross-references

- JSONL store envelope — `docs/architecture/state-model.md`.
- Skills that promote / prune memory (`/polish`, `/ship`, `/memory`) — `docs/architecture/workflow.md`.
- Statusline memory module — `docs/architecture/statusline.md`.
