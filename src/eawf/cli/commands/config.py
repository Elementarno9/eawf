"""``eawf config`` Typer sub-app — get/set/validate/profile enable.

Per ``docs/architecture/cli-surface.md``:

- ``eawf config get <key> [--scope]``           → merged value + source layer
- ``eawf config set <key> <value> --scope L``   → write to layer L (built-in
                                                    is read-only)
- ``eawf config validate [--scope]``            → run merged config through
                                                    the minimal Pydantic
                                                    schema; exit 4 on
                                                    validation failure
- ``eawf config profile enable <profile-id>``   → write profile + materialise
                                                    state keys

Layer label conventions match :data:`eawf.config.layered.LAYER_ORDER`:
``built-in | global | workspace | repo | local | env | cli``. Only file
layers (global / workspace / repo / local) are writable.

Exit-code mapping (per W00 plan / ``cli/exit_codes.py``):

- ``0``: success.
- ``2``: ``NOT_FOUND`` — key not present in merged config (``get``).
- ``3``: ``INVALID_INPUT`` — bad layer name, built-in scope on ``set``,
        unknown profile id.
- ``4``: ``VALIDATION_FAILED`` — malformed YAML in any layer or schema
        rejection during ``validate``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
from pathlib import Path
from typing import Annotated, Any

import orjson
import typer
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.cli.errors import InvalidInput, NotFound, ValidationFailed, emit_error
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.config.layered import (
    LAYER_ORDER,
    WRITABLE_LAYERS,
    get_dotted,
    layer_path,
    merge_config,
)
from eawf.config.loader import load_yaml_layer
from eawf.config.profile import enable_profile
from eawf.lock import portalock
from eawf.profiles.compose import compose
from eawf.profiles.loader import list_profiles, load_profile

logger = logging.getLogger(__name__)


# Resolve repo/workspace anchors. The Phase 2 W06 surface is intentionally
# narrow: workspace comes from the global ``-w`` flag; repo defaults to the
# current working directory. Phase 3 will integrate with state-resolver
# upgrades once those land.
def _resolve_anchors(flags: GlobalFlags) -> tuple[Path, Path | None]:
    """Return ``(repo, workspace)`` anchors for the layered merge.

    ``repo`` defaults to the current working directory; ``workspace`` is
    taken verbatim from ``flags.workspace`` (may be ``None``).
    """
    repo = Path.cwd()
    workspace = flags.workspace
    return repo, workspace


# --- Minimal config schema (Phase 3 W02 will tighten) -----------------------


class _ConfigSchema(BaseModel):
    """Minimal Pydantic schema for ``config validate``.

    Per the plan §W06 acceptance: "config validate runs the merged config
    through Pydantic config schema (placeholder body extended in Phase 3 W02
    — for v0.1 P02 we fill the minimal schema sections from
    ``docs/architecture/state-model.md`` 'Config schema required sections')."

    The minimal contract is: every required top-level section listed in the
    inventory is present and is a mapping (or, for the ``commands`` section,
    a mapping). Deeper structure is left as ``dict[str, Any]`` until Phase 3
    W02 lands the strict per-section models.
    """

    # Pydantic v2 strict per AGENTS.md rule 2 — extra="forbid" on every model.
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    cli: dict[str, Any]
    project: dict[str, Any]
    workspace: dict[str, Any]
    profiles: dict[str, Any]
    runtime: dict[str, Any]
    ui: dict[str, Any]
    storage: dict[str, Any]
    research: dict[str, Any]
    planning: dict[str, Any]
    estimation: dict[str, Any]
    audit: dict[str, Any]
    ship: dict[str, Any]
    review: dict[str, Any]
    polish: dict[str, Any]
    memory: dict[str, Any]
    vcs: dict[str, Any]
    worktrees: dict[str, Any]
    acceptance: dict[str, Any]
    security: dict[str, Any]
    hooks: dict[str, Any]
    mcp: dict[str, Any]
    statusline: dict[str, Any]
    docs: dict[str, Any]
    commands: dict[str, Any]
    state_schema: dict[str, Any]


# --- Sub-app construction ---------------------------------------------------


config_app = typer.Typer(
    name="config",
    help="Manage layered configuration (built-in / global / workspace / repo / local).",
    no_args_is_help=True,
)


profile_app = typer.Typer(
    name="profile",
    help="Manage active profiles.",
    no_args_is_help=True,
)
config_app.add_typer(profile_app, name="profile")


# --- Helpers ---------------------------------------------------------------


def _set_dotted_in_yaml(payload: dict[str, Any], dotted: str, value: Any) -> None:
    """In-place set ``payload[a][b][c] = value`` for ``a.b.c``.

    Auto-creates intermediate dicts. Replaces non-dict intermediates so the
    later set wins (consistent with :func:`eawf.config.layered._set_dotted`).
    """
    parts = dotted.split(".")
    cur: dict[str, Any] = payload
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _coerce_value(raw: str) -> Any:
    """Best-effort scalar coercion of a CLI ``--value`` argument."""
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _atomic_write_yaml(target: Path, payload: dict[str, Any]) -> None:
    """Atomic YAML write (tempfile + fsync + rename). Caller holds the lock."""
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(4)
    tmp = target.with_name(f"{target.name}.tmp.{suffix}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, sort_keys=True, default_flow_style=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        parent_fd = os.open(target.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        logger.info(f"_atomic_write_yaml wrote {target}")
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink(missing_ok=True)


# --- Subcommands ------------------------------------------------------------


@config_app.command("get")
def config_get(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help="Dotted config key (e.g. 'planning.approval').")],
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="Restrict resolution to a single layer (debug)."),
    ] = None,
) -> None:
    """Print the merged value for ``key`` and the layer it came from."""
    flags: GlobalFlags = ctx.obj
    repo, workspace = _resolve_anchors(flags)
    try:
        merged, sources = merge_config(workspace=workspace, repo=repo)
    except ValidationFailed as exc:
        emit_error(exc, flags=flags)
        return  # pragma: no cover  emit_error raises Exit
    if scope is not None and scope not in LAYER_ORDER:
        emit_error(InvalidInput(f"unknown scope {scope!r}"), flags=flags)
        return  # pragma: no cover

    try:
        value = get_dotted(merged, key)
    except KeyError:
        emit_error(NotFound(f"key not found: {key}"), flags=flags)
        return  # pragma: no cover

    source = sources.get(key, "built-in")
    if scope is not None and source != scope:
        emit_error(NotFound(f"key {key} not provided by scope {scope}"), flags=flags)
        return  # pragma: no cover

    payload = {"key": key, "value": value, "source": source}
    text = f"{key} = {value!r}  (source: {source})"
    emit_json_or_text(payload, text, flags=flags)


@config_app.command("set")
def config_set(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help="Dotted config key.")],
    value: Annotated[str, typer.Argument(help="Value to write (string-coerced).")],
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help=("Layer to write to (global | workspace | repo | local). built-in is read-only."),
        ),
    ] = "repo",
) -> None:
    """Write *value* under *key* to the chosen layer file."""
    flags: GlobalFlags = ctx.obj
    repo, workspace = _resolve_anchors(flags)

    if scope == "built-in":
        emit_error(
            InvalidInput("layer 'built-in' is read-only; choose global|workspace|repo|local"),
            flags=flags,
        )
        return  # pragma: no cover
    if scope not in WRITABLE_LAYERS:
        emit_error(
            InvalidInput(
                f"unknown or non-writable scope {scope!r}; choose from {list(WRITABLE_LAYERS)}"
            ),
            flags=flags,
        )
        return  # pragma: no cover

    try:
        target_path = layer_path(scope, workspace=workspace, repo=repo)
    except ValueError as exc:
        emit_error(InvalidInput(str(exc)), flags=flags)
        return  # pragma: no cover

    coerced = _coerce_value(value)
    try:
        with portalock.acquire(target_path):
            existing = load_yaml_layer(target_path)
            _set_dotted_in_yaml(existing, key, coerced)
            _atomic_write_yaml(target_path, existing)
    except ValidationFailed as exc:
        emit_error(exc, flags=flags)
        return  # pragma: no cover  emit_error raises Exit
    except yaml.YAMLError as exc:
        emit_error(
            ValidationFailed(f"config layer is not valid YAML: {exc}"),
            flags=flags,
        )
        return  # pragma: no cover
    except OSError as exc:
        emit_error(
            InvalidInput(f"cannot read or write {target_path}: {exc}"),
            flags=flags,
        )
        return  # pragma: no cover

    payload = {
        "key": key,
        "value": coerced,
        "scope": scope,
        "path": str(target_path),
    }
    text = f"set {key} = {coerced!r}  (scope: {scope}, path: {target_path})"
    emit_json_or_text(payload, text, flags=flags)


@config_app.command("validate")
def config_validate(
    ctx: typer.Context,
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="(reserved) Restrict to one layer's contribution."),
    ] = None,
    composed: Annotated[
        bool,
        typer.Option(
            "--composed",
            help=(
                "Emit the composed-profile view (deep-merge of all enabled "
                "profiles in profiles.enabled) alongside the validation result."
            ),
        ),
    ] = False,
) -> None:
    """Validate the merged config against the minimal Pydantic schema.

    With ``--composed``, additionally compose every profile listed in
    ``profiles.enabled`` and include the merged view in the output envelope.
    The composed view is deterministic (sorted lists, locked render-block
    order) so repeated invocations produce byte-identical JSON.
    """
    flags: GlobalFlags = ctx.obj
    repo, workspace = _resolve_anchors(flags)

    # Argument validation (scope shape) before merge so unknown labels exit 3
    # instead of bubbling up through the merge engine.
    if scope is not None and scope not in LAYER_ORDER:
        emit_error(InvalidInput(f"unknown scope {scope!r}"), flags=flags)
        return  # pragma: no cover

    try:
        merged, _sources = merge_config(workspace=workspace, repo=repo)
    except ValidationFailed as exc:
        emit_error(exc, flags=flags)
        return  # pragma: no cover

    try:
        _ConfigSchema.model_validate(merged)
    except ValidationError as exc:
        emit_error(ValidationFailed(f"config schema rejected: {exc}"), flags=flags)
        return  # pragma: no cover

    payload: dict[str, Any] = {"ok": True, "scope": scope}
    text = "config: ok"

    if composed:
        # Resolve the enabled profile list from the merged config. Unknown
        # ids surface as InvalidInput from load_profile so the user gets a
        # helpful pointer to the registry.
        profiles_section = merged.get("profiles") or {}
        enabled_raw = profiles_section.get("enabled") or []
        if not isinstance(enabled_raw, list):
            emit_error(
                ValidationFailed(
                    f"profiles.enabled must be a list, got {type(enabled_raw).__name__}"
                ),
                flags=flags,
            )
            return  # pragma: no cover
        enabled: list[str] = [str(p) for p in enabled_raw]

        try:
            bodies = [load_profile(pid) for pid in enabled]
        except InvalidInput as exc:
            emit_error(exc, flags=flags)
            return  # pragma: no cover

        composed_view = compose(bodies)
        payload["enabled_profiles"] = enabled
        payload["available_profiles"] = list(list_profiles())
        payload["composed"] = composed_view.model_dump(mode="json")
        text = f"config: ok (composed {len(enabled)} profile(s): {composed_view.name})"

    emit_json_or_text(payload, text, flags=flags)


@profile_app.command("enable")
def profile_enable(
    ctx: typer.Context,
    profile_id: Annotated[str, typer.Argument(help="Profile id (e.g. 'python', 'research').")],
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Layer to write the profile-enable to (default: repo).",
        ),
    ] = "repo",
) -> None:
    """Enable *profile_id* in *scope* and materialise required state keys."""
    flags: GlobalFlags = ctx.obj
    repo, workspace = _resolve_anchors(flags)

    if scope == "built-in":
        emit_error(
            InvalidInput("layer 'built-in' is read-only; choose global|workspace|repo|local"),
            flags=flags,
        )
        return  # pragma: no cover
    if scope not in WRITABLE_LAYERS:
        emit_error(
            InvalidInput(
                f"unknown or non-writable scope {scope!r}; choose from {list(WRITABLE_LAYERS)}"
            ),
            flags=flags,
        )
        return  # pragma: no cover

    try:
        target_path = layer_path(scope, workspace=workspace, repo=repo)
    except ValueError as exc:
        emit_error(InvalidInput(str(exc)), flags=flags)
        return  # pragma: no cover

    state_path = repo / ".ea" / "state.json"
    state_arg = state_path if state_path.exists() else None

    try:
        result = enable_profile(
            profile_id,
            layer=scope,
            layer_file_path=target_path,
            state_path=state_arg,
        )
    except (InvalidInput, NotFound, ValidationFailed) as exc:
        emit_error(exc, flags=flags)
        return  # pragma: no cover
    except yaml.YAMLError as exc:
        emit_error(
            ValidationFailed(f"config layer is not valid YAML: {exc}"),
            flags=flags,
        )
        return  # pragma: no cover
    except OSError as exc:
        emit_error(
            InvalidInput(f"cannot read or write {target_path}: {exc}"),
            flags=flags,
        )
        return  # pragma: no cover

    text = (
        f"profile enable: {result['profile']} (layer: {result['layer']}, "
        f"already_enabled: {result['already_enabled']}, "
        f"state_keys: {result['state_keys_materialised']})"
    )
    emit_json_or_text(result, text, flags=flags)


# Re-export the orjson dependency to keep import surface explicit when callers
# need the exact serialiser used by the CLI envelope. (Some tests stub stdout
# decoders against this.)
__all__ = ["config_app", "orjson"]
