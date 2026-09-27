"""Adopting work that already landed on a Batch's target branch.

Integration normally starts from a sealed candidate: a Run's claim about
its leased workspace, checked against the lease and bound to an accepted
report. Work merged outside the native loop has none of that -- no lease
was ever issued, so no seal can be taken -- yet its commits are on the
branch the Batch integrates into. Replaying it through integration would
author a second commit for changes that already exist, and the head that
generation bound would be one no branch carries.

An adoption is the record that replaces the seal for that case, and only
for that case. It names the landed head and the base the change started
from, both read back from the repository rather than presented, and the
branch head the landed commit was found under. It carries the report
verdict it adopts on the strength of recorded evidence the tree holds, so
a success that nothing recorded cannot be adopted. It proves integration,
never correctness: the Task still completes only on receipts taken at the
adopted head.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from eawf.kernel.delivery.integration import RepoRelativePath
from eawf.kernel.delivery.receipts import canonical_digest
from eawf.kernel.runtime.candidate import DELIVERABLE_VERDICTS
from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.state.epoch2.base import BranchName, Epoch2Model, PrincipalKey
from eawf.kernel.state.epoch2.urns import BatchUrn, TaskUrn
from eawf.kernel.state.models import ShaStr
from eawf.kernel.state.types import UtcDatetime

#: The Batch-ledger key prefix an adoption is filed under.
ADOPTION_KEY_PREFIX: Final = "ADP-"

#: The status an adoption line records.
ADOPTION_STATUS: Final = "adopted"

#: One reference to a recorded audit, decision, artifact or evidence row.
EvidenceRef = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=200)]


class LandedAdoption(Epoch2Model):
    """One already-landed change, adopted as a Batch's next delivery.

    Attributes:
        payload_kind: The discriminator separating an adoption line from
            every other line of the Batch ledger.
        batch_ref: The Batch the change is adopted onto.
        task_refs: The Tasks whose work the change carries.
        base_commit: The commit the change started from.
        head_sha: The landed commit being adopted.
        tree_sha: The tree of that commit, as the repository reports it.
        parent_sha: Its first parent, as the repository reports it.
        target_branch: The branch the landed commit was found on.
        target_head_sha: That branch's head when it was read.
        changed_paths: Every path between the base and the landed head.
        report_verdict: The verdict the adopted Runs' reports carried.
        evidence_refs: The recorded rows that verdict rests on.
        adopted_by: Who adopted it.
        adopted_at: When.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    payload_kind: Literal["landed_adoption"] = "landed_adoption"
    batch_ref: BatchUrn
    task_refs: tuple[TaskUrn, ...] = Field(min_length=1)
    base_commit: ShaStr
    head_sha: ShaStr
    tree_sha: ShaStr
    parent_sha: ShaStr | None
    target_branch: BranchName
    target_head_sha: ShaStr
    changed_paths: tuple[RepoRelativePath, ...] = Field(min_length=1)
    report_verdict: AgentReportVerdict
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    adopted_by: PrincipalKey
    adopted_at: UtcDatetime

    @model_validator(mode="after")
    def _adopts_a_delivered_change(self) -> Self:
        """Refuse an adoption of nothing, or of a report that proposes nothing.

        Raises:
            ValueError: The base is the head, a Task repeats, or the
                verdict does not propose the work for delivery.
        """
        if self.base_commit == self.head_sha:
            raise ValueError("an adoption must span at least one commit past its base")
        if len({str(item) for item in self.task_refs}) != len(self.task_refs):
            raise ValueError("an adoption names the same Task twice")
        if self.report_verdict not in DELIVERABLE_VERDICTS:
            raise ValueError(
                f"verdict {self.report_verdict.value} does not propose the work for delivery"
            )
        return self

    def digest(self) -> str:
        """Return the ``sha256:`` identity of what was adopted.

        The stamp and the adopter are left out, so adopting the same landed
        change twice names the same record rather than a second one.
        """
        return canonical_digest(self.model_dump(mode="json", exclude={"adopted_by", "adopted_at"}))

    def record_key(self) -> str:
        """Return the Batch-ledger key this adoption is filed under."""
        body = self.digest().removeprefix("sha256:")
        return f"{ADOPTION_KEY_PREFIX}{body[:12]}-{self.batch_ref.entity_key}"

    def bundle_key(self) -> str:
        """Return the ``CB-########`` key the adopted generation names as its proposal.

        A generation names the proposal it came from; an adopted one came
        from this record, so the key is derived from it the way a squashed
        delivery derives its key from its manifest.
        """
        body = self.digest().removeprefix("sha256:")
        return f"CB-{int(body[:8], 16) % 100000000:08d}"

    def carries(self, task_ref: str) -> bool:
        """Return whether the adopted change carries *task_ref*'s work."""
        return any(str(item) == task_ref for item in self.task_refs)


__all__ = [
    "ADOPTION_KEY_PREFIX",
    "ADOPTION_STATUS",
    "EvidenceRef",
    "LandedAdoption",
]
