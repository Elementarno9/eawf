"""The provider permission: a held call whose deadline the provider owns.

A provider permission is not a kind of pending action, and the two are
kept apart by type rather than by a flag. A pending action's deadline is
eawf's own: the daemon set it, the daemon can freeze it, and a held
action simply stays open. A provider permission's deadline counts down
inside the provider's call. eawf cannot extend it, and when it lapses the
provider denies the call whether or not anybody here answered. So the
record carries its own URN family, its own deadline and its own
authority, and it resolves without touching any pending action on the
same Run.

Three properties are structural rather than checked by whoever remembers
to check them:

* ``hold_supported`` is ``Literal[False]`` and ``deadline_owner`` is
  ``Literal["provider"]``. A hold offered on this record would promise a
  pause the provider does not grant, so :func:`decide_permission` refuses
  the hold verb on the record's kind before it looks at anything else,
  with :data:`PROTECTED_ACTION_REQUIRED` and the reason.
* A lapse is a resolution, never an absence. :func:`expire_permission`
  writes ``decision = expired`` with the provider as the deciding party,
  because a silent auto-deny is the failure this record exists to make
  visible.
* The repository principal class is inadmissible on every verb of an
  auth, credential or cost-basis permission. :attr:`repository_may_approve`
  is therefore false for those by construction, and a surface renders the
  repository affordance disabled with the authority that does hold it
  named, rather than leaving it out.
"""

from __future__ import annotations

import logging
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, Field, StringConstraints, model_validator

from eawf.kernel.identity import EntityKind, validate_entity_key
from eawf.kernel.runtime.compiled import BoundedText
from eawf.kernel.runtime.provider import RuntimeRecord, ToolCapabilityId, reject_repeats
from eawf.kernel.runtime.semantic import CallId, SemanticToolErrorCode
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictPositiveInt
from eawf.kernel.state.epoch2.urns import PermissionUrn, RunUrn
from eawf.kernel.state.epoch2.values import HOLD_REFUSAL_CODE, HOLD_REFUSAL_REASON
from eawf.kernel.state.types import UtcDatetime

logger = logging.getLogger(__name__)


def _validate_permission_key(value: str) -> str:
    """Admit only a canonical ``PERM-####`` permission key."""
    return validate_entity_key(EntityKind.PERMISSION, value)


#: A ``PERM-####`` provider-permission key. The grammar has one home in
#: the identity package, so the alias delegates rather than re-spelling it.
PermissionKey = Annotated[
    str,
    StringConstraints(strict=True),
    AfterValidator(_validate_permission_key),
]

#: The refusal a hold on a provider permission earns, resolved through the
#: gateway's closed code set so the two spellings cannot drift apart.
PROTECTED_ACTION_REQUIRED: Final = SemanticToolErrorCode(HOLD_REFUSAL_CODE).value


class PermissionVerb(StrEnum):
    """What a principal may ask of a provider permission.

    ``hold`` is a member so that asking for it is a refusal the daemon
    states, rather than a request the schema drops before anyone learns
    why it was not offered.
    """

    APPROVE = "approve"
    DENY = "deny"
    HOLD = "hold"


#: The verbs that decide a permission, and so carry an authority each.
DECIDING_VERBS: Final[tuple[PermissionVerb, ...]] = (PermissionVerb.APPROVE, PermissionVerb.DENY)


class PrincipalClass(StrEnum):
    """Which class of principal may exercise a verb."""

    OPERATOR = "operator"
    WORKSPACE = "workspace"
    REPOSITORY = "repository"


class PermissionActionClass(StrEnum):
    """What the held call would do, which bounds who may decide it."""

    TOOL = "tool"
    FILESYSTEM = "filesystem"
    NETWORK = "network"
    AUTH = "auth"
    CREDENTIAL = "credential"
    COST_BASIS = "cost_basis"


#: The action classes no repository principal may decide. Each one reaches
#: past the repository -- to an account, a secret or a bill -- so policy
#: committed to the repository is not an authority over it.
REPOSITORY_BARRED_CLASSES: Final[frozenset[PermissionActionClass]] = frozenset(
    {
        PermissionActionClass.AUTH,
        PermissionActionClass.CREDENTIAL,
        PermissionActionClass.COST_BASIS,
    }
)

#: The principal classes one verb admits. Repeats are refused, not folded.
PrincipalClasses = Annotated[
    tuple[PrincipalClass, ...], Field(min_length=1), AfterValidator(reject_repeats)
]


class PermissionRefusalCode(StrEnum):
    """Why a request against a provider permission was refused."""

    PROTECTED_ACTION_REQUIRED = PROTECTED_ACTION_REQUIRED
    ALREADY_RESOLVED = "permission_already_resolved"
    LAPSED = "permission_lapsed"
    STALE_REVISION = "stale_revision"
    AUTHORITY_DENIED = "authority_denied"


