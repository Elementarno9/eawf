"""The receipts of perfect-realization packet runs, and what they prove for a release.

One packet run carries a scratch repository from ``eawf init --quick`` through
one planned Task, its native Run on a real runtime, the landed Batch and the
Run's printed transcript; :mod:`eawf.workflow.release.packet_producer` drives
it. Each run appends one receipt to this machine's local store. The stable
checkpoint's ``packet_run`` assertion then asks whether every runtime and
repository origin passed on Linux for the version being tagged.

A receipt binds to the eawf *version* that ran it and to the source commit
the run imported eawf from. The commit a stable tag points at is the
acceptance state commit, which only exists after the runs, so the gate
counts a receipt whose commit is an ancestor of the released commit rather
than equal to it. A run from a dirty source tree counts for nothing, since
no commit names the code it ran.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field, ValidationError, model_validator

from eawf.kernel.runtime.certification import CertifiedVersionRange
from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.state.types import Sha256DigestStr, ShaStr, UtcDatetime
from eawf.workflow.evidence.provider_certification import OsClass

logger = logging.getLogger(__name__)

#: The local directory, relative to the repository root, that holds the
#: receipts and each run's evidence log.
PACKET_RUN_DIR: Final[tuple[str, ...]] = (".ea", "local", "release", "packet-runs")

#: The append-only receipt file inside :data:`PACKET_RUN_DIR`.
PACKET_RECEIPTS_FILENAME: Final[str] = "receipts.jsonl"

#: The runtimes a packet run is dispatched to.
PacketRuntimeId = Literal["claude-code", "codex"]

#: The runtimes the stable checkpoint needs a passing run of.
PACKET_RUNTIMES: Final[tuple[PacketRuntimeId, ...]] = ("claude-code", "codex")

#: The operating-system class the stable checkpoint needs the runs on.
PACKET_OS_CLASS: Final[OsClass] = "linux"


class PacketOrigin(StrEnum):
    """How the scratch repository came to carry eawf.

    Values:
        BORN_NATIVE: eawf was initialised in an empty repository before any
            project file existed.
        ADOPTED: An existing repository with files and history adopted eawf.
    """

    BORN_NATIVE = "born_native"
    ADOPTED = "adopted"


class PacketStep(StrEnum):
    """The steps of one packet run, in order.

    Values:
        SCAFFOLD: Create the scratch repository for the origin.
        INIT: Start the daemon and run ``eawf init --quick``.
        PLAN: Create the Track, Milestone, Task and Batch, activate the
            Milestone and promote the Task with its criterion.
        CERTIFY: Certify the runtime for the scratch tree.
        DISPATCH: Dispatch the Task as a native Run and await its end.
        LAND: Cherry-pick the Run's commit and close the Batch, which
            proves the Task's criterion at the landed head.
        TRANSCRIPT: Print the Run's indexed transcript with
            ``eawf run transcript``.
    """

    SCAFFOLD = "scaffold"
    INIT = "init"
    PLAN = "plan"
    CERTIFY = "certify"
    DISPATCH = "dispatch"
    LAND = "land"
    TRANSCRIPT = "transcript"


class PacketRunReceipt(_StrictModel):
    """What one packet run on this machine showed.

    Attributes:
        schema_version: The receipt format.
        runtime: The runtime the Task was dispatched to.
        origin: How the scratch repository came to carry eawf.
        os_class: The operating-system class the run executed on.
        eawf_version: The eawf version that drove the run.
        eawf_commit: The commit of the source tree the run imported eawf
            from.
        source_dirty: Whether that source tree carried uncommitted changes,
            so the commit does not name the code that ran.
        runtime_version: The runtime's installed version.
        budget_tokens: The token ceiling the dispatched Run was capped at.
        outcome: Whether every step passed.
        failed_step: The first step that failed; set exactly when the
            outcome is ``fail``.
        failure: What the failed step reported.
        started_at: When the run began.
        ended_at: When the run ended.
        evidence_digest: The sha256 of the run's evidence log, stored beside
            the receipts under that digest.
    """

    schema_version: Literal["packet-run/v2"] = "packet-run/v2"
    runtime: PacketRuntimeId
    origin: PacketOrigin
    os_class: OsClass
    eawf_version: Annotated[str, Field(min_length=1, max_length=64)]
    eawf_commit: ShaStr
    source_dirty: bool
    runtime_version: Annotated[str, Field(min_length=1, max_length=64)]
    budget_tokens: Annotated[int, Field(ge=1)]
    outcome: Literal["pass", "fail"]
    failed_step: PacketStep | None = None
    failure: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    started_at: UtcDatetime
    ended_at: UtcDatetime
    evidence_digest: Sha256DigestStr

    @model_validator(mode="after")
    def _names_the_failed_step_exactly_on_fail(self) -> PacketRunReceipt:
        """Reject a receipt whose failure fields disagree with its outcome.

        Raises:
            ValueError: A failed run names no step, a passed run names one,
                or the run ended before it started.
        """
        if (self.outcome == "fail") != (self.failed_step is not None):
            raise ValueError("failed_step is set exactly when the outcome is fail")
        if self.outcome == "pass" and self.failure is not None:
            raise ValueError("a passed packet run carries no failure")
        if self.ended_at < self.started_at:
            raise ValueError("ended_at precedes started_at")
        return self


class PacketReceiptError(ValueError):
    """A receipt line is not a valid packet-run receipt."""


def packet_run_dir(repo_root: Path) -> Path:
    """Return the local directory holding *repo_root*'s packet-run receipts."""
    return repo_root.joinpath(*PACKET_RUN_DIR)


