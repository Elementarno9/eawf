# Machine envelope

Every verb of a contract group answers `--json` with one machine envelope. A contract group is an entity group (`track`, `milestone`, `batch`, `task`, `run`, `release`, `campaign`, `question`, `action`, `decision`) or a cross-cutting group (`workspace`, `config`, `daemon`, `memory`, `ui`, `migrate`, `reflect`). This page states the shape and what changed for callers in v0.7.0rc1.

## Shape

```json
{
  "schema_version": "1",
  "status": "ok",
  "operation": "config get",
  "revision_before": null,
  "revision_after": null,
  "result": {"key": "ui.theme", "value": "dark", "source": "repo"},
  "warnings": [],
  "errors": [],
  "links": {}
}
```

- `status` is `ok` for an answer that stands and `error` for a refusal the daemon returned; `errors` is empty exactly when the status is `ok`.
- `operation` is the daemon route for the native lifecycle, delivery, question and release verbs, and the command path after `eawf` for every other contract verb.
- `revision_before` and `revision_after` carry the subject's revisions for a verb that takes `--expected-revision`, and `null` otherwise.
- `result` is the verb's answer: for a verb that used to print a bare JSON object, that object, unchanged.

A usage or input error raised before any answer still prints the error envelope described in [Exit codes](exit-codes.md), with the same exit codes.

## What changed in v0.7.0rc1

- **`--json` output of the cross-cutting groups and of the entity-group reads is now wrapped.** `config`, `memory`, `workspace`, `daemon`, `migrate`, `reflect` and `campaign` verbs, `release` reads such as `release show` and `release train show`, and every other contract verb that printed a bare object now print the envelope; read the old object from `result`. The native lifecycle, delivery, question and release mutation verbs already answered with the envelope and are unchanged.
- **Verbs outside the contract groups keep their shapes.** `status`, `doctor`, `hook`, `plugin`, `skill`, `coauthor`, `repo`, `init` and the other tooling commands print what they printed before.
- **`eawf question answer` takes `--expected-revision`** in place of `--revision`.
- **`eawf campaign cancel` requires `--expected-revision`** and sends it, rather than the revision it read back, so a Campaign that moved since the caller read it is refused.
- **`eawf repo add`, `register`, `remove` and `prune` accept `--idempotency-key`** and pass it to the registry route; a write of several registry rows files each under the key and its position.
- **`eawf track sync` is retired.** It recomputed epoch-1 outcome standings; it now answers with the flag-day refusal like the other retired epoch-1 verbs.

## Migrating a caller

A script that parsed `eawf --json config get <key>` reads `.result.value` instead of `.value`; the same holds for every verb listed above. A caller that must work across the change can read `payload["result"]` when `payload` carries an `operation` key, and `payload` itself otherwise.
