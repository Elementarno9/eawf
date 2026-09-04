# Epoch-2 state tree: a compact native tree with a separate legacy store

> Status: decision brief (REL-027) · Decision: `D42`, ACTIVE · Scope: the epoch-2 target state tree the dev2 importer writes · Evidence: three read-only spikes over the live epoch-1 corpus · Date: 2026-09-04

## Summary

Epoch-1 state is a single JSON document of 5,743,039 bytes rewritten in full on every mutation, and the `waves` collection alone is 60.1 percent of it (3,451,346 bytes over 1,284 rows) [1]. Two production-scale importer dry-runs measured what an epoch-2 tree would actually hold: the work genuinely in flight compacts to a 242,511 byte document, 4.1 percent of source, while 5,897,055 bytes of terminal history move to append-only ledgers [3]. Total committed bytes do not shrink; what changes is that the multi-megabyte document stops being rewritten per mutation, which is the whole point of `DOM-029` [2].

The question this brief settles is therefore not how to shrink the document. It is where the epoch-1 rows that epoch-2 cannot model natively are allowed to live. Two shapes were weighed: a **compact native tree with a separate legacy store**, recommended here and ratified as `D42`, against a **single merged tree** holding legacy and native records together behind status flags [4][5]. The merged tree reproduces the monolith `DOM-029` exists to end, and it gives every unmodellable epoch-1 row a native-looking referent, which is exactly the fabrication `DOM-010` forbids. The recommended shape drove canonical dangling references from 1,031 to zero on the live corpus without emitting one record that has no source location [3].

The boundary the Decision names, in words: a legacy **reference** is a string carried on a native envelope; a legacy **record** is an envelope in a legacy ledger; neither is ever a canonical reference, so neither can dangle and neither blocks a cutover [3][4].

## The measured input

Every number below comes from a read-only probe over the live corpus; none is an estimate.

| Property of epoch-1 state | Measurement | Why it constrains the target shape |
|---|---|---|
| Document size and dominance | 5,743,039 bytes; `waves` 3,451,346 bytes (60.1 percent) over 1,284 rows, mean 2,688, largest row 25,794 [1] | One collection carries the rewrite cost, so tiering terminal tasks out is where the whole win is |
| Estimate and actual identity | 1,845 rows (996 estimates, 849 actuals) whose map key is the bare wave id while the row `id` carries an `EST-` or `ACT-` prefix [1] | Keying on the row `id` dangles all 1,845; keying on the map key re-points every row with zero orphans [2] |
| Absent versus empty slots | 10 top-level slots serialize as JSON `null` (`tracks`, `outcomes`, `claims`, `hypotheses`, `open_questions`, `mcp_servers`, `mcp_grants`, `health`, `fleet_run`, `workspace`), 6 more as zero-row containers [1][3] | A null slot is not an empty one; an explicit drop needs a null-not-empty proof form distinct from a zero-row count |
| Claim-session references | 1,267 non-empty `claim_session_id` values; 248 resolve to a session row, 1,019 name 354 ids that never existed [3] | The single largest source of dangling references, and unfixable without fabricating 354 sessions |
| Audit references | Document 77 audits, store 101, union 101; of 104 audit references 93 resolve in the document, 1 only in the store, 10 in neither [3] | A second reference class with no possible native referent |
| Operational rows | 45 agent sessions, none carrying a claimed wave id, and 336 worktrees, 26 of them `active` in source [2][3] | Whole rows, not references, that no native epoch-2 kind models |
| Backlog id padding | 2 collision groups (`B070` against `B70`, `B071` against `B71`) with differing content; normalising padding loses 2 rows [1] | Identity must be taken verbatim; no normalisation pass is safe |
| Serialisation | Dumping with two-space indent and no ASCII escaping, plus a trailing newline, reproduces the source bytes exactly; 10,684 objects all key-sorted, 0 duplicate keys, 0 floats failing a repr round-trip, 1,243 non-ASCII strings [1] | Byte-exactness is achievable, so a re-run manifest can be an equality oracle rather than a diff review |
| Measurement population | `attention_eu` null in 849 of 849 actuals, `agent_runtime_eu` in 818, `harness` and `model` in 813 each [1] | The importer inherits mostly empty measurement columns and must not backfill them |

Referential integrity is otherwise clean: wave to iter resolves 1,284 of 1,284, and iter to phase 75 of 75 [1].

## Candidate shapes

| Shape | Where legacy rows live | Measured outcome | Verdict |
|---|---|---|---|
| **A. Compact native tree with a separate legacy store** | Legacy references as strings on native envelopes; legacy records as envelopes in a legacy ledger | Document 242,511 bytes (4.1 percent); ledgers 5,897,055 bytes; 4,108 alias entries over 4,108 distinct targets with 0 collisions; 0 unresolved rows; 0 canonical dangling references; 0 fabricated records [3] | **Recommended, and ratified as `D42`** |
| B. Single merged tree, legacy and native records together behind status flags | Inside the canonical tree, marked by a status flag | Never run to a manifest. Every legacy row becomes a canonical record, so the 1,267 claim ids and 10 audit ids need 364 fabricated referents to stop dangling, against `DOM-010`; and the document keeps the terminal history that makes it 5.9 MB, against `DOM-029` [3][4] | Rejected |
| C. Defer the choice to importer-writing time | Decided implicitly by whatever the importer does first | No manifest and no oracle: the dev2 importer would establish the tree as a side effect, and the cutover would have nothing to validate against | Rejected |

