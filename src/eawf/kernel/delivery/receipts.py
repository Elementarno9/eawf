"""Freshness-keyed proof receipts and the exact revision they bind.

A deterministic proof is worth reusing only while every input that could
change its outcome is still what it was when the proof ran. These models
type those inputs. :class:`RevisionBinding` names the exact code a proof
ran against; :class:`ProofFreshnessKey` joins that binding with the
compiled criterion and gate contract, the selector result, the runner,
the environment, the effective policy and every declared external input;
:class:`ProofReceipt` records one result bound to exactly one key; and
:class:`ReceiptReuseDecision` is the per-leg verdict an audit reads, so a
reused proof and a rerun are both named rather than inferred.

The models are pure. Assembling a key from runtime facts and deciding
reuse live in :mod:`eawf.runtime.verification.receipts`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Any, Final, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    model_validator,
)

from eawf.kernel.identity import EntityKind, validate_entity_key
from eawf.kernel.spec.common import CriterionEvidenceKind
from eawf.kernel.state.enums import GateReceiptResult
from eawf.kernel.state.epoch2.base import (
    Epoch2Model,
    Sha256DigestStr,
    ShaStr,
    SlugStr,
    StrictPositiveInt,
)
from eawf.kernel.state.epoch2.urns import BatchUrn, RepositoryUrn
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.kinds.gate_receipt import GateIdentityStr


def _validate_receipt_key(value: str) -> str:
    """Admit only a canonical ``RCP-####`` receipt key."""
    return validate_entity_key(EntityKind.RECEIPT, value)


#: An ``RCP-####`` receipt key; the grammar has one home in the identity
#: package, so the alias delegates rather than re-spelling the pattern.
ReceiptKey = Annotated[
    str,
    StringConstraints(strict=True),
    AfterValidator(_validate_receipt_key),
]

#: The bare 64-hex SHA-256 a freshness key renders to. It is the same
#: shape the committed gate receipt stores, so either receipt family can
#: be looked up by the one key string.
FreshnessDigestStr = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def canonical_digest(payload: Any) -> str:
    """Return the ``sha256:`` digest of *payload* in canonical JSON form.

    Keys are sorted and insignificant whitespace dropped, so two
    structurally equal payloads digest alike whatever order the author
    wrote them in.

    Args:
        payload: A JSON-serialisable value, typically a ``model_dump`` in
            ``json`` mode.

    Returns:
        The ``sha256:``-prefixed lowercase hex digest.

    Raises:
        TypeError: When *payload* holds a value JSON cannot encode.
    """
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"


