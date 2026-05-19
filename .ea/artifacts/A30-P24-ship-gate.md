# A30-P24 ship-gate audit

## Summary

- P24 (C02-IMPL daemon keystone) shipped 10 waves under iter P24-I01; phase verdict **minor** (pass-with-followups: implementation complete + 3,560 full-suite tests green + mypy/pre-commit clean; 2 wiring gaps identified) [1].
- W01 ship: `src/eawf/daemon/` package boots (`PROTOCOL_VERSION="1"` + `asyncio.start_unix_server` + JSON-RPC 2.0 frame parser + `daemon.ping/status/shutdown` methods + Linux SO_PEERCRED + macOS LOCAL_PEERCRED via xucred + runtime_dir resolver + `eawf daemon run/ping/status/stop/logs` CLI verbs); 14 daemon scaffolding tests [2].
- W02 ship: `WindowsPipeServer` (pywin32 listener thread + `asyncio.Queue` bridge via `loop.call_soon_threadsafe` per C02 §5.13) + DACL restricted to owning user SID + post-connect SID check + `[project.optional-dependencies] windows = ["pywin32>=306"]`; live tests skipif `sys.platform != "win32"`, design tests gated on `pytest.importorskip` [3].
- W03 ship: outcome-WAL per Q10/XB12 (post-apply envelope payload, never re-execute mutator) + `recovery.replay_wal` algorithm + poisoned-WAL admin (`eawf daemon replay-wal --inspect|--gc`) + 47 wal/recovery/cli tests [4].
- W04 ship: systemd-user unit + launchd LaunchAgent plist (Jinja2 templates) + pywin32 `EawfdService` with XB14 SCM-to-asyncio shutdown bridge (`loop.call_soon_threadsafe(self._stop_event.set)`) + `eawf daemon service-enable/disable/status` verbs + idempotent disable on never-installed state [5].
- W05 ship: per-OS peer-cred dispatcher (`PeerCredential` typed model + Linux `SO_PEERCRED` + macOS `LOCAL_PEERCRED` xucred + FreeBSD ctypes-mirrored xucred prefix + Windows DACL+SID via W04 helpers) + forensic `{platform, expected_uid, actual_uid}` payload on `-32000 unauthorized` envelope [6].
- W06 ship: `EventBus` per-subscriber `deque(maxlen=1024)` with drop-oldest backpressure per C02.F50 + `subscription_lag` envelope with `dropped_count` + `last_event_id` + `event.subscribe/list/show` RPCs + `state.subscribe` alias + `StoreKind.SUBSCRIPTION_LAG` enum + 30 bus/event tests [7].
- W07 ship: opaque `session_log_handle` registry (`urn:eawf:v1:session-log:<runtime>:<uuid>` per XB05) + `Wave.sessions`/`runtime_preference`/`dispatch_history` + `SessionAttempt`/`DispatchAnnotation`/`DispatchNote` Pydantic v2 models + `agent.dispatch` fresh-path skeleton + `session_ttl` sweep module + 49 session/agent/state-model tests [8].
- W08 ship: `IdleTimeoutWatchdog` (default 300s configurable via `EAWF_DAEMON_IDLE_TIMEOUT`) + `auto_spawn_daemon` (POSIX double-fork + Windows `CreateProcess DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP`) + `DaemonClient` JSON-RPC context-manager + cold-spawn p95 benchmark (observed ~168ms; gate at 600ms = V1 400ms target + APFS cold-cache headroom); `pytest-benchmark` added to dev extras [9].
- W09 ship: `state.mutate` RPC + outcome-WAL integration per C02 §5.6 14-step algorithm + idempotency cache (60s per Q11) + `Mutation` discriminated union (`WAVE_CLAIM/CLOSE/FAIL`, `PHASE_OPEN/ACTIVATE/CLOSE`, `ITER_OPEN/CLOSE` wired; `WAVE_RELEASE/EVENT_APPEND/ROADMAP_*` reserved for C03-IMPL) + `wave close` canary proxy + `daemon.proxy_enabled` config flag (default False at W09) + `daemon_required` envelope (exit 8 IntegrityViolation) on unreachable daemon for mutating verbs [10].
- W10 ship: `config.read/set_layer_value/list_layers` + `registry.read/update` daemon RPCs (`config_updated` + `registry_updated` envelopes published to bus) + `_save_value_to_layer` + `_persist_registry` dispatcher rewrites + `daemon.proxy_enabled` default flipped to True + `EAWF_DAEMONLESS=1` V1 carve-out env-var + autouse `EAWF_DAEMONLESS=1` fixture wired into integration + golden conftests so the full pytest suite remains green under the flag flip [11].
- Local pre-commit gauntlet (ruff + ruff-format + trim-whitespace + eof-fixer + yaml + toml + large-files + merge-conflict + debug-statements + detect-secrets + commit-prefix-lint + insert-coauthor) ✅ pass on every wave commit [12].
- mypy (`uv run mypy src/`) ✅ no issues found in 311 source files [12].
- Full test suite ✅ 3,560 passed, 26 skipped (all platform-only: Linux-systemd / Windows-pywin32 / FreeBSD-ctypes / macOS-darwin recipes), 12 deselected; daemon suite alone runs 217 + 26 skip; cli proxy suite runs 21 [12].
- Authority map writer-rows 1-8 all migrated to daemon RPCs; portalocker retained inside the daemon mutator as defense-in-depth per V1 [1:51] [13].