def write_packet_evidence(repo_root: Path, log: bytes) -> str:
    """Store one run's evidence log under its digest and return the digest.

    Args:
        repo_root: The repository whose local store receives the log.
        log: The run's evidence log.

    Returns:
        ``sha256:<hex>`` of *log*.
    """
    hexdigest = hashlib.sha256(log).hexdigest()
    directory = packet_run_dir(repo_root)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{hexdigest}.json").write_bytes(log)
    return f"sha256:{hexdigest}"


def append_packet_receipt(repo_root: Path, receipt: PacketRunReceipt) -> Path:
    """Append *receipt* to *repo_root*'s receipt file and return the file.

    Args:
        repo_root: The repository whose local store receives the receipt.
        receipt: A validated receipt.

    Returns:
        The receipt file.
    """
    path = packet_run_dir(repo_root) / PACKET_RECEIPTS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(receipt.model_dump_json() + "\n")
    logger.info(
        f"append_packet_receipt runtime={receipt.runtime} origin={receipt.origin.value} "
        f"outcome={receipt.outcome} version={receipt.eawf_version!r}"
    )
    return path


def read_packet_receipts(repo_root: Path) -> tuple[PacketRunReceipt, ...]:
    """Return every receipt *repo_root* holds, in the order they were appended.

    Args:
        repo_root: The repository whose local store is read.

    Returns:
        The receipts; none when the file is absent.

    Raises:
        PacketReceiptError: A line is not JSON or not a valid receipt,
            named by its line number.
    """
    path = packet_run_dir(repo_root) / PACKET_RECEIPTS_FILENAME
    if not path.is_file():
        return ()
    receipts: list[PacketRunReceipt] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            receipts.append(PacketRunReceipt.model_validate(json.loads(line)))
        except (ValueError, ValidationError) as exc:
            raise PacketReceiptError(
                f"{PACKET_RECEIPTS_FILENAME} line {number} is not a packet-run receipt: "
                f"{str(exc).splitlines()[0]}"
            ) from exc
    return tuple(receipts)


