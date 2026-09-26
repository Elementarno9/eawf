# Console live smoke on this repository after the epoch-2 cutover

## Summary

After this repository was cut over to epoch 2 (the native record store, generation `gen-ca819f3245bf71a8`), the console was driven headless through the Textual Pilot over a real daemon serving this repository's own `.ea` tree, read-only, on a private socket. The five journeys (`scope.home`, `activity`, `attention`, `run.detail`, `settings`) were rendered at 80x24, 120x30 and 160x40 [1]. `eawf tui --plain` and a non-terminal run route to the epoch-1 offline status emitter, so Pilot is the only way to capture the native console headless.

Result in one line: **`scope.home` now lists this repository's own Milestones (`P36` ACTIVE, `P37` and `P38` PLANNED) at every width, no frame carries a prototype literal, and the authority tree is byte-identical after the serve.**

The first recording of this run found `scope.home` drawing its `NOT HELD` frame: the cutover wraps each imported Milestone, Batch and Task in a payload and states no `urn` or `revision` beside it, and the projection refused the whole route over it. The reader now addresses an imported row by the name the legacy continuation gives it, `legacy:<collection>/<key>`, with a revision of one plus each continuation move since the import [3] [8]. The migration writer is unchanged, so the applied manifest digest stands.

The frame check is not vacuous: each journey is rendered clean first, then the golden harness's prototype registers are planted into the same console and its daemon link is dropped, and every journey then draws prototype rows (`eawf-core`, revision `41,208`, `RUN-538453eb`, `EAWF-0042`) that the check names [1].

`frames/` holds each frame as text and `images/` a Textual SVG screenshot of each, named `<route>@<width>x<height>`.

## Findings

Each live frame is compared with the packet mockup of the same route and width, the golden route frames the contract replays [2]. Layout shape (header crumb, summary line, section labels, rules, table header, key bar) is weighted above tokens; the mockup's data is invented, so a different count is expected and not listed.

| # | Route | Widths | Live shape | Mockup shape | Verdict |
|---|---|---|---|---|---|
| F-01 | scope.home | 80, 120, 160 | crumb names this tree's root id, `0 tracks · 3 milestones`, `REGIONS outcomes · attention`, `ROW KIND STATUS` list of `P36`, `P37`, `P38`, `WINDOW 1–3 of 3`, `UNSTATED runs ? · attention ? · progress ?` | crumb `Eä ▸ <scope>`, tracks/runs summary, `MILESTONES RUNS ATTENTION PROGRESS` tree, attention buckets | Resolved: the route was refused (`milestone row 'P36' states no urn`) because imported rows carry only `payload`, `recorded_at` and `status` [4]; the reader now projects them [3]. Residual: a flat list, not the mockup's tree, and no Track, because the import left every Milestone's track unassigned |
| F-02 | scope.home | 80, 120, 160 | `● LIVE` on every frame | `● LIVE` | Resolved with F-01: the `◌ DISCONNECTED` first frame followed the refused read and no longer appears |
| F-03 | activity | 80, 120, 160 | crumb names this tree's root id, `0 runs`, `BUCKETS ∅ unavailable`, `ROW KIND STATUS` table, `WINDOW 0 of 0` | crumb, run count, bucket strip, `RUN TASK STATE REASON AS OF` table, `WINDOW` | Shape matches; the generic `ROW KIND STATUS` columns stand in for the mockup's Run columns because the projection row carries no task, reason or time |
| F-04 | attention | 80, 120, 160 | `MINE ?`, `no epoch-2 producer writes this register yet`, empty table, `UNWRITTEN pending_action ?` | mine/fleet-wide summary, `NEEDS OPERATOR` bucket, one row per question | Shape matches with an extra honesty pane; nothing in this repository has opened a question yet |
| F-05 | run.detail | 80, 120, 160 | `REGIONS timeline`, empty table, `UNSTATED provider ? · elapsed ? · cost ?` | a Run's labelled fields (`PROVIDER`, `STARTED`, `SCOPE`, `USAGE`, `CONTROLS`, `LINEAGE`) | Divergent by design: with no subject and no Run the route lists nothing rather than opening an invented Run |
| F-06 | settings | 80, 120, 160 | `N leaves · N sections · 2 winning layers`, `LEAF EFFECTIVE LAYER` table, `WINDOW`, `READ ONLY` | category/section summary, editable field list, `Enter edit`, `l layer`, `x unset` | Divergent: the live route is a read-only leaf table; the mockup's editing keys are not offered |
| F-07 | all five | 80, 120, 160 | every frame exactly on its grid, key bar last, no escape sequence | same | Matches |

One launch-path gap was found while tracing how the live console is opened, and fixed: `eawf tui` resolved authority at `state_path.parent.parent`, the repository root, but a tree declares epoch 2 inside its `.ea` directory, which is where canary provisioning resolves it [6]. This repository's root resolves to epoch 1 (`undeclared`) and its `.ea` to epoch 2, so an interactive `eawf tui` here opened the epoch-1 app. The launcher now resolves at the `.ea` tree beside the state file and sends the daemon the repository root, which the daemon extends with `.ea` itself [5].

## References

[1] tests/tui/surfaces/tui/console/test_console_live_smoke.py

[2] tests/fixtures/console/golden/sequences/frames-routes-80.json

[3] src/eawf/kernel/projection/compute.py

[4] .ea/generations/gen-ca819f3245bf71a8/state.json

[5] src/eawf/surfaces/tui/launch.py

[6] src/eawf/platform/install/canary.py

[7] tests/tui/surfaces/tui/console/test_console_live_no_fixture.py

[8] src/eawf/kernel/migration/epoch2/continuation.py

## Provenance

Recorded on 2026-09-26 in the wave's own worktree by `EAWF_RECORD_CONSOLE_LIVE=1 uv run pytest tests/tui/surfaces/tui/console/test_console_live_smoke.py -k serve_this_repository` [1].

The daemon was an in-process server bound to a short private runtime directory with its WAL under the test's temporary directory; it was closed when the test finished. The frame check is `assert_no_fixture_literal` [7]. The repository's whitespace hook trims each frame's trailing blanks on commit, so a stored frame's rows are shorter than the grid the test asserted on.

The settings frames reflect the configuration layers visible to the test process, so their leaf counts vary between runs. The comparison with the mockups [2] (and its 120 and 160 siblings) was made by reading both frames side by side.

## Scrub

- status: clean

No absolute path, machine name, account identifier or credential appears in this directory. The root id in each crumb is a digest, not a path. The SVG screenshots reference only the public font CDN Textual embeds.
