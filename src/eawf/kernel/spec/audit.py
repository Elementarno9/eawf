"""AuditSpec — typed declarative audit document.

AuditSpec extends :class:`~eawf.audit_dsl.models.CheckFile` with the
cadence binding, scope linkage, and verdict citations the audit-DSL
runner needs at phase / iter / wave close. Per the C03 D10 lock the
``cadence`` field configures *when* the audit fires; the audit-DSL
runner consults each ``AuditSpec.cadence`` at the close event and
dispatches the named kinds only when the cadence matches the
trigger.

Storage shape (per C03 brief §5.6): on-disk at
``.ea/audits/<scope>.audit.yaml`` (NOT under ``.ea/specs/`` because
audits attach to scopes already in state.json, not to spec docs).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from eawf.audit_dsl.models import CheckSpec
from eawf.kernel.spec.common import VerdictCitation, _StrictModel
from eawf.kernel.state.enums import AuditKind

# The four cadence enum values the AuditSpec.cadence field accepts.
# Mirrors :data:`eawf.audit_dsl.kinds.verify_implements._VALID_CADENCES`
# — the spec model owns the contract, the audit-DSL kind applies it.
AuditCadence = Literal["every-wave", "every-iter", "every-phase", "manual"]

AUDIT_CADENCE_VALUES: frozenset[str] = frozenset(
    {"every-wave", "every-iter", "every-phase", "manual"}
)


class AuditSpec(_StrictModel):
    """Declarative audit document — extends CheckFile with cadence + scope.

    Cadence enum values (per C03 D10):

    * ``every-wave`` — fires on every wave close.
    * ``every-iter`` — fires on iter close.
    * ``every-phase`` — fires on phase close.
    * ``manual`` — only operator-driven invocation fires the audit.

    The audit-DSL runner reads ``cadence`` at the close event firing
    the audit and short-circuits the dispatched check kinds when the
    cadence does not match the trigger (see
    :func:`~eawf.audit_dsl.kinds.verify_implements.check_verify_implements`).
    """

    schema_version: Literal["1.0"] = "1.0"
    kind: Literal["AuditSpec"] = "AuditSpec"

    id: str = Field(min_length=1, max_length=120)
    scope_urn: str = Field(min_length=1)
    audit_kind: AuditKind
    cadence: AuditCadence
    implements: list[VerdictCitation] = Field(default_factory=list)
    checks: list[CheckSpec] = Field(min_length=1)
    fail_fast: bool = False


__all__ = ["AUDIT_CADENCE_VALUES", "AuditCadence", "AuditSpec"]
