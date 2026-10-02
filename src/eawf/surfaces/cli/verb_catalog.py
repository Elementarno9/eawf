"""The introspectable verb catalog: every verb, its entity, parameters, errors and effect.

A verb is one legal operation on one entity group of the verb contract, or
a verb under a root entry the contract declares an exception for. The
catalog is built by walking the live Typer tree under every root entry, so
a verb's parameters are the ones its command actually declares rather
than a list maintained beside it. What the tree cannot say about a verb --
whether it reads, mutates or creates, and which daemon routes it drives -- is
declared once in :data:`CLI_VERB_EFFECTS`, and :func:`build_verb_catalog`
refuses a tree that carries a verb the table does not classify or a table row
the tree no longer carries, so the two cannot drift apart silently.

Some native verbs have no CLI spelling yet: a lifecycle skill drives them
over the daemon directly. They are catalogued from :data:`RPC_VERBS`, with
their parameters read off the closed params model the daemon validates the
request with, and every route projection read joins as a read verb. The
skill catalog's lifecycle routes are joined against this catalog, so a skill
cannot name a route no verb carries.
"""

from __future__ import annotations

import hashlib
import importlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from typing import Final, Literal

import click
import orjson
from pydantic import BaseModel, ConfigDict, Field

from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.verb_closure import ROOT_ENTRY_EXCEPTIONS
from eawf.surfaces.cli.verb_contract import CROSS_CUTTING_GROUPS, ENTITY_GROUPS
from eawf.surfaces.cli.verb_effects import CLI_VERB_EFFECTS, EffectClass, VerbEffect

logger = logging.getLogger(__name__)

VerbSurface = Literal["cli", "rpc"]
ParameterKind = Literal["argument", "option"]

#: The exit statuses a verb that reaches the daemon may end with.
ROUTED_ERRORS: Final[tuple[str, ...]] = tuple(
    exit_codes.name_for(code)
    for code in (
        exit_codes.USER_ERROR,
        exit_codes.VALIDATION_ERROR,
        exit_codes.STATE_CONFLICT,
        exit_codes.DAEMON_UNREACHABLE,
        exit_codes.INTERNAL_ERROR,
        exit_codes.NEEDS_OPERATOR,
    )
)

#: The exit statuses a verb that works on local files alone may end with.
LOCAL_ERRORS: Final[tuple[str, ...]] = tuple(
    exit_codes.name_for(code)
    for code in (exit_codes.USER_ERROR, exit_codes.VALIDATION_ERROR, exit_codes.INTERNAL_ERROR)
)

#: The route prefix and suffix of a route projection read.
_PROJECTION_PREFIX: Final = "projection."
_READ_SUFFIX: Final = ".read"

#: A projection route's entity, where its route key does not name a group.
_PROJECTION_ENTITIES: Final[dict[str, str]] = {
    "attention": "action",
    "backlog": "task",
    "notifications": "action",
}

#: The group a projection route with no entity of its own belongs to.
_PROJECTION_FALLBACK_GROUP: Final = "ui"


class VerbParameter(BaseModel):
    """One parameter a verb accepts.

    Attributes:
        name: The parameter's name, as the verb's request spells it.
        kind: ``argument`` for a positional, ``option`` for a named flag.
        flags: The option strings, empty for an argument.
        required: Whether the verb refuses a call without it.
        multiple: Whether it may be given more than once.
        is_flag: Whether it is a boolean switch that takes no value.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    kind: ParameterKind
    flags: tuple[str, ...] = ()
    required: bool
    multiple: bool = False
    is_flag: bool = False


class VerbEntry(BaseModel):
    """One verb: its identity, entity, parameters, typed errors and effect class.

    Attributes:
        verb: The verb's identity: its CLI path for a CLI verb, its route for
            a verb the daemon carries alone.
        surface: ``cli`` when an operator can type it, ``rpc`` when only a
            daemon client can call it.
        entity: The contract group the verb belongs to.
        effect_class: ``read``, ``mutate`` or ``create``.
        routes: The daemon routes the verb drives, empty for a local verb.
        parameters: What the verb accepts.
        errors: The typed exit statuses it may end with.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    verb: str = Field(min_length=1)
    surface: VerbSurface
    entity: str
    effect_class: EffectClass
    routes: tuple[str, ...] = ()
    parameters: tuple[VerbParameter, ...] = ()
    errors: tuple[str, ...]


