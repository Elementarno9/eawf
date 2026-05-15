# A26-P19-I02 ship-gate audit

## Summary

- P19-I02 reactive iter executed 10 feature/fix waves (W11-W20) plus
  this ship-gate audit (W21); state records all 10 closed prior to this
  audit landing [1].
- Activate-gate completed: `phase activate` now refuses on (a) zero
  planned waves, (b) HEAD behind `origin/<default_branch>`, (c) dirty
  worktree — gates wired in W11 with `--allow-stale` override added in
  W13 [2][3].
- Roadmap revise unlocked on ACTIVE phases for PENDING waves; CLOSED
  waves under the same parent stay immutable (W12) [4].
- `wave close --commit` normalises any ref (short SHA, branch, tag,
  HEAD~N) to the 40-char canonical SHA via `git rev-parse`; the
  `Wave.commit` field returns as `ShaStr | None` on the state model
  (W17) [5].
- `wave update --files` verb lets reactive waves expand/shrink their
  declared file_scopes on PENDING/CLAIMED waves; CLOSED stays
  immutable (W18) [6].
- `artifact verify` CLI added with `--all` / `--refresh` flags; reuses
  the `sha256_file` streaming helper; exit codes 0/2/3/8 per the
  existing taxonomy (W20) [7].
- AGENTS.md decisions section now renderable from typed `Decision`
  records (D01..D23); covers round-trip + scope-filter + idempotency
  paths (W19) [8].
- Backlog hygiene: A25 review audit closed 7 delivered + 2 superseded
  items; tagged 7 publication-cluster items as P3 defer-publication
  (W16) [9].
- Coauthor verification path documented in `docs/architecture/coauthor.md`
  with the canonical `Claude <noreply@anthropic.com>` trailer + manual
  verification ladder; new commit-msg hook `tools/normalize_coauthor.py`
  collapses Claude/Codex variants + dedupes by email (W14, W15) [10][11].
- Test suite at 2529 passed / 12 deselected on main as of the audit
  open; ten landed wave commits picked clean except W17 which needed
  one trivial import-section conflict resolution (W11 added `shutil`,
  W17 added `subprocess` from a 30c252d base; union kept) [12].

## References

[1] `.ea/state.json` waves block — every `P19-I02-W11..W20` carries
    `status="closed"` with the landing commit SHA in `outcome`.
[2] commit `8fb9b40` — `[P19-W11] fix: complete activate-gate
    (waves + currency + dirty)`.
[3] commit `a6300b6` — `[P19-W13] feat: phase activate fetches +
    --allow-stale override`.
[4] commit `2d93247` — `[P19-W12] feat: allow roadmap revise on
    ACTIVE phase for PENDING waves`.
[5] commit `cfd8043` — `[P19-W17] feat: normalise wave close
    --commit to full SHA`.
[6] commit `8576835` — `[P19-W18] feat: eawf wave update --files
    verb`.
[7] commit `f607a41` — `[P19-W20] feat: eawf artifact verify CLI`.
[8] commit `db8d91d` — `[P19-W19] feat: render AGENTS.md decisions
    from typed Decision records`.
[9] commit `9fad87b` — `[P19-W16] chore: bulk-close stale backlog`.
[10] commit `9299ff3` — `[P19-W14] chore: coauthor verification +
    docs note`.
[11] commit `36ffc98` — `[P19-W15] feat: commit-msg normalize-coauthor
    hook`.
[12] `uv run pytest tests/ -q` at HEAD `a5e8549`: `2529 passed,
    12 deselected in 194.83s`.

## Provenance

Audit assembled on 2026-05-15 from the P19-I02 wave outcomes recorded
in `.ea/state.json` plus `git log --grep '\[P19-W' main` to confirm
each cited SHA exists on the main branch. All cited commits carry the
`Co-Authored-By: Claude <noreply@anthropic.com>` trailer per the
runtime resolver. The two reactive deviations recorded in wave
outcomes (W18 deferring `wave_policy.py` from its declared scope; W17
touching `src/eawf/schemas/state.schema.json` as a mechanical regen
byproduct) were reviewed and accepted as in-scope follow-on artifacts
of the named changes.

## Scrub

- status: clean
- notes: repo-relative paths only; no absolute paths, no host-local
  URLs, no PII beyond the canonical `Claude <noreply@anthropic.com>`
  co-author trailer (allowlisted under the secrets-hygiene policy).
