"""Daemon-owned application of digest-guarded Doctor repair plans."""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.config.migration import migrate_config_file
from eawf.kernel.state.models import State
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.validate.strict import validate_state
from eawf.observability.doctor.repair import (
    DoctorRepairAction,
    build_repair_plan,
    digest_repair_actions,
)
from eawf.runtime.daemon.gate_receipt_hygiene import scrub_gate_receipt_store
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.lock import portalock
from eawf.workflow.lifecycle.legacy_audit import acknowledge_invalid_iter_audits
from eawf.workflow.lifecycle.repin_provenance import (
    append_commit_repin_provenance,
    complete_commit_repin_provenance,
)
from eawf.workflow.lifecycle.wave_sha import repair_commit_pins, scan_commit_pins


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


def _apply_audits(state_path: Path) -> tuple[str, int]:
    appended, _existing = acknowledge_invalid_iter_audits(
        state_path,
        reason=(
            "historical close predates the current audit-link contract; "
            "preserved without asserting a pass"
        ),
    )
    return ("applied" if appended else "noop"), appended


def _apply_pins(workspace: Path, state_path: Path) -> tuple[str, int]:
    with portalock.acquire(state_path, timeout=5.0):
        state = State.model_validate_json(state_path.read_bytes())
        issues = scan_commit_pins(state, repo_root=workspace)
        repaired, _skipped = repair_commit_pins(state, issues)
        if not repaired:
            completed = complete_commit_repin_provenance(state_path, state)
            return ("applied" if completed else "noop"), completed
        state.updated_at = datetime.now(UTC)
        payload = state.model_dump(mode="json")
        report = validate_state(payload, strict_optional=False)
        if report.state is None or report.violations:
            details = list(report.schema_errors[:3])
            details.extend(row.code for row in report.violations[:3])
            raise DaemonValidationError(
                "doctor commit repair failed validation: " + "; ".join(details)
            )
        append_commit_repin_provenance(
            state_path,
            repaired,
            status="planned",
        )
        atomic_write_json_locked(state_path, payload)
    append_commit_repin_provenance(
        state_path,
        repaired,
        status="applied",
    )
    return "applied", len(repaired)


def _apply_sync(workspace: Path) -> tuple[str, int]:
    from eawf.platform.profiles.compose import compose
    from eawf.platform.profiles.loader import load_profile
    from eawf.platform.profiles.selection import resolve_enabled_profiles
    from eawf.surfaces.render.agents_md import render_agents_md
    from eawf.surfaces.render.claude_shim import render_claude_md
    from eawf.surfaces.render.manifest import load, save_atomic

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


def _apply_action(
    workspace: Path,
    state_path: Path,
    action: DoctorRepairAction,
) -> tuple[str, int]:
    if action.action_id.startswith("config.normalize."):
        return _apply_config(workspace, action)
    if action.action_id == "receipts.scrub":
        return _apply_receipts(state_path)
    if action.action_id == "audits.acknowledge-legacy":
        return _apply_audits(state_path)
    if action.action_id == "commits.repin":
        return _apply_pins(workspace, state_path)
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
        if action.mutation_class == "user_service":
            continue
        status, records = _apply_action(workspace, state_path, action)
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


__all__ = ["DoctorApplyParams", "apply_repair"]