class VerbCatalog(BaseModel):
    """Every catalogued verb, in tree order and then route order.

    Attributes:
        entries: The verbs.
        refusal_codes: The closed refusal vocabulary every routed verb's
            envelope error row carries its code from.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: tuple[VerbEntry, ...]
    refusal_codes: tuple[str, ...]

    def route_verbs(self, route: str) -> tuple[VerbEntry, ...]:
        """Return every verb that drives *route*.

        Args:
            route: A dotted daemon route name.

        Returns:
            The verbs whose routes include *route*, in catalog order.
        """
        return tuple(entry for entry in self.entries if route in entry.routes)

    def entry(self, verb: str) -> VerbEntry | None:
        """Return the verb whose identity is *verb*, or ``None``.

        Args:
            verb: A CLI path (``task seal``) or an RPC-only route.

        Returns:
            The entry, or ``None`` when no verb has that identity.
        """
        return next((entry for entry in self.entries if entry.verb == verb), None)

    def digest(self) -> str:
        """Return the SHA-256 of the catalog's canonical JSON."""
        payload = orjson.dumps(self.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS)
        return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class RpcVerb:
    """A native verb the daemon carries with no CLI spelling yet.

    Attributes:
        route: The dotted daemon route.
        entity: The contract group it belongs to.
        effect_class: Whether it reads, mutates or creates.
        params_model: ``module:attribute`` of the closed params model the
            daemon validates the request with.
    """

    route: str
    entity: str
    effect_class: EffectClass
    params_model: str


#: The native verbs a shipped skill drives that no CLI verb spells yet.
RPC_VERBS: Final[tuple[RpcVerb, ...]] = (
    RpcVerb(
        "runtime.run.dispatch",
        "run",
        "mutate",
        "eawf.runtime.daemon.native_dispatch:DispatchParams",
    ),
    RpcVerb("runtime.run.retry", "run", "mutate", "eawf.runtime.daemon.native_retry:RetryParams"),
    RpcVerb(
        "runtime.delivery.verify_batch",
        "batch",
        "mutate",
        "eawf.runtime.daemon.methods.delivery:BatchVerifyParams",
    ),
    RpcVerb(
        "runtime.delivery.assess_completion",
        "task",
        "mutate",
        "eawf.runtime.daemon.methods.delivery_completion:TaskCompletionParams",
    ),
    RpcVerb(
        "runtime.delivery.request_acceptance_repair",
        "milestone",
        "mutate",
        "eawf.runtime.daemon.methods.delivery_acceptance:AcceptanceRepairParams",
    ),
    RpcVerb(
        "runtime.permission.decide",
        "action",
        "mutate",
        "eawf.runtime.daemon.methods.permission:_DecideParams",
    ),
    RpcVerb(
        "planning.plan_revision.submit",
        "milestone",
        "create",
        "eawf.runtime.daemon.methods.planning:SubmitPlanParams",
    ),
    RpcVerb(
        "planning.plan_revision.approve",
        "milestone",
        "mutate",
        "eawf.runtime.daemon.methods.planning:ApprovePlanParams",
    ),
    RpcVerb(
        "planning.plan_revision.apply",
        "milestone",
        "mutate",
        "eawf.runtime.daemon.methods.planning:ApplyPlanParams",
    ),
    RpcVerb(
        "projection.milestone.acceptance",
        "milestone",
        "read",
        "eawf.runtime.daemon.methods.projection:AcceptanceParams",
    ),
    RpcVerb(
        "projection.health.verdicts",
        "ui",
        "read",
        "eawf.runtime.daemon.methods.console_records:HealthVerdictsRead",
    ),
    RpcVerb(
        "projection.git.pr.generations",
        "batch",
        "read",
        "eawf.runtime.daemon.methods.console_records:BatchRecordsRead",
    ),
    RpcVerb(
        "projection.git.pr.repository",
        "ui",
        "read",
        "eawf.runtime.daemon.methods.console_records:RepositoryRead",
    ),
    RpcVerb(
        "projection.merge.conflict.frames",
        "batch",
        "read",
        "eawf.runtime.daemon.methods.console_records:BatchRecordsRead",
    ),
    RpcVerb(
        "projection.crash.recovery.boot",
        "ui",
        "read",
        "eawf.runtime.daemon.methods.console_records:BootRecoveryRead",
    ),
    RpcVerb(
        "projection.receipt.proofs",
        "ui",
        "read",
        "eawf.runtime.daemon.methods.console_records:ProofReceiptsRead",
    ),
    RpcVerb(
        "projection.target.resolve",
        "ui",
        "read",
        "eawf.runtime.daemon.methods.console_records:TargetResolve",
    ),
    RpcVerb(
        "projection.history.changes",
        "ui",
        "read",
        "eawf.runtime.daemon.methods.console_records:HistoryChangesRead",
    ),
    RpcVerb(
        "runtime.run.events.read",
        "run",
        "read",
        "eawf.runtime.daemon.methods.run:_RunParams",
    ),
    RpcVerb(
        "semantic.result.read",
        "run",
        "read",
        "eawf.runtime.daemon.methods.semantic:SemanticResultReadParams",
    ),
)

