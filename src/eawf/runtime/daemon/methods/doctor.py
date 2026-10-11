"""Daemon-owned application of digest-guarded Doctor repair plans."""

from __future__ import annotations

import hashlib
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.config.migration import migrate_config_file
from eawf.kernel.identity import EntityKind, format_qualified_urn
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.compaction import (
    document_rows,
    plan_legacy_seal,
    read_document,
    seal_legacy_import,
)
from eawf.kernel.store.ledger import ledger_snapshot
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.doctor.artifact_digests import (
    ARTIFACT_DIGEST_ACTION,
    artifact_digest_preview,
    digested_record,
    plan_artifact_digests,
)
from eawf.observability.doctor.receipt_rekey import (
    RECEIPT_REKEY_ACTION,
    plan_receipt_rekeys,
    receipt_rekey_preview,
    rekeyed_record,
)
from eawf.observability.doctor.repair import (
    ARCHIVE_REF_PREFIX,
    LEDGER_SUPERSEDE_ACTION,
    LEGACY_SEAL_ACTION,
    DoctorRepairAction,
    build_repair_plan,
    digest_repair_actions,
    ledger_supersession_digest,
    legacy_seal_preview,
    plan_ledger_supersessions,
    superseding_record,
)
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.gate_receipt_hygiene import scrub_gate_receipt_store
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register


class DoctorApplyParams(BaseModel):
    """Strict wire contract for ``doctor.apply_repair``."""

    model_config = ConfigDict(extra="forbid")

    repo_root: str
    preview_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    selected_preview_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    action_ids: list[str] = Field(default_factory=list)
    action_previews: dict[str, str] = Field(default_factory=dict)
    idempotency_key: str | None = None


class _CachedDoctorRepair(BaseModel):
    """One in-process replay row for a confirmed Doctor plan."""

    model_config = ConfigDict(extra="forbid")

    result: dict[str, Any]
    cached_at: float = Field(ge=0.0)


_DOCTOR_REPLAY_TTL_SECONDS = 60.0


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _config_backup_dir(workspace: Path, target: Path) -> Path:
    try:
        target.relative_to(workspace / ".ea")
    except ValueError:
        return target.parent / "backups"
    return workspace / ".ea" / "local" / "config-backups"


def _apply_config(workspace: Path, action: DoctorRepairAction) -> tuple[str, int]:
    if action.target is None:
        raise DaemonValidationError("config repair target missing")
    path = Path(action.target)
    if not path.is_file() or _digest_bytes(path.read_bytes()) != action.preview_digest:
        raise DaemonValidationError(f"doctor repair preview changed: {action.action_id!r}")
    _payload, changed, _backup = migrate_config_file(
        path,
        backup_dir=_config_backup_dir(workspace, path),
    )
    return ("applied" if changed else "noop"), int(changed)


def _apply_receipts(state_path: Path) -> tuple[str, int]:
    report = scrub_gate_receipt_store(state_path)
    return ("applied" if report.changed else "noop"), report.migrated_count


def _apply_sync(workspace: Path) -> tuple[str, int]:
    from eawf.platform.profiles.compose import compose
    from eawf.platform.profiles.loader import load_profile
    from eawf.platform.profiles.selection import resolve_enabled_profiles
    from eawf.platform.rules.render import refresh_rule_projections, rule_source_present
    from eawf.surfaces.render.agents_md import render_agents_md
    from eawf.surfaces.render.claude_shim import render_claude_md
    from eawf.surfaces.render.manifest import load, save_atomic

    if rule_source_present(workspace):
        # The rule render owns AGENTS.md and the CLAUDE.md shim here; the
        # profile renderer would overwrite both with the legacy layout.
        changed = len(refresh_rule_projections(workspace))
        return ("applied" if changed else "noop"), changed
    profiles = resolve_enabled_profiles(workspace)
    composed = compose([load_profile(name, workspace=workspace) for name in profiles])
    manifest_path = workspace / ".ea" / "indexes" / "generated.json"
    before = load(manifest_path)
    result, after = render_agents_md(
        composed,
        workspace / "AGENTS.md",
        before,
        generator="eawf-doctor",
    )
    save_atomic(manifest_path, after)
    render_claude_md(workspace / "CLAUDE.md")
    changed = len(result.regions_added) + len(result.regions_updated)
    return ("applied" if changed else "noop"), changed


