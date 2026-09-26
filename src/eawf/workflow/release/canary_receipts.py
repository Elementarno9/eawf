"""The canary-window receipts a product-canary checkpoint is opened on.

The ``product_canary`` profile's seven extra gates are proof commands: each
re-runs the suite that drives one native producer end to end at the pinned
source. That proves the producer works; it does not say that it was used
on this repository. This module records the second fact. Every receipt
names the evidence of one run the native path made here -- a committed
record, a plan document, a proving suite -- and the operator appends the
references a live run leaves behind (an approval, a dispatched Run) as
data in the committed file rather than as code.

The verdict separates what a merge can hold from what only a pushed tag
can. The six pre-merge receipts decide readiness at merge; the one
post-merge receipt is reported pending rather than missing, so a
checkpoint that has everything a merge can give is "release-ready pending
post-merge observation" instead of being either rounded up to ready or
refused for a fact that does not exist yet.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final

from pydantic import AfterValidator, ConfigDict, Field, ValidationError, model_validator

from eawf.kernel.release.gate_binding import (
    PRODUCT_CANARY_POST_MERGE_GATES,
    PRODUCT_CANARY_PRE_MERGE_GATES,
)
from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.spec.release import ReleaseKeyStr
from eawf.kernel.spec.release_config import ReleaseGateName
from eawf.surfaces.cli.errors import UserError
from eawf.workflow.evidence.provider_certification import CANARY_EVIDENCE_DIRS

logger = logging.getLogger(__name__)

#: The receipt file inside a release's canary evidence directory. It sits
#: beside the canary export because both are evidence the checkpoint is
#: opened on, and one directory per rung keeps an earlier rung's files
#: out of a later rung's reach.
CANARY_RECEIPTS_FILENAME: Final[str] = "product-canary-receipts.json"

#: Every gate a receipt may be filed for.
_CANARY_GATES: Final[frozenset[ReleaseGateName]] = frozenset(
    (*PRODUCT_CANARY_PRE_MERGE_GATES, *PRODUCT_CANARY_POST_MERGE_GATES)
)


def _portable_ref(value: str) -> str:
    """Return *value* when it is a URN, a URL or a repository-relative path.

    Raises:
        ValueError: When *value* is absolute, climbs out of the repository
            or names a home directory, since a committed record must
            resolve on any checkout.
    """
    if "://" in value:
        return value
    if value.startswith(("/", "~")) or ".." in value.split("/") or "\\" in value:
        raise ValueError(f"evidence ref {value!r} is not repository-relative")
    return value


#: One evidence reference: a URN or URL, or a path relative to the root.
EvidenceRef = Annotated[str, Field(min_length=1, max_length=500), AfterValidator(_portable_ref)]


class CanaryReceipt(_StrictModel):
    """One canary-window gate bound to the evidence of a run on this repository.

    Attributes:
        gate: The canary-window gate the receipt is filed for.
        evidence_refs: The committed evidence the run left.
        live_refs: References a live run produced after the record was
            first committed, such as a plan approval, appended as data.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate: ReleaseGateName
    evidence_refs: Annotated[tuple[EvidenceRef, ...], Field(min_length=1)]
    live_refs: tuple[EvidenceRef, ...] = ()

    @model_validator(mode="after")
    def _admits_only_canary_window_gates(self) -> CanaryReceipt:
        """Reject a receipt filed for a gate outside the canary window.

        Raises:
            ValueError: When *gate* is not one of the seven canary-window
                gates; the fifteen earlier gates earn their receipts from
                the gate runner, not from this record.
        """
        if self.gate not in _CANARY_GATES:
            raise ValueError(f"gate {self.gate.value!r} is not a canary-window gate")
        return self


