"""Shared CLI/TUI client for applying a Doctor repair plan."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from eawf.observability.doctor.repair import (
    DoctorRepairPlan,
    build_repair_plan,
    digest_repair_actions,
)
from eawf.surfaces.cli._daemon_client import DaemonClient


def apply_repair_plan(plan: DoctorRepairPlan) -> dict[str, Any]:
    """Apply one confirmed plan through canonical writers.

    The full confirmed plan is revalidated first. User-service correction runs
    before daemon RPC so an installed pre-hotfix daemon is replaced before the
    new repair method is called. The daemon then revalidates the selected
    non-service action digest.
    """
    current = build_repair_plan(Path(plan.workspace))
    if current.preview_digest != plan.preview_digest:
        raise ValueError("doctor repair plan changed; rebuild preview")
    service_actions = [action for action in plan.actions if action.mutation_class == "user_service"]
    service_results: list[dict[str, Any]] = []
    if service_actions:
        from eawf.runtime.daemon.service_install import enable_service

        envelope = enable_service()
        service_results = [
            {
                **action.model_dump(mode="json"),
                "status": "applied",
                "record_count": 1,
                "service": asdict(envelope),
            }
            for action in service_actions
        ]

    action_ids = [
        action.action_id for action in plan.actions if action.mutation_class != "user_service"
    ]
    daemon_result: dict[str, Any] = {"actions": [], "applied_count": 0}
    if action_ids:
        selected_actions = [action for action in plan.actions if action.action_id in action_ids]
        with DaemonClient() as client:
            daemon_result = client.call(
                "doctor.apply_repair",
                {
                    "repo_root": plan.workspace,
                    "preview_digest": plan.preview_digest,
                    "selected_preview_digest": digest_repair_actions(selected_actions),
                    "action_ids": action_ids,
                    "action_previews": {
                        action.action_id: action.preview_digest
                        for action in plan.actions
                        if action.action_id in action_ids
                    },
                },
                idempotency_key=f"doctor-fix-{plan.preview_digest.removeprefix('sha256:')}",
            )
    actions = [*daemon_result.get("actions", []), *service_results]
    return {
        "status": "applied",
        "preview_digest": plan.preview_digest,
        "applied_count": sum(row.get("status") == "applied" for row in actions),
        "actions": actions,
    }


__all__ = ["apply_repair_plan"]