class PermissionRefusalError(ValueError):
    """A request against a provider permission was refused.

    Attributes:
        code: Which rule refused it.
        reason: One sentence an operator reads.
    """

    def __init__(self, code: PermissionRefusalCode, reason: str) -> None:
        """Store the code and lead the message with it.

        Args:
            code: The rule that refused the request.
            reason: The operator-facing explanation.
        """
        super().__init__(f"{code.value}: {reason}")
        self.code = code
        self.reason = reason


class ApprovalAuthority(RuntimeRecord):
    """Who may exercise each deciding verb.

    Approve and deny are separate because they need not carry the same
    authority: letting a repository refuse a call is not letting it grant
    one.
    """

    approve: PrincipalClasses
    deny: PrincipalClasses

    def admits(self, verb: PermissionVerb, principal_class: PrincipalClass) -> bool:
        """Return whether *principal_class* may exercise *verb*.

        Args:
            verb: A deciding verb.
            principal_class: The class of the principal asking.

        Returns:
            Whether the class is listed for the verb. ``hold`` admits no
            class, because there is no hold to exercise.
        """
        if verb is PermissionVerb.HOLD:
            return False
        classes = self.approve if verb is PermissionVerb.APPROVE else self.deny
        return principal_class in classes


class PermissionResolution(RuntimeRecord):
    """How a provider permission ended.

    Attributes:
        decision: Approved or denied by a principal, or expired when the
            provider's deadline passed unanswered.
        decided_by: ``principal`` for an answer, ``provider`` for a lapse.
            A lapse is the provider denying the call; nobody here decided it.
        principal_class: The class the answering principal acted as.
        principal_ref: The answering principal.
        decided_at: When the decision was recorded.
    """

    decision: Literal["approved", "denied", "expired"]
    decided_by: Literal["principal", "provider"]
    principal_class: PrincipalClass | None = None
    principal_ref: PrincipalKey | None = None
    decided_at: UtcDatetime

    @model_validator(mode="after")
    def _decider_matches_the_decision(self) -> Self:
        """Bind an expiry to the provider and an answer to a named principal.

        Raises:
            ValueError: An expiry names a principal or is attributed to
                one, or an answer names no principal and class.
        """
        expired = self.decision == "expired"
        if expired != (self.decided_by == "provider"):
            raise ValueError(
                f"decision {self.decision!r} is decided by the provider exactly when it expired"
            )
        named = self.principal_ref is not None and self.principal_class is not None
        if expired and (self.principal_ref is not None or self.principal_class is not None):
            raise ValueError("an expiry is the provider's, so it names no principal")
        if not expired and not named:
            raise ValueError(f"decision {self.decision!r} names the principal and its class")
        return self


class ProviderPermission(RuntimeRecord):
    """One brokered call held pending a principal decision.

    Each line on the run ledger is one revision of the record; the latest
    revision of a key is the record.

    Attributes:
        payload_kind: The discriminator separating a permission line from
            every other line the run collection holds.
        uid: The record's stable identity.
        key: The ``PERM-####`` public key.
        urn: The record's own URN, in the permission family.
        run_ref: The Run the held call belongs to.
        call_ref: The exact call awaiting release.
        tool_id: The tool the call invokes. The permission covers this call
            only and is never widened to the tool or its family.
        action_class: What the call would do.
        request_scope: What the provider asked, in words.
        deadline_owner: Always the provider.
        deadline_at: The provider's deadline as observed. eawf never chose
            it and never extends it.
        hold_supported: Always false, and unsettable.
        expiry_outcome: What the lapse does, stated before it happens.
        approval_authority: Who may approve and who may deny.
        opened_at: When the daemon recorded the held call.
        resolution: How it ended, or ``None`` while it is open.
        revision: The compare-and-swap target of the next decision.
    """

    payload_kind: Literal["provider_permission"] = "provider_permission"
    uid: UUID
    key: PermissionKey
    urn: PermissionUrn
    run_ref: RunUrn
    call_ref: CallId
    tool_id: ToolCapabilityId
    action_class: PermissionActionClass
    request_scope: BoundedText
    deadline_owner: Literal["provider"] = "provider"
    deadline_at: UtcDatetime
    hold_supported: Literal[False] = False
    expiry_outcome: Literal["provider_denied"] = "provider_denied"
    approval_authority: ApprovalAuthority
    opened_at: UtcDatetime
    resolution: PermissionResolution | None = None
    revision: StrictPositiveInt = 1

    @model_validator(mode="after")
    def _key_is_the_urn_key(self) -> Self:
        """Require the URN to address the record's own key.

        Raises:
            ValueError: The URN names another permission.
        """
        if self.urn.entity_key != self.key:
            raise ValueError(f"urn addresses {self.urn.entity_key}, not {self.key}")
        return self

    @model_validator(mode="after")
    def _repository_is_barred_where_it_has_no_reach(self) -> Self:
        """Refuse a repository principal on an auth, credential or cost verb.

        Raises:
            ValueError: The action class reaches past the repository and a
                deciding verb still lists the repository class.
        """
        if self.action_class not in REPOSITORY_BARRED_CLASSES:
            return self
        for verb in DECIDING_VERBS:
            if self.approval_authority.admits(verb, PrincipalClass.REPOSITORY):
                raise ValueError(
                    f"a {self.action_class.value} permission admits no repository principal, "
                    f"but {verb.value} lists one"
                )
        return self

    @model_validator(mode="after")
    def _resolution_respects_the_deadline(self) -> Self:
        """Place an answer before the deadline and an expiry at or after it.

        Raises:
            ValueError: An answer is dated at or past the deadline, which
                the provider had already denied, or an expiry is dated
                before the deadline it claims passed.
        """
        resolution = self.resolution
        if resolution is None:
            return self
        lapsed = resolution.decided_at >= self.deadline_at
        if (resolution.decision == "expired") != lapsed:
            raise ValueError(
                f"decision {resolution.decision!r} at {resolution.decided_at.isoformat()} "
                f"disagrees with the provider deadline {self.deadline_at.isoformat()}"
            )
        return self

    @property
    def repository_may_approve(self) -> bool:
        """Whether the repository principal class may approve this call."""
        return self.approval_authority.admits(PermissionVerb.APPROVE, PrincipalClass.REPOSITORY)