def project_lock_urn(context: Epoch2RootContext) -> str:
    """Return the URN of the tree's one project, the lock a tree-wide append holds.

    Raises:
        DaemonValidationError: The document does not hold exactly one project.
    """
    authority = context.require_selected_generation()
    assert authority.target is not None and authority.generation_id is not None
    document = read_document(
        authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    )
    projects = sorted(document_rows(document, Epoch2Collection.PROJECT))
    if len(projects) != 1:
        raise DaemonValidationError(
            f"ledger repair needs one project row; the selected document holds {len(projects)}"
        )
    return format_qualified_urn(
        workspace_key=projects[0],
        project_key=projects[0],
        repository_key=None,
        kind=EntityKind.PROJECT,
        entity_key=projects[0],
        single_key=True,
    )


def _apply_ledger_supersede(
    ctx: MethodContext, workspace: Path, action: DoctorRepairAction
) -> tuple[str, int]:
    """Append the previewed superseding rows under the tree's session locks.

    The plan is taken again under the locks and must match the preview, so
    no row lands that the operator did not see. Each row is committed
    through the ledger-append path, so it is journalled and sequenced like
    any native line; a commit the rows declare unreachable is pinned under
    its archive ref first, so the declaration never names a ref that does
    not exist.

    Raises:
        DaemonValidationError: The tree is not epoch 2, the preview
            changed, or a planned row's ledger is not the selected
            generation's ledger of its collection.
    """
    authority = resolve_authority(workspace / ".ea")
    if authority.epoch != 2:
        raise DaemonValidationError("ledger repair needs an epoch-2 tree")
    context = ctx.native_root_context(authority.root)
    with context.session([project_lock_urn(context)]) as session:
        rows, _unrepairable = plan_ledger_supersessions(workspace)
        if ledger_supersession_digest(workspace, rows) != action.preview_digest:
            raise DaemonValidationError(
                f"doctor repair preview changed: {action.action_id!r}; rerun `eawf doctor --fix`"
            )
        recorded_at = datetime.now(UTC)
        records = [superseding_record(workspace, row, recorded_at=recorded_at) for row in rows]
        for row, record in zip(rows, records, strict=True):
            # The append path writes the selected generation's ledger of the
            # record's collection; a row read from any other file than that
            # ledger or its sealed snapshot would land somewhere other than
            # the line it supersedes.
            ledger = session.ledger_path(record.collection)
            snapshot = ledger_snapshot(ledger)
            source = (workspace / row.path).resolve()
            if source != ledger.resolve() and (snapshot is None or source != snapshot.resolve()):
                raise DaemonValidationError(
                    f"ledger repair cannot supersede {row.path!r}: it is not the selected "
                    f"generation's {record.collection.value} ledger"
                )
        for row in rows:
            for entry in row.unreachable:
                if entry.archive_ref.startswith(ARCHIVE_REF_PREFIX):
                    subprocess.run(
                        ["git", "update-ref", entry.archive_ref, entry.sha],
                        cwd=workspace,
                        capture_output=True,
                        check=True,
                        **no_window_kwargs(),
                    )
        for record in records:
            commit_ledger_append(session, record)
    return ("applied" if rows else "noop"), len(rows)


