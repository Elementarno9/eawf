# Snapshot-pairing gate

The snapshot-pairing gate enforces the C09 §5.6 snapshot-update flow from the CI side: every commit in a PR range that *mutates* a managed golden surface must carry a wave-form `test:` subject, so a golden byte change can never sneak in under an unrelated `feat:` / `fix:` commit. The gate lives at `tools/snapshot_pairing_gate.py` and runs as the `snapshot-pairing` job in `.github/workflows/ci.yaml` (pull-request events only).

## How it works

The gate walks the commits between the PR base and head. For each commit that modifies, deletes, or renames a file under a managed golden surface (status `M` / `D` / `R`), the subject must match a wave-form `test:` grammar — `[P##-W##] test: ...`, the `[P##-I##-W##] test: ...` iter variant, or the bare `test: ...` conventional form accepted while no phase is active. Pure additions (status `A`) are exempt: a brand-new surface ships its fixtures alongside the `feat:` wave that introduces it.

The watched directories are sourced from the same C09 §5.6 surface inventory the CLI drives (`eawf.surfaces.cli.commands.snapshot.SNAPSHOT_SURFACES`) so the gate and `eawf snapshot update --kind` cannot drift. Golden trees outside the inventory (e.g. the `tests/golden/cli/` help-panel snapshots, which refresh as a side-effect of any wave that adds a CLI command) are deliberately out of scope — they have their own per-wave refresh path.

**Per-commit pairing vs. phase PRs.** Per-commit pairing is the right contract for managed small-CL PRs. Under the one-PR-per-phase model the whole phase ships as a single reviewed unit and the snapshot test suite already asserts every committed golden matches current-code output, so per-commit `test:` pairing is redundant. When the PR range spans more than one iter (the phase-PR signal) the gate lists the bundled golden-touching commits for reviewer visibility and exits `0` instead of failing. Single-iter ranges keep the hard per-commit gate.

## Exit codes

- `0` — every golden-touching commit is correctly paired, no golden files changed, the range spans multiple iters (phase PR — listed, not blocked), or there is no PR base/head (push build).
- `1` — at least one golden-touching commit is unpaired in a single-iter range (offending commits printed to stderr), or the invocation is missing its base/head arguments.

## Running it locally before the phase PR

CI-only gates that fire on the phase PR are cheapest to satisfy when run locally first. To dry-run the gate against the range your phase PR will open, pass the merge-base and HEAD:

```
uv run python tools/snapshot_pairing_gate.py "$(git merge-base origin/main HEAD)" HEAD
```

A green run prints `snapshot pairing gate: ok (all golden changes paired)` (or, for a multi-iter phase range, `phase PR detected ...` followed by the bundled golden commits). A red run names the offending commits; regenerate the affected surface with `eawf snapshot update --kind <kind>` and commit the rewritten bytes as `[P##-W##] test: snapshot update <kind>` before pushing.

## Adding or changing a watched surface

The watch set is the snapshot surface inventory — to guard a new golden tree, add a `SnapshotSurface` to `SNAPSHOT_SURFACES` in `eawf.surfaces.cli.commands.snapshot`. The gate picks it up automatically (its `_WATCHED_DIRS` is derived from each surface's `golden_dir`), and `eawf snapshot update --kind <new-kind>` regenerates it. There is no separate gate-side list to keep in sync.