#: The one parameter a route projection read takes: the tree it reads.
_PROJECTION_PARAMETERS: Final[tuple[VerbParameter, ...]] = (
    VerbParameter(name="repo_root", kind="option", required=False),
)


class VerbCatalogError(ValueError):
    """The command tree and the declared effect table disagree."""


def _parameter(param: click.Parameter) -> VerbParameter | None:
    """Return the catalog row of one click parameter, ``None`` for ``--help``."""
    name = param.name or ""
    if isinstance(param, click.Option):
        if "--help" in param.opts:
            return None
        return VerbParameter(
            name=name,
            kind="option",
            flags=tuple(param.opts),
            required=param.required,
            multiple=param.multiple,
            is_flag=param.is_flag,
        )
    return VerbParameter(
        name=name,
        kind="argument",
        required=param.required,
        multiple=param.nargs == -1,
    )


def _walk(
    command: click.Command, path: tuple[str, ...], ctx: click.Context
) -> Iterable[tuple[tuple[str, ...], click.Command]]:
    """Yield every runnable command under *command* with its path, hidden ones skipped.

    A group that runs its own callback when no subcommand is named (``doctor``,
    ``migrate --to``) is a verb as well as a parent, so it is yielded too.
    """
    if isinstance(command, click.Group):
        if command.invoke_without_command:
            yield path, command
        for name in command.list_commands(ctx):
            child = command.get_command(ctx, name)
            if child is not None and not child.hidden:
                yield from _walk(child, (*path, name), ctx)
        return
    yield path, command


def _cli_entries(root: click.Group) -> tuple[VerbEntry, ...]:
    """Return one entry per CLI verb, the contract groups' first.

    A verb under a root entry the contract declares an exception for is
    catalogued under that entry's name.

    Raises:
        VerbCatalogError: A verb has no declared effect, or a declared verb is
            no longer on the tree.
    """
    ctx = click.Context(root)
    found: dict[str, VerbEntry] = {}
    exceptions = tuple(row.name for row in ROOT_ENTRY_EXCEPTIONS if row.kind != "cross_cutting")
    for group in (*ENTITY_GROUPS, *CROSS_CUTTING_GROUPS, *exceptions):
        command = root.get_command(ctx, group)
        if command is None:
            continue
        for path, leaf in _walk(command, (group,), ctx):
            verb = " ".join(path)
            effect = CLI_VERB_EFFECTS.get(verb)
            if effect is None:
                raise VerbCatalogError(f"verb {verb!r} declares no effect class")
            params = tuple(p for p in map(_parameter, leaf.params) if p is not None)
            found[verb] = VerbEntry(
                verb=verb,
                surface="cli",
                entity=group,
                effect_class=effect.effect_class,
                routes=effect.routes,
                parameters=params,
                errors=ROUTED_ERRORS if effect.routes else LOCAL_ERRORS,
            )
    stale = sorted(set(CLI_VERB_EFFECTS) - set(found))
    if stale:
        raise VerbCatalogError(f"declared verbs {stale} are not on the command tree")
    return tuple(found.values())


def _model_parameters(params_model: str) -> tuple[VerbParameter, ...]:
    """Return the parameters of the closed params model at ``module:attribute``."""
    module_name, _, attribute = params_model.partition(":")
    model: type[BaseModel] = getattr(importlib.import_module(module_name), attribute)
    return tuple(
        VerbParameter(name=name, kind="option", required=field.is_required())
        for name, field in model.model_fields.items()
    )


