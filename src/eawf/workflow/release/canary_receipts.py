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
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, ConfigDict, Field, ValidationError, model_validator

from eawf.kernel.release.gate_binding import (
    PRODUCT_CANARY_POST_MERGE_GATES,
    PRODUCT_CANARY_PRE_MERGE_GATES,
)
from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.spec.release import ReleaseKeyStr
from eawf.kernel.spec.release_config import ReleaseGateName
from eawf.kernel.state.epoch2.base import RunKey
from eawf.surfaces.cli.errors import UserError
from eawf.workflow.evidence.provider_certification import CANARY_EVIDENCE_DIRS, OsClass

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


#: The live dispatch smoke's receipt file, beside the canary receipts in
#: the same per-release evidence directory.
DISPATCH_SMOKE_RECEIPTS_FILENAME: Final[str] = "dispatch-smoke-receipts.json"


class DispatchSmokeReceipt(_StrictModel):
    """One live dispatch smoke on one host class, scored by ``eawf bench dispatch``.

    Attributes:
        os_class: The operating-system class the smoke ran on.
        runtime_id: The runtime the smoke dispatched its Tasks to.
        run_refs: The dispatched Runs the scorecard was checked over.
        scorecard: ``pass`` when every Run met the ``fixture`` tier.
        evidence_refs: The per-Run check documents filed beside the
            receipt, by file name; a passing scorecard counts only when
            each Run's document is there and clean.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    os_class: OsClass
    runtime_id: Annotated[str, Field(min_length=1, max_length=64)]
    run_refs: Annotated[tuple[EvidenceRef, ...], Field(min_length=1)]
    scorecard: Literal["pass", "fail"]
    evidence_refs: tuple[EvidenceRef, ...] = ()


class DispatchSmokeReceiptSet(_StrictModel):
    """The committed dispatch smoke receipts of one checkpoint.

    Attributes:
        release_key: The checkpoint the smokes were run for.
        receipts: At most one receipt per os_class and runtime.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    release_key: ReleaseKeyStr
    receipts: tuple[DispatchSmokeReceipt, ...]

    @model_validator(mode="after")
    def _refuses_a_repeated_receipt(self) -> DispatchSmokeReceiptSet:
        """Reject a set that files two receipts for one os_class and runtime.

        Raises:
            ValueError: When an ``(os_class, runtime_id)`` pair repeats,
                since a reader could not tell which smoke stands.
        """
        pairs = [(receipt.os_class, receipt.runtime_id) for receipt in self.receipts]
        repeated = sorted({pair for pair in pairs if pairs.count(pair) > 1})
        if repeated:
            raise ValueError(f"(os_class, runtime_id) {repeated} carry more than one receipt")
        return self


#: A check row's observed value or limit: a count, a status or a decimal
#: written as a string, or ``None`` when unmeasured or unlimited.
_CheckValue = int | str | None


class DispatchCheckRow(_StrictModel):
    """One metric of a filed check document beside the tier's limit.

    Attributes:
        metric: The scorecard value checked.
        observed: What the Run measured.
        limit: The tier's limit on it.
        breached: Whether the value is past the limit.
    """

    metric: Annotated[str, Field(min_length=1, max_length=64)]
    observed: _CheckValue
    limit: _CheckValue
    breached: bool


class DispatchCheckScorecard(_StrictModel):
    """The scorecard a filed check document was checked over.

    Attributes:
        run_key: The Run scored.
        status: The Run's recorded status.
        turns: How many model calls the Run made.
        read_truncation_rate: Share of cut read windows, as a decimal string.
        identical_reread_rate: Share of identical re-reads, as a decimal string.
        total_tokens: Every token the Run billed, cache reads included.
        billable_tokens: The name an earlier scorecard gave
            ``total_tokens``; the committed rc3 documents carry it.
        cache_read_tokens: The cache-read slice of the token count.
        first_patch_seconds: Seconds to the first succeeded patch.
        wall_seconds: Seconds from the Run's start to its end.
    """

    run_key: RunKey
    status: Annotated[str, Field(min_length=1, max_length=32)]
    turns: Annotated[int, Field(ge=0)]
    read_truncation_rate: str
    identical_reread_rate: str
    total_tokens: Annotated[int, Field(ge=0)] | None = None
    billable_tokens: Annotated[int, Field(ge=0)] | None = None
    cache_read_tokens: Annotated[int, Field(ge=0)] | None
    first_patch_seconds: str | None
    wall_seconds: str | None


