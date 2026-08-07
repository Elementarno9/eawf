# C11 — External integrations — Eä framework long-term specs

- **cluster:** C11
- **title:** External integrations (GitHub mandatory; rest opt-in/deferred)
- **status:** **accepted-final** (ratified 2026-05-17 via R-c11-1 + R-c11-4 AUQ; D11.A..K locked through v0.5 ship; cluster brief sign-off; open Q-c11-1..12 retained as future-AUQ seeds; spec phase deliverable closed; ready for P30/P31 implementation phase planning)
- **created:** 2026-05-17
- **author:** Claude Opus 4.7 (1M context)
- **depends_on:** [C00, C02, C04, C05, C06, C07a, C07b, C08, C09]
- **consumed_by:** none (terminal cluster in spec phase)

## 1. Purpose + scope statement

Locks the bridges between eawf and external systems that the operator and the daemon must talk to over the network or via vendor CLIs. **GitHub is mandatory** at v0.3 — the existing `gh pr` shell-outs in skill bodies graduate to a library-side bridge that respects V1 (daemon mediates) and V8 (session reuse). Everything else (Linear, Jira, Slack, Calendar, Notion, future agent runtimes) is enumerated in the **integration catalog** with a status flag — opt-in or deferred — so the catalog itself does not block v0.3 ship. ~~The HTTP **webhook ingress** surface is mandatory at v0.4 to receive inbound `pull_request`, `check_run`, and arbitrary signed callbacks~~ **REVISED 2026-05-18 per XB23 / Q15: webhook ingress model = local polling for v0.3-v0.5.** The daemon polls the GitHub API (gh API) for `pull_request`, `check_run`, and `workflow_run` updates rather than receiving inbound webhook deliveries. No HTTP listener bound; no public exposure / tunneling / relay needed. Bounded by GitHub API rate limit (5000 req/hr authenticated). **Q26 follow-on (2026-05-18 post-blitz): webhook listener code is DELETED v0.3 — strict YAGNI.** `daemon/webhook_listener.py` + associated tests removed in C11-IMPL W01. HMAC verify_signature primitive (§5.4) stays as reusable library for v0.6+ relay/tunnel ingress; daemon listener code re-implements when ingress model ratifies. The **integration manifest** (how a profile under V3 contributes an integration) is locked at v0.3 so third-party integrations have a target shape from day one even though only the GitHub one ships in-tree.

This is the terminal cluster: it consumes the daemon (C02), the event bus and renderer (C07b), the skill registry (C04), the CLI surface (C05), the TUI surface (C06), the profile loader (C08), and the doctor verbs (C09). It produces no spec other clusters depend on. The brief therefore favours **catalog enumeration over deep design** for deferred items — each deferred row has enough detail to plan the implementation phase but does not block v0.3-v0.5.

## 2. Goals + non-goals

### Goals

1. **GitHub bridge as library code.** Move the `gh pr create / merge / diff / view` shell-outs out of skill prose into `src/eawf/integrations/github/` so the daemon can audit them, retry them, and surface them to the TUI [11; src/eawf/render/skills.py:321,333,349].
2. **HMAC-signed webhook ingress on the daemon.** A single HTTP listener accepts signed payloads, validates them per source, routes to the per-integration handler, and emits typed events on the bus [3:304-310].
3. **In-daemon asyncio worker per integration** (V1). Each enabled integration registers an `IntegrationWorker` instance that the daemon supervises with backoff + restart policy [1:24-53].
4. **Integration manifest tied to V3 profile composition.** A profile declares `integrations: [<name>: <enabled>]`; the loader resolves implementations via the `eawf.integrations` Python entry-point group [1:76-96].
5. **Doctor verb covers every shipped integration.** `eawf doctor --integration <id>` cross-checks auth, last successful sync, queue depth, last error (parity with the runtime doctor at C09).
6. **Catalog every plausible v0.3-v0.6 integration** with status (`shipped` | `opt-in` | `deferred`) + per-integration auth + sync direction so the v0.6+ planner has a starting matrix.

### Non-goals

1. Implementing Linear, Jira, Slack, Calendar, Notion in v0.3-v0.5 — every one of those is `deferred` per operator AUQ 2026-05-17 (§4).
2. Outbound mail / SMS / phone integrations — not in scope this cluster series.
3. Replacing `gh` with PyGithub — the gh CLI is the bridge; native HTTP is reserved for v0.6+ if the operator flips the auth axis (§4 D11.A).
4. Building a generic plugin marketplace for integrations — entry-point discovery is the integration analogue of the C07a plugin model; a separate marketplace is out of scope.
5. Multi-tenant webhook ingress (one daemon receiving for many operators / orgs) — single-user invariant under V1 still holds.

## 3. Prior verdicts cited

### V1 — eawfd daemon Day-1 + smart-spawn writer [1:24-53]

Load-bearing. Integration workers run **inside** the daemon process as `asyncio.Task` instances (D11.B = "In-daemon asyncio tasks"). The daemon's auto-spawn semantic carries — first webhook delivery to the listener triggers spawn just like first mutation; idle-shutdown drains queued deliveries first.

### V3 — Composable profile bundle with declared precedence [1:76-96]

Load-bearing. Profiles declare `integrations:` contributions; the loader composes the enabled set with the same conflict / precedence rules as skills and templates. A profile shipping a third-party integration registers it via the `eawf.integrations` entry-point group (D11.D = "Profile YAML field + Python entry-point").

### V5 — Runtime fallback: reactive switchover on error [1:127-151]

Cited for **outbound** integration calls only (e.g. GitHub API hits a 429). Integration workers reuse the same backoff-and-pause primitives as the runtime ladder, but do NOT participate in the runtime preference ladder itself — an integration call has no "fall back to another runtime" notion.

### V7 — Telemetry: vendor agent-lens schema, rebuild inside eawf [1:184-224]

Cited for the metrics surface: every integration call emits a `dispatch_cost`-shaped event (or a new `integration_call` sub-type — §5.7) so the user-scope DuckDB projects per-integration latency / error / retry counts.

### V8 — Agent dispatch: hybrid session reuse [1:226-271]

Cited for the **GitHub bridge ↔ /ship and /review skill bodies** path. Skills emit a side-effecting verb (`eawf integration github pr create ...`); the daemon routes it through the bridge; the skill's session continues on the same handle. The bridge does NOT spawn a fresh agent session per call.

### V9 — Native per-runtime plugins remain first-class distribution channel [1:273-315]

Cited for the **integration manifest** schema. Each integration's `manifest.yaml` mirrors the plugin-manifest pattern from C07a §5.7 [12:310-370]. The doctor verb walks the same source tree.

### C00 §C11 scope statement [1:947-981]

The cluster catalog row that seeds this brief: integration catalog with per-integration status, auth, sync direction; webhook signing spec; integration-manifest authoring path.

### C02 §5.3.2 — event.subscribe + event.push [3:304-310]

The webhook ingress emits envelopes onto the same event bus the TUI subscribes to (`EnvelopeKind` extends with `integration_*` sub-types — §5.7). The daemon's subscription bus is the only routing surface.

### C02 §5.7 — Subscription bus + backpressure [3:433-456]

The integration listener treats inbound webhooks as another subscriber load source; the same 1024-deque cap applies per-integration.

### C04 §"loop / schedule" skill surface [8]

Calendar integration (deferred) substitutes for `/loop` when cross-machine scheduling is needed. v0.3 ships `/loop` only; calendar deferred to v0.5+.

### C05 §5.1.11 — `pr` verb [6:347-360]

Today the `pr render` verb writes a Markdown body to stdout. C11 extends C05 with `eawf integration` noun-app (§5.6) that owns the actual `gh pr create` invocation. `pr render` remains pure formatter (no state read); `integration github pr create` is the mutator [21:255-262].

### C06 §5.7 — PrListModal [13:749]

The TUI's PR list overlay currently delegates to `gh pr view --web`. With the bridge in place, the overlay subscribes to `integration_github_pr_*` events for live status; the click-through still defers to `gh ... --web` so the operator's authentication flow is unchanged.

### C07a §5.7 — Plugin manifest schema [12:310-370]

Template for the integration manifest schema (§5.2). Same body-hash / timestamp marker pattern; same source-of-truth philosophy.

### C07b §5.4 — Event / audit log + EventPayload [14:353-471]

The closed `StoreKind` enum extends with no new top-level kind; instead new EVENT sub-types under `event_type` cover integration events (§5.7).

### C08 §"integrations:" profile field

Profile YAML grows an `integrations: [...]` list; loader composes per-profile contributions. Specced in C08 brief; consumed here.

### C09 §"doctor" verb

`eawf doctor --integration <id>` parity with `--runtime <id>`. Specced in C09; consumed here.

### Blitz rounds B-c11-1..7 (2026-05-17 fold-back) [31][32][33][34][35][36]

Six single-axis blitz briefs across two rounds verified spec assumptions against live vendor docs + man pages + `gh --help` + source. Round-1 axes (B-c11-1..4): dep transitive gates; gh CLI behavioural verification; webhook signing per vendor; keyring backend matrix. Round-2 axes (B-c11-5, B-c11-7): asyncio + RLIMIT_AS scope bug; gh write-verb idempotency under retry. Verdicts folded into §5.2 (GitHub timestamp correction), §5.3 (gh CLI fixes + `WriteRetryPolicy`), §5.4 (UUID-dedup primitive + aiohttp `AppRunner` + explicit `client_max_size` wiring), §5.5 (RLIMIT_AS scope correction; daemon-wide ceiling replaces wrong per-task claim), §5.7 (`integration_*_call_already_done` event sub-type), §5.8 (vendor stub corrections), §5.9 (keyring version bump + CI matrix), §6 (new failure modes F-W11..F-W13), §7.1 (new `retry_policy.py` module), §7.2 (dep pin bump). Per-axis resolved tags in §8. Full amendment summary in §10.

## 4. Decision matrix