class _FrozenModel(Epoch2Model):
    """Strict and immutable: a proof input edited in place is a forged proof."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RevisionRefKind(StrEnum):
    """The closed semantic class of the Git ref a binding was taken on.

    The class is part of the code identity: a candidate-ref proof and an
    integration-ref proof of the same tree answer different questions, so
    neither may stand in for the other.
    """

    CANDIDATE = "candidate"
    INTEGRATION = "integration"
    REVIEW_CHECKPOINT = "review_checkpoint"
    DELIVERY_CANDIDATE = "delivery_candidate"
    TARGET_BRANCH = "target_branch"
    TAG = "tag"


class RevisionBinding(_FrozenModel):
    """The exact code, and the frozen plan inputs, a proof was taken against.

    The tree is required even beside the commit because it is the content
    identity; the commit additionally pins ancestry and metadata. A Batch
    lives in exactly one repository, so the Batch reference must sit under
    the bound repository. ``parent_sha`` and ``environment_digest`` are
    required keys that may be null, so an unknown value is stated rather
    than defaulted.
    """

    repository_ref: RepositoryUrn
    ref_kind: RevisionRefKind
    head_sha: ShaStr
    tree_sha: ShaStr
    parent_sha: ShaStr | None
    batch_ref: BatchUrn
    integration_generation: StrictPositiveInt
    manifest_digest: Sha256DigestStr
    criteria_digest: Sha256DigestStr
    policy_digest: Sha256DigestStr
    environment_digest: Sha256DigestStr | None
    bound_at: UtcDatetime

    @model_validator(mode="after")
    def _batch_belongs_to_repository(self) -> Self:
        """Require the Batch URN to address the bound repository.

        Raises:
            ValueError: The Batch sits under another workspace, project, or
                repository than ``repository_ref`` names.
        """
        repository = self.repository_ref
        batch = self.batch_ref
        same_container = (
            batch.workspace_key == repository.workspace_key
            and batch.project_key == repository.project_key
            and batch.repository_key == repository.entity_key
        )
        if not same_container:
            raise ValueError(
                f"batch {batch.entity_key!r} is not under repository {repository.entity_key!r}"
            )
        return self

    @model_validator(mode="after")
    def _parent_differs_from_head(self) -> Self:
        """Refuse a commit that names itself as its own parent.

        Raises:
            ValueError: ``parent_sha`` equals ``head_sha``.
        """
        if self.parent_sha is not None and self.parent_sha == self.head_sha:
            raise ValueError("parent_sha cannot equal head_sha")
        return self


class FreshnessComponent(StrEnum):
    """One independently comparable input of a :class:`ProofFreshnessKey`.

    A stale receipt names which of these changed, so an operator reads
    "the environment moved" instead of "the key differs".
    """

    CODE = "code"
    CRITERION = "criterion"
    GATE = "gate"
    SELECTOR = "selector"
    POLICY = "policy"
    RUNNER = "runner"
    ENVIRONMENT = "environment"
    EXTERNAL_INPUT = "external_input"


class ExternalInputDigest(_FrozenModel):
    """One declared external input a proof depends on, by name and content."""

    name: SlugStr
    digest: Sha256DigestStr


#: Which freshness component each :class:`RevisionBinding` field feeds.
#: ``bound_at`` maps to ``None``: it is when the binding was observed, not
#: what was bound, so re-observing the same head must not stale a proof.
REVISION_BINDING_COMPONENTS: Final[Mapping[str, FreshnessComponent | None]] = {
    "repository_ref": FreshnessComponent.CODE,
    "ref_kind": FreshnessComponent.CODE,
    "head_sha": FreshnessComponent.CODE,
    "tree_sha": FreshnessComponent.CODE,
    "parent_sha": FreshnessComponent.CODE,
    "batch_ref": FreshnessComponent.CODE,
    "integration_generation": FreshnessComponent.CODE,
    "manifest_digest": FreshnessComponent.CODE,
    "criteria_digest": FreshnessComponent.CRITERION,
    "policy_digest": FreshnessComponent.POLICY,
    "environment_digest": FreshnessComponent.ENVIRONMENT,
    "bound_at": None,
}

#: Which freshness component each direct :class:`ProofFreshnessKey` field
#: feeds. ``revision_binding`` is split through
#: :data:`REVISION_BINDING_COMPONENTS` instead.
FRESHNESS_KEY_COMPONENTS: Final[Mapping[str, FreshnessComponent]] = {
    "criterion_digest": FreshnessComponent.CRITERION,
    "gate_digest": FreshnessComponent.GATE,
    "selector_digest": FreshnessComponent.SELECTOR,
    "policy_digest": FreshnessComponent.POLICY,
    "runner_digest": FreshnessComponent.RUNNER,
    "environment_digest": FreshnessComponent.ENVIRONMENT,
    "external_inputs": FreshnessComponent.EXTERNAL_INPUT,
}


class ProofFreshnessKey(_FrozenModel):
    """Every input whose change could change a proof's outcome.

    The key is the reuse identity of a receipt: two runs with equal keys
    are the same proof. Its digest is taken per
    :class:`FreshnessComponent` and then over the component digests, so a
    comparison can say which inputs moved. External inputs are a declared
    set, so their order never changes the key.

    ``criterion_digest`` and ``gate_digest`` come from the compiled
    execution contract; ``policy_digest`` is the effective verification
    policy; ``runner_digest`` identifies the runner and toolchain.
    """

    revision_binding: RevisionBinding
    criterion_digest: Sha256DigestStr
    gate_digest: Sha256DigestStr
    selector_digest: Sha256DigestStr
    policy_digest: Sha256DigestStr
    runner_digest: Sha256DigestStr
    environment_digest: Sha256DigestStr
    external_inputs: tuple[ExternalInputDigest, ...] = ()

    @model_validator(mode="after")
    def _external_input_names_unique(self) -> Self:
        """Require each declared external input to appear once.

        Raises:
            ValueError: Two external inputs share a name, which would make
                the declared dependency ambiguous.
        """
        names = [item.name for item in self.external_inputs]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"external_inputs repeats {', '.join(duplicates)}")
        return self

    @model_validator(mode="after")
    def _one_environment(self) -> Self:
        """Refuse a key whose binding and runner disagree on the environment.

        Raises:
            ValueError: The binding records an environment digest that
                differs from ``environment_digest``.
        """
        bound = self.revision_binding.environment_digest
        if bound is not None and bound != self.environment_digest:
            raise ValueError("revision_binding.environment_digest differs from environment_digest")
        return self

    def component_digests(self) -> dict[FreshnessComponent, str]:
        """Return one ``sha256:`` digest per :class:`FreshnessComponent`.

        Returns:
            A digest for every component, in enum order. A component fed by
            several fields digests all of them together.
        """
        groups: dict[FreshnessComponent, dict[str, Any]] = {
            component: {} for component in FreshnessComponent
        }
        binding = self.revision_binding.model_dump(mode="json")
        for name, value in binding.items():
            component = REVISION_BINDING_COMPONENTS[name]
            if component is not None:
                groups[component][f"revision_binding.{name}"] = value
        for name, value in self.model_dump(mode="json", exclude={"revision_binding"}).items():
            if name == "external_inputs":
                value = sorted((item["name"], item["digest"]) for item in value)
            groups[FRESHNESS_KEY_COMPONENTS[name]][name] = value
        return {component: canonical_digest(payload) for component, payload in groups.items()}

    def digest(self) -> str:
        """Return the bare 64-hex key a receipt stores and is looked up by.

        Returns:
            The SHA-256 hex digest over the per-component digests.
        """
        components = {
            component.value: value for component, value in self.component_digests().items()
        }
        body = json.dumps(components, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _require_component_totality(model: type[BaseModel], table: Mapping[str, object]) -> None:
    """Refuse to import when a model field feeds no freshness component.

    A field left out of the tables would change a proof's inputs without
    changing its key, which silently admits a stale receipt.

    Args:
        model: The model whose every field must be mapped.
        table: Field name to component; ``None`` marks a deliberately
            unkeyed field.

    Raises:
        TypeError: The table and the model's fields differ.
    """
    drift = set(model.model_fields) ^ set(table)
    if drift:
        raise TypeError(
            f"{model.__name__} freshness table drifted from its fields: {sorted(drift)}"
        )


_require_component_totality(RevisionBinding, REVISION_BINDING_COMPONENTS)
_require_component_totality(
    ProofFreshnessKey, {**FRESHNESS_KEY_COMPONENTS, "revision_binding": None}
)


class ProofReceipt(_FrozenModel):
    """One gate result bound to exactly one :class:`ProofFreshnessKey`.

    ``freshness_key`` must be the digest of ``freshness``, so a receipt
    cannot claim a key its recorded inputs do not produce. A new proof
    supersedes an old one by reference and never rewrites it.
    """

    id: ReceiptKey
    scope_id: GateIdentityStr
    gate_id: GateIdentityStr
    criterion_ids: tuple[GateIdentityStr, ...] = Field(min_length=1)
    evidence_kind: CriterionEvidenceKind
    freshness: ProofFreshnessKey
    freshness_key: FreshnessDigestStr
    result: GateReceiptResult
    exit_status: StrictInt | None
    started_at: UtcDatetime
    ended_at: UtcDatetime
    supersedes_id: ReceiptKey | None = None

    @model_validator(mode="after")
    def _key_matches_inputs(self) -> Self:
        """Require the stored key to be the digest of the recorded inputs.

        Raises:
            ValueError: ``freshness_key`` is not ``freshness.digest()``.
        """
        if self.freshness_key != self.freshness.digest():
            raise ValueError(f"receipt {self.id!r} freshness_key does not match its inputs")
        return self

    @model_validator(mode="after")
    def _record_is_consistent(self) -> Self:
        """Reject reversed time, repeated criteria, and self-supersession.

        Raises:
            ValueError: ``ended_at`` precedes ``started_at``, a criterion
                id repeats, or the receipt supersedes itself.
        """
        if self.ended_at < self.started_at:
            raise ValueError("ended_at cannot precede started_at")
        if len(set(self.criterion_ids)) != len(self.criterion_ids):
            raise ValueError(f"receipt {self.id!r} repeats a criterion id")
        if self.supersedes_id == self.id:
            raise ValueError(f"receipt {self.id!r} cannot supersede itself")
        return self


class ReuseDisposition(StrEnum):
    """What an audit does with one required verification leg."""

    REUSE = "reuse"
    RERUN = "rerun"
    UNAVAILABLE = "unavailable"


class ReuseReason(StrEnum):
    """Why a leg got its disposition; each reason implies one disposition."""

    FRESH = "fresh"
    MISSING = "missing"
    STALE = "stale"
    NOT_PASSED = "not_passed"
    EXPIRED = "expired"
    NOT_DETERMINISTIC = "not_deterministic"


#: The disposition each reason implies. A judgment proof is never reused
#: and the deterministic runner cannot produce one, so it is unavailable to
#: this path rather than rerun.
REASON_DISPOSITION: Final[Mapping[ReuseReason, ReuseDisposition]] = {
    ReuseReason.FRESH: ReuseDisposition.REUSE,
    ReuseReason.MISSING: ReuseDisposition.RERUN,
    ReuseReason.STALE: ReuseDisposition.RERUN,
    ReuseReason.NOT_PASSED: ReuseDisposition.RERUN,
    ReuseReason.EXPIRED: ReuseDisposition.RERUN,
    ReuseReason.NOT_DETERMINISTIC: ReuseDisposition.UNAVAILABLE,
}

#: Reasons reachable without any candidate receipt to compare against.
_RECEIPTLESS_REASONS: Final = frozenset({ReuseReason.MISSING, ReuseReason.NOT_DETERMINISTIC})


class ComponentComparison(_FrozenModel):
    """One freshness component of a candidate receipt against the expected key."""

    component: FreshnessComponent
    expected_digest: Sha256DigestStr
    recorded_digest: Sha256DigestStr

    @property
    def matches(self) -> bool:
        """Return whether the recorded input equals the expected one."""
        return self.expected_digest == self.recorded_digest


class ReceiptReuseDecision(_FrozenModel):
    """The reuse verdict for one required leg, with the evidence behind it.

    When a candidate receipt exists every component is compared and
    recorded, so a rerun caused by staleness names the inputs that moved.
    """

    scope_id: GateIdentityStr
    gate_id: GateIdentityStr
    criterion_ids: tuple[GateIdentityStr, ...] = Field(min_length=1)
    receipt_id: ReceiptKey | None
    expected_freshness_key: FreshnessDigestStr
    comparisons: tuple[ComponentComparison, ...] = ()
    disposition: ReuseDisposition
    reason: ReuseReason

    @property
    def changed_components(self) -> tuple[FreshnessComponent, ...]:
        """Return the compared components whose digests differ."""
        return tuple(item.component for item in self.comparisons if not item.matches)

    @model_validator(mode="after")
    def _disposition_follows_reason(self) -> Self:
        """Require the disposition the reason implies.

        Raises:
            ValueError: ``disposition`` is not ``REASON_DISPOSITION[reason]``.
        """
        expected = REASON_DISPOSITION[self.reason]
        if self.disposition is not expected:
            raise ValueError(
                f"reason {self.reason.value} implies disposition {expected.value}, "
                f"not {self.disposition.value}"
            )
        return self

    @model_validator(mode="after")
    def _comparisons_match_candidate(self) -> Self:
        """Tie the comparison rows and the reason to the candidate receipt.

        Raises:
            ValueError: A receipt-free decision carries comparisons or a
                reason that needs a receipt; a decision with a receipt does
                not compare every component exactly once; or the reason
                disagrees with whether any component changed.
        """
        if self.receipt_id is None:
            if self.comparisons:
                raise ValueError("comparisons require a candidate receipt")
            if self.reason not in _RECEIPTLESS_REASONS:
                raise ValueError(f"reason {self.reason.value} requires a candidate receipt")
            return self
        if self.reason is ReuseReason.MISSING:
            raise ValueError("reason missing cannot name a candidate receipt")
        compared = [item.component for item in self.comparisons]
        if sorted(compared) != sorted(FreshnessComponent):
            raise ValueError("a candidate receipt must be compared on every component once")
        stale = bool(self.changed_components)
        if stale != (self.reason is ReuseReason.STALE):
            raise ValueError(
                f"reason {self.reason.value} disagrees with changed components "
                f"{[item.value for item in self.changed_components]}"
            )
        return self


class ReceiptReusePlan(_FrozenModel):
    """Every required leg's reuse decision, so an audit names each rerun."""

    decisions: tuple[ReceiptReuseDecision, ...]

    @model_validator(mode="after")
    def _one_decision_per_leg(self) -> Self:
        """Require each ``(scope_id, gate_id)`` leg to be decided once.

        Raises:
            ValueError: A leg carries two decisions.
        """
        legs = [(item.scope_id, item.gate_id) for item in self.decisions]
        if len(set(legs)) != len(legs):
            raise ValueError("a verification leg is decided more than once")
        return self

    def _with(self, disposition: ReuseDisposition) -> tuple[ReceiptReuseDecision, ...]:
        return tuple(item for item in self.decisions if item.disposition is disposition)

    @property
    def reused(self) -> tuple[ReceiptReuseDecision, ...]:
        """Return the legs satisfied by a fresh receipt."""
        return self._with(ReuseDisposition.REUSE)

    @property
    def reruns(self) -> tuple[ReceiptReuseDecision, ...]:
        """Return the legs that must run again, each carrying its reason."""
        return self._with(ReuseDisposition.RERUN)

    @property
    def unavailable(self) -> tuple[ReceiptReuseDecision, ...]:
        """Return the judgment legs this deterministic path cannot settle."""
        return self._with(ReuseDisposition.UNAVAILABLE)


__all__ = [
    "FRESHNESS_KEY_COMPONENTS",
    "REASON_DISPOSITION",
    "REVISION_BINDING_COMPONENTS",
    "ComponentComparison",
    "ExternalInputDigest",
    "FreshnessComponent",
    "FreshnessDigestStr",
    "ProofFreshnessKey",
    "ProofReceipt",
    "ReceiptKey",
    "ReceiptReuseDecision",
    "ReceiptReusePlan",
    "ReuseDisposition",
    "ReuseReason",
    "RevisionBinding",
    "RevisionRefKind",
    "canonical_digest",
]