def _apply_artifact_digests(
    ctx: MethodContext, workspace: Path, action: DoctorRepairAction
) -> tuple[str, int]:
    """Append the previewed digest-carrying artifact rows through the ledger transaction.

    The plan is taken again under the session's lock and must match the
    preview, so no row lands that the operator did not see -- a cited file
    whose content moved since the preview changes the digest too. Each row
    is one committed ledger append, which only extends the ledger.

    Raises:
        DaemonValidationError: The tree is not epoch 2 or the preview changed.
    """
    authority = resolve_authority(workspace / ".ea")
    if authority.epoch != 2:
        raise DaemonValidationError("artifact digest repair needs an epoch-2 tree")
    context = ctx.native_root_context(authority.root)
    with context.session([project_lock_urn(context)]) as session:
        ledger = session.ledger_path(Epoch2Collection.ARTIFACT)
        plan = plan_artifact_digests(workspace, ledger)
        if artifact_digest_preview(ledger, plan) != action.preview_digest:
            raise DaemonValidationError(
                f"doctor repair preview changed: {action.action_id!r}; rerun `eawf doctor --fix`"
            )
        recorded_at = datetime.now(UTC)
        records = [digested_record(ledger, row, recorded_at=recorded_at) for row in plan.rows]
        for record in records:
            commit_ledger_append(session, record)
    return ("applied" if records else "noop"), len(records)


def _apply_receipt_rekey(
    ctx: MethodContext, workspace: Path, action: DoctorRepairAction
) -> tuple[str, int]:
    """Append the previewed re-keyed receipt rows through the ledger transaction.

    The plan is taken again under the session's lock and must match the
    preview, so no row lands that the operator did not see. Each row is one
    committed ledger append, which only extends the ledger.

    Raises:
        DaemonValidationError: The tree is not epoch 2 or the preview changed.
    """
    authority = resolve_authority(workspace / ".ea")
    if authority.epoch != 2:
        raise DaemonValidationError("receipt re-key repair needs an epoch-2 tree")
    context = ctx.native_root_context(authority.root)
    with context.session([project_lock_urn(context)]) as session:
        ledger = session.ledger_path(Epoch2Collection.RECEIPT)
        rows = plan_receipt_rekeys(ledger)
        if receipt_rekey_preview(ledger, rows) != action.preview_digest:
            raise DaemonValidationError(
                f"doctor repair preview changed: {action.action_id!r}; rerun `eawf doctor --fix`"
            )
        recorded_at = datetime.now(UTC)
        records = [rekeyed_record(ledger, row, recorded_at=recorded_at) for row in rows]
        for record in records:
            commit_ledger_append(session, record)
    return ("applied" if records else "noop"), len(records)


def _apply_legacy_seal(
    ctx: MethodContext, workspace: Path, action: DoctorRepairAction
) -> tuple[str, int]:
    """Seal the Task ledger's legacy import under the tree's session lock.

    The seal is measured again under the lock and must match the preview,
    whose digest covers the bytes it moves, so no line moves that the
    operator did not see. The seal adds no line and changes none: the
    ledger reads the same records, in the same order, before and after.

    Raises:
        DaemonValidationError: The tree is not epoch 2 or the preview changed.
    """
    authority = resolve_authority(workspace / ".ea")
    if authority.epoch != 2:
        raise DaemonValidationError("legacy ledger seal needs an epoch-2 tree")
    context = ctx.native_root_context(authority.root)
    with context.session([project_lock_urn(context)]) as session:
        plan = plan_legacy_seal(session.ledger_path(Epoch2Collection.TASK))
        if plan is None or legacy_seal_preview(plan) != action.preview_digest:
            raise DaemonValidationError(
                f"doctor repair preview changed: {action.action_id!r}; rerun `eawf doctor --fix`"
            )
        seal_legacy_import(session.document_path, collection=Epoch2Collection.TASK, plan=plan)
    return "applied", plan.sealed_lines