def decide_permission(
    permission: ProviderPermission,
    *,
    verb: PermissionVerb,
    principal_class: PrincipalClass,
    principal_ref: PrincipalKey,
    expected_revision: int,
    now: datetime,
) -> ProviderPermission:
    """Return the next revision of *permission* after a principal's verb.

    Args:
        permission: The latest revision of the record.
        verb: What the principal asks.
        principal_class: The class the principal acts as.
        principal_ref: The principal.
        expected_revision: The revision the principal decided against.
        now: The daemon's recording clock.

    Returns:
        The resolved record, one revision on.

    Raises:
        PermissionRefusalError: ``protected_action_required`` for a hold,
            whatever the record's state, because the refusal is a property
            of the kind; ``permission_already_resolved`` for a record that
            has ended; ``permission_lapsed`` past the provider's deadline,
            where the caller records the expiry instead, whatever revision
            the principal decided against; ``stale_revision`` for a
            decision against another revision; and
            ``authority_denied`` for a class the verb does not admit.
    """
    if verb is PermissionVerb.HOLD:
        raise PermissionRefusalError(
            PermissionRefusalCode.PROTECTED_ACTION_REQUIRED, HOLD_REFUSAL_REASON
        )
    if permission.resolution is not None:
        raise PermissionRefusalError(
            PermissionRefusalCode.ALREADY_RESOLVED,
            f"{permission.key} already ended as {permission.resolution.decision}",
        )
    if now >= permission.deadline_at:
        raise PermissionRefusalError(
            PermissionRefusalCode.LAPSED,
            f"the provider's deadline for {permission.key} passed at "
            f"{permission.deadline_at.isoformat()}, so the provider has denied the call",
        )
    if expected_revision != permission.revision:
        raise PermissionRefusalError(
            PermissionRefusalCode.STALE_REVISION,
            f"{permission.key} is at revision {permission.revision}, not {expected_revision}",
        )
    if not permission.approval_authority.admits(verb, principal_class):
        raise PermissionRefusalError(
            PermissionRefusalCode.AUTHORITY_DENIED,
            f"{principal_class.value} may not {verb.value} {permission.key}",
        )
    decision: Literal["approved", "denied"] = (
        "approved" if verb is PermissionVerb.APPROVE else "denied"
    )
    logger.info(f"decide_permission key={permission.key} decision={decision}")
    return permission.model_copy(
        update={
            "resolution": PermissionResolution(
                decision=decision,
                decided_by="principal",
                principal_class=principal_class,
                principal_ref=principal_ref,
                decided_at=now,
            ),
            "revision": permission.revision + 1,
        }
    )


def expire_permission(
    permission: ProviderPermission, *, now: datetime
) -> ProviderPermission | None:
    """Return the expiry revision of a lapsed, unanswered permission.

    Args:
        permission: The latest revision of the record.
        now: The daemon's recording clock.

    Returns:
        The record resolved ``expired`` by the provider, one revision on,
        or ``None`` when it has already ended or its deadline is still
        ahead.
    """
    if permission.resolution is not None or now < permission.deadline_at:
        return None
    logger.info(f"expire_permission key={permission.key}")
    return permission.model_copy(
        update={
            "resolution": PermissionResolution(
                decision="expired", decided_by="provider", decided_at=now
            ),
            "revision": permission.revision + 1,
        }
    )


__all__ = [
    "DECIDING_VERBS",
    "PROTECTED_ACTION_REQUIRED",
    "REPOSITORY_BARRED_CLASSES",
    "ApprovalAuthority",
    "PermissionActionClass",
    "PermissionKey",
    "PermissionRefusalCode",
    "PermissionRefusalError",
    "PermissionResolution",
    "PermissionVerb",
    "PrincipalClass",
    "ProviderPermission",
    "decide_permission",
    "expire_permission",
]
