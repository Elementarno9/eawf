# Profiles

A profile is a composable bundle of rules, style conventions, test
discipline, and workflow defaults that an eawf-managed repo opts into. The
project composes the active profiles into the rendered `AGENTS.md` and the
harness plugin trees.

## Bundled profiles

- `core` — the non-negotiable rules every repo inherits.
- `python` — f-strings-only, full type hints, `uv run` invocation,
  pre-commit-before-commit, pytest discipline.
- `research` — hypotheses / audits / decisions as first-class state.
- `quant` (stub) and `ml` (stub) — reserved domain bundles.

## Discovery precedence

Profiles resolve workspace-first, then user, then the bundled set
(Decision D18):

```text
.ea/profiles/        (workspace — highest priority)
~/.eawf/profiles/    (user)
eawf.profiles.data   (bundled with the wheel — lowest priority)
```

A profile defined at a higher layer overrides a same-named profile at a
lower layer. Resolution is cache-invalidated on file mtime and each profile
body is validated through the strict Pydantic model on load.

## Trust (TOFU)

Non-bundled profiles are trust-on-first-use (Decision D19). The first time a
repo activates a profile that did not ship with the wheel, eawf prompts via
an interactive question and records the profile's sha256 in
`.ea/config.yaml` under `profiles.trusted`. If the content hash later drifts
from the recorded value, eawf re-prompts before using it. Bundled profiles
are auto-trusted.

## Composition

Profiles merge in precedence order; the `core` rules always apply. Field-level
conflicts resolve in favour of the higher-precedence layer. The composed
result is what `eawf` renders — never edit the rendered `AGENTS.md` by hand;
edit the profile source and re-render.

## CLI surface

```text
eawf profile list        # show resolved profiles + their source layer
eawf profile new         # scaffold a new workspace/user profile
eawf profile validate    # validate a profile body against the schema
```

See `eawf help urns` for how profile-scoped references are named and
`eawf help migration` for profile-schema version bumps.
