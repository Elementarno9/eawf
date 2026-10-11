"""Commit bindings staged on scratch work and bound to a published commit by tree.

A scratch commit may be rewritten before publish, by a rebase or a
cherry-pick, so a ledger row naming its SHA could bind something no
published branch carries. Adopting work on a scratch commit therefore writes
a staged binding into the generation's machine-local tier instead of the
committed ledger. A rewrite keeps the content, so the staged row is later
bound to the one published commit whose tree equals the staged
``tree_sha``; the scratch ``head_sha`` stays on the row for reference only
and is never matched on.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.delivery.adoption import EvidenceRef
from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.state.epoch2.urns import BatchUrn, TaskUrn
from eawf.kernel.state.models import ShaStr
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.state.writer import atomic_write_json

#: Refusal code: no published commit carries the staged tree.
STAGED_TREE_UNPUBLISHED: Final = "staged_tree_unpublished"

#: Refusal code: more than one published commit carries the staged tree.
STAGED_TREE_AMBIGUOUS: Final = "staged_tree_ambiguous"


class StagedBinding(BaseModel):
    """One Batch's commit binding, held back until its work is published.

    Attributes:
        batch_ref: The Batch the work was adopted onto.
        task_refs: The Tasks the work carries.
        tree_sha: The scratch commit's tree, which the published commit
            must reproduce to be bound.
        head_sha: The scratch commit itself, kept for reference only.
        report_verdict: The verdict the adopted Runs' reports carried.
        evidence_refs: The recorded rows that verdict rests on.
        staged_by: Who adopted the work.
        staged_at: When.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_ref: BatchUrn
    task_refs: tuple[TaskUrn, ...] = Field(min_length=1)
    tree_sha: ShaStr
    head_sha: ShaStr
    report_verdict: AgentReportVerdict
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    staged_by: PrincipalKey
    staged_at: UtcDatetime

    def file_name(self) -> str:
        """Return the staged file's name: one file per Batch and tree.

        Re-adopting the same content on a new scratch commit names the same
        file, so an amend before publish replaces the row instead of leaving
        two rows the matcher would both bind.
        """
        return f"{self.batch_ref.entity_key}-{self.tree_sha}.json"


class PublishedCommit(BaseModel):
    """One commit a publish produced, offered to the matcher.

    Attributes:
        sha: The published commit.
        tree_sha: Its tree.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    sha: ShaStr
    tree_sha: ShaStr


class PublishedBinding(BaseModel):
    """A staged binding resolved to the published commit that carries its tree.

    Attributes:
        staged: The staged row that was bound.
        head_sha: The published commit it now binds.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    staged: StagedBinding
    head_sha: ShaStr


class StagedBindingRefusedError(ValueError):
    """A staged binding matched no published commit, or more than one.

    Attributes:
        code: :data:`STAGED_TREE_UNPUBLISHED` or :data:`STAGED_TREE_AMBIGUOUS`.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def stage_binding(directory: Path, staged: StagedBinding) -> Path:
    """Write *staged* into the staged directory and return its path.

    Args:
        directory: The generation's staged directory; created when absent.
        staged: The binding to hold back until publish.

    Returns:
        The file the row was written to.
    """
    path = directory / staged.file_name()
    atomic_write_json(path, staged.model_dump(mode="json"))
    return path


def bind_staged(staged: StagedBinding, candidates: Sequence[PublishedCommit]) -> PublishedBinding:
    """Bind *staged* to the one candidate whose tree equals its ``tree_sha``.

    Args:
        staged: The row staged when the scratch work was adopted.
        candidates: The commits the publish produced.

    Returns:
        The staged row bound to its published commit.

    Raises:
        StagedBindingRefusedError: No candidate carries the tree, so the work
            was not published as adopted; or several distinct candidates
            do, so the tree alone cannot say which one the work became.
    """
    matches = sorted({item.sha for item in candidates if item.tree_sha == staged.tree_sha})
    batch = staged.batch_ref.entity_key
    if not matches:
        raise StagedBindingRefusedError(
            STAGED_TREE_UNPUBLISHED,
            f"no published commit carries tree {staged.tree_sha} staged for batch {batch}",
        )
    if len(matches) > 1:
        raise StagedBindingRefusedError(
            STAGED_TREE_AMBIGUOUS,
            f"{len(matches)} published commits carry tree {staged.tree_sha} staged for batch "
            f"{batch}: {', '.join(matches)}",
        )
    return PublishedBinding(staged=staged, head_sha=matches[0])


__all__ = [
    "STAGED_TREE_AMBIGUOUS",
    "STAGED_TREE_UNPUBLISHED",
    "PublishedBinding",
    "PublishedCommit",
    "StagedBinding",
    "StagedBindingRefusedError",
    "bind_staged",
    "stage_binding",
]
