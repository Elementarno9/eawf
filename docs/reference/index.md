# Reference

Curated prose plus auto-generated catalogs for the `eawf` framework.

## Curated reference

- [Enums (prose)](enums.md) — annotated `StrEnum` catalog with per-field notes.
- [Exit codes (prose)](exit-codes.md) — exit-code surface plus the `--json` error envelope.
- [Error codes (prose)](error-codes.md) — cause-level `ErrorCode` vocabulary with remediation.
- [Hook events](hook-events.md) — `HookEvent` shape and per-event payloads.
- [Lockfile semantics](lockfile-semantics.md) — `portalocker` sibling-lock contract.
- [URN namespace](urn-namespace.md) — `urn:eawf:v1:*` format rules and kind catalog.
- [Coverage gates](coverage-gates.md) — per-package coverage thresholds.
- [Mutation testing](mutation-testing.md) — rebuild reference; the CI mutation-core job was removed pending a real owner.

## Auto-generated reference

These pages are regenerated from the live source tree by `eawf doc verify
--strict`; a hand edit fails the drift gate.

- [Auto-generated index](autogen/index.md)
- [CLI reference](autogen/cli.md)
- [Skill catalog](autogen/skills.md)
- [JSON Schema reference](autogen/schema.md)
- [State enums (generated)](autogen/enums.md)
- [Error codes (generated)](autogen/error-codes.md)
- [Exit codes (generated)](autogen/exit-codes.md)