Shape B is the honest alternative rather than a straw one. It is simpler to implement, needs no second store kind, and keeps one place to look. It fails on evidence, not on taste: the dry-runs show both of its costs are real, a fabricated-referent count of 364 records and the retained history that makes the document 5.9 MB, which is the rewrite cost that motivated the epoch bump in the first place [3].

## The legacy-store boundary

The boundary `D42` names has three clauses, each with a measured population on this corpus [3][4].

A legacy **reference** is a plain string field on a native envelope. Population: 1,267 claim-session values imported as `legacy_refs.claim_session_id`, plus 10 audit ids that resolve in neither the document nor the store. A resolvable claim id additionally carries the URN of the matching legacy envelope; an empty string imports as an absent field, never as an empty reference.

A legacy **record** is an immutable envelope in a legacy ledger, never a native kind. Population: 45 agent sessions and 336 worktree rows, the latter annotated so that the 26 rows `active` in source can never be read back as a live lease.

Neither clause produces a canonical reference. The no-dangling-reference check is evaluated over canonical references only, so a legacy reference is outside its scope by construction rather than by exemption. That is what takes canonical dangling references from 1,031 to 0 with zero rows invented [3].

## The dev2 importer validation target

The importer is not validated by review. It is validated by re-deriving the rules-closure manifest over the frozen epoch-1 corpus and asserting equality on each row below [3].

| Target | Value to reproduce |
|---|---|
| Collection totality | 38 top-level keys, 0 with an undeclared disposition |
| Row totality | 4,052 document rows mapped, none dropped without a proof form |
| No fabrication | 0 emitted records lacking a source location; 0 sessions minted for the 354 unresolvable claim ids; 0 audits minted for the 10 unresolvable audit refs |
| Alias index injectivity | 4,108 entries, 4,108 distinct targets, 0 collisions |
| Idempotence | Manifest digest `17b6751884748ff8` on two consecutive runs |
| Byte-exact serialiser | Two-space indent with no ASCII escaping, plus a trailing newline, reproduces all 5,883,968 source bytes |
| Residual document | At most 1.6 MB, measured 242,511 bytes; a run above 2.0 MB is a reject |
| Canonical dangling references | 0 |

Two preconditions sit in front of that target, and both are ratified elsewhere: the workspace registry ships before importer apply mode, because every persisted URN needs a workspace key and the registry carries no workspace record at this checkout; and the obsolescence vocabulary is bound to the allowed-legacy-symbol allowlist, which decides the fate of 23 live backlog rows [3][5].

A deterministic gate pins this brief and the ratified decision together, so a later edit that drops a boundary clause or the validation target reds a test rather than passing review [6].

## Open questions

The corpus moved between spikes, from 5,743,039 bytes on 2026-08-12 to 5,883,968 bytes at the dry-run checkout [1][2][3]. The validation target above is stated against the later, frozen corpus; re-freezing it at cutover time is a dev2 step, not a change of shape.

The success arm of the imported-run status rule is untested on this corpus: all 32 derived runs land unclassified, because their 4 distinct provider session ids appear in no role report [3]. The rule is ratified, but its happy path is unexercised.

One row is a required operator assignment rather than an importer output: the single goal maps to a track outcome metric and no track exists in source [3].

## References

[1] `.ea/local/spikes/2026-08-13-v07-preflight/epoch1-state-at-scale/observations.json` — the epoch-1 census: collection shape, identity collisions, referential integrity, measurement-field population, round-trip hazards, size distribution.

[2] `.ea/local/spikes/2026-08-28-dev2-importer-dryrun/observations/findings.md` — the first importer dry-run: opening tiering manifest, 74 unresolved rows, 1,031 dangling canonical references.

[3] `.ea/local/spikes/2026-09-03-importer-rules-closure/observations/findings.md` — the rules-closure run: eleven rules, 0 unresolved rows, 0 dangling canonical references, and the manifest the validation target quotes.

[4] `.ea/state.json` — decision `D42`, status active, with its rejected alternatives and its measured rationale.

[5] `.ea/artifacts/research/2026-09-03-v07-finalization-brief.md` — the round-3 ratification record placing `D42` alongside `D43` and `D44`.

[6] `tests/integration/workflow/release/test_state_tree_decision.py` — the deterministic gate pinning the ratified decision and this brief's boundary clauses.

## Provenance

- kind: research
- slug: 2026-09-04-epoch2-state-tree-shape
- wave: P31-I01-W13 (REL-027)
- method: read-only synthesis of three frozen spike observation sets plus the ratified decision row; this wave performed no state mutation
- decision: `D42`, ratified in AskUserQuestion round 3 of the 2026-09-03 finalization pass, documented here rather than opened here
- gates: the two release tests named in reference 6

## Scrub

- status: clean
- references: repo-relative only
- credentials, PII, machine-specific paths, hostnames: none
