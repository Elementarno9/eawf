"""The qualified epoch-2 URN: parser, formatter and kind agreement.

Every machine API, event, persisted link and deep link addresses an
epoch-2 record by a fully qualified URN::

    eawf://<workspace-key>/<project-key>/<repository-key-or-_>/<entity-kind>/<entity-key>

Three rules make the string more than a formatted tuple.

The repository slot is not optional. Its reserved ``_`` value is
admitted only for a workspace- or project-level entity, so a release
reads ``.../PRJ-EAWF/_/release/REL-0.7.0`` and a milestone reads
``.../PRJ-EAWF/REP-EAWF/milestone/MLS-0030``. A URN that puts a
repository-level entity in the reserved slot has lost the repository it
belongs to, and a URN that names a repository for a workspace-level
entity has invented one.

The entity kind must agree with the discriminator of the reference that
carries it. A reference typed as a task holding a milestone URN is the
defect that renders a plausible and wrong row, so
:func:`parse_qualified_urn` takes the discriminator and fails
``identity_kind_mismatch`` rather than returning a parse the caller then
has to re-check.

One evidence rung of a claim is addressed by the claim's URN plus the
fragment ``#rung-<n>`` for *n* in 1 to 4. The fragment selects a rung of
that claim at the claim's current revision and is the only URN form the
rung card's copy verb yields; no epoch-1 ``urn:eawf:`` form and no minted
event key ever addresses a rung. Both a fragment on a non-claim URN and a
rung number outside the ladder are ``identity_kind_mismatch``: the
fragment names a kind of thing the URN does not address.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from eawf.kernel.identity.errors import IdentityError, IdentityRejection
from eawf.kernel.identity.keys import (
    ENTITY_SCOPES,
    RESERVED_REPOSITORY_SLOT,
    EntityKind,
    EntityScope,
    project_code_of,
    validate_entity_key,
    validate_symbol_key,
)

#: URN scheme of the epoch-2 identity space.
URN_SCHEME: Final = "eawf://"

#: Lowest and highest rung a claim's evidence ladder addresses.
RUNG_MIN: Final = 1
RUNG_MAX: Final = 4

_URN_RE: Final = re.compile(
    r"^eawf://(?P<workspace>[^/#]+)/(?P<project>[^/#]+)/(?P<repository>[^/#]+)"
    r"/(?P<kind>[a-z][a-z-]*)/(?P<entity>[^/#]+)(?:#(?P<fragment>[^#]*))?$"
)
_RUNG_RE: Final = re.compile(r"^rung-(?P<ordinal>\d+)$")

_SLOT_SCOPES: Final = frozenset({EntityScope.WORKSPACE, EntityScope.PROJECT})


@dataclass(frozen=True, slots=True)
class QualifiedUrn:
    """A parsed and validated qualified epoch-2 URN.

    Construction validates, so an instance is always renderable: both
    :func:`parse_qualified_urn` and a direct call funnel through the same
    checks and a caller never holds a half-checked address.

    Attributes:
        workspace_key: The addressing workspace.
        project_key: The addressing project.
        repository_key: The addressing repository, or ``None`` for the
            reserved ``_`` slot of a workspace- or project-level entity.
        kind: The entity kind the URN addresses.
        entity_key: The addressed record's public key.
        rung: The evidence rung selected on a claim URN, or ``None``.
    """

    workspace_key: str
    project_key: str
    repository_key: str | None
    kind: EntityKind
    entity_key: str
    rung: int | None = None

    def __post_init__(self) -> None:
        """Validate every slot, the entity key, and the rung fragment.

        Raises:
            IdentityError: Any slot, key, or fragment rule is broken.
        """
        validate_symbol_key(self.workspace_key, slot="workspace")
        validate_symbol_key(self.project_key, slot="project")
        _validate_repository_slot(self.kind, self.repository_key)
        _validate_container_identity(self)
        validate_entity_key(
            self.kind,
            self.entity_key,
            project_code=project_code_of(self.project_key),
        )
        _validate_rung(self.kind, self.rung)

    @property
    def repository_slot(self) -> str:
        """The repository segment as rendered, reserved slot included."""
        if self.repository_key is None:
            return RESERVED_REPOSITORY_SLOT
        return self.repository_key

    def __str__(self) -> str:
        """Render the canonical URN string."""
        base = (
            f"{URN_SCHEME}{self.workspace_key}/{self.project_key}/"
            f"{self.repository_slot}/{self.kind.value}/{self.entity_key}"
        )
        if self.rung is None:
            return base
        return f"{base}#rung-{self.rung}"


def _validate_repository_slot(kind: EntityKind, repository_key: str | None) -> None:
    """Enforce that the reserved slot and a real repository key do not swap."""
    scope = ENTITY_SCOPES[kind]
    if scope in _SLOT_SCOPES:
        if repository_key is not None:
            raise IdentityError(
                IdentityRejection.REPOSITORY_SLOT_INVALID,
                f"{scope.value}-level kind {kind.value} takes the reserved "
                f"{RESERVED_REPOSITORY_SLOT!r} repository slot, got {repository_key!r}",
            )
        return
    if repository_key is None:
        raise IdentityError(
            IdentityRejection.REPOSITORY_SLOT_INVALID,
            f"repository-level kind {kind.value} needs a repository key, "
            f"got the reserved {RESERVED_REPOSITORY_SLOT!r} slot",
        )
    validate_symbol_key(repository_key, slot="repository")


def _validate_container_identity(urn: QualifiedUrn) -> None:
    """Enforce that a container addresses itself by its own slot key.

    A workspace, project, or repository URN names the same symbol twice
    -- once as the addressing slot and once as the entity key. Letting
    the two differ would make ``.../WSP-A/PRJ-B/_/workspace/WSP-C``
    parse, which addresses one workspace from inside another.
    """
    expected = {
        EntityKind.WORKSPACE: urn.workspace_key,
        EntityKind.PROJECT: urn.project_key,
        EntityKind.REPOSITORY: urn.repository_key,
    }.get(urn.kind)
    if expected is not None and urn.entity_key != expected:
        raise IdentityError(
            IdentityRejection.ENTITY_KEY_INVALID,
            f"{urn.kind.value} URN addresses {expected!r} but keys {urn.entity_key!r}",
        )


def _validate_rung(kind: EntityKind, rung: int | None) -> None:
    """Enforce that a rung fragment sits on a claim and inside the ladder."""
    if rung is None:
        return
    if kind is not EntityKind.CLAIM:
        raise IdentityError(
            IdentityRejection.IDENTITY_KIND_MISMATCH,
            f"a #rung-N fragment addresses a claim's evidence ladder, not a {kind.value}",
        )
    if not RUNG_MIN <= rung <= RUNG_MAX:
        raise IdentityError(
            IdentityRejection.IDENTITY_KIND_MISMATCH,
            f"rung {rung} is outside the {RUNG_MIN}..{RUNG_MAX} evidence ladder",
        )


def _parse_kind(token: str) -> EntityKind:
    try:
        return EntityKind(token)
    except ValueError as exc:
        raise IdentityError(
            IdentityRejection.URN_UNKNOWN_KIND,
            f"unknown entity kind {token!r}",
        ) from exc


def _parse_fragment(fragment: str | None) -> int | None:
    if fragment is None:
        return None
    match = _RUNG_RE.fullmatch(fragment)
    if match is None:
        raise IdentityError(
            IdentityRejection.URN_MALFORMED,
            f"unsupported URN fragment {fragment!r}; only #rung-N is addressable",
        )
    return int(match.group("ordinal"))


def parse_qualified_urn(
    raw: str,
    *,
    expected_kind: EntityKind | None = None,
) -> QualifiedUrn:
    """Parse *raw* into a validated :class:`QualifiedUrn`.

    Args:
        raw: The URN string to parse.
        expected_kind: The discriminator of the reference carrying the
            URN. When given, the URN's own kind must equal it.

    Returns:
        The parsed URN.

    Raises:
        IdentityError: ``urn_malformed`` when *raw* is not a qualified
            URN, ``urn_unknown_kind`` when the kind token names no entity
            family, ``identity_kind_mismatch`` when the kind disagrees
            with *expected_kind* or the rung fragment is misplaced, and
            the slot or key rejections raised by construction.
    """
    match = _URN_RE.fullmatch(raw)
    if match is None:
        raise IdentityError(
            IdentityRejection.URN_MALFORMED,
            f"not a qualified eawf URN: {raw!r}",
        )
    kind = _parse_kind(match.group("kind"))
    if expected_kind is not None and kind is not expected_kind:
        raise IdentityError(
            IdentityRejection.IDENTITY_KIND_MISMATCH,
            f"URN addresses a {kind.value} but the reference discriminates "
            f"{expected_kind.value}: {raw!r}",
        )
    repository = match.group("repository")
    return QualifiedUrn(
        workspace_key=match.group("workspace"),
        project_key=match.group("project"),
        repository_key=None if repository == RESERVED_REPOSITORY_SLOT else repository,
        kind=kind,
        entity_key=match.group("entity"),
        rung=_parse_fragment(match.group("fragment")),
    )


def format_qualified_urn(
    *,
    workspace_key: str,
    project_key: str,
    repository_key: str | None,
    kind: EntityKind,
    entity_key: str,
    rung: int | None = None,
) -> str:
    """Render a validated qualified URN string.

    Args:
        workspace_key: The addressing workspace.
        project_key: The addressing project.
        repository_key: The addressing repository, or ``None`` for the
            reserved slot.
        kind: The entity kind being addressed.
        entity_key: The addressed record's public key.
        rung: The evidence rung to select on a claim URN.

    Returns:
        The canonical URN string.

    Raises:
        IdentityError: Any slot, key, or rung rule is broken.
    """
    return str(
        QualifiedUrn(
            workspace_key=workspace_key,
            project_key=project_key,
            repository_key=repository_key,
            kind=kind,
            entity_key=entity_key,
            rung=rung,
        )
    )
