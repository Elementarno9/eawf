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

Layer label conventions match :data:`eawf.kernel.config.layered.LAYER_ORDER`:
``built-in | global | workspace | repo | local | env | cli``. Only file
layers (global / workspace / repo / local) are writable.

Exit-code mapping (per W00 plan / ``cli/exit_codes.py``):

- ``0``: success.
- ``2``: ``NOT_FOUND`` — key not present in merged config (``get``).
- ``3``: ``INVALID_INPUT`` — bad layer name, built-in scope on ``set``,
        unknown profile id.
- ``4``: ``VALIDATION_FAILED`` — malformed YAML in any layer or schema
        rejection during ``validate``.

Daemon-internal note (P24-W10): :func:`_save_value_to_layer` is the
sole CLI-side mutator for layered YAML; since W10 it dispatches
through the daemon's ``config.set_layer_value`` RPC when
``daemon.proxy_enabled=True`` (the new default). The in-process arm
is retained as the V1 carve-out fallback (CI / read-only one-shot /
recovery shell / ``EAWF_DAEMONLESS=1``). After v0.5 the in-process
arm migrates under ``daemon/_internal/`` and stops being importable
from user code.
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
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydValidationError

from eawf.kernel.config.registry import (
    CONFIG_REGISTRY,
    ConfigKey,
    coerce_and_validate,
    keys_for_tab,
    registry_lookup,
    tabs_sorted,
)
from eawf.kernel.config.schema import EstimationConfig
from eawf.runtime.lock import portalock
from eawf.runtime.vcs.coauthor import VcsConfig
from eawf.surfaces.cli.errors import StateConflict, UserError, ValidationError, emit_error
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

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

    For v0.1 P02 the minimal schema mirrors the section list in
    ``docs/architecture/envelope.md`` "Config schema required sections".
    Phase 3 W02 will tighten each section into a strict Pydantic model.

    The minimal contract is: every required top-level section listed in the
    inventory is present and is a mapping (or, for the ``commands`` section,
    a mapping). Deeper structure is left as ``dict[str, Any]`` until Phase 3
    W02 lands the strict per-section models.
    """

    # Pydantic v2 strict per AGENTS.md rule 2 — extra="forbid" on every model.
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(pattern=r"^\d+\.\d+$")
    # ``config`` top-level section currently holds ``layers_visible``;
    # deeper per-key validation arrives in a later wave.
    config: dict[str, Any] = Field(default_factory=dict)
    cli: dict[str, Any]
    project: dict[str, Any]
    workspace: dict[str, Any]
    profiles: dict[str, Any]
    runtime: dict[str, Any]
    ui: dict[str, Any]
    storage: dict[str, Any]
    research: dict[str, Any]
    planning: dict[str, Any]
    estimation: EstimationConfig
    audit: dict[str, Any]
    ship: dict[str, Any]
    review: dict[str, Any]
    polish: dict[str, Any]
    flow: dict[str, Any]
    memory: dict[str, Any]
    vcs: VcsConfig
    worktrees: dict[str, Any]
    acceptance: dict[str, Any]
    security: dict[str, Any]
    hooks: dict[str, Any]
    mcp: dict[str, Any]
    statusline: dict[str, Any]
    docs: dict[str, Any]
    commands: dict[str, Any]
    state_schema: dict[str, Any]
    # ``daemon`` section pairs with the ``state.mutate`` RPC. Treated as
    # ``dict[str, Any]`` until a later wave hardens the per-key contract
    # (see :mod:`eawf.kernel.config.defaults` for the shipped schema).
    daemon: dict[str, Any]
    # New top-level sections. Each is a loose ``dict[str, Any]`` for
    # now; per-key Pydantic contracts arrive in later waves (CLI
    # surface + telemetry projector).
    telemetry: dict[str, Any] = Field(default_factory=dict)
    dispatch: dict[str, Any] = Field(default_factory=dict)
    language: dict[str, Any] = Field(default_factory=dict)
    # ``preferences`` carries the operator-preference knobs (solution_bias,
    # scope_size, auto_choose). Value-shape validation lives in the leaf
    # catalog + PreferencesConfig; the composed schema only needs to accept
    # the section so a default-bearing merge does not trip extra="forbid".
    preferences: dict[str, Any] = Field(default_factory=dict)
    # ``prose`` carries the doc-clarity prose-lint knobs (level,
    # clarity_judge, block_on_lint). Value-shape validation + the
    # tighten-only authority guard live in the leaf catalog + ProseConfig;
    # the composed schema only needs to accept the section so a
    # default-bearing merge does not trip extra="forbid".
    prose: dict[str, Any] = Field(default_factory=dict)


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
    later set wins (consistent with :func:`eawf.kernel.config.layered._set_dotted`).
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
    import yaml

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
        logger.info(f"_atomic_write_yaml wrote path={target}")
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink(missing_ok=True)


def _layer_label_for_path(target_path: Path) -> str | None:
    """Reverse-resolve a writable layer label from *target_path*.

    The daemon ``config.set_layer_value`` RPC takes a layer *label*
    (``"repo"`` / ``"local"`` / ``"workspace"`` / ``"global"``) rather
    than a path, so the proxy wrapper has to map back. Returns
    ``None`` when the path does not match any of the four canonical
    file-layer paths — callers fall through to the in-process arm.
    """
    from eawf.kernel.config.layered import (
        global_config_path,
        local_config_path,
        repo_config_path,
    )

    resolved = target_path.resolve()
    if resolved == global_config_path().resolve():
        return "global"
    parent = target_path.parent
    if parent.name == ".ea" and resolved == repo_config_path(parent.parent).resolve():
        return "repo"
    if parent.name == "local" and resolved == local_config_path(parent.parent.parent).resolve():
        return "local"
    # Workspace anchor matches the same shape as repo; only the
    # outer caller knows which it is. We treat unambiguous matches
    # only — if both ``repo`` and ``workspace`` candidates exist for
    # the same prefix, fall through to the in-process arm.
    return None


def _daemon_proxy_enabled() -> bool:
    """Return True when ``daemon.proxy_enabled`` is on AND no daemonless override.

    Mirrors :func:`eawf.surfaces.cli._mutation._proxy_enabled` but adds the
    ``EAWF_DAEMONLESS=1`` env-var escape hatch so callers (CI hooks,
    recovery shell) can force the in-process arm without rewriting
    the merged config.
    """
    if os.environ.get("EAWF_DAEMONLESS", "") == "1":
        return False
    from eawf.surfaces.cli._mutation import _proxy_enabled

    return _proxy_enabled(None)


def _save_value_to_layer(
    *,
    target_path: Path,
    key: str,
    value: Any,
    repo_root: Path | None = None,
) -> None:
    """Persist ``key=value`` into the YAML layer at *target_path*.

    Since P24-W10 this helper is a thin dispatcher:

    * **Daemon-proxy arm (default).** When ``daemon.proxy_enabled``
      is ``True`` (the default since W10) AND the daemon is reachable,
      the call routes through ``config.set_layer_value`` RPC. The
      daemon owns the portalock + atomic-rename + bus publish. The
      caller's *repo_root* is forwarded so the daemon resolves the
      target layer against the right repo (the daemon is one per user;
      pre-W03 callers could be mis-routed against the daemon's
      boot-time cwd).
    * **In-process fallback arm.** Reached when (a) ``proxy_enabled``
      is ``False`` (V1 carve-out), (b) ``EAWF_DAEMONLESS=1`` is set,
      (c) the daemon is unreachable, or (d) the path does not map
      onto a canonical writable layer. The legacy lock-read-write
      loop runs under :func:`eawf.runtime.lock.portalock.acquire`.

    Args:
        target_path: Absolute path of the layer's ``config.yaml``.
        key: Dotted config key (e.g. ``"vcs.auto_commit"``).
        value: Typed value to write.
        repo_root: Absolute path of the repo root the layer belongs to
            (e.g. ``flags.workspace`` or ``Path.cwd()``). Forwarded to
            the daemon as the per-request anchor. ``None`` falls back
            to the daemon's boot-time anchor with a one-shot
            ``daemon_anchor_fallback`` warning on the daemon side.

    Raises:
        StateConflict: Daemon required but unreachable
            (``daemon_required`` envelope; ``kind="IntegrityViolation"``).
        ValidationError: Underlying YAML is malformed.
        OSError: Filesystem failure during read or write.
        yaml.YAMLError: Dump failure when serialising the merged payload.
    """
    if _daemon_proxy_enabled():
        layer_label = _layer_label_for_path(target_path)
        if layer_label is not None:
            from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError
            from eawf.surfaces.cli._mutation import _daemon_reachable

            if not _daemon_reachable():
                raise StateConflict(
                    "daemon_required: daemon.proxy_enabled=true but the daemon is unreachable; "
                    "run `eawf daemon start` or set EAWF_DAEMONLESS=1 for the V1 carve-out",
                    kind="IntegrityViolation",
                )
            key_path = key.split(".")
            try:
                with DaemonClient() as client:
                    client.config_set_layer_value(
                        layer=layer_label,
                        key_path=key_path,
                        value=value,
                        repo_root=str(repo_root) if repo_root is not None else None,
                    )
                return
            except DaemonRpcError as exc:
                if exc.code == -32601:
                    # Method not found — fall through to in-process
                    # path so a pre-W10 daemon stays usable.
                    logger.debug("_save_value_to_layer daemon-rpc method-not-found; fallback")
                else:
                    raise

    # In-process fallback arm (V1 carve-out / EAWF_DAEMONLESS=1 / unmapped path).
    from eawf.kernel.config.loader import load_yaml_layer

    with portalock.acquire(target_path):
        existing = load_yaml_layer(target_path)
        _set_dotted_in_yaml(existing, key, value)
        _atomic_write_yaml(target_path, existing)


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
    from eawf.kernel.config.layered import LAYER_ORDER, get_dotted, merge_config

    flags: GlobalFlags = ctx.obj
    repo, workspace = _resolve_anchors(flags)
    try:
        merged, sources = merge_config(workspace=workspace, repo=repo)
    except ValidationError as exc:
        emit_error(exc, flags=flags)
        return  # pragma: no cover  emit_error raises Exit
    if scope is not None and scope not in LAYER_ORDER:
        emit_error(UserError(f"unknown scope {scope!r}", kind="InvalidInput"), flags=flags)
        return  # pragma: no cover

    try:
        value = get_dotted(merged, key)
    except KeyError:
        emit_error(UserError(f"key not found: {key}", kind="NotFound"), flags=flags)
        return  # pragma: no cover

    source = sources.get(key, "built-in")
    if scope is not None and source != scope:
        emit_error(
            UserError(f"key {key} not provided by scope {scope}", kind="NotFound"), flags=flags
        )
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
    import yaml

    from eawf.kernel.config.layered import WRITABLE_LAYERS, layer_path

    flags: GlobalFlags = ctx.obj
    repo, workspace = _resolve_anchors(flags)

    if scope == "built-in":
        emit_error(
            UserError(
                "layer 'built-in' is read-only; choose global|workspace|repo|local",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return  # pragma: no cover
    if scope not in WRITABLE_LAYERS:
        emit_error(
            UserError(
                f"unknown or non-writable scope {scope!r}; choose from {list(WRITABLE_LAYERS)}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return  # pragma: no cover

    try:
        target_path = layer_path(scope, workspace=workspace, repo=repo)
    except ValueError as exc:
        emit_error(UserError(str(exc), kind="InvalidInput"), flags=flags)
        return  # pragma: no cover

    coerced = _coerce_value(value)
    try:
        _save_value_to_layer(target_path=target_path, key=key, value=coerced, repo_root=repo)
    except ValidationError as exc:
        emit_error(exc, flags=flags)
        return  # pragma: no cover  emit_error raises Exit
    except yaml.YAMLError as exc:
        emit_error(
            ValidationError(f"config layer is not valid YAML: {exc}"),
            flags=flags,
        )
        return  # pragma: no cover
    except OSError as exc:
        emit_error(
            UserError(f"cannot read or write {target_path}: {exc}", kind="InvalidInput"),
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
    from eawf.kernel.config.layered import LAYER_ORDER, merge_config

    flags: GlobalFlags = ctx.obj
    repo, workspace = _resolve_anchors(flags)

    # Argument validation (scope shape) before merge so unknown labels exit 3
    # instead of bubbling up through the merge engine.
    if scope is not None and scope not in LAYER_ORDER:
        emit_error(UserError(f"unknown scope {scope!r}", kind="InvalidInput"), flags=flags)
        return  # pragma: no cover

    try:
        merged, _sources = merge_config(workspace=workspace, repo=repo)
    except ValidationError as exc:
        emit_error(exc, flags=flags)
        return  # pragma: no cover

    try:
        _ConfigSchema.model_validate(merged)
    except PydValidationError as exc:
        emit_error(ValidationError(f"config schema rejected: {exc}"), flags=flags)
        return  # pragma: no cover

    payload: dict[str, Any] = {"ok": True, "scope": scope}
    text = "config: ok"

    if composed:
        from eawf.platform.profiles.compose import compose
        from eawf.platform.profiles.loader import list_profiles, load_profile

        # Resolve the enabled profile list from the merged config. Unknown
        # ids surface as UserError (kind="InvalidInput") from load_profile
        # so the user gets a helpful pointer to the registry.
        profiles_section = merged.get("profiles") or {}
        enabled_raw = profiles_section.get("enabled") or []
        if not isinstance(enabled_raw, list):
            emit_error(
                ValidationError(
                    f"profiles.enabled must be a list, got {type(enabled_raw).__name__}"
                ),
                flags=flags,
            )
            return  # pragma: no cover
        enabled: list[str] = [str(p) for p in enabled_raw]

        try:
            bodies = [load_profile(pid) for pid in enabled]
        except UserError as exc:
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
    import yaml

    from eawf.kernel.config.layered import WRITABLE_LAYERS, layer_path
    from eawf.kernel.config.profile import enable_profile

    flags: GlobalFlags = ctx.obj
    repo, workspace = _resolve_anchors(flags)

    if scope == "built-in":
        emit_error(
            UserError(
                "layer 'built-in' is read-only; choose global|workspace|repo|local",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return  # pragma: no cover
    if scope not in WRITABLE_LAYERS:
        emit_error(
            UserError(
                f"unknown or non-writable scope {scope!r}; choose from {list(WRITABLE_LAYERS)}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return  # pragma: no cover

    try:
        target_path = layer_path(scope, workspace=workspace, repo=repo)
    except ValueError as exc:
        emit_error(UserError(str(exc), kind="InvalidInput"), flags=flags)
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
    except (UserError, ValidationError) as exc:
        emit_error(exc, flags=flags)
        return  # pragma: no cover
    except yaml.YAMLError as exc:
        emit_error(
            ValidationError(f"config layer is not valid YAML: {exc}"),
            flags=flags,
        )
        return  # pragma: no cover
    except OSError as exc:
        emit_error(
            UserError(f"cannot read or write {target_path}: {exc}", kind="InvalidInput"),
            flags=flags,
        )
        return  # pragma: no cover

    text = (
        f"profile enable: {result['profile']} (layer: {result['layer']}, "
        f"already_enabled: {result['already_enabled']}, "
        f"state_keys: {result['state_keys_materialised']})"
    )
    emit_json_or_text(result, text, flags=flags)


# --- Interactive menu (questionary, P20-W10) --------------------------------


# Pinned questionary style — mirrors the init wizard's palette so the two
# surfaces feel consistent. Imported lazily inside :func:`_build_menu_style`
# so non-interactive callers do not pay the prompt_toolkit import tax.
_MENU_STYLE: tuple[tuple[str, str], ...] = (
    ("qmark", "fg:#5fafff bold"),
    ("question", "bold"),
    ("answer", "fg:#87ff87 bold"),
    ("pointer", "fg:#5fafff bold"),
    ("highlighted", "fg:#5fafff bold"),
    ("selected", "fg:#87ff87"),
    ("instruction", "fg:#666666 italic"),
)


def _build_menu_style() -> Any:
    """Lazily build the questionary :class:`Style` for the menu.

    Defers the prompt_toolkit import so callers that only invoke
    ``eawf config get/set/validate`` never pay the menu's import tax.
    """
    from questionary import Style

    return Style(list(_MENU_STYLE))


def _ensure_menu_answer(value: Any, *, step: str) -> Any:
    """Map a ``None`` questionary answer to :class:`UserError` (``kind="UserDeclined"``).

    questionary returns ``None`` from ``.ask()`` whenever the operator hits
    Ctrl-C / Esc / EOF. The menu treats that as a user-declined cancellation
    — exit code ``USER_DECLINED`` — so the operator's intent is recorded
    distinctly from validation failures.
    """
    if value is None:
        raise UserError(f"menu cancelled at step {step!r}", kind="UserDeclined")
    return value


def _prompt_for_value(entry: ConfigKey, current: Any) -> Any:
    """Dispatch *entry* to the matching questionary widget and return the answer.

    Args:
        entry: Registry entry describing the key.
        current: Current value pulled from the merged config (used as the
            default in the prompt). Pulled from the merged map so the menu
            surfaces "what would happen if I press Enter" exactly.

    Returns:
        The raw answer — typed by questionary for ``bool`` / ``choice`` /
        ``multichoice``; string for ``text`` / ``int`` / ``float`` (caller
        coerces via :func:`coerce_and_validate`).

    Raises:
        UserError: Operator aborted with Ctrl-C / Esc / EOF
            (``kind="UserDeclined"``).
    """
    import questionary
    from questionary import Choice

    style = _build_menu_style()
    prompt = entry.label
    instruction = entry.description if entry.description else None

    if entry.type == "bool":
        default_bool = bool(current) if current is not None else bool(entry.default)
        return _ensure_menu_answer(
            questionary.confirm(
                prompt, default=default_bool, style=style, instruction=instruction
            ).ask(),
            step=entry.key,
        )
    if entry.type in ("int", "float", "str"):
        default_text = "" if current is None else str(current)
        return _ensure_menu_answer(
            questionary.text(
                prompt, default=default_text, style=style, instruction=instruction
            ).ask(),
            step=entry.key,
        )
    if entry.type == "choice":
        choices = list(entry.choices or ())
        default_choice: str | None
        if current is not None and str(current) in choices:
            default_choice = str(current)
        else:
            default_choice = str(entry.default) if str(entry.default) in choices else None
        return _ensure_menu_answer(
            questionary.select(
                prompt,
                choices=choices,
                default=default_choice,
                style=style,
                instruction=instruction,
                use_search_filter=True,
                use_jk_keys=False,
                use_emacs_keys=False,
            ).ask(),
            step=entry.key,
        )
    if entry.type == "multichoice":
        choices_list = list(entry.choices or ())
        preselected: set[str] = set()
        if isinstance(current, (list, tuple)):
            preselected = {str(item) for item in current}
        opts = [Choice(name, checked=(name in preselected)) for name in choices_list]
        return _ensure_menu_answer(
            questionary.checkbox(
                prompt,
                choices=opts,
                style=style,
                instruction=instruction,
                use_search_filter=True,
                use_jk_keys=False,
                use_emacs_keys=False,
            ).ask(),
            step=entry.key,
        )
    raise UserError(f"unknown registry type: {entry.type}", kind="InvalidInput")


def _menu_get_current_value(merged: dict[str, Any], entry: ConfigKey) -> Any:
    """Return the merged value for *entry*, falling back to the entry default.

    Surface contract: the menu always has something to pre-fill, even on a
    fresh repo with no overlays.
    """
    from eawf.kernel.config.layered import get_dotted

    try:
        return get_dotted(merged, entry.key)
    except KeyError:
        return entry.default


@config_app.command("menu")
def config_menu(
    ctx: typer.Context,
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help=("Layer to write to (global | workspace | repo | local); built-in is read-only."),
        ),
    ] = "repo",
) -> None:
    """Open an interactive ``questionary`` menu for tunable config keys.

    Workflow:

    1. Pick a tab (alphabetical, drawn from the metadata registry).
    2. Pick a field inside the tab (alphabetical by dotted key).
    3. Edit the value with a widget matched to the registered type
       (``bool`` → confirm, ``choice`` / ``multichoice`` → select /
       checkbox, ``int`` / ``float`` / ``str`` → text).
    4. The coerced value is persisted through :func:`_save_value_to_layer`
       — the same mutator path :command:`eawf config set` uses, which means
       ``state.json`` is never touched here and the layered YAML write
       sequence (lock → load → set → atomic-write) stays the single
       writer.

    Operator UX:

    - Hitting Ctrl-C / Esc at any prompt exits with the canonical
      ``USER_DECLINED`` exit code (4xx) — distinct from a validation
      failure, so scripts can branch on the difference.
    - The shell that hosts the menu must be a TTY; questionary refuses to
      prompt against a piped stdin. CI callers should continue to use
      ``eawf config set`` directly.
    """
    import yaml

    from eawf.kernel.config.layered import WRITABLE_LAYERS, layer_path, merge_config

    flags: GlobalFlags = ctx.obj
    repo, workspace = _resolve_anchors(flags)

    if scope == "built-in":
        emit_error(
            UserError(
                "layer 'built-in' is read-only; choose global|workspace|repo|local",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return  # pragma: no cover
    if scope not in WRITABLE_LAYERS:
        emit_error(
            UserError(
                f"unknown or non-writable scope {scope!r}; choose from {list(WRITABLE_LAYERS)}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return  # pragma: no cover

    try:
        target_path = layer_path(scope, workspace=workspace, repo=repo)
    except ValueError as exc:
        emit_error(UserError(str(exc), kind="InvalidInput"), flags=flags)
        return  # pragma: no cover

    try:
        merged, _sources = merge_config(workspace=workspace, repo=repo)
    except ValidationError as exc:
        emit_error(exc, flags=flags)
        return  # pragma: no cover

    if not CONFIG_REGISTRY:  # pragma: no cover  defensive — module asserts non-empty
        emit_error(
            UserError("config metadata registry is empty; cannot open menu", kind="InvalidInput"),
            flags=flags,
        )
        return

    import questionary

    style = _build_menu_style()
    tabs = tabs_sorted()
    try:
        tab = _ensure_menu_answer(
            questionary.select(
                "Select a config tab:",
                choices=list(tabs),
                style=style,
                use_search_filter=True,
                use_jk_keys=False,
                use_emacs_keys=False,
            ).ask(),
            step="tab",
        )
        fields = keys_for_tab(tab)
        if not fields:  # pragma: no cover  registry invariants assert non-empty per tab
            emit_error(
                UserError(f"no fields registered under tab {tab!r}", kind="InvalidInput"),
                flags=flags,
            )
            return

        # Render each field as "key — label", keep a parallel map to the entry.
        field_labels = [f"{entry.key} — {entry.label}" for entry in fields]
        label_to_entry: dict[str, ConfigKey] = dict(zip(field_labels, fields, strict=True))
        chosen_label = _ensure_menu_answer(
            questionary.select(
                f"[{tab}] Select a key to edit:",
                choices=field_labels,
                style=style,
                use_search_filter=True,
                use_jk_keys=False,
                use_emacs_keys=False,
            ).ask(),
            step="field",
        )
        entry = label_to_entry[chosen_label]

        current_value = _menu_get_current_value(merged, entry)
        raw_answer = _prompt_for_value(entry, current_value)
        coerced = coerce_and_validate(entry, raw_answer)
    except UserError as exc:
        emit_error(exc, flags=flags)
        return  # pragma: no cover

    try:
        _save_value_to_layer(target_path=target_path, key=entry.key, value=coerced, repo_root=repo)
    except ValidationError as exc:
        emit_error(exc, flags=flags)
        return  # pragma: no cover
    except yaml.YAMLError as exc:
        emit_error(
            ValidationError(f"config layer is not valid YAML: {exc}"),
            flags=flags,
        )
        return  # pragma: no cover
    except OSError as exc:
        emit_error(
            UserError(f"cannot read or write {target_path}: {exc}", kind="InvalidInput"),
            flags=flags,
        )
        return  # pragma: no cover

    payload = {
        "key": entry.key,
        "value": coerced,
        "scope": scope,
        "path": str(target_path),
        "tab": entry.tab,
    }
    text = (
        f"menu: saved {entry.key} = {coerced!r}  "
        f"(tab: {entry.tab}, scope: {scope}, path: {target_path})"
    )
    emit_json_or_text(payload, text, flags=flags)


# Re-export the orjson dependency to keep import surface explicit when callers
# need the exact serialiser used by the CLI envelope. (Some tests stub stdout
# decoders against this.)
__all__ = [
    "_save_value_to_layer",
    "config_app",
    "orjson",
]


def _menu_registry_check() -> None:
    """Module-load contract: every registry entry's lookup round-trips by key.

    Cheap sanity check that the registry import + lookup helpers are wired.
    A KeyError here means the registry got out of sync with the menu's
    expectations and the module fails to import — loud, deterministic.
    """
    for entry in CONFIG_REGISTRY:
        assert registry_lookup(entry.key) is entry, f"registry_lookup mismatch for {entry.key!r}"


_menu_registry_check()
