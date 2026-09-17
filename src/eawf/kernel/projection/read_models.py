"""Read-model declarations: which typed projection each console route renders.

A widget receives only a projection whose kind is declared here. Each declaration names
the route keys it serves; the console route registry holds the same binding from the
other side, one read model per route row, and :func:`route_binding_mismatches` is the
check that the two tables agree in both directions.

A read model whose producer has not shipped is still declared, so its route renders every
field as an unknown ``TruthField`` instead of being an unbound gap. The entry layer
resolves to :attr:`ReadModelKind.PROCESS_FRAME`, the one kind no projection carries,
because no projection exists before a session does.

The module is stdlib-only so the console can import the declarations without loading the
validation models in :mod:`~eawf.kernel.projection.truth`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class ReadModelKind(StrEnum):
    """The closed set of read models, and of projection kinds a header may name."""

    PROCESS_FRAME = "process_frame"
    SCOPE_HOME_VIEW = "scope_home_view"
    ENTITY_DETAIL_VIEW = "entity_detail_view"
    FLEET_QUERY_PAGE = "fleet_query_page"
    ACTIVITY_PAGE = "activity_page"
    RUN_DETAIL_VIEW = "run_detail_view"
    ATTENTION_PAGE = "attention_page"
    EFFECTIVE_SETTINGS_VIEW = "effective_settings_view"
    ROADMAP_VIEW = "roadmap_view"
    TRUST_VIEW = "trust_view"
    EVIDENCE_VIEW = "evidence_view"
    HEALTH_VIEW = "health_view"
    SANDBOX_LOG_PAGE = "sandbox_log_page"
    UNATTENDED_VIEW = "unattended_view"
    GIT_PR_VIEW = "git_pr_view"
    MERGE_CONFLICT_VIEW = "merge_conflict_view"
    COST_CEILING_VIEW = "cost_ceiling_view"
    CRASH_RECOVERY_VIEW = "crash_recovery_view"
    SEARCH_PAGE = "search_page"
    HISTORY_DIFF_VIEW = "history_diff_view"
    NOTIFICATION_MATRIX_VIEW = "notification_matrix_view"
    NOTICE_DETAIL_VIEW = "notice_detail_view"
    RUN_REPORT_PLAN = "run_report_plan"
    CRITERION_RECEIPT_VIEW = "criterion_receipt_view"
    SETTINGS_LAYER_STACK = "settings_layer_stack"
    BACKLOG_VIEW = "backlog_view"
    CAMPAIGN_VIEW = "campaign_view"
    CAMPAIGN_PLAN_STEP = "campaign_plan_step"
    ARTIFACT_CARD_VIEW = "artifact_card_view"
    ACCEPTANCE_BUNDLE_VIEW = "acceptance_bundle_view"
    RELEASE_READINESS_VIEW = "release_readiness_view"
    HISTORY_PAGE = "history_page"
    TRANSCRIPT_VIEW = "transcript_view"
    EVIDENCE_RUNG_RECORD = "evidence_rung_record"


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadModelSpec:
    """One read-model declaration.

    Attributes:
        kind: The read model, which is also the ``projection_kind`` its header names.
        routes: The console route keys this model serves, in registry order.
        parent: The read model this one is a sub-model of. A sub-model with no route of
            its own is carried by its parent's route.
        projection_backed: Whether a projection carries the model; ``False`` only for the
            process frames drawn before a session exists.
    """

    kind: ReadModelKind
    routes: tuple[str, ...] = ()
    parent: ReadModelKind | None = None
    projection_backed: bool = True


_K = ReadModelKind

READ_MODELS: tuple[ReadModelSpec, ...] = (
    ReadModelSpec(kind=_K.PROCESS_FRAME, routes=("entry",), projection_backed=False),
    ReadModelSpec(kind=_K.SCOPE_HOME_VIEW, routes=("scope.home",)),
    ReadModelSpec(kind=_K.ENTITY_DETAIL_VIEW, routes=("track", "batch.detail", "task.detail")),
    ReadModelSpec(kind=_K.FLEET_QUERY_PAGE, routes=("activity",)),
    ReadModelSpec(kind=_K.ACTIVITY_PAGE, parent=_K.FLEET_QUERY_PAGE),
    ReadModelSpec(kind=_K.RUN_DETAIL_VIEW, routes=("run.detail",)),
    ReadModelSpec(kind=_K.ATTENTION_PAGE, routes=("attention",)),
    ReadModelSpec(kind=_K.EFFECTIVE_SETTINGS_VIEW, routes=("settings",)),
    ReadModelSpec(kind=_K.ROADMAP_VIEW, routes=("roadmap",)),
    ReadModelSpec(kind=_K.TRUST_VIEW, routes=("trust",)),
    ReadModelSpec(kind=_K.EVIDENCE_VIEW, routes=("evidence",)),
    ReadModelSpec(kind=_K.HEALTH_VIEW, routes=("health",)),
    ReadModelSpec(kind=_K.SANDBOX_LOG_PAGE, routes=("sandbox.log",)),
    ReadModelSpec(kind=_K.UNATTENDED_VIEW, routes=("unattended",)),
    ReadModelSpec(kind=_K.GIT_PR_VIEW, routes=("git.pr",)),
    ReadModelSpec(kind=_K.MERGE_CONFLICT_VIEW, routes=("merge.conflict",), parent=_K.GIT_PR_VIEW),
    ReadModelSpec(kind=_K.COST_CEILING_VIEW, routes=("cost.ceiling",)),
    ReadModelSpec(kind=_K.CRASH_RECOVERY_VIEW, routes=("crash.recovery",)),
    ReadModelSpec(kind=_K.SEARCH_PAGE, routes=("search",)),
    ReadModelSpec(kind=_K.HISTORY_DIFF_VIEW, routes=("history.diff",)),
    ReadModelSpec(kind=_K.NOTIFICATION_MATRIX_VIEW, routes=("notifications",)),
    # the notice detail is a record form of the notifications route, never a route
    ReadModelSpec(kind=_K.NOTICE_DETAIL_VIEW, parent=_K.NOTIFICATION_MATRIX_VIEW),
    ReadModelSpec(kind=_K.RUN_REPORT_PLAN, routes=("export",), parent=_K.RUN_DETAIL_VIEW),
    ReadModelSpec(kind=_K.CRITERION_RECEIPT_VIEW, routes=("receipt",)),
    ReadModelSpec(
        kind=_K.SETTINGS_LAYER_STACK,
        routes=("settings.stack",),
        parent=_K.EFFECTIVE_SETTINGS_VIEW,
    ),
    ReadModelSpec(kind=_K.BACKLOG_VIEW, routes=("backlog",)),
    ReadModelSpec(kind=_K.CAMPAIGN_VIEW, routes=("campaign",)),
    ReadModelSpec(kind=_K.CAMPAIGN_PLAN_STEP, routes=("campaign.step",), parent=_K.CAMPAIGN_VIEW),
    ReadModelSpec(
        kind=_K.ARTIFACT_CARD_VIEW, routes=("campaign.artifact",), parent=_K.CAMPAIGN_VIEW
    ),
    ReadModelSpec(kind=_K.ACCEPTANCE_BUNDLE_VIEW, routes=("milestone",)),
    ReadModelSpec(kind=_K.RELEASE_READINESS_VIEW, routes=("release",)),
    ReadModelSpec(kind=_K.HISTORY_PAGE, routes=("history",)),
    ReadModelSpec(kind=_K.TRANSCRIPT_VIEW, routes=("transcript",)),
    ReadModelSpec(
        kind=_K.EVIDENCE_RUNG_RECORD, routes=("evidence.digest",), parent=_K.EVIDENCE_VIEW
    ),
)


def _declaration_defects(declarations: tuple[ReadModelSpec, ...]) -> list[str]:
    """Return every reason ``declarations`` is not a usable read-model table."""
    kinds = [spec.kind for spec in declarations]
    defects = [
        f"{kind} declared twice" for kind in sorted({k for k in kinds if kinds.count(k) > 1})
    ]
    defects += [f"{kind} not declared" for kind in ReadModelKind if kind not in kinds]
    # a sub-model hangs off a top-level model, so a parent chain is never deeper than one
    sub_models = {spec.kind for spec in declarations if spec.parent is not None}
    served: dict[str, list[str]] = {}
    for spec in declarations:
        for route in spec.routes:
            served.setdefault(route, []).append(spec.kind)
        if spec.parent in sub_models:
            defects.append(f"{spec.kind} has parent {spec.parent}, which is itself a sub-model")
        if not spec.routes and spec.parent is None:
            defects.append(f"{spec.kind} serves no route and has no parent to carry it")
    defects += [
        f"route {route!r} is served by {', '.join(owners)}"
        for route, owners in sorted(served.items())
        if len(owners) > 1
    ]
    return defects


def index_read_models(
    declarations: Iterable[ReadModelSpec],
) -> Mapping[ReadModelKind, ReadModelSpec]:
    """Return the declarations keyed by kind, refusing a table the console cannot bind.

    Args:
        declarations: The read-model declarations, one per kind.

    Returns:
        A read-only mapping from each kind to its declaration.

    Raises:
        ValueError: a kind is declared twice or not at all, a parent is itself a
            sub-model (a model naming itself included), a model with no route has no
            parent, or a route is served by more than one model.
    """
    table = tuple(declarations)
    defects = _declaration_defects(table)
    if defects:
        raise ValueError(f"read-model declarations are invalid: {'; '.join(defects)}")
    return MappingProxyType({spec.kind: spec for spec in table})


def route_binding_mismatches(
    binding: Mapping[str, ReadModelKind],
    read_models: Mapping[ReadModelKind, ReadModelSpec],
) -> tuple[str, ...]:
    """Return every route on which the registry binding and the declarations disagree.

    Args:
        binding: The read model each registry route key renders, holes left out.
        read_models: The indexed declarations whose ``routes`` must name the same pairs.

    Returns:
        One message per disagreeing route key, sorted by key; empty when both directions
        are equal.
    """
    served = {route: spec.kind for spec in read_models.values() for route in spec.routes}
    return tuple(
        f"route {route!r} binds {binding.get(route) or 'no read model'} "
        f"but {served.get(route) or 'no read model'} names it"
        for route in sorted(binding.keys() | served.keys())
        if binding.get(route) != served.get(route)
    )


READ_MODEL_BY_KIND: Mapping[ReadModelKind, ReadModelSpec] = index_read_models(READ_MODELS)
