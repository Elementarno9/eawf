"""REL-024: the registry of durable-effect boundaries a publication crosses.

A publication episode is a sequence of moments where something outside
this process becomes true and stays true: a tag lands on a remote, an
operation snapshot lands in the ledger, a leg is dispatched, a receipt
is written. Each of those moments is a *boundary*, and each boundary is
a place a crash can land on either side of. Recovery is only provable if
the set of boundaries is known, so this module is the place that knows
it.

Two halves make the set trustworthy:

* **A site carries its boundary id.** Every durable write site in the
  package is decorated with :func:`durable_boundary`, naming the
  boundaries whose durable effect that function is. The id lives at the
  site rather than in a table read from elsewhere, so a reader of the
  function sees which crash boundary it is.
* **The scan keeps the set total.** :func:`assert_boundaries_total`
  parses the package and refuses when any function that writes durably
  carries no boundary id. Without the scan the registry is a list
  someone has to remember to extend, which is the same as no registry:
  the write site that gets forgotten is exactly the one that
  double-publishes.

:data:`EXTERNAL_BOUNDARIES` names the one boundary whose durable write
is not in this package. The tag push is ``git push`` inside the
``eawf release tag`` verb, and it is registered here anyway: leaving it
out would make the set look total while the first external effect of the
whole release went unaccounted for.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, TypeVar

logger = logging.getLogger(__name__)


class PublicationBoundary(StrEnum):
    """A moment in a publication where a durable effect becomes true.

    Values:
        TAG_PUSH: The annotated tag reaching the remote, which is what
            triggers the pipeline. External to this package.
        OPERATION_OPEN: The publication operation becoming durable, so a
            crash after it replays instead of re-opening.
        TARGET_DISPATCH: One leg's attempt row being queued, which is
            the record that an external call is owed.
        EFFECT_RECEIPT_WRITE: An adapter's own word being written onto
            the attempt row.
        OBSERVATION_RECEIPT_WRITE: An independent read-back being
            written onto the attempt row.
        TRANSITION_APPLY: The release record moving to its successor
            status.
    """

    TAG_PUSH = "tag_push"
    OPERATION_OPEN = "operation_open"
    TARGET_DISPATCH = "target_dispatch"
    EFFECT_RECEIPT_WRITE = "effect_receipt_write"
    OBSERVATION_RECEIPT_WRITE = "observation_receipt_write"
    TRANSITION_APPLY = "transition_apply"


#: Boundaries whose durable write lives outside ``eawf.workflow.release``
#: and is therefore not reachable by :func:`assert_boundaries_total`.
EXTERNAL_BOUNDARIES: Final[frozenset[PublicationBoundary]] = frozenset(
    {PublicationBoundary.TAG_PUSH}
)

#: Boundaries the package's own scan must find at least one site for.
IN_PACKAGE_BOUNDARIES: Final[frozenset[PublicationBoundary]] = (
    frozenset(PublicationBoundary) - EXTERNAL_BOUNDARIES
)

#: Attribute :func:`durable_boundary` writes onto the function it marks.
BOUNDARY_ATTR: Final[str] = "__publication_boundaries__"

#: Name of the marker decorator, as the scan reads it out of the AST.
BOUNDARY_DECORATOR: Final[str] = "durable_boundary"

#: The calls that make a function a durable write site. A function is a
#: site when it *is* one of these -- the three reducer applies and the
#: two append primitives are themselves the write -- or when it *calls*
#: one, which is how the publication and observation verbs qualify. One
#: hop is enough: a caller of a caller is a site through its own hop.
DURABLE_WRITE_CALLS: Final[frozenset[str]] = frozenset(
    {
        "advance_release",
        "advance_target_attempt",
        "append_envelope",
        "assert_append_only",
        "open_target_attempt",
        "record_operation",
    }
)

_F = TypeVar("_F", bound=Callable[..., Any])


@dataclass(frozen=True, slots=True)
class BoundarySite:
    """One durable write site and the boundaries it is the write of.

    Attributes:
        module: Module stem inside ``eawf.workflow.release``.
        function: Name of the function performing the durable write.
        boundaries: The ids the site declares. Empty means undecorated,
            which is what :func:`assert_boundaries_total` refuses.
    """

    module: str
    function: str
    boundaries: frozenset[PublicationBoundary]

    @property
    def label(self) -> str:
        """Return the ``module:function`` address used in messages."""
        return f"{self.module}:{self.function}"


class UnregisteredBoundaryError(ValueError):
    """A durable write site carries no usable boundary id.

    Attributes:
        sites: The ``module:function`` addresses at fault, sorted.
        missing: Registered in-package boundaries no site claims.
    """

    def __init__(
        self,
        message: str,
        *,
        sites: tuple[str, ...] = (),
        missing: frozenset[PublicationBoundary] = frozenset(),
    ) -> None:
        """Store the offending addresses alongside the operator message."""
        super().__init__(message)
        self.sites = sites
        self.missing = missing


def durable_boundary(*boundaries: PublicationBoundary) -> Callable[[_F], _F]:
    """Mark a function as the durable write of *boundaries*.

    The marker does not wrap: it writes :data:`BOUNDARY_ATTR` onto the
    function and returns the same object, so decorating a reducer costs
    nothing at call time and cannot change what the reducer returns.

    More than one id is accepted because one primitive can be the write
    of several boundaries -- the ledger append is how the operation
    opens, how a dispatch is remembered and how both receipts become
    durable, and collapsing that to a single id would misname three of
    the four.

    Args:
        *boundaries: The ids this site is the durable write of. At least
            one.

    Returns:
        A decorator returning its argument unchanged.

    Raises:
        ValueError: When no boundary is named. A marker that names
            nothing reads as coverage while providing none.
    """
    if not boundaries:
        raise ValueError(f"{BOUNDARY_DECORATOR} requires at least one boundary id")
    declared = frozenset(boundaries)

    def mark(fn: _F) -> _F:
        setattr(fn, BOUNDARY_ATTR, declared)
        return fn

    return mark


def boundaries_of(fn: Callable[..., Any]) -> frozenset[PublicationBoundary]:
    """Return the boundary ids *fn* was marked with.

    Args:
        fn: A function expected to carry the marker.

    Returns:
        The declared ids.

    Raises:
        KeyError: When *fn* carries no marker, which is the runtime
            counterpart of the scan's refusal.
    """
    declared: frozenset[PublicationBoundary] | None = getattr(fn, BOUNDARY_ATTR, None)
    if declared is None:
        raise KeyError(
            f"{getattr(fn, '__qualname__', fn)!r} carries no publication boundary id; "
            f"mark it with @{BOUNDARY_DECORATOR}(...)"
        )
    return declared


def package_sources(package_root: Path | None = None) -> dict[str, str]:
    """Return the package's module sources, keyed by module stem.

    Args:
        package_root: Directory to read. Defaults to this package.

    Returns:
        One entry per module, ``__init__`` excluded because it declares
        no functions.
    """
    root = package_root if package_root is not None else Path(__file__).parent
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(root.glob("*.py"))
        if path.stem != "__init__"
    }


def durable_write_sites(sources: Mapping[str, str]) -> tuple[BoundarySite, ...]:
    """Return every durable write site found in *sources*.

    Args:
        sources: Module stem to source text. Passing synthetic sources
            is how a seeded write site is scanned without editing the
            shipped package.

    Returns:
        The sites, ordered by module then function.

    Raises:
        SyntaxError: When a source does not parse.
        UnregisteredBoundaryError: When a marker names something that is
            not a :class:`PublicationBoundary` member.
    """
    sites: list[BoundarySite] = []
    for module in sorted(sources):
        tree = ast.parse(sources[module], filename=f"{module}.py")
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not _writes_durably(node):
                continue
            sites.append(
                BoundarySite(
                    module=module,
                    function=node.name,
                    boundaries=_declared_boundaries(node, module=module),
                )
            )
    return tuple(sorted(sites, key=lambda site: site.label))


def assert_boundaries_total(sources: Mapping[str, str] | None = None) -> tuple[BoundarySite, ...]:
    """Return the scanned sites, or refuse an incomplete boundary set.

    Args:
        sources: Module stem to source text. ``None`` reads the shipped
            package.

    Returns:
        Every durable write site, each carrying at least one id.

    Raises:
        UnregisteredBoundaryError: When a site carries no id, or when a
            registered in-package boundary has no site at all -- an id
            nothing claims is a boundary nobody is testing.
    """
    scanned = durable_write_sites(sources if sources is not None else package_sources())
    undecorated = tuple(site.label for site in scanned if not site.boundaries)
    if undecorated:
        raise UnregisteredBoundaryError(
            f"unregistered_publication_boundary: {list(undecorated)} write durably but carry "
            f"no boundary id; mark each with @{BOUNDARY_DECORATOR}(...)",
            sites=undecorated,
        )
    claimed = {boundary for site in scanned for boundary in site.boundaries}
    missing = IN_PACKAGE_BOUNDARIES - claimed
    if missing:
        raise UnregisteredBoundaryError(
            f"unclaimed_publication_boundary: {sorted(b.value for b in missing)} are registered "
            f"but no durable write site declares them",
            missing=frozenset(missing),
        )
    logger.debug(f"assert_boundaries_total sites={len(scanned)} claimed={len(claimed)}")
    return scanned


def _writes_durably(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return whether *node* is a durable write site.

    Args:
        node: A function definition.

    Returns:
        ``True`` when the function is one of
        :data:`DURABLE_WRITE_CALLS` or calls one of them.
    """
    if node.name in DURABLE_WRITE_CALLS:
        return True
    return bool(_called_names(node) & DURABLE_WRITE_CALLS)