| Axis | Options | Operator pick (2026-05-17 AUQ) | Rationale |
|---|---|---|---|
| **D11.A — GitHub auth source of truth** | (a) gh CLI subshell; (b) PAT in OS keyring + PyGithub; (c) GitHub App per-installation token; (d) hybrid gh-then-PAT | **(a) gh CLI subshell** | Zero net-new auth code; matches the existing skill bodies at `src/eawf/render/skills.py:321,333,349`; `gh` already owns the OAuth refresh dance + scope prompts; eawf inherits that surface for free. v0.6+ may revisit when GitHub App audit perms become a hard requirement. |
| **D11.B — Per-integration worker model** | (a) in-daemon asyncio tasks; (b) subprocess worker per integration; (c) external cron + one-shot CLI; (d) profile-contributed worker class | **(a) in-daemon asyncio tasks** | V1 already commits the daemon to coordinator status. Workers share the same state lock, event bus, runtime ladder, and supervisor. Per-integration crash isolation is solved by `Task.add_done_callback` + supervisor restart, not by extra processes. Single-OOM-kills-all is real but mitigated by per-task memory caps + the V1 idle-respawn loop. |
| **D11.C — Webhook ingress auth** | (a) HMAC signed payload only; (b) HMAC + IP allowlist; (c) mTLS only; (d) bearer token in header | **(a) HMAC only** | GitHub + Slack + Stripe all use HMAC-SHA256 over body + timestamp. IP allowlists rot fast (GitHub's egress range alone changed three times in 2025). mTLS is unavailable from most SaaS senders. Bearer tokens lack replay protection. HMAC + timestamp window is the industry-default sweet spot. |
| **D11.D — Linear / Jira sync direction at v0.5** | (a) read-only inbound; (b) bidirectional via explicit verb; (c) auto bidirectional; (d) defer to v0.6+ | **(d) defer to v0.6+** | Operator scope-cut. Linear/Jira go into the catalog with `status: deferred`; §5.8 holds the spec stub so v0.6+ planning has a starting point. |
| **D11.E — Slack event-filter granularity at v0.3** | (a) per-event-kind allowlist; (b) severity-only; (c) DSL filter; (d) plugin callable | **defer Slack to v0.6+** (operator explicit) | Same scope-cut as Linear. Catalogued; spec stub at §5.8. |
| **D11.F — Notion render trigger** | (a) manual CLI verb only; (b) auto on artifact promotion; (c) hybrid + per-artifact flag; (d) defer | **(d) defer to v0.6+** | Same. |
| **D11.G — Calendar integration scope** | (a) defer to v0.5+; (b) read-only event surface; (c) trigger-only push; (d) bidirectional wave deadlines | **(a) defer to v0.5+** | `/loop` covers the in-tree scheduling need; calendar revisits when cross-machine routine sync becomes a real ask. |
| **D11.H — Integration manifest authoring** | (a) profile YAML field + Python entry-point; (b) single global `.ea/integrations.yaml`; (c) per-integration dir + walk; (d) defer manifest to v0.5 | **(a) profile YAML + entry-point** | Mirrors C07a §5.7 plugin manifest; same loader code path; same doctor walk; same body-hash marker. Third-party integrations get a stable target shape at v0.3. |
| **D11.I — Webhook listener port + bind** | (a) ephemeral on `<host>` only; (b) operator-configurable port + bind | **(b)** — picked here, no AUQ needed (single answer is obvious for ingress) | Operator configures `integrations.webhook.bind: <host>:7681` (default). Public bind requires explicit `0.0.0.0` and surfaces a doctor warning that HMAC alone is insufficient — must front with a reverse proxy. |
| **D11.J — Outbound retry / backoff** | (a) shared with V5 runtime ladder; (b) per-integration policy | **(b) per-integration policy** — picked here | An integration that talks to GitHub has different RPS budgets than one talking to Slack. Per-integration `RetryPolicy(max_attempts, backoff_base_ms, max_backoff_ms, jitter)` field on the manifest. |
| **D11.K — Idempotency keys for outbound writes** | (a) integration-emitted; (b) bridge-generated; (c) none | **(b) bridge-generated** — picked here | Bridge mints `idempotency_key = sha256(integration_id + verb + scope_id + payload_canonical)` per call; daemon dedupes within the C02 §Q7 60-s window [3:262] (same locked window as agent dispatch). |

## 5. Proposed schema / API / protocol

### 5.1 Integration catalog (v0.3 → v0.6)

| Integration ID | Status (v0.3-v0.5) | Direction | Auth | Worker | Webhook in | Out verbs |
|---|---|---|---|---|---|---|
| `github` | **shipped (mandatory v0.3)** | bidirectional (out via gh; in via webhook) | gh CLI subshell | yes (asyncio Task) | yes (`pull_request`, `check_run`, `issue_comment`, `workflow_run`) | `pr create / merge / diff / view`; `release create`; `issue list / view` |
| `webhook` (generic ingress) | **shipped (mandatory v0.4)** | inbound only | HMAC-SHA256 per source | listener only (no Worker) | yes (any source) | n/a |
| `linear` | deferred v0.6+ | bidirectional planned | OAuth refresh in keyring | yes | planned | planned |
| `jira` | deferred v0.6+ | bidirectional planned | OAuth refresh + Atlassian Cloud OAuth dance | yes | planned | planned |
| `slack` | deferred v0.6+ | outbound (events → channel notifications); inbound (slash-command webhooks) | bot token in keyring | yes | planned | `notify` |
| `calendar-gcal` | deferred v0.5+ | inbound (events trigger skills) | OAuth + IMAP-style refresh | yes | planned | none (read-only) |
| `notion` | deferred v0.6+ | outbound (artifact → page) | integration token | yes | no | `publish` |
| `aider` | future runtime per G4 | n/a (this is a runtime adapter, lives in C07a) | n/a | n/a | n/a | n/a |
| `cursor` | future runtime per G4 | n/a (runtime adapter) | n/a | n/a | n/a | n/a |
| `cline` | future runtime per G4 | n/a (runtime adapter) | n/a | n/a | n/a | n/a |
| `discord` | catalogued; status `none` (no demand signal) | outbound notification candidate | bot token | yes | n/a | `notify` |
| `email-smtp` | catalogued; status `none` | outbound (digest emails) | SMTP creds in keyring | yes | no | `notify` |
| `prometheus` | catalogued; status `none` | outbound metrics push | bearer in keyring | yes | no | `push` |

**Catalog invariants.**

1. Every row carries `status ∈ {shipped, opt-in, deferred, none}`. `shipped` requires in-tree code under `src/eawf/integrations/<id>/`. `opt-in` requires schema + doctor + a v0.X label; `deferred` requires a §5.8 stub; `none` is enumeration-only (no commitment).
2. Aider / Cursor / Cline are **runtime adapters**, not integrations — they belong under C07a §5.1 RuntimeAdapter Protocol. They appear in this catalog only to mark the cross-cluster boundary so the C11 reader does not re-spec them here.
3. The catalog is **closed at C11** for v0.3-v0.5 — new rows arrive in C11-revisions per the post-v0.5 spec cadence. The DAG never re-opens C11 within the v0.3 → v0.5 window.

### 5.2 Integration manifest schema

Canonical shape lives at `src/eawf/integrations/<id>/manifest.yaml`. Pydantic-validated with `extra="forbid"` per AGENTS rule 2. Mirrors C07a §5.7 plugin manifest [12:310-370].

```yaml
# src/eawf/integrations/<id>/manifest.yaml
schema_version: "1"
integration:
  id: github                           # unique per repo install
  name: "GitHub bridge"
  version: "1.0"
  description: "PR create / merge / status sync via gh CLI; webhook ingress for pull_request / check_run / issue_comment / workflow_run"
  category: shipped                    # shipped | opt-in | deferred | none
  direction: bidirectional             # outbound | inbound | bidirectional
  worker_class: eawf.integrations.github.worker:GitHubWorker
  webhook:
    enabled: true
    sources:                           # one signing-secret per source slug
      - slug: github
        signature_header: X-Hub-Signature-256
        signature_format: "sha256={hex}"      # literal prefix per [33] U1
        signature_algorithm: hmac-sha256
        signed_string_template: "{body}"      # raw body bytes, no canonicalization [33]
        timestamp_header: null                # GitHub sends NO timestamp header [33] — replay protection via UUID dedup, NOT time window (B-c11-3 correction)
        timestamp_max_skew_seconds: null
        delivery_id_header: X-GitHub-Delivery # UUID; listener dedups against ring buffer (§5.4)
        secret_keyring_account: eawf-integration-github-webhook
        event_header: X-GitHub-Event
        allowed_event_types: [pull_request, check_run, issue_comment, workflow_run]
  auth:
    kind: subshell                     # subshell | keyring | oauth | none
    subshell:
      command: gh
      probe_args: [auth, status]
  retry_policy:
    max_attempts: 5
    backoff_base_ms: 200
    max_backoff_ms: 30000
    jitter: full
  rate_limit:
    rps_max: 5
    burst: 10
  contributes:
    cli_subcommands:                   # extends `eawf integration github ...`
      - pr.create
      - pr.merge
      - pr.diff
      - pr.view
      - issue.list
      - issue.view
      - release.create
    skills:                            # skill bodies eligible to call this integration
      - ship
      - review
      - polish
    events_emitted:                    # appear under StoreKind=EVENT, event_type=integration_*
      - integration_github_pr_opened
      - integration_github_pr_merged
      - integration_github_pr_status_changed
      - integration_github_check_run_completed
      - integration_github_workflow_run_completed
      - integration_github_webhook_received
      - integration_github_webhook_signature_invalid
      - integration_github_call_succeeded
      - integration_github_call_failed
      - integration_github_call_rate_limited
managed:
  body_hash_field: __eawf_managed.body_hash
  timestamp_field: __eawf_managed.timestamp
  source_files:
    - src/eawf/integrations/github/worker.py
    - src/eawf/integrations/github/bridge.py
    - src/eawf/integrations/github/webhook.py
```

**Loader path (C08 binding).** `eawf.integrations` Python entry-point group; each entry exports a `register() -> IntegrationManifest` callable. Profile YAML lists `integrations: [github, ...]`; loader resolves each ID against the entry-point registry; conflict / precedence rules carry over from V3.

### 5.3 GitHub bridge (mandatory v0.3)

The bridge is a thin async layer over `gh` subprocess invocations. Library code only; CLI dispatch in `src/eawf/cli/commands/integration_github.py`.

```python
# src/eawf/integrations/github/bridge.py — new module

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GitHubCallRequest(_StrictModel):
    """One outbound GitHub action through the gh subshell."""

    verb: Literal[
        "pr.create",
        "pr.merge",
        "pr.diff",
        "pr.view",
        "issue.list",
        "issue.view",
        "release.create",
    ]
    repo: str                                            # owner/name
    args: dict[str, Any]
    scope_id: str                                        # urn:eawf:v1:wave:P##-W##
    idempotency_key: str = Field(min_length=1)           # bridge-minted; see D11.K
    timeout_seconds: float = 30.0


class GitHubCallResult(_StrictModel):
    verb: str
    repo: str
    scope_id: str
    idempotency_key: str
    exit_code: int
    stdout_preview: str = Field(max_length=2000)        # truncated; full in event payload
    stderr_preview: str = Field(max_length=2000)
    elapsed_ms: int
    retried_count: int = 0
    rate_limited: bool = False


async def execute(
    request: GitHubCallRequest,
    *,
    cwd: str,
    env_overrides: dict[str, str] | None = None,
) -> GitHubCallResult:
    """Execute one GitHub action; retry per manifest RetryPolicy; emit events.

    Raises:
        GitHubAuthError: gh subshell reports auth failure (token expired / scope missing).
        GitHubRateLimitExhausted: retry budget exhausted on 429 / secondary-rate-limit.
        GitHubCallError: any other non-zero exit unrecoverable by retry.
    """
    cmd = _build_gh_argv(request)
    logger.info(
        f"execute repo={request.repo!r} verb={request.verb} scope={request.scope_id!r}"
    )
    # ... retry loop, exponential backoff, event emission to event.jsonl
    ...
```

**Doctor probe (B-c11-2 verified [32]).** `eawf doctor --integration github` runs **plain `gh auth status`** (NOT `--json` — that variant always exits 0 even on auth failure per gh-CLI help text). Exit-code semantics: `0` = logged in + valid; `1` = not logged in OR invalid token; `127` = `gh` not on PATH (shell error, not gh's). Doctor parses the active host = `github.com` and active scopes include `repo` + `read:org` from stdout. Then runs `gh api rate_limit` and reads `resources.core.{limit, used, remaining, reset}` (NOT the legacy top-level `rate.*` alias) into the user-scope DuckDB. Doctor also gates on `gh --version >= 2.40.0` and warns below that floor.

**Bridge merge-strategy enforcement (B-c11-2 verified [32]).** `gh pr merge` validates strategy flags client-side at line 106-120 of `pkg/cmd/pr/merge/merge.go` and returns exit 1 + `"only one of --merge, --rebase, or --squash can be enabled"` if more than one is passed. The bridge MUST validate caller-supplied args client-side BEFORE the subshell call: reject the call site if `--squash` or `--merge` appears in `args["flags"]`, then always append `--rebase`. Two-layer defence: never rely on gh-side rejection alone (TUI gets raw stderr; future gh versions could relax). Per `feedback_pr_merge_strategy` memory the policy is non-negotiable.

**Prompt disabling (B-c11-2 verified [32]).** `GH_PROMPT_DISABLED=1` env var is correct (verified via `gh help environment`). **There is no `--no-prompt` flag on any gh subcommand** — the §6 F-W9 reference to it is incorrect and corrected in the amendment log §10. Bridge sets only the env var.

**Body via stdin (B-c11-2 verified [32]).** `gh pr create --body-file -` reads body from stdin — bridge pipes the deterministic rendered body. `--draft` works. **Bridge never uses `--fill`** (non-deterministic across rebases — pulls from commit log which mutates).

**Write-verb idempotency (B-c11-7 verified [36]).** `gh pr create` and `gh release create` are **NOT** server-idempotent — duplicate calls create duplicate PRs / releases. `gh pr merge` is partially safe-on-replay (already-merged returns exit 1 but desired outcome is achieved; `state==MERGED` reclassifies as success). The C11 `idempotency_key` minted by the bridge (D11.K) suppresses redundant *daemon* dispatches within 60 s but does not prevent server-side duplication when the daemon restarts mid-flight, exceeds the window, or retries from another worker. Therefore the bridge MUST run a `WriteRetryPolicy`-defined probe BEFORE every write verb; on already-done, the bridge redirects (PR exists → `gh pr edit <N>`), reclassifies (already merged → success), or skips (release exists → emit `integration_github_call_already_done`) instead of failing. TOCTOU race windows are accepted for v0.3 (single-user daemon serializes the bridge; recoverable stderr patterns cover the collision case). `issue.create` is **NOT** in the v0.3 bridge — no reliable dedup primitive exists at the GitHub API level for issues (titles fuzzy-match only); future addition requires a content-addressed sentinel scheme.

```python
# src/eawf/integrations/github/retry_policy.py — new module (B-c11-7 [36])

from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProbeSpec(_StrictModel):
    command: list[str]                  # template vars: {branch}, {tag}, {pr_number}
    already_done_jq: str                # jq on probe stdout; truthy → already done
    already_done_exit_codes: list[int] = [0]
    not_found_exit_codes: list[int] = [1]


class WriteRetryPolicy(_StrictModel):
    verb: Literal["pr.create", "pr.merge", "release.create"]
    server_idempotent: bool
    probe: ProbeSpec
    on_already_done: Literal["skip", "redirect_edit", "reclassify_success"]
    on_already_done_event: str          # event_type sub-type under EVENT
    recoverable_stderr_patterns: list[str] = []


PR_CREATE_POLICY = WriteRetryPolicy(
    verb="pr.create",
    server_idempotent=False,
    probe=ProbeSpec(
        command=["gh", "pr", "list", "--head", "{branch}", "--state", "open",
                 "--json", "number,url", "--jq", ".[0]"],
        already_done_jq=". != null",
        already_done_exit_codes=[0],
        not_found_exit_codes=[0],
    ),
    on_already_done="redirect_edit",
    on_already_done_event="integration_github_call_already_done",
    recoverable_stderr_patterns=["a pull request for branch", "already exists"],
)

PR_MERGE_POLICY = WriteRetryPolicy(
    verb="pr.merge",
    server_idempotent=False,
    probe=ProbeSpec(
        command=["gh", "pr", "view", "{pr_number}", "--json", "state,mergedAt"],
        already_done_jq='.state == "MERGED"',
    ),
    on_already_done="reclassify_success",
    on_already_done_event="integration_github_call_already_done",
    recoverable_stderr_patterns=["not mergeable", "HTTP 405"],
)

RELEASE_CREATE_POLICY = WriteRetryPolicy(
    verb="release.create",
    server_idempotent=False,
    probe=ProbeSpec(
        command=["gh", "release", "view", "{tag}", "--json", "tagName,url"],
        already_done_jq=".tagName != null",
    ),
    on_already_done="skip",
    on_already_done_event="integration_github_call_already_done",
    recoverable_stderr_patterns=["Validation Failed", "HTTP 422"],
)
```

`execute()` dispatch from §5.3 looks up the policy by `request.verb`, runs `probe.command` template-filled, parses stdout with `already_done_jq`, and routes per `on_already_done` before invoking the actual write subprocess. New event sub-type added to §5.7 catalog: `integration_github_call_already_done`.

**Mutator surface (AGENTS rule 4 compliance).** The bridge does NOT write to `state.json`. Every GitHub action emits a typed event onto the bus; the daemon's state-writer subscribes to those events and projects them into the appropriate state field (e.g. `state.waves[*].pr_url` populated on `integration_github_pr_opened`). The bridge is therefore part of the **outbound-call** path, not the **state-mutator** path.

### 5.4 HTTP webhook ingress (HMAC signing)

Single listener on the daemon. One process; one port; per-integration sub-router.

```python
# src/eawf/daemon/webhook_listener.py — new module

from __future__ import annotations

import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class WebhookListenerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bind_host: str = Field(default="<host>", min_length=1)
    bind_port: int = Field(default=7681, ge=1024, le=65535)
    tls_cert_path: str | None = None                    # optional reverse-proxy bypass; v0.6+ if needed
    tls_key_path: str | None = None
    max_body_bytes: int = Field(default=1_048_576, ge=1024)  # 1 MiB default; MUST be wired into aiohttp explicitly (B-c11-5 [35])
    timestamp_window_seconds: int = Field(default=300, ge=10, le=3600)


# B-c11-5 [35]: aiohttp + existing-loop integration. Daemon supervisor owns the
# loop; use AppRunner + TCPSite, NEVER aiohttp.web.run_app (run_app owns the
# loop and conflicts with eawfd's existing event loop). `client_max_size`
# MUST be passed explicitly to web.Application — otherwise aiohttp silently
# uses its 1 MiB default regardless of WebhookListenerConfig.max_body_bytes.
async def start_webhook_listener(cfg: WebhookListenerConfig) -> "web.AppRunner":
    """Attach the webhook listener to the daemon's existing asyncio loop.

    The caller's responsibility: persist the returned AppRunner and call
    `await runner.cleanup()` on daemon shutdown.

    Raises:
        OSError: bind_port already in use → caller emits
            `integration_webhook_listener_bind_failed`.
    """
    from aiohttp import web
    app = web.Application(client_max_size=cfg.max_body_bytes)  # explicit wiring
    app.router.add_post("/webhook/{source_slug}", _handle_delivery)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.bind_host, cfg.bind_port)
    await site.start()
    logger.info(f"start_webhook_listener bind={cfg.bind_host!r}:{cfg.bind_port}")
    return runner


def verify_hmac(
    *,
    body: bytes,
    timestamp: str | None,
    signature_header: str,
    secret: bytes,
    signed_string_template: str = "{body}",      # e.g. "v0:{timestamp}:{body}" for Slack
    signature_format: str = "sha256={hex}",      # e.g. "v0={hex}" for Slack, "{hex}" bare for Linear
    algorithm: str = "hmac-sha256",
) -> bool:
    """Constant-time HMAC verification with per-vendor templating (B-c11-3 [33]).

    The signed string is templated from the vendor spec; GitHub signs raw body
    (`"{body}"`), Slack signs `"v0:{timestamp}:{body}"`, Linear signs raw body
    but emits the hex bare without prefix.

    Raises:
        ValueError: algorithm unsupported, template references {timestamp} but
            timestamp is None, or signature_format malformed.
    """
    if algorithm != "hmac-sha256":
        raise ValueError(f"unsupported algorithm: {algorithm!r}")
    if "{timestamp}" in signed_string_template and timestamp is None:
        raise ValueError("template references {timestamp} but timestamp is None")
    # XB22 fix 2026-05-18: HMAC signs RAW BYTES, not decoded text.
    # body.decode("utf-8") loses byte-level fidelity (BOM, invalid UTF-8, locale variance);
    # valid signatures fail, malformed payloads may pass. Operate on bytes throughout.
    if "{timestamp}" in signed_string_template or "{body}" in signed_string_template:
        # Template uses str interpolation — render with bytes-safe substitution
        ts_str = (timestamp or "")
        # Templates that mix timestamp + body MUST sign the body as raw bytes.
        # Approach: pre-encode the template prefix/suffix, splice body bytes in.
        if "{body}" in signed_string_template:
            prefix, suffix = signed_string_template.split("{body}", 1)
            prefix_bytes = prefix.format(timestamp=ts_str).encode("utf-8")
            suffix_bytes = suffix.format(timestamp=ts_str).encode("utf-8")
            signed_bytes = prefix_bytes + body + suffix_bytes      # body stays raw
        else:
            signed_bytes = signed_string_template.format(timestamp=ts_str).encode("utf-8")
    else:
        signed_bytes = body                                          # raw bytes
    expected_hex = hmac.new(secret, signed_bytes, hashlib.sha256).hexdigest()
    expected_header = signature_format.format(hex=expected_hex)
    return hmac.compare_digest(expected_header, signature_header)


def verify_timestamp(*, timestamp_header: str, now_unix: float, window_seconds: int) -> bool:
    """Reject deliveries older than the window. Defence against replay.

    Raises:
        ValueError: header not parseable as int unix seconds.
    """
    try:
        delivered_at = int(timestamp_header)
    except ValueError as e:
        raise ValueError(f"bad timestamp header: {timestamp_header!r}") from e
    return abs(now_unix - delivered_at) <= window_seconds


# B-c11-3 [33]: GitHub-class vendors send NO timestamp header. Replay defence is
# UUID-dedup via a ring buffer keyed on `delivery_id_header` (e.g.
# `X-GitHub-Delivery`, `X-Atlassian-Webhook-Identifier`). Bounded LRU; default
# cap 10_000 UUIDs per source. Window-bypass for these sources is intentional.
class DeliveryIdDedup:
    """Bounded LRU of recent delivery UUIDs per source-slug (B-c11-3 [33])."""

    def __init__(self, *, cap: int = 10_000) -> None:
        from collections import OrderedDict
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._cap = cap

    def seen(self, delivery_id: str) -> bool:
        if delivery_id in self._seen:
            self._seen.move_to_end(delivery_id)
            return True
        self._seen[delivery_id] = None
        if len(self._seen) > self._cap:
            self._seen.popitem(last=False)
        return False
```

**Endpoint shape.**

```
POST /webhook/<source-slug>
Headers:
  Content-Type: application/json
  <signature-header>:   e.g. X-Hub-Signature-256: sha256=<hex>
  <timestamp-header>:   e.g. X-GitHub-Delivery: <unix-seconds-or-uuid>
  <event-header>:       e.g. X-GitHub-Event: pull_request
Body: vendor-shaped JSON (passed through to the integration handler)
```

**Lifecycle per delivery.**

1. Listener reads body up to `max_body_bytes` (rejects with `413 Payload Too Large` if exceeded; emits `integration_webhook_payload_too_large` event).
2. Looks up `<source-slug>` in the manifest registry. Unknown slug → `404`.
3. Loads the signing secret from the OS keyring under `secret_keyring_account`. Missing secret → `503` + emits `integration_webhook_secret_missing`.
4. Calls `verify_hmac()`. Failure → `401` + emits `integration_<id>_webhook_signature_invalid`. Failures NEVER carry the body in the event payload (PII / data-exfiltration risk).
5. Calls `verify_timestamp()` if `timestamp_header` is set on the manifest. Failure → `401` + emits `integration_<id>_webhook_timestamp_out_of_window`. **Per B-c11-3 [33]**: GitHub (and Jira) have NO timestamp header — listener falls through to step 5a instead.
5a. **UUID dedup (B-c11-3 [33])**: if `delivery_id_header` is set on the manifest, listener consults `DeliveryIdDedup.seen()` for that source-slug + delivery UUID. Already-seen → `200 OK` immediately + emits `integration_<id>_webhook_replay_suppressed` (no handler call; vendor's retry is intentionally absorbed).
6. Routes to the manifest-declared handler. Handler returns a typed `WebhookDecision` (`accept | skip | retry`). Listener responds `2xx` for accept/skip, `503` + queue-for-retry for retry.
7. On accept, listener emits `integration_<id>_webhook_received` envelope onto the event bus; downstream subscribers (e.g. the GitHub worker's pr-state projector) consume it.

**Backpressure.** Listener shares the C02 §5.7 subscription bus; per-integration deliveries route through the same 1024-deque cap. Drop policy: on overflow, listener returns `503` + emits `integration_<id>_webhook_backpressure_drop`; vendor's own redelivery semantics handle the retry.

### 5.5 In-daemon asyncio worker model

```python
# src/eawf/integrations/base.py — new module

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class IntegrationWorkerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    integration_id: str
    enabled: bool = True
    poll_interval_seconds: float | None = None          # None = event-driven only
    retry_policy_max_attempts: int = 5
    retry_policy_backoff_base_ms: int = 200
    retry_policy_max_backoff_ms: int = 30_000


class IntegrationWorker(ABC):
    """Abstract base for every in-daemon integration worker (V1).

    Lifecycle methods are awaited by the daemon supervisor. A worker that raises
    out of run() is restarted with exponential backoff per the manifest
    RetryPolicy. Five consecutive failures → BLOCKED + operator-notify event;
    supervisor stops restarting until operator clears via `eawf integration
    <id> resume`.
    """

    config: IntegrationWorkerConfig

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def run(self) -> None:
        """Main loop. Subscribes to bus events; handles webhooks; emits results.

        Raises:
            asyncio.CancelledError: supervisor signalled shutdown; clean up.
        """

    @abstractmethod
    async def stop(self) -> None: ...
```

**Supervisor (lives in eawfd).** Reads enabled integrations from the manifest registry; instantiates one `IntegrationWorker` per enabled row; tracks `(integration_id, task, restart_count, last_error, last_success_at)`. Exposes a `daemon.integration.status` RPC method for `eawf doctor --integration <id>` to read.

**Daemon-wide memory ceiling (NOT per-task — B-c11-5 [35] correction).** The original spec wrongly claimed `resource.setrlimit(RLIMIT_AS, ...)` provides a per-asyncio-task cap. `RLIMIT_AS` is **process-level** per `man 2 setrlimit` ("Resource limits are per-process attributes that are shared by all of the threads in a process") — it caps the entire daemon address space, not the offending integration worker. asyncio has no native per-task memory cap; no in-process primitive can enforce one without process isolation (which D11.B rejected). Three-layer defence in place of the wrong per-task cap:

1. **Process-level RLIMIT_AS ceiling on POSIX.** Daemon sets a single `RLIMIT_AS` cap at startup, sized for `sum(per_worker_soft_cap) × overrun_factor + base_daemon_overhead`. Default: 2 GiB total daemon footprint. Tunable via `daemon.memory_ceiling_mib`. Windows: skipped (no `setrlimit` equivalent; daemon relies on OS OOM-killer).
2. **Per-worker restart on `MemoryError`.** Each `IntegrationWorker.run()` is wrapped in `try/except MemoryError: raise asyncio.CancelledError(...)` so a worker that hits its own soft budget (via voluntary `tracemalloc.take_snapshot()` checks at quiet points) cooperatively cancels itself; supervisor restarts per V1.
3. **V1 OOM-killer + WAL recovery.** If the daemon process itself dies (single-OOM-kills-all risk per D11.B; F-W4), systemd / launchd / Windows Service respawns it; WAL replay from C02 §5.6 restores in-flight state. This is the explicitly-accepted residual risk per D11.B.

**Residual risk acknowledged.** Single OOM does kill all workers. v0.6+ may revisit (cgroups v2 conditional on Linux; subprocess workers on a per-integration basis if a misbehaving integration is identified). Not deferred via gap-in-spec; deferred via known-accepted trade-off.

### 5.6 CLI verb-noun: `eawf integration *`

Extends C05 §5.1 with a new noun-app. CLI is dispatch; library implements (AGENTS rule 1) [6:989].

| Verb | Subverb | Tier | Mut | Esc | Exit codes | Notes |
|---|---|---|---|---|---|---|
| `integration` | `list` | stable | R | bypass-ok | 1 | Lists enabled integrations + status from the manifest registry. |
| `integration` | `enable <id>` | experimental | W | daemon | 1, 3 | Sets `integrations.<id>.enabled: true` via the layered-config writer at `_save_value_to_layer`. Supervisor picks up on next reload. |
| `integration` | `disable <id>` | experimental | W | daemon | 1, 3 | Inverse. |
| `integration` | `status <id>` | stable | R | bypass-ok | 1 | Queue depth, last success, restart count, current backoff. Wrapped by `eawf doctor --integration <id>`. |
| `integration` | `secret set <id> <field>` | experimental | W | n/a | 1 | Writes a credential into the OS keyring under the manifest-declared `keyring_account`. NEVER prints the value back. |
| `integration` | `secret rm <id> <field>` | experimental | W | n/a | 1 | Removes the keyring entry. |
| `integration` | `resume <id>` | experimental | W | daemon | 1 | Clears BLOCKED status after operator inspection; resets restart counter. |
| `integration` | `github pr create` | stable | W | daemon | 1, 3 | Subshells `gh pr create` via the bridge. |
| `integration` | `github pr merge` | stable | W | daemon | 1, 3 | Wraps `gh pr merge` with the locked merge strategy from `feedback_pr_merge_strategy` memory: always `--rebase`, never `--squash`. |
| `integration` | `github pr diff <pr>` | stable | R | bypass-ok | 1 | Wraps `gh pr diff`. |
| `integration` | `github pr view <pr>` | stable | R | bypass-ok | 1 | Wraps `gh pr view`. |
| `integration` | `github issue list` | stable | R | bypass-ok | 1 | Wraps `gh issue list`. |
| `integration` | `github issue view <issue>` | stable | R | bypass-ok | 1 | Wraps `gh issue view`. |
| `integration` | `github release create` | stable | W | daemon | 1, 3 | Wraps `gh release create`. |
| ~~`integration`~~ | ~~`webhook show-secret <id> <source>`~~ | **REMOVED 2026-05-18 per XB24 / H-03** | — | — | — | **Verb removed.** Per XB24 audit fix: displaying the secret to stdout normalises secret disclosure; terminal scrollback persists; shell history may capture. Use `webhook generate-secret <id>` (one-time at registration) + `webhook set-secret <id> <name>` (operator pastes from vendor UI). For verification, use `webhook verify-secret <id> <name>` returning hash-prefix only (never the full secret). |
| `integration` | `webhook generate-secret <id>` | stable | W | daemon | 1 | One-time at registration. Generates a fresh HMAC secret + stores in keyring. Outputs a copy-paste prompt for the operator to add to the vendor UI. Re-runs require explicit `--force` + supersede confirmation. |
| `integration` | `webhook set-secret <id> <name>` | stable | W | daemon | 1 | Operator pastes a vendor-provided secret. Stored in keyring; never echoed. |
| `integration` | `webhook verify-secret <id>` | stable | R | bypass-ok | 1 | Returns hash-prefix (first 8 chars of SHA256) of the stored secret. Operator can compare against vendor UI display without exposing the full secret. |
| `integration` | `webhook test <id> <source>` | experimental | R | bypass-ok | 1 | Sends a synthetic signed payload to the local listener; verifies the round-trip; emits `integration_<id>_webhook_test_ok` envelope. Useful as a doctor sub-step. |
| `integration` | `webhook tail` | stable | R | bypass-ok | 1 | Tails recent `integration_*_webhook_received` events; `--follow`. |

**Total new verbs: 1 top-level noun-app + 16 subverbs.** Extends C05 §5.1's "11 new in v0.3" to 12 net-new top-level verbs.

### 5.7 Event bus binding

The closed `StoreKind` enum from C07b §5.4 [14:417-451] does NOT grow a new top-level kind. Instead, EVENT-kind sub-types extend with `integration_*` rows.

**New `event_type` sub-types under `EVENT` (v0.3-v0.5):**

```
Webhook ingress (mandatory v0.4):
- integration_<id>_webhook_received
- integration_<id>_webhook_signature_invalid
- integration_<id>_webhook_timestamp_out_of_window
- integration_<id>_webhook_secret_missing
- integration_<id>_webhook_payload_too_large
- integration_<id>_webhook_backpressure_drop
- integration_<id>_webhook_test_ok

Outbound call lifecycle (per integration; mandatory v0.3 for github):
- integration_<id>_call_started
- integration_<id>_call_succeeded
- integration_<id>_call_failed
- integration_<id>_call_rate_limited
- integration_<id>_call_idempotent_replay        # dedup hit within 60-s window
- integration_<id>_call_already_done             # B-c11-7 [36] WriteRetryPolicy probe found prior write

Worker supervisor (mandatory v0.3):
- integration_<id>_worker_started
- integration_<id>_worker_restarted
- integration_<id>_worker_blocked                # 5 consecutive failures
- integration_<id>_worker_resumed                # operator cleared block
- integration_<id>_worker_oom_killed             # POSIX RLIMIT_AS trip

GitHub-specific projections (v0.3):
- integration_github_pr_opened
- integration_github_pr_merged
- integration_github_pr_status_changed
- integration_github_check_run_completed
- integration_github_workflow_run_completed
```

**Event payload schema.** Reuses `EventPayload` from C07b §5.4 [14:399-412]; the `event_type` field carries the underscored sub-type; the existing `command`, `args_hash`, `status`, `message` fields project naturally. New fields are NOT added to `EventPayload` in v0.3-v0.5 — additional context lives in the un-typed `Envelope.payload` dict (closed schema bump deferred to v0.5+ per [14:453]).

**Audit-replay chain (AGENTS rule 19, agent-report contract).** Every `integration_<id>_call_*` event records the `idempotency_key`, the verb, the scope_id, and the `gh` argv (with secrets redacted by the bridge before emit). Replay against the JSONL reconstructs the exact call sequence — same property as the runtime-dispatch chain [14:471].

### 5.8 Deferred integration spec stubs

Operator-flipped to defer; each carries enough detail to plan the v0.6+ implementation phase without re-research.

#### 5.8.1 Linear (deferred v0.6+) — B-c11-3 verified [33]

- **Auth:** OAuth 2.0 refresh; tokens in keyring under `eawf-integration-linear-{access,refresh}`.
- **Direction:** bidirectional; explicit verbs (`integration linear sync push <wave>`, `integration linear sync pull`).
- **Sync mapping:** `Wave.linkages.linear_issue_id: str | None` field on the state model; populated by manual `eawf wave link linear <issue-id>`. Sync push writes wave status as a comment + label update, never as a body rewrite.
- **Rate budget:** Linear's GraphQL API has 1500 req/min per access token; per-integration rate-limit cap defaults to 100 RPM (well under quota).
- **Webhook signing (B-c11-3 [33]):** `Linear-Signature` header carries **bare hex** — NO `sha256=` prefix (unique among the four vendors). Raw body signed; timestamp is `webhookTimestamp` field **inside the JSON body** (Unix ms), not in any HTTP header. Recommended replay window: **60 s** (not 300). Manifest entry: `signature_header: Linear-Signature`, `signature_format: "{hex}"`, `signed_string_template: "{body}"`, `timestamp_header: null`, `body_timestamp_jsonpath: "$.webhookTimestamp"`, `timestamp_max_skew_seconds: 60`.

#### 5.8.2 Jira (deferred v0.6+) — B-c11-3 verified [33]

- **Auth:** Atlassian Cloud OAuth 2.0 (3LO); tokens in keyring; per-cloud-id scoping.
- **Direction:** bidirectional; same verb shape as Linear.
- **Sync mapping:** `Wave.linkages.jira_issue_key: str | None` (e.g. `PROJ-123`).
- **Rate budget:** 10 req/sec per app per cloud id.
- **Webhook signing (B-c11-3 [33]):** `X-Hub-Signature` header (same name as GitHub's deprecated SHA-1 header, but Jira uses **SHA-256**). Format follows WebSub `method=signature` envelope — listener parses the `method=` prefix dynamically. **No timestamp header**; replay protection via `X-Atlassian-Webhook-Identifier` UUID dedup (same primitive as GitHub §5.4). Manifest: `signature_header: X-Hub-Signature`, `signature_format: "{method}={hex}"`, `delivery_id_header: X-Atlassian-Webhook-Identifier`, `timestamp_header: null`.

#### 5.8.3 Slack (deferred v0.6+) — B-c11-3 verified [33]

- **Auth:** Bot User OAuth Token (`xoxb-...`) in keyring under `eawf-integration-slack-bot`.
- **Direction:** outbound (event notifications); inbound (slash commands / Events API).
- **Outbound model:** event-driven; worker subscribes to `event.subscribe`, applies an event-kind allowlist (axis D11.E will be re-asked at v0.6), formats with the same renderer chassis as TUI notifications [13:740-757], posts via Web API `chat.postMessage`.
- **Inbound webhook signing (B-c11-3 [33]):** `X-Slack-Signature` header carries `v0=<hex>`; signed string is **`v0:<timestamp>:<body>`** (NOT just a prefix — full composite). Timestamp in `X-Slack-Request-Timestamp` (Unix seconds). 5-minute replay window (`abs(now - timestamp) > 300` → reject). Manifest entry (correcting the original `signature_prefix_template`): `signature_header: X-Slack-Signature`, `signature_format: "v0={hex}"`, `signed_string_template: "v0:{timestamp}:{body}"`, `timestamp_header: X-Slack-Request-Timestamp`, `timestamp_max_skew_seconds: 300`.
- **Channel routing:** `integrations.slack.routes: { <event_type_glob>: <channel-id> }` table.

#### 5.8.4 Calendar — Google Calendar (deferred v0.5+)

- **Auth:** Google OAuth 2.0 with `calendar.events.readonly` scope.
- **Direction:** inbound only at v0.5+ (event → daemon trigger). Bidirectional (deadline → calendar event) reconsidered at v0.6+.
- **Trigger model:** worker polls every 60 s (calendar push API is not freely available without a registered domain); on event start, daemon invokes `eawf skill run <skill>` with scope per the event description metadata `eawf-skill: <name>; eawf-scope: <urn>`.
- **Webhook:** Google Calendar push channels require a verified HTTPS domain — deferred until eawf grows a tunnel / domain story (v0.6+).

#### 5.8.5 Notion (deferred v0.6+) — B-c11-3 verified [33]

- **Auth:** Notion internal integration token in keyring under `eawf-integration-notion-token`.
- **Direction:** outbound only at v0.6+ (artifact → page). **No webhook surface:** B-c11-3 confirmed Notion has no public production HMAC signing spec — all webhook-specific URLs return 404; status remains internal-beta as of 2026-05-17. If Notion ships a public webhook surface later, slot in as a follow-up blitz.
- **Render pipeline:** `eawf integration notion publish <artifact-id>` reads the promoted artifact under `.ea/artifacts/<artifact-id>.md`, applies the renderer chassis [14:497-510], converts to Notion blocks via the official `notion-client` Python SDK, pushes via `pages.create` or `blocks.children.append` (per `--mode create|append`).
- **Reverse-sync (page → artifact):** out of scope through v0.6.

#### 5.8.6 Discord (catalogued, status `none`)

Same notification pattern as Slack §5.8.3; webhook signing via Ed25519 (Discord's primitive). Adapter slot reserved; no roadmap commitment.

#### 5.8.7 Email SMTP (catalogued, status `none`)

Outbound digest emails; SMTP creds in keyring. Same notification-renderer chassis as Slack / Discord. No webhook (email lacks one).

#### 5.8.8 Prometheus textfile collector (catalogued, status `none`)

`eawf integration prometheus push --endpoint <pushgateway-url>` — wraps the V7 metrics export [1:206-208]. Adapter slot reserved; if operator picks up V7 with a Pushgateway target this drops in as a 50-LOC integration.

### 5.9 Auth keyring schema (B-c11-4 verified [34])

Single canonical writer: `src/eawf/integrations/secrets.py`. AGENTS rule 4 (single-mutator invariant) extended: the **keyring writer** is a fourth mutator surface beyond the three named in `AGENTS.md` §`mutator-path-precision`.

```python
# src/eawf/integrations/secrets.py — new module

from __future__ import annotations

import keyring  # PyPI: keyring (cross-platform: Keychain / Secret Service / Credential Manager)
import re

KEYRING_SERVICE = "eawf"   # one service namespace per integration

# B-c11-4 [34]: account string restriction. `PlaintextKeyring` uses configparser
# which treats `:` as delimiter; macOS Keychain + Windows Credential Manager + Linux
# SecretService all tolerate `[a-z0-9_-]+`. Lowest-common-denominator restriction.
_ACCOUNT_RE = re.compile(r"^[a-z0-9_-]+$")


def keyring_account(integration_id: str, field: str) -> str:
    """Canonical account name for an integration secret.

    Format: `{integration_id}-{field}`. Restricted to `[a-z0-9_-]+` per B-c11-4
    backend-portability requirement. Examples:
        - github-webhook → eawf / github-webhook
        - slack-bot → eawf / slack-bot
        - linear-access → eawf / linear-access
        - linear-refresh → eawf / linear-refresh

    Raises:
        ValueError: integration_id or field contains characters outside
            `[a-z0-9_-]+`. Restriction is lowest-common-denominator across all
            supported backends (B-c11-4 [34]).
    """
    account = f"{integration_id}-{field}"
    if not _ACCOUNT_RE.fullmatch(account):
        raise ValueError(f"keyring account contains illegal chars: {account!r}")
    return account


def set_secret(integration_id: str, field: str, value: str) -> None:
    """Store a secret. Never log, never print.

    Raises:
        keyring.errors.PasswordSetError: backend rejected the write.
    """
    keyring.set_password(KEYRING_SERVICE, keyring_account(integration_id, field), value)


def get_secret(integration_id: str, field: str) -> str | None:
    return keyring.get_password(KEYRING_SERVICE, keyring_account(integration_id, field))


def delete_secret(integration_id: str, field: str) -> None:
    """Idempotent delete. No error if the entry is already gone.

    Raises:
        keyring.errors.PasswordDeleteError: backend errored on a present entry.
    """
    try:
        keyring.delete_password(KEYRING_SERVICE, keyring_account(integration_id, field))
    except keyring.errors.PasswordDeleteError:
        pass
```

**Backend matrix (per `keyring` 25.x — B-c11-4 verified [34]; current stable 25.7.0; original 24.x reference bumped).**

| OS | Backend | Notes |
|---|---|---|
| macOS | Keychain Services | per-user; native; same-process access never prompts. **CI gotcha (B-c11-4 [34]):** `macos-latest` runners boot with keychain **locked** — first call raises `keyring.errors.KeyringLocked` (distinct from `NoKeyringError`); CI must either unlock via `security unlock-keychain` or fall back to plaintext. |
| Linux | Secret Service (gnome-keyring / KWallet) | requires DBus session. **B-c11-4 [34]:** when `DBUS_SESSION_BUS_ADDRESS` absent or `org.freedesktop.secrets` not registered, `keyring` raises `keyring.errors.NoKeyringError` **deterministically** (post Launchpad bug #1864204 fix). Hang risk is bounded to the DBus socket timeout (~25 s) when socket present but daemon dead — `secretstorage.check_service_availability()` guards against infinite block. |
| Windows | Credential Manager | per-user (`CRED_PERSIST_ENTERPRISE`, HKCU); native; works in CI without setup. |
| Headless CI (Ubuntu, locked-macOS) | `keyrings.alt.file.PlaintextKeyring` (PyPI `keyrings.alt` 5.0.2 — B-c11-4 [34]) under `$XDG_DATA_HOME/python_keyring/keyring_pass.cfg`; doctor warns when active. |

**Backend selection (B-c11-4 [34]).** Load order is `PYTHON_KEYRING_BACKEND` env override → `keyringrc.cfg` → priority-max entry-point. `keyring.get_keyring()` is **safe to call as a probe** (does not touch the store). Doctor probe sequence:

```python
def detect_keyring_state() -> KeyringProbeResult:
    """Two-phase keyring probe (B-c11-4 [34]).

    Phase 1: module inspection via `keyring.get_keyring()` — never calls
    `get_password()` before the probe (avoids DBus socket-timeout hang on
    misconfigured Linux). Phase 2: synthetic roundtrip write/read/delete
    against a probe account.
    """
    backend = keyring.get_keyring()
    module = type(backend).__module__
    if "fail" in module:
        return KeyringProbeResult(state=KeyringState.unavailable, reason="no usable backend")
    if "keyrings.alt" in module:
        return KeyringProbeResult(state=KeyringState.plaintext_fallback_active, reason=f"backend={module}")
    # Phase 2: roundtrip
    try:
        probe = f"_probe-{os.getpid()}"
        keyring.set_password(KEYRING_SERVICE, probe, "x")
        if keyring.get_password(KEYRING_SERVICE, probe) != "x":
            return KeyringProbeResult(state=KeyringState.unavailable, reason="roundtrip mismatch")
        keyring.delete_password(KEYRING_SERVICE, probe)
    except keyring.errors.KeyringLocked as e:
        return KeyringProbeResult(state=KeyringState.unavailable, reason=f"locked: {e!s}")
    except keyring.errors.NoKeyringError as e:
        return KeyringProbeResult(state=KeyringState.unavailable, reason=f"no keyring: {e!s}")
    return KeyringProbeResult(state=KeyringState.native_ok, reason=f"backend={module}")
```

**CI matrix reality (B-c11-4 [34]).** `ubuntu-latest`: no DBus session by default — plaintext fallback required (or operator installs+starts gnome-keyring). `macos-latest`: keychain locked at boot — `security unlock-keychain` or plaintext. `windows-latest`: Credential Manager works without setup.

**Headless CI override.** Doctor flags any active plaintext keyring with `INTEGRATION_SECRET_BACKEND_INSECURE` warning. CI test integrations use ephemeral synthetic secrets; production CI must mount a real Secret Service equivalent or pre-unlock the macOS keychain.

## 6. Failure modes + named edge cases

### Failure modes

| ID | Class | Trigger | Detection | Recovery |
|---|---|---|---|---|
| F-W1 | gh subshell missing | `gh` not on PATH | doctor `gh auth status` fails with `127` | operator installs `gh`; integration stays `enabled` but `BLOCKED` until detected present |
| F-W2 | gh auth expired | gh returns `not logged in` | call emits `integration_github_call_failed` with `cause: auth` | operator runs `gh auth login`; supervisor probes on next call |
| F-W3 | GitHub secondary-rate-limit | 403 with `secondary-rate-limit` body | bridge classifies; emits `integration_github_call_rate_limited`; backoff per `Retry-After` | bridge retries up to `max_attempts`; ladder exhaust → BLOCKED |
| F-W4 | OOM kills daemon (one worker eats all RAM) | POSIX OOM-killer SIGKILLs eawfd | V1 supervisor not running (it's dead) | systemd / launchd restarts daemon; WAL replay [3:1038]; per-worker RLIMIT_AS caps mitigate (§5.5) |
| F-W5 | Webhook signature drift after secret rotation | vendor rotated; eawf still using old | `integration_<id>_webhook_signature_invalid` events stack up | operator `integration secret set <id> webhook <new>`; supervisor reloads; events stop |
| F-W6 | Webhook listener port collision | `:7681` already bound | listener startup fails; daemon emits `integration_webhook_listener_bind_failed` | operator changes `integrations.webhook.bind_port` via `eawf config set ...` |
| F-W7 | Idempotent replay window cliff | retry at second 61 mints new key | bridge logs `integration_<id>_call_idempotent_window_expired` | acceptable; window matches C02 §Q7 60-s [3:262]; documented |
| F-W8 | Webhook handler synchronous slow | handler blocks bus | `subscription_dropped` event from C02 §5.7 [3:444]; bus overflow | per-integration `WebhookHandler` MUST be async; lint rule enforces |
| F-W9 | gh prompt blocks subshell | `gh` interactive prompt on first call | bridge sets `GH_PROMPT_DISABLED=1` env var (**there is no `--no-prompt` flag** per B-c11-2 [32] — earlier draft was wrong); on detect, emit `integration_github_call_failed` with `cause: prompt_blocked` | doctor reminds operator to pre-authenticate |
| F-W10 | Keyring backend missing on Linux | no DBus session | `keyring.errors.NoKeyringError` on first `get_secret` (B-c11-4 [34] deterministic post-#1864204 fix) | doctor flags; operator either starts gnome-keyring-daemon or switches to file-keyring opt-in |
| F-W11 | GitHub webhook replay flood | vendor re-delivers same event; UUID-dedup ring overflow (>10 K UUIDs in TTL window) | `DeliveryIdDedup` LRU evicts oldest; replay no longer suppressed → handler runs twice; idempotency-key dedup at the call layer catches it (per D11.K) | bump ring cap via `integrations.<id>.delivery_dedup_cap`; default 10 K is sized for ~5 K events/day per source |
| F-W12 | macOS CI keychain locked | `macos-latest` runner boots with keychain locked; bridge first call raises `keyring.errors.KeyringLocked` (B-c11-4 [34], distinct from `NoKeyringError`) | doctor probe Phase 2 surfaces `KeyringState.unavailable` with `locked: ...` reason | CI unlocks via `security unlock-keychain -p "$KEY_PASSWORD" build.keychain` OR sets `PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring` for plaintext mode |
| F-W13 | DBus socket present but secrets daemon dead | Linux: `DBUS_SESSION_BUS_ADDRESS` set, socket file exists, but `org.freedesktop.secrets` not registered | first call blocks for DBus timeout (~25 s) then raises `RuntimeError` from `secretstorage` (B-c11-4 [34]); bounded — not infinite | doctor probe phase 1 (`keyring.get_keyring()`) is safe; phase 2 roundtrip emits `integration_keyring_dbus_timeout` after the ~25 s block; operator restarts gnome-keyring-daemon |

### Edge cases

- **EC-W1 — Daemonless one-shot calls.** `eawf integration github pr create` invoked without a daemon? Cold-spawn per V1 [1:32]; if daemon stays single-shot, the worker supervisor does not start; bridge call still goes through (just-the-bridge path); event still appends to `event.jsonl` via the canonical store appender; webhook ingress unavailable until daemon spawns and persists.
- **EC-W2 — Disabled-but-credential-present.** `integrations.<id>.enabled: false` but a secret exists in keyring. Doctor flags as `STALE_SECRET`; operator either re-enables or `integration secret rm <id> <field>`.
- **EC-W3 — Profile composition conflict.** Two profiles both contribute integration `github` with different `manifest.yaml` shapes. V3 loader runs declared-conflict check; integration loader extends with same primitive; first-declared wins per `profile_priority` [1:79].
- **EC-W4 — Webhook arrives during daemon shutdown.** Listener returns `503` immediately; vendor redelivers per its own backoff. Drain only the supervisor-owned outbound queue, not the inbound socket — the socket closes when the listener exits.
- **EC-W5 — gh subshell stdout overrun.** `gh pr diff <big-pr>` returns 50 MiB. Bridge streams to a tempfile under `<local-path>`; event payload carries the tempfile path + size + sha256; caller (TUI / skill) reads from path. Bridge truncates `stdout_preview` to 2000 chars in the event.
- **EC-W6 — Multiple worktrees, single daemon.** Daemon serves all worktrees of the same repo through the same integration workers — `scope_id` URN distinguishes which wave the call belongs to. AGENTS rule 11 (worktree discipline) untouched: each worktree subagent emits its own bridge call; supervisor multiplexes.
- **EC-W7 — Recovery shell (V1 carve-out 3).** Recovery-shell reads (`eawf state show`) bypass the daemon; the integration verb-noun is NOT exposed in recovery shell at all (writes only; daemon-required).
- **EC-W8 — Cross-platform path normalisation for tempfile.** §5.8.5 Notion + EC-W5 tempfile path use `pathlib.Path` only — no string concatenation. Windows-safe per `feedback_python314_except` memory and C07b §Q5 [14:728-748].
- **EC-W9 — Brand glyph in event payloads.** Integration events MUST stay ASCII per C07b §Q11 [14:785]. Outbound notifications (Slack §5.8.3) MAY render with brand glyphs via the renderer chassis [14:497].

## 7. Migration plan

### 7.1 Net-new modules

```
src/eawf/integrations/__init__.py                    # entry-point loader
src/eawf/integrations/base.py                        # IntegrationWorker ABC + manifest model
src/eawf/integrations/secrets.py                     # keyring writer (single mutator)
src/eawf/integrations/registry.py                    # in-memory manifest registry; populated at daemon startup
src/eawf/integrations/github/__init__.py             # entry-point register()
src/eawf/integrations/github/manifest.yaml           # §5.2
src/eawf/integrations/github/bridge.py               # §5.3 outbound calls
src/eawf/integrations/github/retry_policy.py         # B-c11-7 [36] WriteRetryPolicy + per-verb instances
src/eawf/integrations/github/worker.py               # IntegrationWorker subclass; event-driven
src/eawf/integrations/github/webhook.py              # incoming webhook handler
src/eawf/daemon/webhook_listener.py                  # §5.4 HTTP ingress
src/eawf/daemon/integration_supervisor.py            # in-daemon asyncio supervisor (V1)
src/eawf/cli/commands/integration.py                 # noun-app; §5.6
src/eawf/cli/commands/integration_github.py          # github subverbs
src/eawf/cli/commands/integration_webhook.py         # webhook subverbs
tests/integrations/test_secrets.py
tests/integrations/test_github_bridge.py
tests/integrations/test_github_retry_policy.py        # B-c11-7 [36] per-verb probe + redirect coverage
tests/integrations/test_webhook_listener.py
tests/integrations/test_github_webhook_handler.py
tests/integrations/test_integration_supervisor.py
tests/data/integrations/github/webhooks/pull_request.json
tests/data/integrations/github/webhooks/check_run.json
tests/data/integrations/github/webhooks/issue_comment.json
tests/data/integrations/github/webhooks/workflow_run.json
```

### 7.2 Existing surfaces that change

| File | Change | Reason |
|---|---|---|
| `src/eawf/render/skills.py:321,333,349` | Replace `gh pr create / merge / diff` shell-out prose in skill bodies with `eawf integration github pr create / merge / diff` | Skill bodies become dispatch dispatch; library implements (AGENTS rule 1) |
| `build/eawf-plugin/skills/ship/SKILL.md` + `build/eawf-codex-marketplace/.../ship.md` | Plugin-render outputs of the above; regenerated by `eawf plugin sync` | Source-of-truth change cascades |
| `build/eawf-plugin/skills/review/SKILL.md` + codex equivalents | Same | Same |
| `src/eawf/state/models.py` | Add `Wave.linkages: WaveLinkages \| None` (with `pr_url`, `pr_number`, `linear_issue_id`, `jira_issue_key` fields, all `Optional[str]`); add `WaveLinkages` Pydantic model with `extra="forbid"` | Webhook projector populates `pr_url` on `integration_github_pr_opened` |
| `src/eawf/state/enums.py` | No change (StoreKind stays closed; event_type sub-types are strings, not enum) | Per [14:453]; closed-enum bump deferred |
| `src/eawf/validate/invariants.py` | New invariant `INV.INTEGRATION.MANIFEST_REGISTERED`: every `state.waves[*].linkages.linear_issue_id` non-null requires the `linear` integration enabled in `state.integrations` | Same shape as the existing `INV.REF.MCP_GRANT_SERVER_MISSING` |
| `src/eawf/cli/app.py` | Register the `integration` noun-app per C05 §5.11 static registration table [6:1005-1124] | Adds 1 row |
| `src/eawf/runtimes/claude/plugin_install.py` (and codex/opencode equivalents) | Doctor walk includes `src/eawf/integrations/<id>/manifest.yaml` body-hash | Parity with C07a §5.7 |
| `<local-path>` | Add `integrations: {<id>: {enabled, last_success_at, restart_count}}` per-repo snapshot | Reader-only via `eawf integration list`; supervisor writes via the registry writer (AGENTS rule 4 third mutator) |
| `pyproject.toml` | Add runtime deps: **`keyring>=25.0,<26.0`** (lower bound bumped from 24 to 25 per B-c11-1 [31] — current stable 25.7.0; original 24 floor was unnecessarily conservative), **`aiohttp>=3.10,<4.0`** (pins confirmed safe; 4.x has not shipped per B-c11-1 [31]); add conditional CI extra: `keyrings.alt>=5.0,<6.0` under `[project.optional-dependencies] ci-headless` for plaintext fallback. B-c11-1 verified both deps net-new (not transitively pulled in by any existing 63-package resolution); both pin floors valid; both upper bounds safe. | One new HTTP server dep + one new keyring dep; pre-add verification per AGENTS Python anti-pattern — done via B-c11-1 |

### 7.3 Per-phase rollout

| Phase | Wave | Scope | Deps |
|---|---|---|---|
| P30 (v0.3 + 1) W01 | Manifest schema + entry-point loader + base ABC | none | C00, C07a §5.7 stable |
| P30 W02 | Keyring writer + secrets CLI subverbs + doctor probe | W01 | — |
| P30 W03 | GitHub bridge (outbound only) + bridge unit tests + replace one skill body `gh pr create` shell-out | W02 | — |
| P30 W04 | Replace remaining `gh pr` skill-body shell-outs (merge, diff, view) + regen plugin trees | W03 | — |
| P31 W01 | Webhook listener scaffolding (no signing verification yet) + bind config | P30 W01 | — |
| P31 W02 | HMAC signing + timestamp window + listener integration tests | P31 W01 | — |
| P31 W03 | GitHub webhook handler + state projector (`pr_url` populated) + webhook test verb | P31 W02 | — |
| P31 W04 | Integration supervisor + IntegrationWorker for github + restart/block lifecycle + doctor `--integration <id>` | P31 W03 | — |
| P31 W05 | Per-integration metrics emit + V7 DuckDB projector entries + TUI banner for BLOCKED workers | P31 W04 + V7 stable | C09 metrics surface |
| P32 W01..W04 | (deferred to v0.6 unless v0.5 ratifies a Linear/Jira/Slack pick — see §8) | — | — |

### 7.4 Backward-compat constraints

- Existing skill bodies invoking `gh pr ...` MUST keep working through v0.4 — the bridge is additive, not destructive. Per `feedback_pr_merge_strategy` memory the merge strategy stays `gh pr merge --rebase`; the bridge enforces it (call-site refuses `--squash`).
- `state.json` schema additions are non-breaking — every new field is `Optional[...]` and absent in pre-P30 state files.
- Pre-existing per-runtime plugin trees regenerate cleanly via `eawf plugin sync` after P30 W04.

### 7.5 Rollback plan

Per-phase rollback is `git revert` of the phase PR. The webhook listener gated behind a daemon feature flag `daemon.integrations.webhook_listener.enabled: false` (default `true` once P31 W02 lands; flip to `false` for soak rollback). The supervisor gated similarly. The bridge has no rollback flag (it replaces skill-body prose; rollback = revert).

## 8. Open questions for operator

These remained after the 2026-05-17 AUQ; pre-drafted as future AUQ seeds. **Blitz round B-c11-1..4 (2026-05-17) resolved the four verifiable axes** — see resolved tags inline and §10 amendment log. The 10 questions below stay open because they are taste / policy / forward-trigger calls that no vendor probe can lock.

### Resolved via blitz (2026-05-17)

- **B-c11-1 [31]** — `aiohttp` + `keyring` dep gates. **Resolved.** Both deps net-new (not transitively pulled); `aiohttp>=3.10,<4.0` pin stays (4.x unshipped); `keyring` lower bound bumped to `25.0` (current stable 25.7.0); `aiohttp` retained over stdlib (`asyncio.start_server` would need 200+ LOC of HTTP/1.1 framing). Folded into §7.2.
- **B-c11-2 [32]** — `gh` CLI behavioural verification. **Resolved.** `gh pr merge` rejects multi-strategy flags client-side at line 106-120 of merge.go — bridge enforces by validating args + always appending `--rebase`. `GH_PROMPT_DISABLED=1` correct; **no such `--no-prompt` flag exists** (F-W9 corrected in §10). `gh auth status` exit codes locked (`0` ok, `1` fail, `127` missing); doctor uses **plain** `gh auth status` (not `--json` — that variant always exits 0). `gh api rate_limit` JSON: read `resources.core.*`, not legacy top-level `rate.*`. Floor: **`gh >= 2.40.0`**. `--body-file -` for stdin; never `--fill` (non-deterministic). Folded into §5.3.
- **B-c11-3 [33]** — webhook signing per vendor. **Resolved.** GitHub: `X-Hub-Signature-256`, raw-body, `sha256={hex}` prefix, **NO timestamp header** → replay defence via UUID dedup (§5.4 amended). Slack: `v0:{ts}:{body}` composite signed string (NOT just prefix — original §5.8.3 `signature_prefix_template` field renamed). Linear: **bare hex** (no prefix), JSON-body timestamp at `$.webhookTimestamp`. Jira: `X-Hub-Signature` SHA-256 with `method=signature` envelope, `X-Atlassian-Webhook-Identifier` dedup. Notion: no public HMAC spec, internal-beta confirmed. Manifest stub at §5.2 corrected (`timestamp_header: null` for GitHub). Folded into §5.2, §5.4, §5.8.
- **B-c11-4 [34]** — keyring backend matrix. **Resolved.** Linux DBus: `NoKeyringError` raised deterministically (post #1864204 fix); hang bounded to ~25 s when socket present but daemon dead. `keyrings.alt` 5.0.2 confirmed maintained; `PlaintextKeyring` at canonical path. `PYTHON_KEYRING_BACKEND` env override confirmed; `keyring.get_keyring()` safe as probe. Account string restriction: `[a-z0-9_-]+` (colons break `PlaintextKeyring`'s configparser). macOS CI gotcha: keychain locked at boot → `KeyringLocked` distinct from `NoKeyringError`. Two-phase `detect_keyring_state()` probe added in §5.9. CI matrix table added. Folded into §5.9 + §6 F-W11..F-W13.
- **B-c11-5 [35]** — asyncio per-task memory cap + aiohttp AppRunner. **Resolved.** Original §5.5 spec bug: `RLIMIT_AS` is **process-level** per `man 2 setrlimit`, not per-asyncio-task. No native asyncio per-task cap; cgroups v2 + subprocess isolation would reverse D11.B. Verdict: drop per-task claim; use daemon-wide ceiling + per-worker `MemoryError` restart + V1 idle-respawn as three-layer defence; document single-OOM-kills-all as accepted residual risk. aiohttp `AppRunner` + `TCPSite` is the documented pattern for integrating with an existing event loop (NEVER `run_app` — that owns the loop). `client_max_size` defaults to 1 MiB and MUST be wired explicitly to `web.Application(client_max_size=cfg.max_body_bytes)` or operator config is silently ignored. Folded into §5.4 + §5.5.
- **B-c11-7 [36]** — gh write-verb idempotency. **Resolved.** `gh pr create` + `gh release create` are NOT server-idempotent (duplicate calls create duplicate objects); `gh pr merge` is safe-on-replay (`state==MERGED` reclassifies as success). C11 idempotency_key only suppresses redundant daemon dispatches within 60 s — does not prevent server-side duplication across daemon restarts. Bridge MUST run a `WriteRetryPolicy` probe BEFORE every non-idempotent write verb (`gh pr list --head <branch>` for pr.create; `gh pr view --json state,mergedAt` for pr.merge; `gh release view <tag>` for release.create), then redirect (`gh pr edit`) / reclassify (success) / skip and emit `integration_github_call_already_done`. TOCTOU window accepted for v0.3 (single-user daemon serializes). `issue.create` deliberately excluded from v0.3 bridge — no GitHub-side dedup primitive exists. Folded into §5.3 + §5.7 + §7.1.

### Open (post-blitz)

### Q-c11-1 — GitHub App migration trigger

Operator picked `gh CLI subshell` (D11.A.a). At what point does eawf revisit?

- (a) v0.6+ regardless — schedule a one-axis re-AUQ then.
- (b) **(Recommended)** Trigger-driven: revisit when (i) a multi-org operator surfaces, (ii) gh CLI auth surface materially regresses, or (iii) GitHub deprecates classic PAT entirely. No calendar commitment.
- (c) Never — gh is the long-term answer.

**Rationale for (b):** the gh subshell answer is correct *until* multi-org auditing or PAT deprecation forces the change. Calendar-driven re-AUQs without a trigger waste cycles.

### Q-c11-2 — Webhook ingress public-internet exposure

D11.I locked `<host>` default. The realistic public path is reverse-proxy (nginx / caddy / cloudflared) terminating TLS + forwarding to the local daemon. Should eawf:

- (a) **(Recommended)** Document the recipe in `docs/operations/webhook-tunnel.md` (Caddy + Cloudflare Tunnel example) and leave bind/TLS to the operator.
- (b) Ship a built-in `eawf integration webhook tunnel` verb that wraps `cloudflared` or `ngrok`.
- (c) Add native TLS support (cert paths in `WebhookListenerConfig` are already there per §5.4) and document Let's Encrypt setup.

**Rationale for (a):** reverse-proxy + tunnel is well-trodden territory; eawf owning the tunnel is a maintenance treadmill it should not enter at v0.4.

### Q-c11-3 — Linear / Jira pick at v0.6 planning

Operator deferred (D11.D = d). At v0.6 planning, which lands first?

- (a) Linear first (cleaner API surface; smaller adapter).
- (b) Jira first (larger eawf-adoption surface; more users on Jira).
- (c) Both in same phase (~+8 EU).
- (d) **(Recommended)** Pick driven by first-user-demand signal — whichever the first non-author adopter uses.

### Q-c11-4 — Slack at v0.6+ event filter (D11.E re-ask)

Deferred per the AUQ. At v0.6 reopen, re-ask D11.E. Options remain (a) per-event-kind allowlist, (b) severity-only, (c) DSL filter, (d) plugin callable. Recommendation pre-staged: **(a) per-event-kind allowlist** with template defaults per profile (research-profile: `[audit_failed, incident]`; engineering-profile: `[phase_close, pr_opened, runtime_switched]`).

### Q-c11-5 — Notion at v0.6+ trigger (D11.F re-ask)

Deferred. Re-ask at v0.6. Recommendation pre-staged: **(a) manual CLI verb only** — promotion auto-publish is irrecoverable on rate-limit failures and conflates two state machines.

### Q-c11-6 — Calendar at v0.5+ scope (D11.G re-ask)

Deferred. Re-ask at v0.5+. Pre-stage: **(a) defer further** unless cross-machine scheduling demand surfaces — `/loop` keeps covering the in-tree case for the foreseeable future.

### Q-c11-7 — Integration manifest version-bump cadence

D11.H locks manifest schema v1. When does v2 land?

- (a) Per-minor eawf bump (matches the runtime capability matrix cadence C07a §4 [12:578]).
- (b) **(Recommended)** Trigger-driven — bump when a manifest field needs a breaking change (new required field; semantic shift). Schema additions (new optional fields) stay schema v1.
- (c) Never bump — extend in place.

### Q-c11-8 — Webhook secret rotation surface

§5.6 has `integration secret set/rm`; rotation is operator-manual. At v0.5+:

- (a) **(Recommended)** Stay manual. Vendor UI is the source of truth; eawf mirrors.
- (b) Add `eawf integration secret rotate <id> <field>` that generates a fresh secret, prints it for vendor-side update, then atomically swaps the keyring entry after operator confirmation.
- (c) Auto-rotate on a schedule.

### Q-c11-9 — `integration list` output format default

C05 §5.2 hoists output flags. `integration list` default:

- (a) **(Recommended)** Text table — readable in the terminal.
- (b) JSON by default — better for skills + scripting.
- (c) `--json` default when stdout is non-tty (auto-detect).

Recommendation (a) matches the rest of `eawf <noun> list` defaults; `--json` available for skills.

### Q-c11-10 — Catalog status `none` rows

§5.1 enumerates Discord, email-SMTP, Prometheus as `none`. Two views:

- (a) Keep them catalogued so the v0.6+ planner has a starting list.
- (b) **(Recommended)** Drop them from the catalog — `none` means "no plan", which is just absence. Re-introduce when demand surfaces.
- (c) Move them to a separate `wishlist.md` outside the cluster brief.

Recommendation (b): the catalog should describe commitments. Wishlist items live elsewhere.

### Q-c11-11 — UUID dedup ring size (B-c11-3 follow-up)

§5.4 + §6 F-W11 size `DeliveryIdDedup` at 10 K UUIDs per source. Sized for ~5 K events/day with a TTL slop margin. Trigger to revisit:

- (a) **(Recommended)** Leave at 10 K through v0.5; revisit when any source emits `integration_<id>_webhook_replay_suppressed` *or* `integration_<id>_webhook_replay_evicted` (new event) at >1 K/day rate.
- (b) Bump unconditionally to 100 K — overprovision now.
- (c) Per-integration tunable in manifest with a sensible per-vendor default.

### Q-c11-12 — CI keyring strategy (B-c11-4 follow-up)

§5.9 documents the macOS `security unlock-keychain` recipe vs `PYTHON_KEYRING_BACKEND=keyrings.alt.file.PlaintextKeyring`. Which does eawf's own CI use?

- (a) **(Recommended)** Plaintext fallback on all CI runners — keyring code paths still exercised end-to-end; no per-runner secret-injection ceremony.
- (b) Native keychain on macOS + Credential Manager on Windows + plaintext on Ubuntu — closer to production but adds CI complexity.
- (c) Mock the keyring layer entirely in CI — fastest but loses contract testing.

## 9. References

[1] `.ea/local/research/long-term/2026-05-16-c00-spec-index.md` — C00 spec index (V1..V9; cluster catalog; cross-cluster invariants)

[2] `.ea/local/research/long-term/2026-05-16-c01-foundations.md` — vocabulary + URN + entities + lifecycle

[3] `.ea/local/research/long-term/2026-05-16-c02-daemon-topology.md` — daemon + topology + security spine (event.subscribe at §5.3.2 [3:304-310]; subscription bus + backpressure at §5.7 [3:433-456]; idempotency window 60 s at Q7 [3:262]; service registration at §5.10 [3:493-650])

[4] `.ea/local/research/long-term/2026-05-16-c03-spec-infrastructure.md` — spec infrastructure

[5] `.ea/local/research/long-term/2026-05-16-c04-workflow-skills.md` — workflow skills (loop / schedule / review / ship)

[6] `.ea/local/research/long-term/2026-05-16-c05-cli-surface.md` — CLI surface (verb-noun matrix at §5.1; ship surfaces at §5.1.11 [6:347-360]; daemon verbs at §5.1.13 [6:371-385]; static registration at §5.11 [6:1005-1124])

[7] `.ea/local/research/2026-05-11-tui-ux-resolved.md` — TUI UX resolved decisions (events overlay [7:304-326]; detail backdrop [7:586-606])

[8] `.ea/local/research/long-term/2026-05-16-c04-workflow-skills.md` — `/loop` skill body

[9] `.ea/local/research/2026-05-11-mcp-via-eawf.md` — MCP integration pattern (ownership marker `__eawf_owner` per integration entry; §1.1 [9:14-22]; manifest schema parity §4.3 [9:209-232])

[10] `src/eawf/runtimes/claude/plugin_install.py` — current plugin-install entry; pattern reused for integration manifest doctor

[11] `src/eawf/render/skills.py:321,333,349` — current `gh pr create / merge / diff` references in skill bodies (verified path:line per AGENTS verify-before-claim)

[12] `.ea/local/research/long-term/2026-05-16-c07a-runtime-skill-dispatch.md` — runtime / skill / agent dispatch (plugin manifest at §5.7 [12:310-370]; capability matrix at Q6 [12:578])

[13] `.ea/local/research/long-term/2026-05-17-c06-operator-surface.md` — operator surface TUI + web stub (modal stack inventory at §5.7 [13:740-757]; daemon-push protocol binding at §5.8 [13:775-827]; V5 runtime-switched banner at §5.9 [13:829-883])

[14] `.ea/local/research/long-term/2026-05-16-c07b-vcs-worktree-events.md` — VCS / worktree / events / render / brand (event / audit log at §5.4 [14:353-471]; EventPayload at [14:399-412]; closed StoreKind at [14:417-451]; renderer chassis at [14:497-510]; brand glyph ASCII-in-commits at Q11 [14:785]; registry path Windows at Q5 [14:728-748])

[15] `.ea/local/research/long-term/2026-05-16-c08-configurability-profiles.md` — config + profile composition (`integrations:` profile field; loader)

[16] `.ea/local/research/long-term/2026-05-15-long-term-roadmap-synthesis.md:132,221,286` — 429 vendor-pause pattern extended by V5; reused for integration outbound retry

[17] `AGENTS.md` — non-negotiable rules (rule 1 CLI is dispatch; rule 2 strict config; rule 4 single-canonical-mutator extended here; rule 16 secrets/PII hygiene; rule 17 naming conventions)

[18] `src/eawf/state/models.py` — `Wave` state model home for `WaveLinkages` extension

[19] `src/eawf/store/event.py` — current envelope writer (canonical append; pattern reused)

[20] `src/eawf/store/append.py` — canonical `append_envelope` writer

[21] `src/eawf/render/skills.py` — full file context for §11 references

[22] `https://github.com/jaraco/keyring` — `keyring` package (cross-platform secret storage)

[23] `https://docs.aiohttp.org/` — `aiohttp` HTTP server (webhook listener stack)

[24] `https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries` — GitHub HMAC webhook signing

[25] `https://api.slack.com/authentication/verifying-requests-from-slack` — Slack `v0:` prefix HMAC signing (deferred integration; spec reference)

[26] `https://developer.atlassian.com/cloud/jira/platform/webhooks/` — Jira webhook signing (deferred; spec reference)

[27] `https://developers.linear.app/docs/graphql/webhooks` — Linear webhook signing (deferred; spec reference)

[28] `https://developers.google.com/calendar/api/guides/push` — Google Calendar push channels (deferred; domain-verification dependency)

[29] `https://developers.notion.com/reference/intro` — Notion API (deferred; spec reference)

[30] `https://cli.github.com/manual/gh_pr_create` — `gh pr create` reference (bridge wraps this verb)

[31] `.ea/local/research/long-term/2026-05-17-c11-blitz-deps.md` — B-c11-1 dep-gate blitz: `aiohttp` + `keyring` transitive availability + version-pin verification

[32] `.ea/local/research/long-term/2026-05-17-c11-blitz-gh-cli.md` — B-c11-2 gh CLI behavioural blitz: merge-strategy enforcement, `GH_PROMPT_DISABLED`, `gh auth status` exit codes, `gh api rate_limit` shape, body-via-stdin, version floor

[33] `.ea/local/research/long-term/2026-05-17-c11-blitz-webhook-signing.md` — B-c11-3 webhook signing blitz: GitHub / Slack / Linear / Jira / Notion HMAC schemes; per-vendor `WebhookSigningSpec` shapes; GitHub timestamp-absence correction

[34] `.ea/local/research/long-term/2026-05-17-c11-blitz-keyring-backends.md` — B-c11-4 keyring backend matrix blitz: Linux DBus deterministic raise, `keyrings.alt` status, account string restriction, macOS CI locked-keychain, two-phase probe sketch, CI matrix table

[35] `.ea/local/research/long-term/2026-05-17-c11-blitz-asyncio-cap.md` — B-c11-5 asyncio per-task memory-cap + aiohttp AppRunner blitz: `RLIMIT_AS` process-level scope confirmation; no per-asyncio-task cap primitive; AppRunner+TCPSite pattern; explicit `client_max_size` wiring requirement

[36] `.ea/local/research/long-term/2026-05-17-c11-blitz-gh-idempotency.md` — B-c11-7 gh write-verb idempotency blitz: `pr.create` + `release.create` NOT server-idempotent; `pr.merge` partially safe; `WriteRetryPolicy` Pydantic shape + per-verb instances; `issue.create` deliberately excluded

## 10. Amendment log (B-c11-1..4 fold-back, 2026-05-17)

Verbatim diff against the original 2026-05-17 brief (status `accepted`), now `accepted + amended`:

| Section | Change | Source |
|---|---|---|
| §5.2 | `signature_format` field added; `signed_string_template` field added (was implicit); `timestamp_header: X-GitHub-Delivery` → `null` + `delivery_id_header: X-GitHub-Delivery` (X-GitHub-Delivery is a UUID, not a timestamp) | B-c11-3 [33] U1 |
| §5.3 | Doctor probe: plain `gh auth status` (not `--json`); exit-code matrix + `gh --version >= 2.40.0` floor; `resources.core.*` not `rate.*`; bridge enforces no-squash client-side; `--no-prompt` flag does not exist; `--body-file -` for stdin; never `--fill` | B-c11-2 [32] |
| §5.4 | `verify_hmac()` parameterised per-vendor (`signed_string_template`, `signature_format`); new `DeliveryIdDedup` LRU primitive for vendors without timestamp; lifecycle step 5a inserted | B-c11-3 [33] |
| §5.8.1 | Linear: bare hex (no `sha256=` prefix); JSON-body `webhookTimestamp` field; 60 s replay window | B-c11-3 [33] U3 |
| §5.8.2 | Jira: `X-Hub-Signature` SHA-256 with `method=signature` envelope; no timestamp header; `X-Atlassian-Webhook-Identifier` dedup | B-c11-3 [33] U4 |
| §5.8.3 | Slack: full composite signed string `v0:{ts}:{body}` (was wrongly described as just a prefix); manifest field renamed `signature_prefix_template` → `signed_string_template` | B-c11-3 [33] U2 |
| §5.8.5 | Notion: confirmed no public webhook surface (internal-beta as of 2026-05-17) | B-c11-3 [33] U5 |
| §5.9 | `keyring` version range bumped `>=24.0` → `>=25.0` (current stable 25.7.0); account string `[a-z0-9_-]+` restriction added; `KeyringLocked` distinguished from `NoKeyringError`; per-OS CI matrix table; `detect_keyring_state()` two-phase probe sketch | B-c11-4 [34] |
| §6 | F-W10 amended (deterministic raise post-#1864204); F-W11 added (UUID dedup ring overflow); F-W12 added (macOS CI locked keychain); F-W13 added (DBus socket present but daemon dead, bounded ~25 s) | B-c11-4 [34], B-c11-3 [33] |
| §7.2 | `keyring>=25.0,<26.0` (lower bound bumped); `keyrings.alt>=5.0,<6.0` added as `ci-headless` extra; both deps confirmed net-new (not transitively pulled in by 63-package resolution) | B-c11-1 [31], B-c11-4 [34] |
| §8 | Added "Resolved via blitz (2026-05-17)" sub-section with four blitz tags; Q-c11-11 (UUID dedup ring size) + Q-c11-12 (CI keyring strategy) added as new open Qs | this fold-back |
| §9 | [31]..[34] added pointing at the four blitz briefs | this fold-back |

**Net-effect lint pass (round 1).** Original brief had **one factual bug** (GitHub `X-GitHub-Delivery` mis-typed as a timestamp source) and **one prose error** (`--no-prompt` flag claim). Both corrected.

### Round 2 fold-back (2026-05-17, B-c11-5 + B-c11-7)

| Section | Change | Source |
|---|---|---|
| §5.3 | New "Write-verb idempotency" sub-section + `WriteRetryPolicy` Pydantic shape + per-verb instances (`PR_CREATE_POLICY`, `PR_MERGE_POLICY`, `RELEASE_CREATE_POLICY`); bridge dispatch runs probe BEFORE every non-idempotent write subprocess | B-c11-7 [36] |
| §5.4 | `WebhookListenerConfig.max_body_bytes` comment notes explicit-wiring requirement; new `start_webhook_listener()` snippet showing `AppRunner` + `TCPSite` pattern + `client_max_size=cfg.max_body_bytes` explicit pass-through to `web.Application` | B-c11-5 [35] |
| §5.5 | "Per-task memory cap" paragraph replaced. Original RLIMIT_AS-as-per-task claim was wrong (process-level only per man 2 setrlimit). Replacement: "Daemon-wide memory ceiling (NOT per-task)" with three-layer defence (process RLIMIT_AS ceiling + per-worker MemoryError restart + V1 OOM-respawn); single-OOM-kills-all explicitly accepted as residual risk; v0.6+ subprocess escalation noted but not committed | B-c11-5 [35] |
| §5.7 | New event sub-type: `integration_<id>_call_already_done` | B-c11-7 [36] |
| §7.1 | New module: `src/eawf/integrations/github/retry_policy.py` + test file `tests/integrations/test_github_retry_policy.py` | B-c11-7 [36] |
| §8 | "Resolved via blitz" sub-section extended with B-c11-5 + B-c11-7 entries | this fold-back |
| §9 | [35]..[36] added | this fold-back |

**Net-effect lint pass (round 2).** Found **one factual bug** (RLIMIT_AS-as-per-task-cap claim was structurally wrong — masks a real architectural trade-off the brief now states honestly). Found **one bridge safety gap** (`gh pr create` retry would create duplicate PRs without a probe-first recipe — addressed by `WriteRetryPolicy`). Both corrected before implementation. Implementation phase (P30/P31 waves) now inherits a spec that is structurally honest about per-task isolation limits and operationally safe under retry.

**Cumulative bug count across both rounds.** 3 factual bugs caught + fixed (GitHub timestamp source, `--no-prompt` flag, RLIMIT_AS scope). 1 safety gap caught + fixed (gh write-verb retry duplication). 0 spec rewrites required; all corrections folded inline.

### Round 3 ratification (2026-05-17, R-c11-1 + R-c11-4)

No new blitzes — operator chose ratify-only mode. Locked outcomes:

- **R-c11-1 LOCKED.** D11.A..K (eleven decision axes from §4) ratified through **v0.5 ship**. Re-AUQ only after v0.5 ratification or on documented trigger (per Q-c11-1 trigger-driven re-ask policy). No mid-cycle flip-flops. Affects axes:
  - D11.A (GitHub auth = gh CLI subshell)
  - D11.B (in-daemon asyncio worker model)
  - D11.C (webhook ingress auth = HMAC only)
  - D11.D (Linear/Jira deferred v0.6+)
  - D11.E (Slack deferred v0.6+)
  - D11.F (Notion deferred v0.6+)
  - D11.G (Calendar deferred v0.5+)
  - D11.H (integration manifest = profile YAML + entry-point)
  - D11.I (webhook listener `<host>` default + operator-tunable bind)
  - D11.J (per-integration retry policy)
  - D11.K (bridge-generated idempotency keys + 60-s daemon dedup window)
- **R-c11-4 LOCKED.** Brief status flips from `accepted + amended` → **`accepted-final`**. Spec phase deliverable closed. Open Q-c11-1..12 retained as future-AUQ seeds (do not block ship; re-asked per their declared triggers).

**Implementation phase gate.** C11 brief is now the load-bearing spec for the GitHub bridge + webhook ingress + integration manifest waves under P30 + P31. Wave dispatch renderer (per spike workflow + AGENTS §`spike-workflow`) surfaces this brief under `## References` for matching wave/iter/phase IDs.

**Re-open trigger.** C11 re-opens only on (a) v0.5 ratification cycle, (b) operator-flagged trigger from Q-c11-1..12, or (c) downstream cluster (none — C11 is terminal) requesting a spec change. Status `accepted-final` is non-mutable except via explicit `eawf roadmap revise` per AGENTS rule 20 planned-scope-revisability.

**Cumulative effort.** 1 main brief (1141 lines after fold-back) + 6 blitz briefs (1751 lines) = **2892 lines** of C11 cluster output across 2 rounds of blitz + 1 round of ratification (2026-05-17 single-day session).

## Provenance

- `store_record=none (local-only research)`
- `commit=3b86f7a (parent feature/eawf-v0.3-p20; revisions 2026-05-18)`
- `supersedes=none`
- `session=eawf-spec-c11-2026-05-17`
- `last_revised=2026-05-18 (audit-driven: HMAC over raw bytes per XB22 / H-01; webhook ingress = local polling for v0.3-v0.5 per XB23 / H-02 / Q15; show-secret removed per XB24 / H-03 — replaced by generate-secret + set-secret + verify-secret; memory model = daemon-wide concurrency cap per Codex C11-I001; no rotation policy v0.3-v0.5 per Q21; event payload consumes C07b canonical Event model per H-12 / Q14; log keys scope→scope_id per Codex C11-I010 / BOT-06)`
- `audit_consumed=2026-05-17-spec-series-combined-audit.md (4 BLOCKERs XB22/XB23/XB24 + Codex C11-I011; 12 Codex issues)`
- `authority_binding=Q1 (2026-05-18): daemon = sole writer for integration state. github bridge (local polling) routes through daemon's asyncio task graph; webhook listener (gated to v0.6+) likewise.`
- `operator_axes_locked=D11.A..D11.H (2026-05-17, two AskUserQuestion rounds — GH-auth / worker-model / webhook-auth / Linear-Jira / Slack / Notion / Calendar / manifest)`
- `verified_path_line_claims=src/eawf/render/skills.py:321,333,349 (gh pr references) + cited [3:...] [12:...] [13:...] [14:...] line refs in dependency cluster briefs`
- `blitz_round_1=B-c11-1..4 (2026-05-17, four parallel sonnet subagents — deps / gh-cli / webhook-signing / keyring-backends; verdicts folded back into §5.2 §5.3 §5.4 §5.8 §5.9 §6 §7.2 §8 §9; full diff in §10 amendment log)`
- `blitz_round_2=B-c11-5, B-c11-7 (2026-05-17, two parallel sonnet subagents — asyncio per-task cap + aiohttp AppRunner; gh write-verb idempotency; verdicts folded back into §5.3 §5.4 §5.5 §5.7 §7.1 §8 §9; full diff in §10 round-2 amendment table)`
- `blitz_corrections_cumulative=4 (3 factual bugs + 1 safety gap): GitHub X-GitHub-Delivery is UUID not timestamp (§5.2); gh-CLI --no-prompt flag does not exist (§6 F-W9); RLIMIT_AS is process-level not per-asyncio-task (§5.5 architectural rewrite); gh pr create + release create not server-idempotent — bridge needs WriteRetryPolicy probe (§5.3 amendment)`
- `ratification_round=R-c11-1 + R-c11-4 (2026-05-17, operator AUQ): D11.A..K locked through v0.5 ship; status flipped accepted+amended → accepted-final; open Q-c11-1..12 retained as future-AUQ seeds; spec phase deliverable closed`
- `re_open_trigger=v0.5 ratification cycle OR operator-flagged Q-c11-1..12 trigger OR downstream cluster request (none — C11 is terminal in DAG)`

## Scrub

- status: clean
- references: repo-relative or external URL only; vendor docs hyperlinked
- local paths: none (all `src/...`, `.ea/...`, `build/...`, `<local-path>` are repo-relative or canonical)
- real emails: none
- abstract placeholder names: not applicable (no mockup repos cited)
- secrets in payloads: none (HMAC secret examples carry only canonical account names, never values)
