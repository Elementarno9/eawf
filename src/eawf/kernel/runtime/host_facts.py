"""Host facts: the one record of what each supported runtime was measured to do.

Each supported runtime carries its own closed record of the facts that decide
whether a projection reaches a session whole: the project-document cap, the
context window, the auto-compaction threshold and the tool-output cap. A fact
is either certified -- a measured default value with the evidence of the
boundary it claims, the date it was measured and how long that measurement is
trusted -- or uncertified with the reason, and it is never borrowed from
another runtime: a record whose fact was measured on a different runtime, or
that cites another runtime's evidence, is refused.

The record also carries how each runtime discovers policy: which projections
it reads, and the file it owns when it discovers policy only through that file
and so needs an import shim.

The rule render budget and runtime certification both read this record, so a
certified value changes in one place. The module performs no I/O; the shipped
document is loaded by :mod:`eawf.platform.rules.host_facts`.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import ClassVar, Final, Literal, Self

from pydantic import Field, PositiveInt, model_validator

from eawf.kernel.runtime.provider import RuntimeRecord
from eawf.observability.telemetry.models import RuntimeName

#: A projection a runtime can read: the committed card or the policy file.
ProjectionKind = Literal["card", "policy"]

#: The facts every runtime record carries, addressable by name.
HostFactName = Literal[
    "project_document_cap_bytes",
    "context_window_tokens",
    "auto_compaction_threshold_tokens",
    "tool_output_cap_tokens",
]

#: Every host fact, in record order.
HOST_FACT_NAMES: Final[tuple[HostFactName, ...]] = (
    "project_document_cap_bytes",
    "context_window_tokens",
    "auto_compaction_threshold_tokens",
    "tool_output_cap_tokens",
)

#: Every supported runtime, in record order.
HOST_RUNTIMES: Final[tuple[RuntimeName, ...]] = ("claude", "codex", "opencode")


class HostFactError(Exception):
    """Base class for a host-fact record that cannot be trusted.

    Deliberately not a ``ValueError``: a refusal raised while validating a
    record must surface as itself rather than be folded into a generic schema
    error, because it names a certification defect, not a typo.

    Attributes:
        code: The stable failure code.
    """

    code: ClassVar[str] = "host_fact_error"


class HostFactInheritanceError(HostFactError):
    """A runtime's fact was measured on, or copied from, another runtime."""

    code: ClassVar[str] = "host_fact_inherited"


class HostFactEvidenceError(HostFactError):
    """A fact's status disagrees with the value and evidence it carries."""

    code: ClassVar[str] = "host_fact_evidence"


class HostFact(RuntimeRecord):
    """One fact of one runtime, certified or explicitly uncertified.

    Attributes:
        status: ``certified`` when a measured default backs the value.
        measured_on: The runtime the measurement was taken on.
        default_value: The measured default; only a certified fact has one.
        evidence: How the boundary was measured; required when certified.
        measured_at: The day the boundary was measured; required when
            certified.
        remeasure_after_days: How many days the measurement is trusted
            before it must be re-measured; required when certified.
        reason: Why the fact is uncertified; required when uncertified.
    """

    status: Literal["certified", "uncertified"]
    measured_on: RuntimeName
    default_value: PositiveInt | None = None
    evidence: str | None = Field(default=None, min_length=1)
    measured_at: date | None = None
    remeasure_after_days: PositiveInt | None = None
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _status_carries_its_evidence(self) -> Self:
        """Bind the value, evidence, measurement date and reason to the status.

        Raises:
            HostFactEvidenceError: A certified fact lacks its value, evidence,
                measurement date or re-measure age, or carries a reason; or
                an uncertified fact carries any of them or lacks its reason.
        """
        measurement = (self.default_value, self.evidence, self.measured_at)
        if self.status == "certified":
            if any(part is None for part in measurement) or self.reason is not None:
                raise HostFactEvidenceError(
                    "a certified host fact needs a default_value and the evidence of its "
                    "boundary with its measured_at date, and carries no reason"
                )
            if self.remeasure_after_days is None:
                raise HostFactEvidenceError(
                    "a certified host fact states remeasure_after_days, so an old "
                    "measurement is reported stale rather than trusted"
                )
        elif (
            any(part is not None for part in measurement)
            or self.remeasure_after_days is not None
            or self.reason is None
        ):
            raise HostFactEvidenceError(
                "an uncertified host fact carries no default_value, evidence or "
                "measurement date and states the reason it is uncertified"
            )
        return self

    @property
    def remeasure_due(self) -> date | None:
        """Return the last day the measurement is trusted; ``None`` if uncertified."""
        if self.measured_at is None or self.remeasure_after_days is None:
            return None
        return self.measured_at + timedelta(days=self.remeasure_after_days)