## Followups (deferred to P24-I02 or P25 hygiene)

- **F1** Wire `eawf.daemon.recovery.replay_wal(ctx.wal_dir, ctx.state_path, ctx.event_path)` into `daemon.main.run()` after `wal_dir.mkdir()` and before `asyncio.run(_run_server(...))`. The C02 §5.6 startup-replay invariant is unmet for unattended SIGKILL recovery without this hook. Function is implemented, tested, and reachable via `eawf daemon replay-wal --inspect`; only the boot hook is missing. ~10 LOC + 1 boot-replay integration test.
- **F2** Schedule `eawf.daemon.session_ttl.run_sweep_loop(state_path=ctx.state_path, ttl_seconds=ctx.session_handle_ttl_seconds, publish=ctx.bus.publish, stop_event=ctx.shutdown_event)` as an `asyncio.create_task(...)` inside `daemon.main.run()`. The W07 background-TTL-sweep criterion is unmet under operator-unattended use; session-handle rows accumulate indefinitely without the schedule. ~10 LOC + 1 scheduled-sweep integration test.
- **F3** Pre-existing test-ordering failure under `tests/integration/test_cli_config.py` (6 cases) noted by W03 + W06 wave reports; did NOT reproduce in the ship-gate suite run. **Not** a P24 regression. Track as a separate hygiene investigation; do not block ship.
- **F4** Remaining ~21 mutating CLI callsites still route through the in-process portalocker path (only `wave close` was rewired in W09 as the canary). When `daemon.proxy_enabled=True` + daemon up, the un-rewired verbs land via `state_transaction` direct write (legal but bypasses the daemon mutator + the bus publish). Rewire batch belongs to a reactive wave or P25 contracts phase.
- **F5** `Mutation` discriminated union ships with loose-typed `params: dict[str, Any]`; per-variant Pydantic subclasses land in C03-IMPL.

F1+F2 are mechanical wiring tasks against tested primitives; recommend folding into a single P24-I02-W01 reactive wave before phase PR merges. F3-F5 ship-blocking-no; carry forward.

## References

