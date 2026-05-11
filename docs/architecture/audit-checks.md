# Audit-check DSL

*Yaml-declarative check spec + frozen registry of check kinds. Wired
into `eawf audit run --checks <yaml>`.*

The DSL replaces the Phase-2 fixture-driven stub of `eawf audit run`.
A yaml file declares an ordered list of checks; the runner dispatches
each through a frozen registry of check kinds and stores the
pass/fail booleans on the resulting audit record. The original
`--fixture <json>` option is retained for the v0.2 cycle as an escape
hatch but is mutually exclusive with `--checks`.

Decision: D02 (yaml-declarative + check-kind registry).
Backlog: B019 (DSL skeleton); B044 (sandbox-policy enforcement,
v0.3).

## Grammar

```yaml
schema_version: "1.0"            # only accepted value in v0.2
checks:                          # ordered list, executes top-to-bottom
  - kind: <check-kind>
    name: <stable, unique within file>
    args: { ... }                # kind-specific shape
```

Top-level extra keys are rejected (`extra="forbid"`). Each entry in
`checks` is a `CheckSpec(kind, name, args)`; `kind` is a `Literal`
constrained to the registered check-kind set, so an unknown kind is
rejected at load time.

## Frozen v0.2 registry (5 kinds)

| Kind | Args | Pass condition |
| --- | --- | --- |
| `file_exists` | `{path: str}` | `Path(path).is_file()` |
| `path_glob_nonempty` | `{pattern: str}` | `len(list(Path(".").glob(pattern))) >= 1` |
| `regex_in_file` | `{path: str, pattern: str}` | `re.search(pattern, body)` matches |
| `state_field_equals` | `{field: str, value: Any, state_path: str = ".ea/state.json"}` | `dotpath(state, field) == value` |
| `command_exit_zero` | `{argv: list[str]}` | `subprocess.run(argv).returncode == 0` |

Path-bearing kinds resolve relative paths against the runner's `cwd`
(the CLI passes the repo root — `state_path.parent.parent` —
typically the project root). Absolute paths are honoured as-is.

`state_field_equals` parses the target file as JSON and walks the
dot-separated `field`; missing segments fail the check with a
diagnostic `details` string rather than raising.

## Example

The canonical golden example lives at
`tests/golden/audit_dsl/sample.yaml`:

```yaml
schema_version: "1.0"
checks:
  - kind: file_exists
    name: pyproject_present
    args:
      path: pyproject.toml
  - kind: regex_in_file
    name: phase_p13_in_changelog
    args:
      path: CHANGELOG.md
      pattern: "P13"
  - kind: state_field_equals
    name: schema_version_pinned
    args:
      field: schema_version
      value: "1.0"
  - kind: path_glob_nonempty
    name: src_python_files
    args:
      pattern: "src/eawf/**/*.py"
  - kind: command_exit_zero
    name: ruff_no_errors
    args:
      argv: ["echo", "ok"]
```

## CLI surface

```
eawf audit run <audit_id> --scope-id <scope> --kind <kind> \
                          --checks <yaml-path>
```

Behaviour:

- `--checks` and `--fixture` are mutually exclusive; passing both
  raises `InvalidInput` with exit code 3.
- The DSL runner converts each `CheckResult` into the audit's
  `check_results` field (`name`, `passed`, `details`).
- Verdict is `pass` when every result passes, otherwise `major`
  (matches the legacy fixture path).

The library entrypoint is `eawf.audit_dsl.runner.run_checks(specs,
cwd=...)`; importing the package directly is equivalent (`from
eawf.audit_dsl import run_checks, load_spec`).

## Sandbox-policy boundary (B044)

`command_exit_zero` shells out via `subprocess.run`. In v0.2 the DSL
runner does **not** consult the sandbox / permission policy table
(rendered by `eawf wave policy show`). Enforcement is the caller's
responsibility:

- For CI: gate the policy check in the wave's CI pre-step.
- For developer invocation: surface the policy table before invoking
  `audit run --checks` against an untrusted yaml file.

Hardening — pulling the policy check inside the runner so a hostile
`argv` cannot reach `subprocess.run` — is tracked as backlog item
**B044** for v0.3. The yaml schema remains stable across that
transition; only the runtime check changes.

## Error handling

| Failure mode | Surface |
| --- | --- |
| Missing yaml file | `InvalidInput("audit-check spec ... not found")` |
| Malformed yaml | `InvalidInput("... not valid yaml: ...")` |
| Schema mismatch | `InvalidInput("... schema mismatch: ...")` |
| Missing kind-arg | `ValueError` from the registry function; bubbles up as a CLI error |
| Both `--checks` and `--fixture` | `InvalidInput` |

`InvalidInput` maps to exit code 3 via `eawf.cli.exit_codes`.

## Source layout

- `src/eawf/audit_dsl/models.py` — `CheckSpec`, `CheckResult`,
  `CheckFile`.
- `src/eawf/audit_dsl/registry.py` — `CHECK_REGISTRY` dispatch table.
- `src/eawf/audit_dsl/runner.py` — `load_spec`, `run_checks`.
- `tests/unit/test_audit_dsl.py` — boundary + error tests per kind.
- `tests/golden/audit_dsl/sample.yaml` — the canonical multi-kind
  example.