def _called_names(node: ast.AST) -> frozenset[str]:
    """Return the bare names of every call made inside *node*.

    Args:
        node: Any AST node.

    Returns:
        Callee names, with an attribute call reduced to its attribute.
    """
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Name):
            names.add(child.func.id)
        elif isinstance(child.func, ast.Attribute):
            names.add(child.func.attr)
    return frozenset(names)


def _declared_boundaries(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    module: str,
) -> frozenset[PublicationBoundary]:
    """Return the ids the marker on *node* names.

    Args:
        node: A function definition.
        module: Module stem, for the error message.

    Returns:
        The declared ids, empty when the function is unmarked.

    Raises:
        UnregisteredBoundaryError: When the marker names something that
            is not a :class:`PublicationBoundary` member, or names it in
            a form the scan cannot read.
    """
    declared: set[PublicationBoundary] = set()
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call) or _callee_name(decorator) != BOUNDARY_DECORATOR:
            continue
        for argument in decorator.args:
            declared.add(_boundary_member(argument, label=f"{module}:{node.name}"))
    return frozenset(declared)


def _callee_name(call: ast.Call) -> str | None:
    """Return the bare name of *call*'s callee, or ``None``."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _boundary_member(argument: ast.expr, *, label: str) -> PublicationBoundary:
    """Return the boundary the marker argument names.

    Args:
        argument: One argument of a :func:`durable_boundary` call.
        label: ``module:function`` address, for the error message.

    Returns:
        The named member.

    Raises:
        UnregisteredBoundaryError: When the argument is not a
            ``PublicationBoundary.<MEMBER>`` attribute, or names a
            member that does not exist.
    """
    known = PublicationBoundary.__members__
    if not isinstance(argument, ast.Attribute) or argument.attr not in known:
        rendered = ast.unparse(argument)
        raise UnregisteredBoundaryError(
            f"unknown_publication_boundary: {label} declares {rendered!r}, which is not a "
            f"PublicationBoundary member; known: {sorted(PublicationBoundary.__members__)}",
            sites=(label,),
        )
    return PublicationBoundary[argument.attr]


__all__ = [
    "BOUNDARY_ATTR",
    "BOUNDARY_DECORATOR",
    "DURABLE_WRITE_CALLS",
    "EXTERNAL_BOUNDARIES",
    "IN_PACKAGE_BOUNDARIES",
    "BoundarySite",
    "PublicationBoundary",
    "UnregisteredBoundaryError",
    "assert_boundaries_total",
    "boundaries_of",
    "durable_boundary",
    "durable_write_sites",
    "package_sources",
]
