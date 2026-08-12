<!-- Generated from the eawf profile render block `gate-fire-proof-sunset`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=gate-fire-proof-sunset version=1.0 hash=d12ddf57096bacd3 -->
# `gate-fire-proof-sunset`

A new gate ships with a test proving it reds on a real defect, and sunsets at phase close if it never fired.

### Rationale

Gates accumulate: each is justified on its own and none is ever removed, so the surface grows while the failures it was built for keep landing. The pathological case is a gate that has never once fired — it reads as protection, costs time on every run, and proves nothing. Bounding growth by evidence rather than by quota keeps the ones that earn their place.


### Mechanism

A new lint, doctor check, or CI gate ships with a test that reds on a **real** defect from this repo, not a synthetic fixture written to satisfy the test. Record what it caught. At phase close, review the gates added during the phase: one that never fired on real work is retired, not kept "just in case". Where the defect population is too large to fix at once, pin it as a ratchet — an exact set or a ceiling that can only shrink — rather than an allowlist that can quietly absorb new entries.


### Verification

Each gate added in a phase names the real defect it reds on. The phase-close review lists gates added, whether each fired, and the disposition of those that did not. A ratchet is exact-set or ceiling based, so repairing an entry forces its removal and a new offender fails even while the list is non-empty.
<!-- END EAWF:managed id=gate-fire-proof-sunset -->