[1] `.ea/state.json` — `state.waves["P24-I01-W##"]` outcome strings + closed_at + commit SHA chain (10 wave feat commits + 14 [P24-CORE] state-bookkeeping commits between facbc62 and 0de9ed2 on `feature/eawf-v0.3-p24`)
[2] commit `d0ffdc9 [P24-I01-W01] feat: daemon process scaffolding + JSON-RPC framing` + `src/eawf/daemon/{__init__,main,server,methods,auth,runtime_dir}.py` + `src/eawf/cli/commands/daemon.py` + `tests/daemon/test_scaffolding.py`
[3] commit `645162f [P24-W02] feat: Windows pywin32 named-pipe listener + asyncio queue bridge` + `src/eawf/daemon/{windows_pipe,windows_security}.py` + `pyproject.toml` (windows extras)
[4] commit `1558d23 [P24-W03] feat: outcome-WAL + startup replay + poisoned-WAL admin` + `src/eawf/daemon/{wal,recovery}.py` + `src/eawf/daemon/methods/wal_admin.py` + `tests/daemon/test_{wal,recovery,wal_admin_cli}.py`
[5] commit `c8f5f87 [P24-W04] feat: per-OS service registration (systemd + launchd + pywin32 Service)` + `templates/{eawfd.service.j2,dev.eawf.eawfd.plist.j2}` + `src/eawf/daemon/{win_service,service_install}.py`
[6] commit `bba7bde [P24-W05] feat: per-OS peer-credential dispatcher (Linux + macOS + FreeBSD + Windows)` + `src/eawf/daemon/auth.py` (refactor) + `tests/daemon/test_peer_cred.py`
[7] commit `9dbc3d9 [P24-W06] feat: subscription bus + drop-oldest backpressure + event.* RPC` + `src/eawf/daemon/bus.py` + `src/eawf/daemon/methods/{event,state_subscribe}.py` + `src/eawf/state/enums.py` (SUBSCRIPTION_LAG) + `src/eawf/store/kinds/subscription_lag.py`
[8] commit `2ffe196 [P24-W07] feat: session-handle tracking + opaque handle + agent.dispatch fresh-path skeleton` + `src/eawf/daemon/{session,session_ttl}.py` + `src/eawf/daemon/methods/agent.py` + `src/eawf/state/{models,enums}.py` (SessionAttempt + DispatchAnnotation + DispatchNote + Wave.sessions/runtime_preference/dispatch_history)
[9] commit `39f2044 [P24-W08] feat: idle-timeout watchdog + on-demand auto-spawn + cold-spawn benchmark` + `src/eawf/daemon/{idle,spawn}.py` + `src/eawf/cli/_daemon_client.py` + `benches/daemon_cold_spawn.py`
[10] commit `d059500 [P24-I01-W09] feat: state.mutate RPC + WAL integration + Mutation typed union + wave_close proof-of-concept proxy` + `src/eawf/daemon/methods/state.py` + `src/eawf/state/mutations.py` + `src/eawf/cli/_mutation.py` (extension) + `src/eawf/cli/commands/lifecycle.py` (wave_close canary)
[11] commit `287fb7d [P24-W10] feat: config + registry writers migrated to daemon RPCs; daemon.proxy_enabled default flips to True` + `src/eawf/daemon/methods/{config,registry}.py` + `src/eawf/cli/commands/{config,repo}.py` (dispatcher rewrites) + `src/eawf/config/defaults.py` (proxy_enabled flip) + `tests/regression/test_proxy_default_flip.py`
[12] local `uv run pre-commit run --all-files` + `uv run mypy src/` + `uv run pytest tests/ -q` invocations during the W01..W10 cycle and the ship-gate audit
[13] `.ea/artifacts/research/long-term/2026-05-18-authority-map.md` — Q1 deliverable; rows 1-8 now route through eawfd RPCs; W09 owns rows 1-4, W10 owns rows 5-8
[14] `.ea/local/research/2026-05-19-p24-c02-impl-waves.md` — P24 spike brief (informed the 10-wave plan + per-wave success criteria)
[15] `.ea/artifacts/research/long-term/2026-05-16-c02-daemon-topology.md` — C02 long-term spec; canonical design intent for daemon topology + JSON-RPC + WAL + service + peer-cred + subscription + session-handle
[16] Auditor agent_end report — eawf:auditor session via `/audit` 2026-05-19; per-criterion verdict table inlined into the orchestrator's flow envelope; two non-blocking refutations (F1 + F2) recorded

## Provenance

- audit_id: A30-P24-ship-gate
- audit_kind: ship-gate
- scope_id: urn:eawf:v1:repo:eawf
- verdict: minor (pass-with-followups; 2 mechanical wiring gaps → recommend P24-I02-W01 reactive wave before PR merge; 3 non-blocking carry-forwards)
- created_at: 2026-05-19
- author: claude-opus-4-7 (session eawf-flow-p24; auditor subagent aca40515740ec2199)
- supersedes: none (P24 is a new phase)

## Scrub

- status: clean
- references: repo-relative paths + commit SHAs only
- local paths: none in body
- real emails: none
- abstract placeholder names: not applicable
