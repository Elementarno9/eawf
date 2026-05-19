# SDK pre-release probe — pre-2026-06-15 baseline snapshot

**Created:** 2026-05-19
**Phase / wave:** P25-I01-W18 (cluster C07a)
**Cut-off context:** Anthropic subscription-billed SDK ships 2026-06-15 with per-user credit pools; `claude -p` subprocess gets swept into the same pool [1]. This artifact captures the *advertised* runtime contract **before** that date so a v0.4 hygiene wave can diff the re-probe and detect drift.

## Summary

C07a §5.2 [1] locks the v0.3 dispatch surface to **subprocess-primary** for all three runtimes (`claude -p`, `codex exec`, `opencode run`); SDK adoption is gated on the 2026-06-15 credit-pool reinstatement [1] and three relaxation triggers (operator BYOK, ≥$100 Pro credit, subscription-OAuth without API-rate metering) [1]. The runtime-capability matrix the eawf adapter relies on (subprocess primary surface + advertised SDK feature flags) is a moving target — `claude` ships near-weekly; `codex` lists 74 feature flags as of 2026-05-19; `opencode` upstream issue #17910 [1] is still live. Without a baseline, the v0.4 re-probe has nothing to diff against.

This wave is the snapshot. The probe `src/eawf/runtimes/probes/sdk_baseline.py` invokes each binary with `--version` + `--help`, parses for the eawf-adapter-relevant flag set [1], hashes the help body so future drift detection can run without re-shipping ~80 lines of help prose, and emits a typed JSON snapshot. Binaries absent from `$PATH` degrade gracefully (`installed: false`) so the probe runs on any host. The artifact body records the **2026-05-19 baseline rows** below; the v0.4 re-probe re-runs the same probe + diffs.

### Baseline rows (2026-05-19)

| Runtime | Bin | Version | Primary subprocess surface (per [1] §5.2) | Advertised SDK-shaped flags | Notes |
|---|---|---|---|---|---|
| `claude-code` | `claude` | `2.1.144 (Claude Code)` | `claude -p <prompt> --output-format=json --session-id=<uuid>` | 13 hits incl. `--session-id`, `--continue`, `--resume`, `--fork-session`, `--output-format`, `--input-format`, `--json-schema`, `--max-budget-usd`, `--mcp-config`, `--allowedTools`, `--allowed-tools`, `--print`, `-p` | Anthropic SDK package separate (`claude-agent-sdk`); CLI is the v0.3 path. |
| `codex` | `codex` | `codex-cli 0.130.0` | `codex exec <prompt> --json --model <model>` | 10 hits incl. `exec`, `resume`, `fork`, `mcp`, `mcp-server`, `exec-server`, `features`, `--enable`, `--disable`, `-c` | `codex features list` returns **74** rows: 22 stable+enabled, 0 stable+disabled, 5 experimental, 28 under-development, 16 removed, 3 deprecated [2]. |
| `opencode` | `opencode` | `1.14.33` | `opencode run <message> --format json --session <sid>` | 8 hits incl. `--continue`, `--session`, `--fork`, `--model`, `--agent`, `run`, `serve`, `session` | `--help` writes the body to **stderr** (yargs convention); the probe coalesces both streams. |

Per-runtime `help_excerpt_sha256` rows live in the JSON snapshot; they are the drift anchor the v0.4 re-probe diffs.

### What the probe records

The snapshot shape is a frozen dataclass tree (`BaselineSnapshot` → `tuple[RuntimeProbeRow, ...]`) [3]; `dataclasses.asdict` + `json.dumps(indent=2)` give a stable JSON serialisation. Fields per row:

- `runtime_id` — `claude-code` / `codex` / `opencode` (matches the eawf adapter id per [1] §5.1).
- `bin_name` — the on-disk binary name the probe shells out to.
- `installed` — `False` when `shutil.which(bin_name)` resolves to `None`.
- `bin_basename` + `bin_parent_kind` — basename + coarse parent classification (`homebrew` / `user-local` / `system` / `other`). The probe never serialises an absolute path; AGENTS rule 16 [4] enforces no machine-path leaks in committed artifacts (no home-prefix paths reach the snapshot).
- `version` — first non-empty line of `<bin> --version` (`stderr` falls back when `stdout` is empty).
- `subprocess_primary_surface` — the eawf-adapter invocation form (hard-coded per [1] §5.2); populated even when `installed=false` so the surface is comparable across hosts.
- `advertised_sdk_flags` — per-runtime hint-list intersection against the `--help` body (literal token match); the hint sets cover the session-resume + output-format + tool-allowlist + MCP-config flags the adapter relies on.
- `advertised_features` — populated for `codex` only via `codex features list`; rows shaped `<name>:<stage>:<bool>`. Stage values observed today: `stable`, `experimental`, `under development`, `removed`, `deprecated`.
- `help_excerpt_sha256` — SHA-256 of the rendered help body (stdout-first, stderr-fallback) so a future re-probe can detect drift without storing ~80 lines of prose per runtime.
- `error` — captured failure mode (timeout, non-zero exit, missing binary mid-probe); the row stays structurally complete.