def _apply_action(
    ctx: MethodContext,
    workspace: Path,
    state_path: Path,
    action: DoctorRepairAction,
) -> tuple[str, int]:
    if action.action_id == LEGACY_SEAL_ACTION:
        return _apply_legacy_seal(ctx, workspace, action)
    if action.action_id == LEDGER_SUPERSEDE_ACTION:
        return _apply_ledger_supersede(ctx, workspace, action)
    if action.action_id == ARTIFACT_DIGEST_ACTION:
        return _apply_artifact_digests(ctx, workspace, action)
    if action.action_id == RECEIPT_REKEY_ACTION:
        return _apply_receipt_rekey(ctx, workspace, action)
    if action.action_id.startswith("config.normalize."):
        return _apply_config(workspace, action)
    if action.action_id == "receipts.scrub":
        return _apply_receipts(state_path)
    if action.action_id == "lifecycle.sync":
        return _apply_sync(workspace)
    raise DaemonValidationError(f"unsupported doctor repair action: {action.action_id!r}")


def _doctor_cache(ctx: MethodContext) -> dict[str, Any]:
    cache = ctx.idempotency_cache if isinstance(ctx.idempotency_cache, dict) else {}
    ctx.idempotency_cache = cache
    return cache


def _cached_doctor_result(
    cache: dict[str, Any],
    cache_key: str | None,
) -> dict[str, Any] | None:
    if cache_key is None:
        return None
    cached = cache.get(cache_key)
    if not isinstance(cached, _CachedDoctorRepair):
        return None
    if time.monotonic() - cached.cached_at <= _DOCTOR_REPLAY_TTL_SECONDS:
        return {**cached.result, "idempotent_replay": True}
    cache.pop(cache_key, None)
    return None


def _validated_selected_actions(
    parsed: DoctorApplyParams,
    current_actions: list[DoctorRepairAction],
) -> tuple[set[str], list[DoctorRepairAction]]:
    selected = set(parsed.action_ids)
    current_by_id = {action.action_id: action for action in current_actions}
    unknown = selected - set(current_by_id)
    if unknown:
        raise DaemonValidationError(f"doctor repair action unavailable: {sorted(unknown)!r}")
    selected_actions = [action for action in current_actions if action.action_id in selected]
    if digest_repair_actions(selected_actions) != parsed.selected_preview_digest:
        raise DaemonValidationError("doctor repair plan changed; rerun `eawf doctor --fix`")
    for action in selected_actions:
        expected = parsed.action_previews.get(action.action_id)
        if expected is None or action.preview_digest != expected:
            raise DaemonValidationError(
                f"doctor repair preview changed: {action.action_id!r}; rerun `eawf doctor --fix`"
            )
    return selected, selected_actions


@register("doctor.apply_repair")
async def apply_repair(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Apply selected non-service repair actions after digest revalidation."""
    parsed = DoctorApplyParams.model_validate(params)
    workspace = Path(parsed.repo_root).resolve()
    cache_key = (
        f"doctor:{workspace}:{parsed.idempotency_key}"
        if parsed.idempotency_key is not None
        else None
    )
    cache = _doctor_cache(ctx)
    replay = _cached_doctor_result(cache, cache_key)
    if replay is not None:
        return replay

    current = build_repair_plan(workspace)
    selected, _selected_actions = _validated_selected_actions(
        parsed,
        current.actions,
    )

    results: list[dict[str, Any]] = []
    state_path = workspace / ".ea" / "state.json"
    for action in current.actions:
        if action.action_id not in selected:
            continue
        if action.mutation_class in {"user_service", "user_process"}:
            continue
        status, records = _apply_action(ctx, workspace, state_path, action)
        results.append(
            {
                **action.model_dump(mode="json"),
                "status": status,
                "record_count": records,
            }
        )
    result = {
        "preview_digest": parsed.preview_digest,
        "actions": results,
        "applied_count": sum(row["status"] == "applied" for row in results),
        "idempotent_replay": False,
    }
    if cache_key is not None:
        cache[cache_key] = _CachedDoctorRepair(
            result=result,
            cached_at=time.monotonic(),
        )
    return result


__all__ = ["DoctorApplyParams", "apply_repair", "project_lock_urn"]
