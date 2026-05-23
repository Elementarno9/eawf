# Mutation testing (core)

Mutation testing for the four load-bearing "core" packages — `state/`, `lifecycle/`, `daemon/`, `validate/`. It complements the coverage gates: coverage proves a line *ran*, mutation testing proves a test would *fail* if that line were broken. The campaign runs as a **publish-not-block** CI job; the documented score target is **>= 70%**.

## How it works

[mutmut](https://mutmut.readthedocs.io/) rewrites small pieces of source (a `+` becomes `-`, a `>` becomes `>=`, a return value is replaced) one mutation at a time, then runs the test suite against each mutant. A mutant the suite catches is **killed**; one the suite still passes against **survived** — a survivor marks a behaviour no test pins down.

Configuration lives in `[tool.mutmut]` in `pyproject.toml`:

- `paths_to_mutate` — the four core packages (`src/eawf/state/`, `src/eawf/lifecycle/`, `src/eawf/daemon/`, `src/eawf/validate/`).
- `tests_dir` — `tests/` (mutmut runs the project suite per mutant).
- `mutate_only_covered_lines = true` — skip lines no test exercises; an uncovered line yields a meaningless "no_tests" mutant, not a real survivor.
- `do_not_mutate` — the Windows-only daemon transport modules (`windows_pipe.py`, `windows_security.py`, `win_service.py`) are import-guarded off-win32 so their mutants can never be killed on the POSIX CI host; `__init__.py` re-export shims carry no logic. Excluding them keeps the survivor count honest.

mutmut is **not** a runtime dependency and is **not** in the committed `uv.lock` or the dev dependency group — it is a campaign tool, overlaid on the synced venv via `uv run --with mutmut==3.5.0` so a developer's everyday environment stays lean.

## CI job

The `mutation-core` job in `.github/workflows/ci.yaml` runs on pull requests only (a mutation run is far slower than the unit suite). It is **publish-not-block**:

- `continue-on-error: true` at the job level plus `|| true` on the campaign step — the campaign never reds the PR check, whether a mutant survives, the run is partial, or it fails outright.
- It builds a dedicated campaign venv (non-editable `eawf` wheel + frozen dev deps + `mutmut`, see [Running a campaign locally](#running-a-campaign-locally)) rather than `uv sync`-ing the editable project, then runs `mutmut run` (followed by `mutmut export-cicd-stats`) to write `mutants/mutmut-cicd-stats.json` with `killed` / `survived` / `total` counts.
- A small reporter computes the score and prints it to the job log and the GitHub step summary; a missing stats file is reported as "no score" without failing.

The score is review signal, not a merge gate: a PR that lowers the score below 70% surfaces the regression for a reviewer but does not block the merge.

## Score target

The documented target is **>= 70%**, computed as `killed / (killed + survived)` (the conventional mutmut score, which excludes `no_tests` / `skipped` mutants from the denominator). 70% is a pragmatic floor for a first campaign on a large existing surface; it is tightened toward in later waves the same way the coverage ratchet is.

## Running a campaign locally

A full campaign over all four packages is slow (minutes to tens of minutes). To run it:

mutmut rewrites the source into a `mutants/` sandbox and runs the suite from there. That sandbox does **not** tolerate an editable install — `from eawf import __version__` re-triggers the dynamic hatch-version build hook (whose `tools/` is not copied into the sandbox). Run the campaign from a venv where `eawf` is installed as a **built wheel** (non-editable), with the dev test deps and mutmut alongside — the same shape the `mutation-core` CI job builds:

```bash
# Build a campaign venv: non-editable eawf + frozen dev deps + mutmut.
uv export --frozen --only-dev --no-emit-project -o /tmp/dev-reqs.txt
uv venv /tmp/mutenv
VIRTUAL_ENV=/tmp/mutenv uv pip install . -r /tmp/dev-reqs.txt mutmut==3.5.0

# Full core campaign (all four packages). Run the venv's interpreter
# directly — no ``uv run`` wrapper — so changing cwd into ``mutants/``
# never re-resolves the project.
/tmp/mutenv/bin/python -m mutmut run

# Publish the score (writes mutants/mutmut-cicd-stats.json).
/tmp/mutenv/bin/python -m mutmut export-cicd-stats

# Browse results: surviving mutants, per-file breakdown.
/tmp/mutenv/bin/python -m mutmut results
/tmp/mutenv/bin/python -m mutmut browse
```

To scope a faster local run to a single module while iterating, narrow `paths_to_mutate` in `pyproject.toml` (or pass explicit mutant names to `mutmut run`); restore the four-package list before committing so CI mutates the full core surface.

## Sandbox caveats

The job is **publish-not-block** by design partly because mutmut's source-rewriting sandbox does not yet run this tree's full suite cleanly:

- **pydantic-core compiled validators.** mutmut's import trampoline can confuse pydantic-core's compiled validators, surfacing as a spurious `ValidationError` (for example "Decimal input should be an integer, float, string or Decimal object" on a value that already *is* a `Decimal`) while collecting tests that pull in the daemon/telemetry models. This aborts the stats phase, so `mutmut-cicd-stats.json` may not be produced. The publish step treats a missing stats file as "no score" and exits cleanly; the CI job never reds the build.

Closing this is mutmut-runner tuning (e.g. constraining the trampoline, or a hammett-based runner), tracked as follow-up work — it is intentionally outside the config-only wiring this page documents. Until then the wiring is in place and the score populates as soon as the sandbox runs the suite end to end.

## Acting on survivors

A surviving mutant is a missing or weak assertion. The fix is a test, not a config change:

1. `mutmut results` (or `mutmut browse`) to find the surviving mutant and its source location.
2. `mutmut show <mutant>` to see the exact source change the test failed to catch.
3. Add or strengthen a test so the mutated behaviour fails, then re-run.

Never weaken `do_not_mutate` or drop a package from `paths_to_mutate` to raise the score — that hides the gap instead of closing it.