class RuntimeHostFacts(RuntimeRecord):
    """The certified facts and policy discovery of one runtime.

    Attributes:
        runtime: The runtime the record describes.
        reads: The projections the runtime loads at session start.
        import_shim: The file the runtime discovers policy through when it
            reads policy only through a file it owns; ``None`` when it reads
            a projection directly.
        project_document_cap_bytes: Bytes of project document delivered.
        context_window_tokens: The context window.
        auto_compaction_threshold_tokens: Where auto-compaction triggers.
        tool_output_cap_tokens: The largest tool output delivered.
    """

    runtime: RuntimeName
    reads: tuple[ProjectionKind, ...] = Field(min_length=1)
    import_shim: str | None = Field(min_length=1)
    project_document_cap_bytes: HostFact
    context_window_tokens: HostFact
    auto_compaction_threshold_tokens: HostFact
    tool_output_cap_tokens: HostFact

    @model_validator(mode="after")
    def _facts_are_this_runtimes_own(self) -> Self:
        """Refuse a fact measured on another runtime and an impossible threshold.

        Raises:
            HostFactInheritanceError: A fact names another runtime as the
                one it was measured on.
            HostFactEvidenceError: ``reads`` repeats a projection, or the
                certified compaction threshold exceeds the certified window.
        """
        for name in HOST_FACT_NAMES:
            fact = self.fact(name)
            if fact.measured_on != self.runtime:
                raise HostFactInheritanceError(
                    f"{self.runtime} {name} was measured on {fact.measured_on}; a host fact "
                    f"is certified per runtime and never inherited from another"
                )
        if len(set(self.reads)) != len(self.reads):
            raise HostFactEvidenceError(f"{self.runtime} reads repeats a projection: {self.reads}")
        window = self.context_window_tokens.default_value
        threshold = self.auto_compaction_threshold_tokens.default_value
        if window is not None and threshold is not None and threshold > window:
            raise HostFactEvidenceError(
                f"{self.runtime} auto_compaction_threshold_tokens {threshold} exceeds "
                f"context_window_tokens {window}"
            )
        return self

    def fact(self, name: HostFactName) -> HostFact:
        """Return the fact called ``name``.

        Args:
            name: The fact name.

        Returns:
            The fact.

        Raises:
            KeyError: ``name`` is not a host fact.
        """
        if name not in HOST_FACT_NAMES:
            raise KeyError(name)
        fact: HostFact = getattr(self, name)
        return fact