class DispatchCheckBody(_StrictModel):
    """A scorecard checked against one tier, as ``bench dispatch --json`` writes it.

    Attributes:
        tier: The tier checked against.
        scorecard: The scorecard checked.
        checks: One row per metric.
        breaches: The metrics past their limit.
    """

    tier: Literal["fixture", "real"]
    scorecard: DispatchCheckScorecard
    checks: Annotated[tuple[DispatchCheckRow, ...], Field(min_length=1)]
    breaches: tuple[str, ...]

    @model_validator(mode="after")
    def _breaches_match_the_rows(self) -> DispatchCheckBody:
        """Reject a document whose breach list disagrees with its rows.

        Raises:
            ValueError: When ``breaches`` is not exactly the breached
                rows' metrics, since an emptied list would otherwise
                clear a Run its own rows show breached.
        """
        breached = tuple(row.metric for row in self.checks if row.breached)
        if self.breaches != breached:
            raise ValueError(f"breaches {list(self.breaches)} disagree with rows {list(breached)}")
        return self


class DispatchCheckDocument(_StrictModel):
    """One Run's check document, filed beside the dispatch smoke receipt.

    Attributes:
        check: The scorecard checked against its tier.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    check: DispatchCheckBody


class DispatchSmokeGap(StrEnum):
    """Why a claimed os_class is not backed by a passing dispatch smoke.

    Values:
        MISSING: No receipt was filed for the class.
        FAILED: A receipt for the class carries a failing scorecard.
        CHECK_MISSING: A passing receipt lacks a check document, beside
            the receipt file, for one of its Runs.
        CHECK_MISMATCHED: A passing receipt's documents do not name its
            Runs one for one.
        CHECK_BREACHED: A passing receipt's document records a breach.
    """

    MISSING = "dispatch_smoke_missing"
    FAILED = "dispatch_smoke_failed"
    CHECK_MISSING = "dispatch_smoke_check_missing"
    CHECK_MISMATCHED = "dispatch_smoke_check_mismatched"
    CHECK_BREACHED = "dispatch_smoke_check_breached"


def read_dispatch_smoke_receipts(path: Path) -> DispatchSmokeReceiptSet | None:
    """Return the receipt set filed at *path*, or ``None`` when no file is there.

    Args:
        path: A ``dispatch-smoke-receipts.json`` file.

    Returns:
        The validated set, or ``None`` when *path* is not a file.

    Raises:
        ValueError: When the file is not valid JSON or does not validate.
    """
    if not path.is_file():
        return None
    try:
        return DispatchSmokeReceiptSet.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"{DISPATCH_SMOKE_RECEIPTS_FILENAME} is invalid: {exc}") from exc


def load_dispatch_smoke_receipts(
    repo_root: Path, release_key: str
) -> DispatchSmokeReceiptSet | None:
    """Return *release_key*'s committed dispatch smoke receipts, or ``None`` when absent.

    Args:
        repo_root: Checkout the receipts were committed in.
        release_key: The checkpoint whose receipts are wanted.

    Returns:
        The validated set, or ``None`` when no file is committed.

    Raises:
        KeyError: When no canary evidence directory is mapped for
            *release_key*.
        ValueError: When the file does not read back or was filed for
            another checkpoint.
    """
    path = repo_root.joinpath(*CANARY_EVIDENCE_DIRS[release_key], DISPATCH_SMOKE_RECEIPTS_FILENAME)
    receipts = read_dispatch_smoke_receipts(path)
    if receipts is not None and receipts.release_key != release_key:
        raise ValueError(
            f"{DISPATCH_SMOKE_RECEIPTS_FILENAME} under {release_key} was filed for "
            f"{receipts.release_key}"
        )
    return receipts


def read_dispatch_checks(
    directory: Path, receipts: DispatchSmokeReceiptSet | None
) -> dict[str, DispatchCheckDocument]:
    """Return every check document a passing receipt names that is filed in *directory*.

    Only a bare file name resolves: the smoke writes each document beside
    the receipt file, so a reference into another directory is left out
    and reads as missing.

    Args:
        directory: The directory the receipt file was read from.
        receipts: The set read there, or ``None`` when none is filed.

    Returns:
        Each present document, validated, by the file name a receipt
        cites it by.

    Raises:
        ValueError: When a present document is not valid JSON or does
            not validate.
    """
    filed = receipts.receipts if receipts is not None else ()
    names = {
        ref
        for receipt in filed
        if receipt.scorecard == "pass"
        for ref in receipt.evidence_refs
        if "://" not in ref and Path(ref).name == ref
    }
    documents: dict[str, DispatchCheckDocument] = {}
    for name in sorted(names):
        path = directory / name
        if not path.is_file():
            continue
        try:
            documents[name] = DispatchCheckDocument.model_validate(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ValueError(f"{name} is invalid: {exc}") from exc
    return documents


def _check_gap(
    receipt: DispatchSmokeReceipt, documents: dict[str, DispatchCheckDocument]
) -> DispatchSmokeGap | None:
    """Return why *receipt*'s check documents do not back its passing scorecard.

    Args:
        receipt: A receipt whose scorecard reads ``pass``.
        documents: The documents read beside it, by file name.

    Returns:
        The first gap of missing, mismatched and breached, or ``None``
        when each Run has exactly one clean document naming it.
    """
    if len(receipt.evidence_refs) != len(receipt.run_refs) or any(
        ref not in documents for ref in receipt.evidence_refs
    ):
        return DispatchSmokeGap.CHECK_MISSING
    cited = [documents[ref] for ref in receipt.evidence_refs]
    named = sorted(document.check.scorecard.run_key for document in cited)
    if named != sorted(ref.rsplit("/", 1)[-1] for ref in receipt.run_refs):
        return DispatchSmokeGap.CHECK_MISMATCHED
    if any(document.check.breaches for document in cited):
        return DispatchSmokeGap.CHECK_BREACHED
    return None


def dispatch_smoke_gaps(
    receipts: DispatchSmokeReceiptSet | None,
    claims: tuple[tuple[str, OsClass], ...],
    documents: dict[str, DispatchCheckDocument],
) -> tuple[tuple[str, OsClass, DispatchSmokeGap], ...]:
    """Return each claimed runtime and os_class a passing dispatch smoke does not back.

    A smoke backs only the runtime it dispatched to, so a claude-code
    smoke on a class says nothing about a codex claim on it. A passing
    scorecard is the smoke's own word, so it backs a claim only when the
    check documents filed beside it show every Run clean.

    Args:
        receipts: The committed set, or ``None`` when none is committed.
        claims: The ``(runtime_id, os_class)`` pairs the checkpoint
            claims, in claim order.
        documents: The check documents read beside the set, by file name,
            as :func:`read_dispatch_checks` returns them.

    Returns:
        One ``(runtime_id, os_class, gap)`` per pair with no receipt, a
        failing one, or a passing one its documents do not back; empty
        when every pair is backed.
    """
    filed = receipts.receipts if receipts is not None else ()
    gaps: list[tuple[str, OsClass, DispatchSmokeGap]] = []
    for runtime_id, os_class in dict.fromkeys(claims):
        matching = [
            receipt
            for receipt in filed
            if (receipt.runtime_id, receipt.os_class) == (runtime_id, os_class)
        ]
        if not matching:
            gaps.append((runtime_id, os_class, DispatchSmokeGap.MISSING))
            continue
        # The set refuses a repeated pair, so one receipt stands per pair.
        (receipt,) = matching
        gap = (
            DispatchSmokeGap.FAILED
            if receipt.scorecard == "fail"
            else _check_gap(receipt, documents)
        )
        if gap is not None:
            gaps.append((runtime_id, os_class, gap))
    return tuple(gaps)


__all__ = [
    "CANARY_RECEIPTS_FILENAME",
    "DISPATCH_SMOKE_RECEIPTS_FILENAME",
    "CanaryReadiness",
    "CanaryReadinessVerdict",
    "CanaryReceipt",
    "CanaryReceiptSet",
    "DispatchCheckDocument",
    "DispatchSmokeGap",
    "DispatchSmokeReceipt",
    "DispatchSmokeReceiptSet",
    "EvidenceRef",
    "assert_pre_merge_receipts_bound",
    "assess_canary_receipts",
    "canary_receipts_path",
    "dispatch_smoke_gaps",
    "load_canary_receipts",
    "load_dispatch_smoke_receipts",
    "read_dispatch_checks",
    "read_dispatch_smoke_receipts",
]
