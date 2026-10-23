# Snapshot-pairing gate

The snapshot-pairing gate enforces the C09 §5.6 snapshot-update flow from the CI side: every commit in a PR range that *mutates* a managed golden surface must name the unit of work that owns the refresh, so a golden byte change can never sneak in under an unattributed `feat:` / `fix:` commit. The gate lives at `tools/snapshot_pairing_gate.py` and runs as the `snapshot-pairing` job in `.github/workflows/ci.yaml` (pull-request events only).

## What the gate requires

The gate walks the commits between the PR base and head in one `git log --name-status -z` pass. For each commit that modifies, deletes, or renames a file under a managed golden surface (status `M` / `D` / `R`), the commit must name its owner in one of these forms:

- a body with no `Batch:` trailer and exactly one `Task: <KEY>` trailer naming a Task of the repo's generation — the form every delivery commit lands in, one commit per Task with its code, tests and goldens together, its Batch read from the Task's `batch_ref`;
- a `<type>: <summary>` subject whose body carries exactly one `Batch: BAT-<NNNN>` trailer naming a Batch of the generation;
- a bare conventional `test: <summary>` subject.

A commit carrying any `Batch:` trailer is judged by it alone: two trailers, a malformed key or a Batch the generation does not hold leave it unpaired, and a `Task:` trailer cannot rescue it. Batch and Task keys are corroborated against the committed generation, so a fabricated key buys nothing, and a tree with no readable generation knows no keys and fails closed. Pure additions (status `A`) are exempt: a brand-new surface ships its fixtures alongside the commit that introduces it. The subject forms of the retired epoch-1 lifecycle still pass, so historical ranges stay green.

The watched directories are sourced from the same C09 §5.6 surface inventory the CLI drives (`eawf.surfaces.cli.commands.snapshot.SNAPSHOT_SURFACES`) so the gate and `eawf snapshot update --kind` cannot drift. Golden trees outside the inventory (e.g. the `tests/golden/cli/` help-panel snapshots, which refresh as a side-effect of any change that adds a CLI command) are deliberately out of scope — they have their own refresh path.

## Exit codes

- `0` — every golden-touching commit is correctly paired, no golden files changed, or there is no PR base/head (push build).
- `1` — at least one golden-touching commit is unpaired (offending commits printed to stderr), or the invocation is missing its base/head arguments.

## Running it locally before the Milestone PR

CI-only gates that fire on the Milestone pull request are cheapest to satisfy when run locally first. To dry-run the gate against the range your pull request will open, pass the merge-base and HEAD:

```
uv run python tools/snapshot_pairing_gate.py "$(git merge-base origin/main HEAD)" HEAD
```

A green run prints `snapshot pairing gate: ok (all golden changes paired)`. A red run names the offending commits; regenerate the affected surface with `eawf snapshot update --kind <kind>` and carry the rewritten bytes in the commit of the Task that caused them, with its `Task:` trailer.

## Adding or changing a watched surface

The watch set is the snapshot surface inventory — to guard a new golden tree, add a `SnapshotSurface` to `SNAPSHOT_SURFACES` in `eawf.surfaces.cli.commands.snapshot`. The gate picks it up automatically (its `_WATCHED_DIRS` is derived from each surface's `golden_dir`), and `eawf snapshot update --kind <new-kind>` regenerates it. There is no separate gate-side list to keep in sync.