class HostFactRegistry(RuntimeRecord):
    """The closed set of runtime records, one per supported runtime.

    Attributes:
        schema_version: Gates unknown future formats.
        claude: The Claude record.
        codex: The Codex record.
        opencode: The OpenCode record.
    """

    schema_version: Literal[1]
    claude: RuntimeHostFacts
    codex: RuntimeHostFacts
    opencode: RuntimeHostFacts

    @model_validator(mode="after")
    def _no_record_borrows_another(self) -> Self:
        """Refuse a record filed under another runtime or citing its evidence.

        Raises:
            HostFactInheritanceError: A record describes a runtime other
                than the one it is filed under, or two runtimes cite the same
                evidence for one certified fact.
            HostFactEvidenceError: Two runtimes own the same shim file.
        """
        for name in HOST_RUNTIMES:
            record = self.runtime(name)
            if record.runtime != name:
                raise HostFactInheritanceError(
                    f"the {name} host-fact record describes {record.runtime}"
                )
        for fact_name in HOST_FACT_NAMES:
            cited: dict[str, RuntimeName] = {}
            for name in HOST_RUNTIMES:
                evidence = self.runtime(name).fact(fact_name).evidence
                if evidence is None:
                    continue
                if evidence in cited:
                    raise HostFactInheritanceError(
                        f"{name} {fact_name} cites the evidence certified for "
                        f"{cited[evidence]}; each runtime carries its own measurement"
                    )
                cited[evidence] = name
        shims = [r.import_shim for r in self.records if r.import_shim is not None]
        if len(set(shims)) != len(shims):
            raise HostFactEvidenceError(f"two runtimes own the same import shim: {shims}")
        return self

    @property
    def records(self) -> tuple[RuntimeHostFacts, ...]:
        """Return every runtime record, in record order."""
        return tuple(self.runtime(name) for name in HOST_RUNTIMES)

    def runtime(self, name: RuntimeName) -> RuntimeHostFacts:
        """Return the record of runtime ``name``.

        Args:
            name: The runtime.

        Returns:
            Its record.

        Raises:
            KeyError: ``name`` is not a supported runtime.
        """
        if name not in HOST_RUNTIMES:
            raise KeyError(name)
        record: RuntimeHostFacts = getattr(self, name)
        return record


class StaleHostFact(RuntimeRecord):
    """A certified fact whose measurement is older than it is trusted for.

    Attributes:
        runtime: The runtime the fact belongs to.
        fact: The stale fact.
        measured_at: The day it was measured.
        remeasure_due: The last day the measurement was trusted.
    """

    runtime: RuntimeName
    fact: HostFactName
    measured_at: date
    remeasure_due: date

    @property
    def note(self) -> str:
        """Return a one-line operator report of the stale fact."""
        return (
            f"stale host fact: {self.runtime} {self.fact} was measured {self.measured_at} "
            f"and was due for re-measurement by {self.remeasure_due}; it is reported, "
            f"not trusted, until it is re-measured"
        )


def parse_host_facts(raw: object) -> HostFactRegistry:
    """Validate a host-fact document into the registry.

    Args:
        raw: The parsed YAML document.

    Returns:
        The registry.

    Raises:
        ValidationError: The document does not match the closed schema.
        HostFactInheritanceError: A fact was borrowed from another runtime.
        HostFactEvidenceError: A fact's status disagrees with its evidence.
    """
    return HostFactRegistry.model_validate(raw)


def stale_host_facts(registry: HostFactRegistry, *, today: date) -> tuple[StaleHostFact, ...]:
    """Name every certified fact past its re-measure age.

    A fact is stale from the day after its re-measure date: on the due date
    itself the measurement is still trusted.

    Args:
        registry: The host facts.
        today: The day the facts are judged on.

    Returns:
        The stale facts, in runtime then fact order.
    """
    stale: list[StaleHostFact] = []
    for record in registry.records:
        for name in HOST_FACT_NAMES:
            fact = record.fact(name)
            due = fact.remeasure_due
            if fact.measured_at is None or due is None or today <= due:
                continue
            stale.append(
                StaleHostFact(
                    runtime=record.runtime,
                    fact=name,
                    measured_at=fact.measured_at,
                    remeasure_due=due,
                )
            )
    return tuple(stale)


__all__ = [
    "HOST_FACT_NAMES",
    "HOST_RUNTIMES",
    "HostFact",
    "HostFactError",
    "HostFactEvidenceError",
    "HostFactInheritanceError",
    "HostFactName",
    "HostFactRegistry",
    "ProjectionKind",
    "RuntimeHostFacts",
    "StaleHostFact",
    "parse_host_facts",
    "stale_host_facts",
]
