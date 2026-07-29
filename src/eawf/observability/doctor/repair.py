"""Typed, digest-guarded repair planning for ``eawf doctor --fix``."""

from __future__ import annotations

import hashlib
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

import orjson
import yaml
from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.config.layered import global_config_path
from eawf.kernel.config.migration import migrate_config_payload
from eawf.kernel.state.enums import AuditKind, IterStatus, StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.gate_receipt import GateReceipt, LegacyGateReceipt
from eawf.kernel.store.paths import store_path
from eawf.observability.doctor.checks import (
    check_launchd_agent,
    check_manifest_in_sync,
)
from eawf.workflow.lifecycle._audit_acceptance import (
    ITER_CLOSE_AUDIT_CHECK_ORDER,
    assess_close_audit,
)
from eawf.workflow.lifecycle.legacy_audit import (
    disposition_matches,
    load_legacy_audit_dispositions,
)
from eawf.workflow.lifecycle.wave_sha import CommitPinIssue, scan_commit_pins

DoctorRepairMutationClass = Literal[
    "local_diagnostics",
    "committed_config",
    "committed_state",
    "committed_store",
    "managed_rules",
    "user_service",
]
DoctorRepairStatus = Literal[
    "planned",
    "applied",
    "noop",
    "skipped",
    "conflict",
    "failed",
]


class DoctorRepairAction(BaseModel):
    """One immutable repair preview row."""

    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    preview_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    mutation_class: DoctorRepairMutationClass
    record_count: int = Field(ge=0)
    restart_required: bool = False
    status: DoctorRepairStatus = "planned"
    detail: str = Field(min_length=1)
    target: str | None = None


class DoctorRepairPlan(BaseModel):
    """Shared CLI/TUI repair plan."""

    model_config = ConfigDict(extra="forbid")

    workspace: str
    preview_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    actions: list[DoctorRepairAction] = Field(default_factory=list)
    unresolved_findings: list[str] = Field(default_factory=list)
    status: Literal["ready", "healthy", "needs_user"] = "ready"
    rerun_command: str = "eawf doctor --fix --yes"


def _digest_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _digest_json(value: object) -> str:
    return _digest_bytes(orjson.dumps(value, option=orjson.OPT_SORT_KEYS))


def digest_repair_actions(actions: list[DoctorRepairAction]) -> str:
    """Return the canonical aggregate digest for ordered repair actions."""
    return _digest_json([action.model_dump(mode="json") for action in actions])


