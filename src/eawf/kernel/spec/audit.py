"""AuditSpec — typed declarative audit document.

AuditSpec extends :class:`~eawf.workflow.audit_dsl.models.CheckFile` with the
cadence binding, scope linkage, and verdict citations the audit-DSL
runner needs at Task / Batch / Milestone close. Per the C03 D10 lock the
``cadence`` field configures *when* the audit fires; the audit-DSL
runner consults each ``AuditSpec.cadence`` at the close event and
dispatches the named kinds only when the cadence matches the
trigger.

Storage shape (per C03 brief §5.6): on-disk at
``.ea/audits/<scope>.audit.yaml`` (NOT under ``.ea/specs/`` because
audits attach to scopes already in state.json, not to spec docs).
"""

from __future__ import annotations

from typing import Annotated, Literal, get_args

from pydantic import BeforeValidator, Field

from eawf.kernel.spec.cadence import refuse_retired_cadence
from eawf.kernel.spec.common import GateCadence, VerdictCitation, _StrictModel
from eawf.kernel.state.enums import AuditKind
from eawf.workflow.audit_dsl.models import CheckSpec

# One cadence enum serves the audit, the gate and the profile floor check
# (AGENTS naming rule 17): the spec model owns the contract, the audit-DSL
# kind and the floor-pack callers apply it.
AuditCadence = GateCadence

AUDIT_CADENCE_VALUES: frozenset[str] = frozenset(get_args(AuditCadence))

# The cadence of an authored document: an epoch-1 value is refused with the
# native value that replaced it rather than read silently.
AuthoredCadence = Annotated[AuditCadence, BeforeValidator(refuse_retired_cadence)]


class AuditSpec(_StrictModel):
    """Declarative audit document — extends CheckFile with cadence + scope.

    Cadence enum values (per C03 D10):

    * ``every-task`` — fires on every Task close.
    * ``every-batch`` — fires on Batch close.
    * ``every-milestone`` — fires on Milestone close.
    * ``ship`` — fires on the ship-CI / pre-merge gauntlet.
    * ``manual`` — only operator-driven invocation fires the audit.

    The audit-DSL runner reads ``cadence`` at the close event firing
    the audit and short-circuits the dispatched check kinds when the
    cadence does not match the trigger (see
    :func:`~eawf.workflow.audit_dsl.kinds.verify_implements.check_verify_implements`).
    """

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["AuditSpec"] = "AuditSpec"

    id: str = Field(min_length=1, max_length=120)
    scope_urn: str = Field(min_length=1)
    audit_kind: AuditKind
    cadence: AuthoredCadence
    implements: list[VerdictCitation] = Field(default_factory=list)
    checks: list[CheckSpec] = Field(min_length=1)
    fail_fast: bool = False


__all__ = ["AUDIT_CADENCE_VALUES", "AuditCadence", "AuditSpec", "AuthoredCadence"]