class CanaryReceiptSet(_StrictModel):
    """The committed canary-window receipts of one checkpoint.

    Attributes:
        release_key: The checkpoint the receipts were filed for.
        receipts: At most one receipt per gate.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    release_key: ReleaseKeyStr
    receipts: tuple[CanaryReceipt, ...]

    @model_validator(mode="after")
    def _refuses_a_repeated_receipt(self) -> CanaryReceiptSet:
        """Reject a set that files two receipts for one gate.

        Raises:
            ValueError: When a gate appears twice.
        """
        gates = [receipt.gate for receipt in self.receipts]
        repeated = sorted({gate.value for gate in gates if gates.count(gate) > 1})
        if repeated:
            raise ValueError(f"gate(s) {repeated} carry more than one receipt")
        return self


class CanaryReadinessVerdict(StrEnum):
    """What a checkpoint's canary-window receipts support.

    Values:
        RELEASE_READY: All seven receipts are bound.
        PENDING_POST_MERGE: Every pre-merge receipt is bound and only the
            post-merge observation is outstanding.
        NOT_READY: At least one pre-merge receipt is unbound.
    """

    RELEASE_READY = "release_ready"
    PENDING_POST_MERGE = "release_ready_pending_post_merge_observation"
    NOT_READY = "not_ready"


@dataclass(frozen=True, slots=True)
class CanaryReadiness:
    """The verdict over one receipt set, with what it is waiting on.

    Attributes:
        verdict: What the receipts support.
        unbound_pre_merge: Pre-merge gates with no receipt, in profile
            order; non-empty exactly when the verdict is ``NOT_READY``.
        pending_post_merge: Post-merge gates with no receipt yet.
    """

    verdict: CanaryReadinessVerdict
    unbound_pre_merge: tuple[ReleaseGateName, ...]
    pending_post_merge: tuple[ReleaseGateName, ...]


def assess_canary_receipts(receipts: CanaryReceiptSet) -> CanaryReadiness:
    """Return what *receipts* support at the moment they are read.

    Args:
        receipts: A validated receipt set.

    Returns:
        The verdict, naming every unbound pre-merge gate and every
        outstanding post-merge one.
    """
    bound = {receipt.gate for receipt in receipts.receipts}
    unbound = tuple(gate for gate in PRODUCT_CANARY_PRE_MERGE_GATES if gate not in bound)
    pending = tuple(gate for gate in PRODUCT_CANARY_POST_MERGE_GATES if gate not in bound)
    if unbound:
        verdict = CanaryReadinessVerdict.NOT_READY
    elif pending:
        verdict = CanaryReadinessVerdict.PENDING_POST_MERGE
    else:
        verdict = CanaryReadinessVerdict.RELEASE_READY
    logger.info(
        f"assess_canary_receipts key={receipts.release_key!r} verdict={verdict.value!r} "
        f"unbound={[gate.value for gate in unbound]}"
    )
    return CanaryReadiness(verdict, unbound, pending)


def canary_receipts_path(repo_root: Path, release_key: str) -> Path:
    """Return where *release_key*'s receipt set is committed under *repo_root*.

    Args:
        repo_root: Checkout the receipts were committed in.
        release_key: The checkpoint whose receipts are wanted.

    Returns:
        The receipt file's path, whether or not it exists.

    Raises:
        KeyError: When no canary evidence directory is mapped for
            *release_key*.
    """
    return repo_root.joinpath(*CANARY_EVIDENCE_DIRS[release_key], CANARY_RECEIPTS_FILENAME)


def load_canary_receipts(repo_root: Path, release_key: str) -> CanaryReceiptSet | None:
    """Return *release_key*'s committed receipt set, or ``None`` when absent.

    Args:
        repo_root: Checkout the receipts were committed in.
        release_key: The checkpoint whose receipts are wanted.

    Returns:
        The validated set, or ``None`` when no file is committed or no
        evidence directory is mapped for *release_key*.

    Raises:
        ValueError: When the file is not valid JSON, does not validate,
            or was filed for another checkpoint. An unreadable record is
            not an absent one, so it must never read as unbound either.
    """
    if release_key not in CANARY_EVIDENCE_DIRS:
        return None
    path = canary_receipts_path(repo_root, release_key)
    if not path.is_file():
        return None
    try:
        receipts = CanaryReceiptSet.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"{CANARY_RECEIPTS_FILENAME} for {release_key} is invalid: {exc}") from exc
    if receipts.release_key != release_key:
        raise ValueError(
            f"{CANARY_RECEIPTS_FILENAME} under {release_key} was filed for {receipts.release_key}"
        )
    return receipts


def assert_pre_merge_receipts_bound(release_key: str, receipts: CanaryReceiptSet | None) -> None:
    """Refuse to open a product-canary checkpoint missing a pre-merge receipt.

    Args:
        release_key: The checkpoint being opened.
        receipts: Its committed receipt set, or ``None`` when none is
            committed.

    Raises:
        UserError: ``kind="canary_receipts_unbound"`` naming every
            pre-merge gate without a receipt.
    """
    if receipts is None:
        unbound: tuple[ReleaseGateName, ...] = PRODUCT_CANARY_PRE_MERGE_GATES
    else:
        unbound = assess_canary_receipts(receipts).unbound_pre_merge
    if unbound:
        raise UserError(
            f"{release_key} has no canary-window receipt for "
            f"{[gate.value for gate in unbound]}; file each in its {CANARY_RECEIPTS_FILENAME}",
            kind="canary_receipts_unbound",
        )


__all__ = [
    "CANARY_RECEIPTS_FILENAME",
    "CanaryReadiness",
    "CanaryReadinessVerdict",
    "CanaryReceipt",
    "CanaryReceiptSet",
    "EvidenceRef",
    "assert_pre_merge_receipts_bound",
    "assess_canary_receipts",
    "canary_receipts_path",
    "load_canary_receipts",
]
