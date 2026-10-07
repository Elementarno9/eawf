"""Build the dispatch request for one Task, so the daemon never takes one from a caller.

A dispatch carries a compile request, provider documents, bindings, a
capsule and a prompt. Every one of them is derived here from the Task and
the installed facts, which is what keeps an operator from handing a worker
authority the Task never declared: the write set is the Task's write
claims, the criteria digest is taken over the Task's own criteria, and the
provider documents are the ``runtime`` mapping of each config layer.

The request is compiled once here, because the workspace lease must
outlive the compiled wall-clock limit and that limit is only known after
the least-authority merge. A request the compiler would refuse is
therefore refused before a Run is ever leased.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from eawf.kernel.config.providers import ProviderRegistry
from eawf.kernel.runtime.capsule import StopCondition
from eawf.kernel.runtime.compiled import RunCompileRequest, RuntimeBinding, canonical_digest
from eawf.kernel.runtime.semantic import SemanticToolId
from eawf.kernel.state.enums import AgentSessionRole
from eawf.kernel.state.epoch2.run import RunPurpose, TaskScope
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.native_dispatch import (
    DEFAULT_TOOL_GRANTS,
    CapsuleRequest,
    DispatchParams,
    DispatchRefusal,
    compile_for_dispatch,
    refused,
)
from eawf.surfaces.render.agents import AGENT_REGISTRY
from eawf.workflow.runtime.compile import CompileRejection

logger = logging.getLogger(__name__)

#: A Task record names no role, so every Task Run is an executor Run.
_TASK_ROLE: Final = AgentSessionRole.EXECUTOR

#: The ceiling :class:`DispatchParams` puts on a workspace lease.
_MAX_LEASE_TTL_SECONDS: Final = 86_400

#: How long the lease outlives the compiled wall-clock limit, so a worker
#: stopped at its limit still holds the workspace while its residue is
#: inspected.
_LEASE_GRACE_SECONDS: Final = 600

#: The tool the closing instruction of every prompt names.
_REPORT_TOOL: Final = DEFAULT_TOOL_GRANTS[0]

#: The tool every file change of a Task Run goes through. The provider's
#: own edit and shell tools are withheld, so without it a Task Run could
#: report on work it had no way to do.
_PATCH_TOOL: Final = SemanticToolId.WORKSPACE_APPLY_PATCH.value

#: The tools a Task Run reads its workspace through, for the same reason:
#: with the provider's own file tools withheld, nothing else reads the tree.
_READ_TOOLS: Final = (SemanticToolId.REPO_READ.value, SemanticToolId.REPO_SEARCH.value)

#: What a Task Run may call: read and write its workspace, then report.
_TASK_TOOL_GRANTS: Final = (_PATCH_TOOL, *_READ_TOOLS, *DEFAULT_TOOL_GRANTS)


def compile_task_request(
    task: Task,
    run_ref: RunUrn,
    *,
    provider: str,
    config: Mapping[str, Mapping[str, Any]],
    registry: ProviderRegistry,
    bindings: Sequence[RuntimeBinding],
    budget: int | None,
    base: str,
    actor: str,
    now: datetime,
) -> DispatchParams:
    """Return the dispatch request that runs *task* as *run_ref*.

    Args:
        task: The planned Task the Run executes.
        run_ref: The Run created for this dispatch.
        provider: The provider id the Run is dispatched to, in the
            vocabulary a driver manifest names (``claude``, ``codex``).
        config: Each provider layer's whole config document, keyed by
            layer name (``global``, ``workspace``, ``repository``); only
            its ``runtime`` mapping is read.
        registry: The installed driver, auth and certification facts.
        bindings: The installed drivers; only those of *provider* are bound.
        budget: The Run's token ceiling, or ``None`` for an uncapped Run.
        base: The resolved commit SHA the workspace is materialized at.
        actor: The principal key the dispatch is recorded against.
        now: The compile stamp capability evidence is judged at.

    Returns:
        The validated dispatch request, compiled once to size its lease.

    Raises:
        DaemonValidationError: A layer is unknown or its ``runtime`` entry
            is not a mapping (``dispatch_provider_config_invalid``), no
            binding serves *provider* (``no_eligible_profile``), or the
            compiler refuses the request under its own code. A Task that
            declares no write claims is refused with
            ``dispatch_write_claims_required``.
        pydantic.ValidationError: The rendered prompt is longer than a
            dispatch admits.
    """
    if not task.write_claims:
        # A Task may stay unnarrowed while people do its work, but an
        # implement Run's write set is taken from the claims and must not
        # be empty, so the refusal names the Task field to set.
        raise refused(
            DispatchRefusal.WRITE_CLAIMS_REQUIRED,
            f"task {task.key} declares no write_claims, so an implement Run has no "
            "path it may write; promote the Task with updates.write_claims naming the "
            "repository paths it may touch (demote and re-promote a planned Task that "
            "was never claimed)",
        )
    compile_request = RunCompileRequest(
        run_ref=run_ref,
        run_scope=TaskScope(
            scope_kind="task",
            purpose=RunPurpose.IMPLEMENT,
            task_ref=task.urn,
            write_set=task.write_claims,
        ),
        purpose=RunPurpose.IMPLEMENT,
        agent_role=_TASK_ROLE,
        unattended=True,
    )
    report_schema_ref = f"schema://{_TASK_ROLE.value}-report/v1"
    fields: dict[str, Any] = {
        "urn": run_ref,
        "actor": actor,
        "idempotency_key": canonical_digest({"task": str(task.urn), "run": str(run_ref)}),
        "compile_request": compile_request,
        "provider_documents": _runtime_documents(config),
        "provider_registry": registry,
        "bindings": _provider_bindings(bindings, provider),
        "capsule": CapsuleRequest(
            criteria_digest=canonical_digest(
                [criterion.model_dump(mode="json") for criterion in task.criteria]
            ),
            report_schema_ref=report_schema_ref,
            tool_grants=_TASK_TOOL_GRANTS,
            stop_conditions=tuple(StopCondition),
            token_budget=budget,
        ),
        "base": base,
        "prompt": _render_prompt(task, report_schema_ref=report_schema_ref),
    }
    provisional = DispatchParams(**fields, lease_ttl_seconds=_MAX_LEASE_TTL_SECONDS)
    spec = compile_for_dispatch(provisional, now=now)
    ttl = min(_MAX_LEASE_TTL_SECONDS, spec.limits.wall_seconds + _LEASE_GRACE_SECONDS)
    logger.info(
        f"task request compiled task={task.urn} run={run_ref} provider={provider} "
        f"contract={spec.contract_digest} lease_ttl={ttl}"
    )
    return DispatchParams(**fields, lease_ttl_seconds=ttl)


def _render_prompt(task: Task, *, report_schema_ref: str) -> str:
    """Return the prompt a Task Run's provider process is started with.

    The role's agent body comes first so the worker reads its conduct
    before its assignment; the Task follows, then the one way to change
    files and the one way to finish.

    Args:
        task: The Task the Run executes.
        report_schema_ref: The schema the terminal report is sealed to.

    Returns:
        The prompt, holding only repository-relative paths.
    """
    body = {spec.role: spec.body for spec in AGENT_REGISTRY}[_TASK_ROLE.value]
    criteria = "\n".join(f"- {criterion.id} {criterion.text}" for criterion in task.criteria)
    claims = "\n".join(f"- {path}" for path in task.write_claims)
    return (
        f"{body.rstrip()}\n\n"
        f"# Task {task.key}\n\n"
        f"## Intent\n\n{task.intent}\n\n"
        f"## Criteria\n\n{criteria}\n\n"
        f"## Write claims\n\n{claims}\n\n"
        "## Changing files\n\n"
        f"Read the workspace through `{_READ_TOOLS[0]}` and `{_READ_TOOLS[1]}`. "
        f"Make every file change through `{_PATCH_TOOL}`: pass `files`, a list of "
        "`path` and `content` pairs, where `path` is repository-relative and inside the "
        "write claims above and `content` is the file's whole new text. You have no other "
        "way to write; eawf commits your changes on your Run's branch once your report "
        "is accepted.\n\n"
        f"## Finish\n\n"
        f"Finish by calling `{_REPORT_TOOL}` with a `verdict`, a one-line `summary`, "
        "`criteria` naming each criterion above by id as `met`, `unmet` or "
        "`not_attempted`, and your report as `body`. Send only that content: eawf fills "
        f"the report schema `{report_schema_ref}` and the contract digest from your Run "
        "and stores the body.\n"
    )


def _runtime_documents(config: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return the ``runtime`` mapping of each layer's config document.

    The layer names pass through unchanged, so the dispatch compiler is the
    one place that decides which layers exist.

    Raises:
        DaemonValidationError: A layer's ``runtime`` entry is not a mapping.
    """
    documents: dict[str, dict[str, Any]] = {}
    for layer, document in config.items():
        runtime = document.get("runtime", {})
        if not isinstance(runtime, Mapping):
            raise refused(
                DispatchRefusal.PROVIDER_CONFIG_INVALID,
                f"{layer} runtime must be a mapping, not {type(runtime).__name__}",
            )
        documents[layer] = dict(runtime)
    return documents


def _provider_bindings(
    bindings: Sequence[RuntimeBinding], provider: str
) -> tuple[RuntimeBinding, ...]:
    """Return the bindings whose driver serves *provider*.

    Raises:
        DaemonValidationError: No binding serves *provider*, which the
            compiler would report as no eligible profile had it been
            handed the empty set.
    """
    chosen = tuple(b for b in bindings if b.manifest.provider_id == provider)
    if not chosen:
        raise DaemonValidationError(
            f"validation_failed: {CompileRejection.NO_ELIGIBLE_PROFILE.value}: "
            f"no installed driver serves provider {provider!r}"
        )
    return chosen


__all__ = ["compile_task_request"]
