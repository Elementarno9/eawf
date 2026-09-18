# Two measured contracts re-probed under concurrent native dispatch

## Summary

`MCT-26081302` was measured from `--version` and `--help` output, and `MCT-26081303` against a scratch daemon with nothing dispatching. Neither had ever watched a provider process run. This directory records both surfaces measured again the other way round: one real native Run per installed runtime, driven through `runtime.run.dispatch` inside a disposable canary, with the JSON-RPC ladder climbing while both children were alive.

The headline is that nothing breached. Every frozen limit of both contracts held, so neither record needs a successor and neither was touched.

The two things worth knowing are not numbers.

**The shipped codex launcher cannot start codex on this platform.** `CodexNativeLauncher` passes no sandbox flag, and codex applies its own seatbelt sandbox inside the eawf seatbelt jail; macOS refuses the nesting and the child dies before the model is reached. This probe had to supply `-s danger-full-access` to get a running child, which the shipped path never does. Every codex row in `cross-provider-reprobe.json` therefore rests on a launch the production launcher could not have performed, and the record says so on the dispatch row rather than in a footnote.

**The claude child inherits the operator's whole environment.** The dispatched child ran the workstation's own `SessionStart` hooks and announced 77 tools, 42 skills, 13 agent definitions, 75 slash commands and several personal MCP servers — none of which the compiled spec asked for. The capability rows that rest on that announcement describe this workstation, not a clean install, and that is recorded in the boundary. It also means a provider stream is a PII surface: nothing raw from either stream is stored here.

`cross-provider-reprobe.json` and `daemon-rpc-reprobe.json` are the machine records. Both validate against the strict re-probe model, which refuses an unknown field, a breach with no successor contract, and an absolute path or a raw vendor session id in any value at any depth.

## What actually ran

| Runtime | Model | Stage | Handshake | Output tokens | Stream lines | Wall |
|---|---|---|---|---|---|---|
| claude-code | haiku | announced | accepted | 310 | 18 | 7.2 s |
| codex | gpt-5.6-luna | announced | accepted | 295 | 7 | 11.9 s |

Both Runs were compiled, leased, spawned and announced through the registered `runtime.run.dispatch` verb against a canary provisioned in a throwaway tree, under a runtime directory and a daemon of its own. Vendor sessions are cited by digest; the identifiers themselves are not recorded.

## The RPC ladder, driven under load

| Concurrency | Calls | Errors | p50 | p95 | Frozen ceiling |
|---|---|---|---|---|---|
| 1 | 20 | 0 | 0.12 ms | 0.35 ms | 5.32 ms |
| 2 | 40 | 0 | 0.14 ms | 0.29 ms | 5.32 ms |
| 4 | 80 | 0 | 0.15 ms | 0.20 ms | 5.32 ms |
| 8 | 160 | 0 | 0.22 ms | 0.32 ms | 5.32 ms |
| 16 | 320 | 0 | 0.50 ms | 0.98 ms | 5.32 ms |
| 32 | 640 | 0 | 0.87 ms | 2.27 ms | 5.32 ms |
| 64 | 1280 | 0 | 1.79 ms | 4.61 ms | 5.32 ms |

Every rung recorded two live provider children at its start and at its end, so no rung fell back to measuring an idle daemon. The ladder stops at 64 because that is the contract's own `ping_concurrency` ceiling: a rung beyond it would be outside the measured envelope rather than a better result.

Only `daemon.ping` was driven. The frozen contract's `state_read_concurrency` and `portalock_writer_concurrency` ceilings are untouched by this re-probe, and the operator's own daemon was never contacted.

## The capability rows

Twenty-four rows, three runtimes by eight capabilities. Sixteen belong to the two installed runtimes and every one of them resolved; the eight opencode rows are `UNKNOWN`, because the binary is absent and an absent binary is not a passing one. No row drifted from its declared cell.

Each row records what its observation rests on, because the sources are not equally strong.

Nine rows rest on the dispatched turn itself: the tools the child announced, the `tool_use` blocks it emitted, the stream lines the driver drained, the cache tokens the envelope disclosed.

Five rest on the driver seam being exercised. Both resume paths raise `NotImplementedError`, both error paths classify into the closed error-class set, and the codex deny-list inversion produces the tool-allowlist restriction its `partial` plan-mode cell names.

Two rest on a zero-token call of the installed codex CLI, which is the weakest source here and is labelled as such: the one-shot codex stream announces no tool, skill or agent inventory the way the claude stream does.

One correction to the frozen record falls out of this. The frozen probe marked `session_resume` as DRIFT on both installed runtimes, because the CLIs advertise resume flags while the matrix declares the capability unsupported. Driven through the driver instead of read off `--help`, both resume seams raise `NotImplementedError`, so the declaration is correct and the drift was an artifact of comparing a declaration about the eawf adapter against a fact about the vendor CLI.

## Boundary

One workstation, one account tier, one turn per runtime, on `darwin` under CPython 3.14.3. The latency figures come from a canary-scoped daemon serving only `daemon.ping` while two children ran; the frozen figures came from an idle daemon, so a future divergence between them is two populations rather than a regression. The codex rows rest on a launch the shipped launcher cannot perform. The claude rows describe a child that inherited this workstation's hooks, plugins and MCP servers.

## Scrub

Neither record carries an absolute path, a home-relative path, a Windows drive root, a hostname or a raw vendor session identifier. The record model rejects all of those in any value at any depth, and both suites re-scan the committed bytes with an independent pattern so a future probe cannot regress the guarantee by editing the model.

## References

| Artifact | What it holds |
|---|---|
| `cross-provider-reprobe.json` | `MCT-26081302` re-probed: 24 capability rows, 2 dispatched Runs, 4 limit comparisons |
| `daemon-rpc-reprobe.json` | `MCT-26081303` re-probed: 7 ladder rungs, 2540 pings, 8 limit comparisons |
| `src/eawf/workflow/evidence/contract_reprobe.py` | The strict record, the scrub detectors and the limit comparison |
| `src/eawf/workflow/evidence/measured_contract.py` | The two frozen contracts, unchanged |
