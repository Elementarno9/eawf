"""``eawf sync`` Typer command — re-render managed assets, report drift.

Surface contract::

    eawf sync                        # re-render in place; exit 0
    eawf sync --dry-run              # show what would change; never write; exit 0
    eawf sync --check                # check-only; exit 4 on any planned change

The command rebuilds the managed-region content of ``AGENTS.md`` (composed
from the profiles enabled in ``.ea/config.yaml``) and the ``CLAUDE.md`` shim,
plus the manifest at ``.ea/indexes/generated.json``. Hand-written content
*outside* managed regions is preserved by :func:`eawf.surfaces.render.agents_md.render_agents_md`.

W03 also regenerates Markdown projections of ``memory.jsonl`` under
``.ea/artifacts/rendered/memory/<scope>.md`` (plus ``_all.md`` union view).
The whole projection lives inside one managed region per file so re-running
sync overwrites curated bytes — memory content is curated in
``memory.jsonl``, not the views.

Implementation note (``--dry-run`` / ``--check``)
-------------------------------------------------

To avoid duplicating the AGENTS.md renderer, the dry-run / check paths execute
the renderer against a **shadow tree** under :func:`tempfile.TemporaryDirectory`
that mirrors the on-disk layout (``<shadow>/AGENTS.md``,
``<shadow>/CLAUDE.md``, ``<shadow>/.ea/indexes/generated.json``). The shadow
tree is seeded with the existing target bytes (so unmanaged content round-trips
identically) and the existing manifest. After the renderer runs we compare:

- ``<target>/AGENTS.md`` vs. ``<shadow>/AGENTS.md`` (byte equality);
- ``<target>/CLAUDE.md`` vs. ``<shadow>/CLAUDE.md`` (byte equality);
- the rendered :class:`~eawf.surfaces.render.agents_md.RenderResult` carries the
  per-region ``regions_added/updated/unchanged`` lists used to populate the
  envelope (so the report is identical between dry-run and write paths).

The shadow approach trades a tiny copy + temp-dir creation for not having to
refactor the renderer's signature. The renderer already writes atomically and
acquires a portalock on its target, so executing it against a shadow path has
no observable effect on the real workspace.

Exit codes:

- ``0`` — sync completed (write path) or report rendered (dry-run/check, no drift).
- ``3`` (``INVALID_INPUT``) — ``.ea/config.yaml`` missing or malformed,
  enabled-profile typo.
- ``4`` (``VALIDATION_FAILED``) — ``--check`` and the renderer would have
  emitted any region update/add (the spec exit code for "drift detected").
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import orjson
import typer

from eawf.kernel.state.enums import StoreKind
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

if TYPE_CHECKING:
    from eawf.kernel.state.models import State
    from eawf.surfaces.render.agents_md import RenderResult
    from eawf.surfaces.render.manifest import Manifest

logger = logging.getLogger(__name__)


_AGENTS_MD: str = "AGENTS.md"
_CLAUDE_MD: str = "CLAUDE.md"
_MANIFEST_RELPATH: str = ".ea/indexes/generated.json"
_STATE_RELPATH: str = ".ea/state.json"
_MEMORY_VIEWS_RELDIR: str = ".ea/artifacts/rendered/memory"


def _load_state_or_none(state_path: Path) -> State | None:
    """Best-effort state load; returns None when the workspace has no ``state.json``.

    Sync runs against bare directories at ``init`` time — the memory view
    pipeline must degrade gracefully when there is no state yet rather than
    explode. Validation errors propagate to the caller as
    :class:`cli_errors.UserError` (``kind="InvalidInput"``) so the existing
    error envelope still fires.
    """
    from eawf.kernel.validate.strict import validate_state

    if not state_path.exists():
        return None
    payload = orjson.loads(state_path.read_bytes())
    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        raise cli_errors.UserError(
            f"state schema invalid: {'; '.join(report.schema_errors[:3])}", kind="InvalidInput"
        )
    return report.state


def _seed_state_for_shadow(target: Path, shadow: Path) -> None:
    """Copy ``.ea/state.json`` and ``store/memory.jsonl`` into *shadow*.

    The memory-view renderer reads both. The shadow tree is sparse — only the
    files we actually consume need to be mirrored.
    """
    from eawf.kernel.store.paths import store_path

    src_state = target / _STATE_RELPATH
    if src_state.exists():
        dst_state = shadow / _STATE_RELPATH
        dst_state.parent.mkdir(parents=True, exist_ok=True)
        dst_state.write_bytes(src_state.read_bytes())
    src_memory = store_path(src_state, StoreKind.MEMORY)
    if src_memory.exists():
        rel = src_memory.relative_to(target)
        dst_memory = shadow / rel
        dst_memory.parent.mkdir(parents=True, exist_ok=True)
        dst_memory.write_bytes(src_memory.read_bytes())


def _render_memory_views(*, target_root: Path, write: bool) -> list[Path]:
    """Render ``<state_dir>/artifacts/rendered/memory/<scope>.md`` files.

    Returns the sorted list of view paths (or paths that *would* be written
    when ``write=False``). When the workspace has no ``.ea/state.json`` (e.g.
    the bare-directory init path) the function returns an empty list.
    """
    from eawf.kernel.store.paths import store_path
    from eawf.platform.memory.markdown_view import render_all_views

    state_path = target_root / _STATE_RELPATH
    state = _load_state_or_none(state_path)
    if state is None:
        return []
    memory_path = store_path(state_path, StoreKind.MEMORY)
    output_dir = target_root / _MEMORY_VIEWS_RELDIR
    return render_all_views(
        state=state,
        memory_path=memory_path,
        output_dir=output_dir,
        write=write,
    )


def _detect_memory_view_changes(target_root: Path, shadow_root: Path) -> list[str]:
    """Byte-diff each ``<scope>.md`` under the memory-views directory.

    Returns the list of relative paths whose bytes differ between *shadow*
    (the freshly-rendered candidate) and *target* (the on-disk file). A
    missing target counts as a change. A missing shadow path means the view
    would be removed (returned with the same path so the caller can surface
    drift).
    """
    rel = _MEMORY_VIEWS_RELDIR
    target_dir = target_root / rel
    shadow_dir = shadow_root / rel
    target_files = {p.relative_to(target_root): p for p in target_dir.glob("*.md")}
    shadow_files = {p.relative_to(shadow_root): p for p in shadow_dir.glob("*.md")}
    changed: list[str] = []
    for relpath in sorted(set(target_files) | set(shadow_files)):
        tpath = target_root / relpath
        spath = shadow_root / relpath
        if not spath.exists():
            changed.append(str(relpath))
            continue
        if not tpath.exists():
            changed.append(str(relpath))
            continue
        if tpath.read_bytes() != spath.read_bytes():
            changed.append(str(relpath))
    return changed


def _resolve_enabled_profiles(target: Path) -> list[str]:
    """Read ``profiles.enabled`` from the layered config rooted at *target*.

    Args:
        target: Workspace root. ``.ea/config.yaml`` is read via
            :func:`eawf.kernel.config.layered.merge_config` so the return value
            honours the full layer stack (built-in → global → workspace →
            repo → local → env). For the sync surface we treat *target* as
            the repo anchor.

    Returns:
        The ``profiles.enabled`` list, validated as a list of strings. Empty
        list when the section is absent (the renderer then composes nothing —
        which is a legitimate "no managed AGENTS.md regions" outcome).

    Raises:
        UserError: ``profiles.enabled`` exists but is not a list of
            strings, or the layered merge raises (malformed YAML in any
            layer).
    """
    from eawf.kernel.config.layered import merge_config
    from eawf.platform.profiles.loader import list_profiles

    try:
        merged, _sources = merge_config(repo=target, workspace=target)
    except Exception as exc:
        raise cli_errors.UserError(
            f"layered config merge failed: {exc}", kind="InvalidInput"
        ) from exc

    profiles_section = merged.get("profiles") if isinstance(merged, dict) else None
    if not isinstance(profiles_section, dict):
        return []
    raw_enabled = profiles_section.get("enabled")
    if raw_enabled is None:
        return []
    if not isinstance(raw_enabled, list):
        raise cli_errors.UserError(
            f"profiles.enabled must be a list of profile ids; got {type(raw_enabled).__name__}",
            kind="InvalidInput",
        )
    enabled = [p for p in raw_enabled if isinstance(p, str)]
    if len(enabled) != len(raw_enabled):
        raise cli_errors.UserError(
            "profiles.enabled entries must all be strings", kind="InvalidInput"
        )
    known = set(list_profiles())
    unknown = sorted(set(enabled) - known)
    if unknown:
        raise cli_errors.UserError(
            f"unknown profile(s) in profiles.enabled: {unknown}; choose from {sorted(known)}",
            kind="InvalidInput",
        )
    return enabled


def _seed_shadow(
    target: Path,
    shadow: Path,
    manifest_relpath: str,
) -> None:
    """Mirror the existing AGENTS.md / CLAUDE.md / manifest under *shadow*.

    Files that do not exist at *target* are simply absent in *shadow* — the
    renderer creates them on first call, exactly as it would at the real path.
    """
    for name in (_AGENTS_MD, _CLAUDE_MD):
        src = target / name
        if src.exists():
            (shadow / name).write_bytes(src.read_bytes())
    src_manifest = target / manifest_relpath
    if src_manifest.exists():
        dst_manifest = shadow / manifest_relpath
        dst_manifest.parent.mkdir(parents=True, exist_ok=True)
        dst_manifest.write_bytes(src_manifest.read_bytes())


def _render_into(
    *,
    target_root: Path,
    enabled_profiles: list[str],
    generator: str,
    write_manifest: bool,
) -> tuple[RenderResult, Manifest, Manifest]:
    """Run the AGENTS.md + CLAUDE.md + manifest pipeline against *target_root*.

    Returns:
        ``(agents_result, manifest_before, manifest_after)`` — the renderer
        result (carries the per-region delta lists), the manifest snapshot
        loaded *before* rendering (for ``regions_added/updated`` diffing),
        and the manifest snapshot returned by
        :func:`eawf.surfaces.render.agents_md.render_agents_md`. When *write_manifest*
        is True the after-manifest is also persisted via
        :func:`eawf.surfaces.render.manifest.save_atomic`.
    """
    from eawf.platform.profiles.compose import compose
    from eawf.platform.profiles.loader import load_profile
    from eawf.surfaces.render.agents_md import render_agents_md
    from eawf.surfaces.render.claude_shim import render_claude_md
    from eawf.surfaces.render.manifest import load as load_manifest
    from eawf.surfaces.render.manifest import save_atomic as save_manifest_atomic

    composed = compose([load_profile(p) for p in enabled_profiles])
    agents_md_path = (target_root / _AGENTS_MD).resolve()
    claude_md_path = (target_root / _CLAUDE_MD).resolve()
    manifest_path = (target_root / _MANIFEST_RELPATH).resolve()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    manifest_before = load_manifest(manifest_path)
    agents_result, manifest_after = render_agents_md(
        composed,
        agents_md_path,
        manifest_before,
        generator=generator,
    )
    if write_manifest:
        save_manifest_atomic(manifest_path, manifest_after)
    render_claude_md(claude_md_path)
    return agents_result, manifest_before, manifest_after


def _detect_changes(
    *,
    target: Path,
    shadow: Path,
    agents_result: RenderResult,
    manifest_relpath: str,
) -> dict[str, object]:
    """Compute the per-target byte-equality + per-region change report.

    Returns:
        ``{"agents_md_changed": bool, "claude_md_changed": bool,
           "manifest_changed": bool, "regions_added": list[str],
           "regions_updated": list[str], "regions_unchanged": list[str]}``.
    """
    targets_changed: dict[str, bool] = {}
    for name in (_AGENTS_MD, _CLAUDE_MD):
        src = target / name
        dst = shadow / name
        if not dst.exists():
            targets_changed[name] = src.exists()
            continue
        targets_changed[name] = not src.exists() or src.read_bytes() != dst.read_bytes()
    src_manifest = target / manifest_relpath
    dst_manifest = shadow / manifest_relpath
    if not dst_manifest.exists():
        manifest_changed = src_manifest.exists()
    else:
        manifest_changed = (
            not src_manifest.exists() or src_manifest.read_bytes() != dst_manifest.read_bytes()
        )
    return {
        "agents_md_changed": targets_changed[_AGENTS_MD],
        "claude_md_changed": targets_changed[_CLAUDE_MD],
        "manifest_changed": manifest_changed,
        "regions_added": list(agents_result.regions_added),
        "regions_updated": list(agents_result.regions_updated),
        "regions_unchanged": list(agents_result.regions_unchanged),
    }


def _build_payload(
    *,
    target: Path,
    enabled_profiles: list[str],
    report: dict[str, object],
    mode: str,
) -> dict[str, object]:
    """Render the JSON envelope payload + a one-line text body for emission."""
    return {
        "target": str(target),
        "mode": mode,
        "profiles_enabled": enabled_profiles,
        "agents_md_changed": report["agents_md_changed"],
        "claude_md_changed": report["claude_md_changed"],
        "manifest_changed": report["manifest_changed"],
        "regions_added": report["regions_added"],
        "regions_updated": report["regions_updated"],
        "regions_unchanged": report["regions_unchanged"],
        "memory_views_regenerated": report.get("memory_views_regenerated", []),
        "memory_views_changed": report.get("memory_views_changed", []),
    }


def _format_text(payload: dict[str, object]) -> str:
    """One-line summary suited for the text branch of :func:`emit_json_or_text`.

    The payload comes from :func:`_build_payload`, which always populates the
    list-typed fields with concrete ``list[str]`` values; the dict is typed
    as ``dict[str, object]`` to keep the JSON-serialisable contract loose at
    the boundary, so we round-trip through ``repr`` for the formatting line —
    safe because ``list[str].__repr__`` is byte-stable across runs.
    """
    return (
        f"eawf sync ({payload['mode']!r}): "
        f"target={payload['target']!r} "
        f"profiles={payload['profiles_enabled']!r} "
        f"added={payload['regions_added']!r} "
        f"updated={payload['regions_updated']!r} "
        f"unchanged={payload['regions_unchanged']!r} "
        f"agents_md_changed={payload['agents_md_changed']} "
        f"claude_md_changed={payload['claude_md_changed']} "
        f"manifest_changed={payload['manifest_changed']}"
    )


def sync_cmd(
    ctx: typer.Context,
    target: Annotated[
        Path | None,
        typer.Option(
            "--target",
            help="Workspace root (defaults to the current working directory).",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show what would change without writing anything.",
        ),
    ] = False,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Exit 4 if any managed region would be added/updated.",
        ),
    ] = False,
) -> None:
    """Re-render managed assets and report drift.

    Behaviour matrix:

    +-------------+-------------+--------------------------------------------+
    | --dry-run   | --check     | Effect                                     |
    +=============+=============+============================================+
    | False       | False       | Re-render in place, persist manifest.      |
    +-------------+-------------+--------------------------------------------+
    | True        | False       | Compute report; never write; exit 0.       |
    +-------------+-------------+--------------------------------------------+
    | False       | True        | Compute report; never write;               |
    |             |             | exit 4 if any region added/updated.        |
    +-------------+-------------+--------------------------------------------+
    | True        | True        | Same as --check (no write, exit 4 on drift)|
    +-------------+-------------+--------------------------------------------+

    The detection is hash-stable: re-running ``eawf sync`` after a successful
    write reports zero updates (all regions land in ``regions_unchanged``).
    """
    flags: GlobalFlags = ctx.obj
    target_dir = (target or flags.workspace or Path.cwd()).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        enabled_profiles = _resolve_enabled_profiles(target_dir)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    if dry_run or check:
        # Shadow-tree path. Mirror existing files into a tempdir, run the
        # renderer there, then diff bytes back into the real workspace.
        with tempfile.TemporaryDirectory() as tmp_root:
            shadow = Path(tmp_root)
            _seed_shadow(target_dir, shadow, _MANIFEST_RELPATH)
            _seed_state_for_shadow(target_dir, shadow)
            try:
                agents_result, _before, _after = _render_into(
                    target_root=shadow,
                    enabled_profiles=enabled_profiles,
                    generator="eawf-sync",
                    write_manifest=True,  # in shadow, not the real workspace
                )
            except cli_errors.CliError as exc:
                cli_errors.emit_error(exc, flags=flags)
                return
            try:
                shadow_view_paths = _render_memory_views(target_root=shadow, write=True)
            except cli_errors.CliError as exc:
                cli_errors.emit_error(exc, flags=flags)
                return
            memory_views_drift = _detect_memory_view_changes(target_dir, shadow)
            report = _detect_changes(
                target=target_dir,
                shadow=shadow,
                agents_result=agents_result,
                manifest_relpath=_MANIFEST_RELPATH,
            )
            # Project view paths against the real workspace so the JSON
            # surface is independent of the shadow tempdir.
            target_view_paths = sorted(
                str(target_dir / p.relative_to(shadow)) for p in shadow_view_paths
            )
            report["memory_views_regenerated"] = target_view_paths
            report["memory_views_changed"] = memory_views_drift
        mode = "check" if check else "dry-run"
        payload = _build_payload(
            target=target_dir,
            enabled_profiles=enabled_profiles,
            report=report,
            mode=mode,
        )
        emit_json_or_text(payload, _format_text(payload), flags=flags)
        if check:
            # Spec: exit 4 (VALIDATION_FAILED) when sync would emit any
            # added/updated region OR any memory view would change. Unchanged
            # regions and unchanged views are not drift.
            any_drift = (
                bool(report["regions_added"])
                or bool(report["regions_updated"])
                or bool(report["memory_views_changed"])
            )
            if any_drift:
                raise typer.Exit(code=4)
        return

    # Default path — re-render in place.
    try:
        agents_result, _before, _after = _render_into(
            target_root=target_dir,
            enabled_profiles=enabled_profiles,
            generator="eawf-sync",
            write_manifest=True,
        )
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    try:
        view_paths = _render_memory_views(target_root=target_dir, write=True)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    report = {
        "agents_md_changed": bool(agents_result.regions_added or agents_result.regions_updated),
        "claude_md_changed": False,  # CLAUDE.md is a constant payload; bytes never drift.
        "manifest_changed": True,  # manifest is rewritten every call (timestamp moves).
        "regions_added": list(agents_result.regions_added),
        "regions_updated": list(agents_result.regions_updated),
        "regions_unchanged": list(agents_result.regions_unchanged),
        "memory_views_regenerated": [str(p) for p in view_paths],
        "memory_views_changed": [],
    }
    payload = _build_payload(
        target=target_dir,
        enabled_profiles=enabled_profiles,
        report=report,
        mode="write",
    )
    emit_json_or_text(payload, _format_text(payload), flags=flags)
    # Post-render byte-cap check: the doctor check owns the blocking verdict, but
    # sync is where AGENTS.md is (re)written, so measure the fresh file here too.
    # Codex silently truncates a project doc past its byte cap, dropping the
    # guidance tail; recording the outcome at render time (WARNING when over)
    # gives the operator a trail without a separate `eawf doctor` run. Logged,
    # not echoed, so the ``--json`` stdout envelope stays a single clean object.
    from eawf.observability.doctor.checks import check_agents_md_byte_cap

    byte_cap = check_agents_md_byte_cap(workspace=target_dir)
    if byte_cap.status == "fail":
        logger.warning(f"sync_cmd agents_md_over_cap detail={byte_cap.detail!r}")
    logger.info(
        f"sync_cmd target={target_dir} profiles={enabled_profiles} "
        f"added={report['regions_added']} updated={report['regions_updated']} "
        f"unchanged={report['regions_unchanged']} "
        f"memory_views_regenerated={report['memory_views_regenerated']} "
        f"agents_md_byte_cap={byte_cap.status}"
    )


__all__ = ["sync_cmd"]