### v0.4 re-probe checkpoint

The v0.4 hygiene wave [1] re-runs `uv run python -m eawf.runtimes.probes.sdk_baseline` after 2026-06-15 and compares the new snapshot to this baseline. Expected diff axes:

1. **Version drift.** Major-version bump in `version` for any runtime — re-pin the adapter's `--help` flag-set hints.
2. **Surface drift.** `subprocess_primary_surface` mismatch is a contract break (`-p` no longer accepted; `codex exec` superseded; `opencode run` deprecated). If hit, the eawf adapter halts the C07a W10 RuntimeAdapter implementation pending a roadmap revise.
3. **Flag set drift.** `advertised_sdk_flags` length / contents change — e.g. `--max-budget-usd` retired post credit-pool reinstatement; `--session-id` renamed.
4. **Codex feature graduation.** `advertised_features` rows shift stage (`under development` → `stable`; `experimental` → `stable`; `stable` → `removed`).
5. **`help_excerpt_sha256` mismatch.** Coarse smoke test — any change anywhere in the help body flips this hash. Use it to gate the deeper field-by-field diff (no hash change → trust the field-by-field result is identical too).

The re-probe is intentionally *additive*: it never mutates this baseline file. It writes a fresh snapshot under `.ea/artifacts/research/long-term/<post-cutoff-date>-sdk-postrelease-probe.md` + JSON sidecar, and the comparison happens at audit time.

### Scope guardrails

This wave operates on **raw subprocess + advertised flags only**. It does NOT import `RuntimeAdapter` (lands in W10 [5]) or any runtime-adapter module — independence verified at code review. The probe's only runtime contact is `subprocess.run([<bin>, '--version' | '--help'])` plus a `codex features list` call. No network, no state mutation, no daemon RPC.

## References

[1] `.ea/artifacts/research/long-term/2026-05-16-c07a-runtime-skill-dispatch.md` — C07a runtime / skill / dispatch brief; §5.1 RuntimeAdapter Protocol; §5.2 SDK tradeoff matrix + adoption gate (V8 + blitz r3); §8 Q5 SDK adoption gate verdict; references [20]/[21]/[23] for OpenCode/Codex/Anthropic surface evidence.
[2] `src/eawf/runtimes/probes/sdk_baseline.py` — probe script emitted by this wave; `probe_all()` returns the typed snapshot; `main(argv)` is the CLI entry point.
[3] `tests/integration/test_sdk_probe_baseline.py` — integration tests covering installed + not-installed paths, parent-kind classification, machine-path scrub, and stdout/path emit modes.
[4] `AGENTS.md` — non-negotiable rules; rule 9 (f-strings only); rule 16 (secrets and PII hygiene — no absolute home-prefix paths in committed artifacts); rule 17 (naming conventions — `output_dir`, log-key form); rule 18 (artifact chassis + citation policy); rule 25 (no design-decision references in source comments).
[5] `.ea/local/research/2026-05-19-p25-c03-c07a-c07b-c08-waves.md` — P25 wave plan; §3 row W18 (this wave); §6 OQ-3 captures the SDK-probe re-run deferral rationale.

## Provenance

- `store_record=none (research artifact, no Decision URN; pre-2026-06-15 baseline only)`
- `commit=pending (wave commit lands this artifact + probe + tests)`
- `cluster=C07a`
- `phase=P25`
- `wave=P25-I01-W18`
- `consumes=[1] §5.2 SDK tradeoff matrix + §8 Q5 verdict + [5] §6 OQ-3`
- `supersedes=none`
- `audit_consumed=none`
- `session=eawf-flow-p25-w18-2026-05-19`

## Scrub

- status: clean
- references: repo-relative only
- local paths: 0 (probe records `bin_basename` + `bin_parent_kind` only — absolute paths never serialised)
- real emails: 0
- abstract placeholder names: not applicable