def _projection_entity(route: str) -> str:
    """Return the group a ``projection.<route>.read`` verb belongs to."""
    key = route.removeprefix(_PROJECTION_PREFIX).split(".", 1)[0]
    if key in ENTITY_GROUPS:
        return key
    return _PROJECTION_ENTITIES.get(key, _PROJECTION_FALLBACK_GROUP)


def _rpc_entries(registered: Iterable[str]) -> tuple[VerbEntry, ...]:
    """Return the RPC-only verbs, declared and projection-read alike."""
    declared = tuple(
        VerbEntry(
            verb=row.route,
            surface="rpc",
            entity=row.entity,
            effect_class=row.effect_class,
            routes=(row.route,),
            parameters=_model_parameters(row.params_model),
            errors=ROUTED_ERRORS,
        )
        for row in RPC_VERBS
    )
    projections = tuple(
        VerbEntry(
            verb=route,
            surface="rpc",
            entity=_projection_entity(route),
            effect_class="read",
            routes=(route,),
            parameters=_PROJECTION_PARAMETERS,
            errors=ROUTED_ERRORS,
        )
        for route in sorted(registered)
        if route.startswith(_PROJECTION_PREFIX) and route.endswith(_READ_SUFFIX)
    )
    return declared + projections


def build_verb_catalog(root: click.Group, registered: Iterable[str]) -> VerbCatalog:
    """Build the verb catalog from a command tree and the daemon's routes.

    Args:
        root: The root click group of the ``eawf`` app.
        registered: Every route the daemon registers.

    Returns:
        The catalog: CLI verbs in tree order, then RPC-only verbs.

    Raises:
        VerbCatalogError: A tree verb is unclassified, a classified verb left
            the tree, or a verb names a route the daemon does not register.
    """
    from eawf.runtime.daemon.methods.domain_envelope import DomainErrorCode

    routes = frozenset(registered)
    entries = _cli_entries(root) + _rpc_entries(routes)
    unregistered = sorted({r for entry in entries for r in entry.routes} - routes)
    if unregistered:
        raise VerbCatalogError(f"verbs name unregistered routes {unregistered}")
    logger.debug(f"build_verb_catalog verbs={len(entries)}")
    return VerbCatalog(entries=entries, refusal_codes=tuple(code.value for code in DomainErrorCode))


@cache
def verb_catalog() -> VerbCatalog:
    """Return the catalog of the installed ``eawf`` app and daemon.

    Returns:
        The catalog :func:`build_verb_catalog` builds from the live tree.
    """
    import typer

    from eawf.runtime.daemon.methods import registered_methods
    from eawf.surfaces.cli.app import app

    root = typer.main.get_command(app)
    assert isinstance(root, click.Group), "the eawf root command is a group"
    return build_verb_catalog(root, registered_methods())


def verb_catalog_text(catalog: VerbCatalog) -> str:
    """Return the human rendering: one line per verb, then its parameters.

    Args:
        catalog: The catalog to render.

    Returns:
        The text body, carrying every fact the machine rendering carries.
    """
    lines: list[str] = []
    for entry in catalog.entries:
        routes = ", ".join(entry.routes) or "local"
        lines.append(
            f"{entry.verb}  [{entry.entity}] {entry.effect_class} {entry.surface} -> {routes}"
        )
        for param in entry.parameters:
            spelled = "/".join(param.flags) or f"<{param.name}>"
            marks = [
                mark
                for mark, on in (
                    ("required", param.required),
                    ("repeatable", param.multiple),
                    ("flag", param.is_flag),
                )
                if on
            ]
            lines.append(f"    {spelled}" + (f" ({', '.join(marks)})" if marks else ""))
        lines.append(f"    errors: {', '.join(entry.errors)}")
    lines.append(f"refusal codes: {', '.join(catalog.refusal_codes)}")
    return "\n".join(lines)


__all__ = [
    "CLI_VERB_EFFECTS",
    "LOCAL_ERRORS",
    "ROUTED_ERRORS",
    "RPC_VERBS",
    "EffectClass",
    "RpcVerb",
    "VerbCatalog",
    "VerbCatalogError",
    "VerbEffect",
    "VerbEntry",
    "VerbParameter",
    "build_verb_catalog",
    "verb_catalog",
    "verb_catalog_text",
]
