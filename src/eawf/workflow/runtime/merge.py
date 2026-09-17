"""Least-authority merge of a policy layer stack.

Layers apply in soft precedence: safe baseline, global, workspace,
repository, track, milestone, batch, then Run override. Authority does not
follow that precedence. Each field has one merge operation, and every
layer that declares the field takes part in it:

- grants (tool, model, and variable-name sets) intersect;
- denials union, and a denied tool leaves the grant;
- ceilings (limits, context size, sandbox, network, authority) take the
  minimum;
- floors (remaining context, required capabilities) take the maximum.

A more specific layer can therefore only narrow what a less specific one
allowed. References, provider options, and executables have no overlay
field at all, so no repository or Run override can name new path,
network, or secret authority.

The safe baseline is compiled from the request. A Run whose purpose does
not mutate gets a read-only sandbox and workspace ceiling. A task-scoped
Run gets a child-Run ceiling of zero, because one lease admits one writer.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from operator import attrgetter
from typing import Any, Final

from eawf.kernel.config.providers import LayeredProfile
from eawf.kernel.runtime.compiled import (
    UNSEALED_DIGEST,
    AuthorityCeiling,
    ContextNarrowing,
    EnvironmentNarrowing,
    LimitCeilings,
    MergeOperation,
    ModelNarrowing,
    PolicyOverlay,
    ResolvedFieldSource,
    RunCompileRequest,
    SandboxNarrowing,
    ToolNarrowing,
)
from eawf.kernel.runtime.provider import (
    AUTHORITY_LEVEL_ORDER,
    NETWORK_MODE_ORDER,
    SANDBOX_MODE_ORDER,
    SOURCE_LAYER_PRECEDENCE,
    SourceLayer,
)
from eawf.kernel.state.epoch2.run import MUTATING_PURPOSES, TaskScope

SAFE_BASELINE_REF: Final = "policy://baseline/compiled"


@dataclass(frozen=True)
class FieldProvenance:
    """Where a merged field came from, before its value digest is known.

    Attributes:
        source_layer: The layer holding the effective value.
        source_ref: That layer's configuration or policy reference.
        merge_operation: How the declaring layers were combined.
        superseded_source_refs: The other declaring layers' references.
        constraint_reason: How many layers took part, when more than one.
    """

    source_layer: SourceLayer
    source_ref: str
    merge_operation: MergeOperation
    superseded_source_refs: tuple[str, ...] = ()
    constraint_reason: str | None = None

    def row(self, path: str) -> ResolvedFieldSource:
        """Return the source-map row for *path*, digest left for sealing."""
        return ResolvedFieldSource(
            field_path=path,
            effective_value_digest=UNSEALED_DIGEST,
            source_layer=self.source_layer,
            source_ref=self.source_ref,
            merge_operation=self.merge_operation,
            superseded_source_refs=self.superseded_source_refs,
            constraint_reason=self.constraint_reason,
        )


@dataclass(frozen=True)
class MergedPolicy:
    """The least-authority merge of one layer stack.

    Attributes:
        values: The effective value of every compiled field path that at
            least one layer declares, including ``/model_id`` when the
            model allowlists leave a model.
        provenance: Where each of those values came from.
        model_allowed: The intersected model allowlist, in the order of
            the least specific layer that declared one.
    """

    values: Mapping[str, Any]
    provenance: Mapping[str, FieldProvenance]
    model_allowed: tuple[str, ...]


Declared = Sequence[tuple[PolicyOverlay, Any]]


def _bound(pick: Callable[..., Any], order: Sequence[str] | None = None) -> Callable[..., Any]:
    """Return a combiner keeping the extreme value, held by its latest layer."""

    def combine(declared: Declared) -> tuple[Any, PolicyOverlay]:
        best = pick((value for _, value in declared), key=order.index if order else None)
        holder = next(layer for layer, value in reversed(declared) if value == best)
        return best, holder

    return combine


def _intersect(declared: Declared) -> tuple[tuple[Any, ...], PolicyOverlay]:
    """Keep the members every declaring layer grants."""
    first = declared[0][1]
    kept = tuple(member for member in first if all(member in value for _, value in declared[1:]))
    return kept, declared[-1][0]


def _unite(declared: Declared) -> tuple[tuple[Any, ...], PolicyOverlay]:
    """Keep every member any declaring layer names, sorted."""
    return tuple(sorted({member for _, value in declared for member in value})), declared[-1][0]


def _non_empty(getter: Callable[[PolicyOverlay], tuple[Any, ...]]) -> Callable[..., Any]:
    """Read a set a layer declares only by naming at least one member."""
    return lambda overlay: getter(overlay) or None


@dataclass(frozen=True)
class _FieldRule:
    """One compiled field: how a layer declares it and how layers combine."""

    path: str
    operation: MergeOperation
    read: Callable[[PolicyOverlay], Any]
    combine: Callable[[Declared], tuple[Any, PolicyOverlay]]


_RULES: Final[tuple[_FieldRule, ...]] = (
    *(
        _FieldRule(
            f"/authority/{axis}",
            "minimum",
            attrgetter(f"authority_ceiling.{axis}"),
            _bound(min, order),
        )
        for axis, order in AUTHORITY_LEVEL_ORDER.items()
    ),
    *(
        _FieldRule(f"/limits/{name}", "minimum", attrgetter(f"limits.{name}"), _bound(min))
        for name in LimitCeilings.model_fields
    ),
    _FieldRule(
        "/sandbox/mode", "minimum", attrgetter("sandbox.mode"), _bound(min, SANDBOX_MODE_ORDER)
    ),
    _FieldRule(
        "/sandbox/network",
        "minimum",
        attrgetter("sandbox.network"),
        _bound(min, NETWORK_MODE_ORDER),
    ),
    _FieldRule(
        "/context_policy/max_input_tokens",
        "minimum",
        attrgetter("context_policy.max_input_tokens"),
        _bound(min),
    ),
    _FieldRule(
        "/context_policy/minimum_remaining_tokens",
        "maximum",
        attrgetter("context_policy.minimum_remaining_tokens"),
        _bound(max),
    ),
    _FieldRule(
        "/tool_policy/max_parallel_calls",
        "minimum",
        attrgetter("tool_policy.max_parallel_calls"),
        _bound(min),
    ),
    _FieldRule("/tool_policy/allow", "intersection", attrgetter("tool_policy.allow"), _intersect),
    _FieldRule(
        "/tool_policy/deny", "union_deny", _non_empty(attrgetter("tool_policy.deny")), _unite
    ),
    _FieldRule(
        "/environment/allowed_variable_names",
        "intersection",
        attrgetter("environment.allowed_variable_names"),
        _intersect,
    ),
    _FieldRule("/capabilities", "maximum", _non_empty(attrgetter("required_capabilities")), _unite),
)


def _provenance(
    operation: MergeOperation, holder: PolicyOverlay, declared: Iterable[PolicyOverlay]
) -> FieldProvenance:
    """Describe a merge result held by *holder* among the *declared* layers."""
    layers = list(declared)
    superseded = tuple(
        dict.fromkeys(o.source_ref for o in layers if o.source_ref != holder.source_ref)
    )
    reason = f"{operation} over {len(layers)} declaring layers" if len(layers) > 1 else None
    return FieldProvenance(holder.layer, holder.source_ref, operation, superseded, reason)


def _apply_denials(values: dict[str, Any], provenance: dict[str, FieldProvenance]) -> None:
    """Remove denied tools from the grant, so a denial always wins."""
    denied = set(values.get("/tool_policy/deny", ()))
    granted = values.get("/tool_policy/allow")
    if granted is None or not denied & set(granted):
        return
    values["/tool_policy/allow"] = tuple(tool for tool in granted if tool not in denied)
    source = provenance["/tool_policy/allow"]
    provenance["/tool_policy/allow"] = replace(
        source, constraint_reason=f"{source.constraint_reason or 'intersection'} minus union_deny"
    )


def _merge_model(
    ordered: Sequence[PolicyOverlay],
) -> tuple[tuple[str, ...], str | None, FieldProvenance | None]:
    """Intersect model allowlists and pick the most specific default inside.

    Returns:
        ``(allowed, model_id, provenance)``; the last two are ``None``
        when no model survives the intersection.
    """
    declared = [(o, o.model_policy.allowed) for o in ordered if o.model_policy.allowed is not None]
    if not declared:
        return (), None, None
    allowed, _ = _intersect(declared)
    if not allowed:
        return allowed, None, None
    defaults = [o for o in ordered if o.model_policy.default in allowed]
    holder = defaults[-1] if defaults else declared[0][0]
    model_id = holder.model_policy.default if defaults else allowed[0]
    others = [o for o, _ in declared if o is not holder]
    source = _provenance("replace", holder, [holder, *others])
    reason = f"most specific default inside {len(declared)} intersected model allowlists"
    return allowed, model_id, replace(source, constraint_reason=reason)


def merge_policy_layers(stack: Sequence[PolicyOverlay]) -> MergedPolicy:
    """Merge *stack* under least authority.

    Args:
        stack: Every contributing layer. They are applied in
            :data:`~eawf.kernel.runtime.provider.SOURCE_LAYER_PRECEDENCE`
            order; overlays of one layer keep their given order.

    Returns:
        The effective values and their provenance.
    """
    ordered = sorted(stack, key=lambda overlay: SOURCE_LAYER_PRECEDENCE.index(overlay.layer))
    values: dict[str, Any] = {}
    provenance: dict[str, FieldProvenance] = {}
    for rule in _RULES:
        declared = [(o, value) for o in ordered if (value := rule.read(o)) is not None]
        if not declared:
            continue
        values[rule.path], holder = rule.combine(declared)
        provenance[rule.path] = _provenance(rule.operation, holder, (o for o, _ in declared))
    _apply_denials(values, provenance)
    allowed, model_id, model_source = _merge_model(ordered)
    if model_id is not None and model_source is not None:
        values["/model_id"] = model_id
        provenance["/model_id"] = model_source
    return MergedPolicy(values=values, provenance=provenance, model_allowed=allowed)


def safe_baseline(request: RunCompileRequest) -> PolicyOverlay:
    """Return the ceiling a request's scope and purpose impose.

    Args:
        request: The Run being compiled.

    Returns:
        The ``safe_baseline`` layer for *request*.
    """
    mutating = request.purpose in MUTATING_PURPOSES
    return PolicyOverlay(
        layer="safe_baseline",
        source_ref=SAFE_BASELINE_REF,
        sandbox=SandboxNarrowing(
            mode="workspace_write" if mutating else "read_only", network="brokered_allowlist"
        ),
        authority_ceiling=AuthorityCeiling(
            state="proposal_only",
            workspace="scoped_write" if mutating else "read_only",
            git="read_metadata",
            external_effects="proposal_only",
            credentials="reference_only",
            config="read_effective",
            publish="none",
            merge="none",
        ),
        limits=LimitCeilings(child_runs=0 if isinstance(request.run_scope, TaskScope) else 32),
    )


def profile_layer(entry: LayeredProfile) -> PolicyOverlay:
    """Return the narrowing view of an operator profile.

    Args:
        entry: The profile and its declaring layer.

    Returns:
        The profile's contribution to the merge.
    """
    profile = entry.profile
    return PolicyOverlay(
        layer=entry.layer,
        source_ref=profile.source_ref,
        profile_id=profile.profile_id,
        model_policy=ModelNarrowing(
            allowed=profile.model_policy.allowed, default=profile.model_policy.default
        ),
        tool_policy=ToolNarrowing(
            allow=profile.tool_policy.allow,
            deny=profile.tool_policy.deny,
            max_parallel_calls=profile.tool_policy.max_parallel_calls,
        ),
        sandbox=SandboxNarrowing(mode=profile.sandbox.mode, network=profile.sandbox.network),
        environment=EnvironmentNarrowing(
            allowed_variable_names=profile.environment.allowed_variable_names
        ),
        context_policy=ContextNarrowing(
            max_input_tokens=profile.context_policy.max_input_tokens,
            minimum_remaining_tokens=profile.context_policy.minimum_remaining_tokens,
        ),
        limits=LimitCeilings.model_validate(profile.limits.model_dump()),
        authority_ceiling=AuthorityCeiling.model_validate(profile.authority_ceiling.model_dump()),
        required_capabilities=profile.required_capabilities,
    )