def _current_branch(workspace: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=workspace,
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    branch = result.stdout.strip()
    return branch or None


def _config_paths(workspace: Path) -> list[tuple[str, Path]]:
    candidates: list[tuple[str, Path]] = [
        ("user", global_config_path()),
        ("workspace", workspace / ".ea" / "config.yaml"),
        ("local", workspace / ".ea" / "local" / "config.yaml"),
    ]
    branch = _current_branch(workspace)
    if branch:
        candidates.append(("branch", workspace / ".ea" / "branches" / f"{branch}.yaml"))
    candidates.extend(
        ("branch", path) for path in sorted((workspace / ".ea" / "branches").glob("**/*.yaml"))
    )
    seen: set[Path] = set()
    result: list[tuple[str, Path]] = []
    for scope, path in candidates:
        resolved = path.expanduser().resolve()
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        result.append((scope, resolved))
    return result


def _config_actions(workspace: Path) -> list[DoctorRepairAction]:
    actions: list[DoctorRepairAction] = []
    for index, (scope, path) in enumerate(_config_paths(workspace), start=1):
        raw = path.read_bytes()
        parsed = yaml.safe_load(raw)
        if not isinstance(parsed, dict):
            continue
        _upgraded, changed = migrate_config_payload(parsed)
        if not changed:
            continue
        actions.append(
            DoctorRepairAction(
                action_id=f"config.normalize.{index}",
                scope=scope,
                preview_digest=_digest_bytes(raw),
                mutation_class="committed_config",
                record_count=1,
                detail=f"normalize deprecated configuration leaves in {scope} layer",
                target=str(path),
            )
        )
    return actions


def _receipt_action(workspace: Path, state_path: Path) -> DoctorRepairAction | None:
    path = store_path(state_path, StoreKind.GATE_RECEIPT)
    if not path.is_file():
        return None
    raw = path.read_bytes()
    legacy_markers = (
        b'"argv"',
        b'"command"',
        b'"details"',
        b'"stdout_tail"',
        b'"stderr_tail"',
        b'"full_log_ref"',
    )
    if not any(marker in raw for marker in legacy_markers):
        return None
    records = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            envelope = Envelope.model_validate(orjson.loads(line))
        except ValueError:
            continue
        try:
            GateReceipt.model_validate(envelope.payload)
            continue
        except ValueError:
            pass
        try:
            LegacyGateReceipt.model_validate(envelope.payload)
        except ValueError:
            continue
        records += 1
    return DoctorRepairAction(
        action_id="receipts.scrub",
        scope="repo",
        preview_digest=_digest_bytes(raw),
        mutation_class="committed_store",
        record_count=records,
        detail="move raw gate diagnostics local and rewrite minimal receipts",
        target=str(path),
    )


def _invalid_audit_count(state_path: Path, state: State) -> int:
    dispositions = load_legacy_audit_dispositions(state_path)
    invalid = 0
    for iter_id, iter_row in state.iters.items():
        if iter_row.status is not IterStatus.CLOSED:
            continue
        assessment = assess_close_audit(
            state,
            audit_id=iter_row.audit_id,
            allowed_scope_ids=frozenset({iter_id}),
            required_kind=AuditKind.EVALUATION,
            check_order=ITER_CLOSE_AUDIT_CHECK_ORDER,
            require_passing_check=True,
        )
        if assessment.issue is None:
            continue
        if disposition_matches(
            dispositions,
            iter_id=iter_id,
            audit_id=iter_row.audit_id,
            issue=assessment.issue.value,
        ):
            continue
        invalid += 1
    return invalid


def _audit_action(state_path: Path, state: State) -> DoctorRepairAction | None:
    count = _invalid_audit_count(state_path, state)
    if not count:
        return None
    disposition_path = store_path(state_path, StoreKind.LEGACY_AUDIT_DISPOSITION)
    input_bytes = state_path.read_bytes()
    if disposition_path.is_file():
        input_bytes += disposition_path.read_bytes()
    return DoctorRepairAction(
        action_id="audits.acknowledge-legacy",
        scope="repo",
        preview_digest=_digest_bytes(input_bytes),
        mutation_class="committed_store",
        record_count=count,
        detail="append unverified legacy dispositions without changing audit verdicts",
        target=str(disposition_path),
    )


def _pin_action(
    workspace: Path,
    state_path: Path,
    issues: list[CommitPinIssue],
) -> DoctorRepairAction | None:
    repairable = [issue for issue in issues if issue.repairable]
    if not repairable:
        return None
    preview = [
        {
            "wave_id": issue.wave_id,
            "old": issue.state_commit,
            "new": issue.git_commit,
            "identity": issue.git_identity_digest,
            "basis": issue.repair_basis,
        }
        for issue in repairable
    ]
    basis_counts: dict[str, int] = {}
    for issue in repairable:
        basis = issue.repair_basis or "unknown"
        basis_counts[basis] = basis_counts.get(basis, 0) + 1
    basis_summary = ", ".join(f"{basis}={count}" for basis, count in sorted(basis_counts.items()))
    return DoctorRepairAction(
        action_id="commits.repin",
        scope="repo",
        preview_digest=_digest_json(
            {"state": _digest_bytes(state_path.read_bytes()), "repairs": preview}
        ),
        mutation_class="committed_state",
        record_count=len(repairable),
        detail=(f"re-pin uniquely resolved first-parent successors; {basis_summary}"),
        target=str(state_path),
    )


def _desired_sync_drift(workspace: Path) -> bool:
    """Return whether current config/profile composition would change rules."""
    from eawf.platform.profiles.compose import compose
    from eawf.platform.profiles.loader import load_profile
    from eawf.platform.profiles.selection import resolve_enabled_profiles
    from eawf.surfaces.render.agents_md import render_agents_md
    from eawf.surfaces.render.claude_shim import render_claude_md
    from eawf.surfaces.render.manifest import load, save_atomic

    enabled = resolve_enabled_profiles(workspace)
    composed = compose([load_profile(profile_id, workspace=workspace) for profile_id in enabled])
    with tempfile.TemporaryDirectory() as tmp:
        shadow = Path(tmp)
        for relative in ("AGENTS.md", "CLAUDE.md", ".ea/indexes/generated.json"):
            source = workspace / relative
            if source.is_file():
                target = shadow / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
        manifest_path = shadow / ".ea" / "indexes" / "generated.json"
        before = load(manifest_path)
        _result, after = render_agents_md(
            composed,
            shadow / "AGENTS.md",
            before,
            generator="eawf-doctor-preview",
        )
        save_atomic(manifest_path, after)
        render_claude_md(shadow / "CLAUDE.md")
        for relative in ("AGENTS.md", "CLAUDE.md"):
            source = workspace / relative
            target = shadow / relative
            if not source.is_file() or not target.is_file():
                return source.is_file() != target.is_file()
            if source.read_bytes() != target.read_bytes():
                return True
    return False


def _sync_action(workspace: Path) -> DoctorRepairAction | None:
    check = check_manifest_in_sync(workspace=workspace)
    if check.status == "ok" and not _desired_sync_drift(workspace):
        return None
    inputs: list[tuple[str, str | None]] = []
    for relative in (
        "AGENTS.md",
        "CLAUDE.md",
        ".ea/config.yaml",
        ".ea/indexes/generated.json",
        ".ea/state.json",
        ".ea/store/memory.jsonl",
    ):
        path = workspace / relative
        inputs.append((relative, _digest_bytes(path.read_bytes()) if path.is_file() else None))
    for scope, path in _config_paths(workspace):
        inputs.append(
            (
                f"config:{scope}:{path.name}",
                _digest_bytes(path.read_bytes()),
            )
        )
    from eawf.platform.profiles.discovery import (
        user_profiles_dir,
        workspace_profiles_dir,
    )

    for scope, root in (
        ("profile:user", user_profiles_dir()),
        ("profile:workspace", workspace_profiles_dir(workspace)),
    ):
        for path in sorted(root.glob("*.yaml")):
            inputs.append((f"{scope}:{path.name}", _digest_bytes(path.read_bytes())))
    return DoctorRepairAction(
        action_id="lifecycle.sync",
        scope="repo",
        preview_digest=_digest_json(inputs),
        mutation_class="managed_rules",
        record_count=1,
        detail="re-render managed lifecycle blocks and profile overlays",
        target=str(workspace),
    )


def _service_action() -> DoctorRepairAction | None:
    check = check_launchd_agent()
    if check.status == "ok":
        return None
    return DoctorRepairAction(
        action_id="daemon.rerender-restart",
        scope="user",
        preview_digest=_digest_json(
            {"name": check.name, "status": check.status, "detail": check.detail}
        ),
        mutation_class="user_service",
        record_count=1,
        restart_required=True,
        detail="re-render and restart stale daemon service definition",
    )


def build_repair_plan(workspace: Path) -> DoctorRepairPlan:
    """Build the shared read-only repair preview for one managed workspace."""
    root = workspace.resolve()
    state_path = root / ".ea" / "state.json"
    actions = _config_actions(root)
    unresolved: list[str] = []
    if state_path.is_file():
        state = State.model_validate_json(state_path.read_bytes())
        pin_issues = scan_commit_pins(state, repo_root=root)
        unresolved_pins = [issue for issue in pin_issues if not issue.repairable]
        if unresolved_pins:
            unresolved.append(f"commits.unresolved={len(unresolved_pins)}")
        for action in (
            _receipt_action(root, state_path),
            _audit_action(state_path, state),
            _pin_action(root, state_path, pin_issues),
        ):
            if action is not None:
                actions.append(action)
    for action in (_sync_action(root), _service_action()):
        if action is not None:
            actions.append(action)
    digest = digest_repair_actions(actions)
    status: Literal["ready", "healthy", "needs_user"]
    if actions:
        status = "ready"
    else:
        from eawf.observability.doctor.checks import run_all
        from eawf.observability.doctor.report import overall_status

        status = (
            "healthy"
            if not unresolved and overall_status(run_all(workspace=root)) == "ok"
            else "needs_user"
        )
    return DoctorRepairPlan(
        workspace=str(root),
        preview_digest=digest,
        actions=actions,
        unresolved_findings=unresolved,
        status=status,
        rerun_command=(f"eawf --workspace {shlex.quote(str(root))} doctor --fix --yes"),
    )


def render_repair_plan(plan: DoctorRepairPlan) -> str:
    """Render the shared CLI/TUI repair preview."""
    if not plan.actions:
        if plan.status == "healthy":
            return "doctor repair: no actions needed; Doctor is healthy"
        detail = ", ".join(plan.unresolved_findings) or "manual diagnosis required"
        return f"doctor repair: no automatic actions available; {detail}"
    lines = [f"doctor repair preview: {len(plan.actions)} action(s)"]
    for action in plan.actions:
        restart = " · restart required" if action.restart_required else ""
        lines.append(
            f"{action.action_id} · {action.scope} · "
            f"{action.mutation_class} · {action.record_count} record(s){restart}"
        )
        lines.append(f"  {action.detail}")
    return "\n".join(lines)


__all__ = [
    "DoctorRepairAction",
    "DoctorRepairMutationClass",
    "DoctorRepairPlan",
    "DoctorRepairStatus",
    "build_repair_plan",
    "digest_repair_actions",
    "render_repair_plan",
]
