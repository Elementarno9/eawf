# Claims and their evidence ladder

A claim is one sentence about a Task, Batch or Milestone, backed by evidence records, spans of repository files and, optionally, the gate receipt whose result proves it. Four rungs score every claim: resolve (each cited record is held at its digest), anchor (each span still reads as it did when filed), screen (an in-process check that the cited records support the claim) and entail (the named gate receipt passed). Only a claim whose rungs 1 and 4 pass on the automated path is filed `SUPPORTED`; every other claim stays `OPEN` with the reasons it cannot promote.

## Filing a claim

```text
eawf claim file eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042 \
  --title "Replay keeps event order" \
  --evidence EVD-0003 --evidence RCP-0007 \
  --anchor docs/replay-notes.md:3-4 \
  --expected-revision 812 --idempotency-key replay-claim-1 --actor OPERATOR
```

- The daemon files the claim under the next `CLM-####` key of the subject's project and answers with its URN, its status and each rung's outcome; the ladder is scored as the claim lands.
- `--expected-revision` is the tree's canonical sequence the claim was decided against; a tree that moved since is refused with `revision_conflict`. A retry under the same `--idempotency-key` replays the first answer and allocates nothing.
- `--evidence` takes `EVD-####` evidence keys and at most one `RCP-####` gate receipt, which rung 4 reads.
- `--anchor` takes `[EVD-####:]path:start-end`. The evidence key may be left out when the claim cites exactly one evidence record. Each span's sha256 is taken from the working tree at filing; a path outside the repository, a missing file or a span past the file's end is refused with `anchor_unreadable`.
- `--dry-run` prints the consequence and sends nothing; `--yes` sends without asking.

Nothing files a claim automatically yet: `eawf claim file` is the only producer.

## Working a filed claim

- `eawf claim check <claim-urn>[#rung-<n>] --expected-revision <r>` re-runs the named rung and every rung above it, for example after the anchored file changed or the receipt landed.
- `eawf claim attest <claim-urn>#rung-4 --evidence <evd-urn> --outcome passed|failed --finding <text>` records what an outside party decided about rung 4; an attestation never certifies.

The console's Evidence route lists every filed claim with its rung outcomes, and the rung card shows what each rung ran over.