@dataclass(frozen=True, slots=True)
class PacketCoverage:
    """What the receipts show for one version across every runtime and origin.

    Attributes:
        passed: The ``runtime/origin`` pairs whose latest counted run passed
            on a certified runtime version.
        failed: The pairs whose latest counted run failed, each with its step.
        refused: The pairs whose latest counted run passed on a runtime
            version outside the certified range, each with the reason.
        missing: The pairs with no counted run.
        ignored: Why each receipt of the version on Linux was not counted.
    """

    passed: tuple[str, ...]
    failed: tuple[tuple[str, PacketStep], ...]
    refused: tuple[tuple[str, str], ...]
    missing: tuple[str, ...]
    ignored: tuple[str, ...]


def _range_refusal(
    receipt: PacketRunReceipt, ranges: Mapping[str, tuple[CertifiedVersionRange, ...]]
) -> str | None:
    """Return why *receipt*'s runtime version is not certified, or ``None`` when it is."""
    certified = ranges.get(receipt.runtime, ())
    version = receipt.runtime_version
    if not certified:
        return f"{receipt.runtime} {version} ran, but no certified version range is recorded"
    named = ", ".join(str(item) for item in certified)
    try:
        if any(item.covers(version) for item in certified):
            return None
    except ValueError:
        return f"{receipt.runtime} {version} is not a SemVer version inside {named}"
    return f"{receipt.runtime} {version} is outside the certified range {named}"


def packet_coverage(
    receipts: tuple[PacketRunReceipt, ...],
    *,
    version: str,
    released: Callable[[str], bool],
    ranges: Mapping[str, tuple[CertifiedVersionRange, ...]],
) -> PacketCoverage:
    """Return which runtime and origin pairs passed, failed, were refused or never ran.

    A receipt counts when it ran *version* on Linux from a clean source tree
    whose commit *released* admits. Of the counted receipts only the latest
    of each pair decides it, so a later pass supersedes an earlier failure.
    A latest pass on a runtime version none of *ranges* covers is refused.

    Args:
        receipts: The receipts in append order.
        version: The eawf version being released.
        released: Whether a source commit is the released commit or one of
            its ancestors.
        ranges: The certified version ranges of each runtime.

    Returns:
        Every pair in exactly one of passed, failed, refused or missing, in
        runtime then origin order, and why each uncounted receipt was set
        aside.
    """
    latest: dict[str, PacketRunReceipt] = {}
    ignored: list[str] = []
    for receipt in receipts:
        if receipt.eawf_version != version or receipt.os_class != PACKET_OS_CLASS:
            continue
        pair = f"{receipt.runtime}/{receipt.origin.value}"
        if receipt.source_dirty:
            ignored.append(f"{pair} at {receipt.eawf_commit[:12]} ran from a dirty source tree")
        elif not released(receipt.eawf_commit):
            ignored.append(
                f"{pair} at {receipt.eawf_commit[:12]} is not an ancestor of the released commit"
            )
        else:
            latest[pair] = receipt
    passed: list[str] = []
    failed: list[tuple[str, PacketStep]] = []
    refused: list[tuple[str, str]] = []
    missing: list[str] = []
    for runtime in PACKET_RUNTIMES:
        for origin in PacketOrigin:
            pair = f"{runtime}/{origin.value}"
            last = latest.get(pair)
            if last is None:
                missing.append(pair)
            elif last.failed_step is not None:
                failed.append((pair, last.failed_step))
            elif (refusal := _range_refusal(last, ranges)) is not None:
                refused.append((pair, refusal))
            else:
                passed.append(pair)
    return PacketCoverage(
        tuple(passed), tuple(failed), tuple(refused), tuple(missing), tuple(ignored)
    )


__all__ = [
    "PACKET_OS_CLASS",
    "PACKET_RECEIPTS_FILENAME",
    "PACKET_RUNTIMES",
    "PACKET_RUN_DIR",
    "PacketCoverage",
    "PacketOrigin",
    "PacketReceiptError",
    "PacketRunReceipt",
    "PacketRuntimeId",
    "PacketStep",
    "append_packet_receipt",
    "packet_coverage",
    "packet_run_dir",
    "read_packet_receipts",
    "write_packet_evidence",
]
