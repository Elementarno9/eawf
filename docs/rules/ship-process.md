<!-- Generated from the eawf profile render block `ship-process`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=ship-process version=1.2 hash=4ac13c9735c3334d -->
# `ship-process`

Ship rides the phase-co-closing iter: open the one phase PR, pass CI, address review by appending waves to that same iter, then close and merge with rebase.

### Ship process

Ship is the phase's terminal pass, and it rides the phase-co-closing iter (the final iter of the phase) rather than a fresh iter. The ordered steps from green waves to a merged, tagged phase:

1. **Open the phase PR.** One PR per phase, body per the ``pr-template`` (``## Summary`` + ``## Test plan`` + ``## Phase deliverables``). The PR head is the long-running ``feature/<symbol>-v<X.Y>`` branch with every worktree wave already cherry-picked in.
2. **Pass CI gates.** Beyond the per-commit lint + test gauntlet, two gates fire only on the phase PR and so are easy to miss locally: the per-package **coverage gate** (CI parses ``coverage.xml`` against the ``[tool.eawf.coverage]`` thresholds) and the **snapshot-pairing gate** (``tools/snapshot_pairing_gate.py`` over the PR base..head range, which rejects a managed golden-surface mutation that lacks a wave-form ``test:`` subject). Run both locally before pushing the PR so they do not surface late.
3. **Pass the review.** ``/ship`` runs the PR review pass. Address feedback by **appending waves to the same co-closing iter** via ``eawf roadmap revise --add-wave`` (ACTIVE-phase ``add_wave_plan`` keeps the iter ACTIVE and lands the new waves PENDING). Do NOT open a second iter for routine review follow-ups -- see ``iter-phase-close-timing``.
4. **Close + merge.** Once CI is green AND the review-passed branch is on the remote, the phase-close mutation rides the single ``[P<NN>] state: close iter + phase (audit=<id>)`` commit that bundles iter close + phase close (see ``iter-phase-close-timing``). Merge with rebase (never squash) so the per-wave ``[P<NN>-W<NN>]`` history survives; the merge ends the phase.
5. **Tag the release.** Under the ``per-phase`` cadence the post-merge ``.github/workflows/phase-release.yaml`` checks the ``(release=v<X.Y.Z>)`` annotation off the phase-close commit against the package version and prints the tag command; it pushes no tag. A repo that releases through an eawf release train then runs ``eawf release tag --push`` from a clean checkout of the merge commit, and that push starts the publish workflows; every other repo keeps its own tag flow. Nothing publishes until the tag is pushed -- see ``release-process`` for both tagging paths and the pre-flight gate that ``eawf phase close`` enforces.
<!-- END EAWF:managed id=ship-process -->
