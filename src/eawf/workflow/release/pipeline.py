"""One resumable pipeline from a merged phase to a baked, advanced checkpoint.

A checkpoint cannot be pinned before its tag is pushed from ``main``:
the candidate manifest is frozen from the publication receipts only the
tag's publish jobs write. Walking the rest by hand after every merge
made each rung's approval and bake a wave of the *next* phase. This
module runs the whole post-merge walk as one verb instead:

pin the source, fetch the build receipts, tag and push (only with the
operator's publish flag), wait for the publish runs, open the record,
pin the candidate, prove the gates, sweep readiness, approve, publish,
reconcile, observe to BAKED, advance the train, check the phase's wave
pins by trailer, and land the evidence.

Every step is journaled once it completes, one row per step, under the
checkout's ignored ``.ea/local`` tree. A rerun skips the journaled steps
and resumes at the first one that is not, and every step is also safe to
repeat on its own: it reads the stored record first and does nothing
when the record already stands where the step would move it. A step that
cannot proceed raises :class:`PipelineRefusal` naming the step, a
:class:`PipelineRefusalCode` and the operator's next action, and leaves
no journal row, so the rerun retries exactly that step.

The pipeline itself touches nothing external. The daemon's ``release.*``
methods arrive as a :data:`ReleaseRpc` callable, and git, the forge and
the isolated gate proofs as a :class:`PipelineHost`, so a test drives the
real step logic against fakes and the CLI wires the real adapters.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from pydantic import ConfigDict, Field, ValidationError

from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.spec.release import (
    ReleaseChannel,
    ReleaseStatus,
    ReleaseTargetStatus,
    channel_for_version,
    normalize_version,
    release_key,
    semver_equivalent,
)
from eawf.kernel.state.types import UtcDatetime
from eawf.runtime.lock import portalock
from eawf.workflow.lifecycle.wave_trailer_repin import TrailerRepinError, resolve_trailer_repins
from eawf.workflow.release.advance import TrainAdvanceRecord
from eawf.workflow.release.pipeline_files import baseline_evidence, flatten_artifacts
from eawf.workflow.release.pipeline_receipts import RECEIPT_FILENAMES, read_build_receipt
from eawf.workflow.release.publication_receipt import (
    read_receipts,
    receipt_filename,
    reported_status,
)

logger = logging.getLogger(__name__)


class PipelineStep(StrEnum):
    """The post-merge steps, in the order they run."""

    PIN_SOURCE = "pin_source"
    BUILD_RECEIPTS = "build_receipts"
    TAG = "tag"
    PUBLISH_WAIT = "publish_wait"
    CREATE = "create"
    CANDIDATE = "candidate"
    RECEIPTS = "receipts"
    READINESS = "readiness"
    APPROVE = "approve"
    PUBLISH = "publish"
    RECONCILE = "reconcile"
    OBSERVE = "observe"
    ADVANCE = "advance"
    REPIN = "repin"
    EVIDENCE = "evidence"


#: Declaration order is run order; the journal's resume point is the
#: first step of this tuple it has no row for.
PIPELINE_STEPS: Final[tuple[PipelineStep, ...]] = tuple(PipelineStep)


class PipelineRefusalCode(StrEnum):
    """Why a step stopped the pipeline."""

    DIRTY_RELEASE_TREE = "dirty_release_tree"
    SOURCE_NOT_ON_MAIN = "source_not_on_main"
    SOURCE_MOVED = "source_moved"
    PROOF_TOOLCHAIN_MISSING = "proof_toolchain_missing"
    BUILD_RECEIPTS_UNAVAILABLE = "build_receipts_unavailable"
    RELEASE_NOT_READY = "release_not_ready"
    PUBLISH_FLAG_MISSING = "publish_flag_missing"
    TAG_CONFLICT = "tag_conflict"
    PUBLICATION_RUN_FAILED = "publication_run_failed"
    PUBLICATION_RECEIPT_INVALID = "publication_receipt_invalid"
    DAEMON_REFUSED = "daemon_refused"
    RECORD_STATUS_UNEXPECTED = "record_status_unexpected"
    EVIDENCE_MISSING = "evidence_missing"
    GATE_RECEIPTS_REFUSED = "gate_receipts_refused"
    PUBLICATION_LEG_FAILED = "publication_leg_failed"
    OBSERVATION_INCONCLUSIVE = "observation_inconclusive"
    TRAILER_REPIN_UNDECIDED = "trailer_repin_undecided"
    TRAILER_REPIN_PENDING = "trailer_repin_pending"
    EVIDENCE_BASELINE_FAILED = "evidence_baseline_failed"


class PipelineRpcError(RuntimeError):
    """The daemon refused one ``release.*`` call, or could not be reached."""


class PipelineHostError(RuntimeError):
    """A git, forge or proof-subprocess action of the host failed."""


class PipelineRefusal(Exception):  # noqa: N818 -- a refusal is the answer, not an error
    """A step that cannot proceed, with the operator's next action.

    Attributes:
        step: The step that refused; the rerun resumes here.
        code: Why it refused.
        detail: What the step found.
        remedy: What to do before rerunning.
        outcomes: The steps that stood completed when it refused, set by
            the runner so a surface can show how far the walk got.
    """

    def __init__(
        self, step: PipelineStep, code: PipelineRefusalCode, *, detail: str, remedy: str
    ) -> None:
        super().__init__(f"{step.value}: {code.value}: {detail} -- {remedy}")
        self.step = step
        self.code = code
        self.detail = detail
        self.remedy = remedy
        self.outcomes: tuple[StepOutcome, ...] = ()


class PipelineJournalRow(_StrictModel):
    """The row one completed step leaves in the journal.

    Attributes:
        version: The checkpoint the pipeline walks.
        step: The step that completed.
        completed_at: When it completed (timezone-aware UTC).
        facts: What the step pinned or learned, as strings; a later step
            reads the source commit and the evidence directory from here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1)
    step: PipelineStep
    completed_at: UtcDatetime
    facts: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """How one step stood after a run.

    Attributes:
        step: The step.
        disposition: ``ran`` when this run completed it, ``resumed`` when
            the journal already held it.
        facts: The facts its journal row carries.
    """

    step: PipelineStep
    disposition: Literal["ran", "resumed"]
    facts: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """A pipeline run that reached the end.

    Attributes:
        version: The checkpoint walked.
        outcomes: One outcome per step, in run order.
        advance: The train-advance row this run produced, or ``None``
            when the advance was journaled by an earlier run or the train
            had already moved past the checkpoint.
    """

    version: str
    outcomes: tuple[StepOutcome, ...]
    advance: TrainAdvanceRecord | None


#: A phase id, e.g. ``P35``. The workflow that prints the pipeline
#: command falls back to the literal ``P<NN>`` when it cannot extract
#: one from the merge commit subject, and that literal would otherwise
#: reach ``phase_wave_pins`` as a real phase id and read no wave pins
#: at all rather than refusing.
_PHASE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^P\d{2,}$")


@dataclass(frozen=True, slots=True)
class PipelineOptions:
    """What the operator asked the pipeline to do.

    Attributes:
        version: The checkpoint version the merged phase released.
        phase_id: The merged phase whose wave pins the repin step checks.
        publish: Whether the operator decided to publish; without it the
            tag step refuses before pushing anything.
        membership_refs: Acceptance bundles the record is opened with.
        remote: The remote ``main`` and the tag live on.
        observe_attempts: Read-backs per target before an inconclusive
            registry refuses; a registry lags a few minutes after publish.
        observe_backoff_seconds: Wait between read-backs of one target.
    """

    version: str
    phase_id: str
    publish: bool
    membership_refs: tuple[str, ...] = ()
    remote: str = "origin"
    observe_attempts: int = 10
    observe_backoff_seconds: float = 60.0

    def __post_init__(self) -> None:
        """Refuse a ``phase_id`` that is not a real phase id.

        Raises:
            ValueError: When ``phase_id`` does not match ``P<NN>``.
        """
        if not _PHASE_ID_RE.fullmatch(self.phase_id):
            raise ValueError(f"phase_id {self.phase_id!r} is not a phase id (want P<NN>, e.g. P35)")


#: One ``release.*`` JSON-RPC call: method, params -> result. Raises
#: :class:`PipelineRpcError` when the daemon refuses or is unreachable.
ReleaseRpc = Callable[[str, Mapping[str, Any]], dict[str, Any]]


class PipelineHost(Protocol):
    """Git, the forge and the isolated proofs, as the pipeline needs them."""

    @property
    def repo_root(self) -> Path:
        """The checkout the pipeline runs in."""
        ...

    def dirty_paths(self) -> tuple[str, ...]:
        """Return the uncommitted paths of the checkout, minus the release stores."""
        ...

    def head(self) -> str:
        """Return the checkout's HEAD commit."""
        ...

    def remote_main(self) -> str:
        """Fetch the remote and return the commit its ``main`` points at."""
        ...

    def which(self, program: str) -> str | None:
        """Return where *program* is on PATH, or ``None``."""
        ...

    def target_ids(self, version: str) -> tuple[str, ...]:
        """Return the publication targets the checkpoint configuration declares."""
        ...

    def preflight(self, version: str, *, source: str) -> str | None:
        """Sweep readiness at *source*; ``None`` when ready, else the first red row."""
        ...

    def remote_tag(self, tag: str) -> str | None:
        """Return the commit *tag* points at on the remote, or ``None``."""
        ...

    def push_tag(self, tag: str, *, revision: str) -> None:
        """Create *tag* at *revision* and push it; the push publishes."""
        ...

    def run_build_receipts(self, *, channel: str, source: str, dest: Path) -> None:
        """Dispatch the release dry run at *source*, wait, download its receipts."""
        ...

    def wait_publication(self, *, tag: str, dest: Path) -> None:
        """Wait for the publish runs of *tag* and download their receipts."""
        ...

    def prove_gates(self, version: str) -> dict[str, Any]:
        """Run ``release receipts`` in an isolated runtime; return its reply."""
        ...

    def phase_wave_pins(self, phase_id: str) -> dict[str, str]:
        """Return wave id -> pinned commit for the closed waves of *phase_id*."""
        ...

    def sleep(self, seconds: float) -> None:
        """Wait *seconds*."""
        ...


#: Where the journal and the staged evidence live, under the repo root.
#: Ignored by git, so neither dirties the tree the readiness sweep checks.
PIPELINE_DIRNAME: Final[str] = ".ea/local/release-pipeline"

#: Where the evidence lands for commit, under the repo root.
EVIDENCE_DIRNAME: Final[str] = ".ea/artifacts/evidence"

#: The publish workflows the tag push starts; together they write one
#: publication receipt per target.
PUBLISH_WORKFLOWS: Final[tuple[str, ...]] = ("release.yaml", "plugin-release.yaml")

#: The programs the pipeline shells out to. The gate proofs run ``uv``
#: and ``uvx`` inside a runtime whose PATH hides other agent CLIs.
REQUIRED_PROGRAMS: Final[tuple[str, ...]] = ("git", "gh", "uv", "uvx")

#: Record statuses of the happy path, in the order the walk reaches them.
#: A status outside this map is a record that needs a human verb.
_STATUS_RANK: Final[Mapping[ReleaseStatus, int]] = {
    ReleaseStatus.DRAFT: 0,
    ReleaseStatus.CANDIDATE: 1,
    ReleaseStatus.APPROVED: 2,
    ReleaseStatus.PUBLISHING: 3,
    ReleaseStatus.VERIFYING: 4,
    ReleaseStatus.BAKED: 5,
    ReleaseStatus.RELEASED: 6,
}

_REPORTED: Final[frozenset[str]] = frozenset(
    {ReleaseTargetStatus.REPORTED_SUCCESS.value, ReleaseTargetStatus.OBSERVED_SUCCESS.value}
)
_FAILED: Final[frozenset[str]] = frozenset(
    {ReleaseTargetStatus.REPORTED_FAILURE.value, ReleaseTargetStatus.OBSERVED_MISMATCH.value}
)
_DRY_RUN_CHANNEL: Final[Mapping[ReleaseChannel, str]] = {
    ReleaseChannel.DEV: "alpha",
    ReleaseChannel.RC: "rc",
    ReleaseChannel.STABLE: "stable",
}
_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(r"^\d+\.\d+\.\d+\.?(?P<suffix>(?:dev|rc)\d+)?$")


def rung_label(version: str) -> str:
    """Return the short name evidence files carry, e.g. ``dev3`` for ``0.7.0.dev3``.

    Args:
        version: Normalized checkpoint version.

    Returns:
        The ``devN`` / ``rcN`` suffix, or the whole version when stable.
    """
    match = _SUFFIX_RE.fullmatch(version)
    return version if match is None or match.group("suffix") is None else match.group("suffix")


@dataclass(frozen=True, slots=True)
class PipelineLayout:
    """Where one checkpoint's pipeline keeps its files.

    Attributes:
        repo_root: The checkout.
        version: The checkpoint.
    """

    repo_root: Path
    version: str

    @property
    def work_dir(self) -> Path:
        """The pipeline's ignored working directory for this checkpoint."""
        return self.repo_root / PIPELINE_DIRNAME / self.version

    @property
    def journal(self) -> Path:
        """The journal file."""
        return self.work_dir / "journal.jsonl"

    @property
    def staged(self) -> Path:
        """Evidence staged until the evidence step lands it for commit."""
        return self.work_dir / "evidence"

    @property
    def downloads(self) -> Path:
        """Raw ``gh run download`` output, before flattening."""
        return self.work_dir / "downloads"

    @property
    def build_receipts(self) -> Path:
        """The flat directory the readiness sweep reads build receipts from."""
        return self.repo_root / "dist" / "release-receipts"

    @property
    def publication_receipts(self) -> Path:
        """The flat directory ``release.candidate`` reads this version's receipts from."""
        return self.repo_root / "dist" / "publication-receipts" / self.version


def read_journal(path: Path) -> list[PipelineJournalRow]:
    """Return the journal rows at *path*, oldest first.

    Args:
        path: The journal file; absent means nothing has completed.

    Returns:
        The rows.

    Raises:
        ValueError: When a line does not validate, or two rows name one
            step -- the journal is one row per completed step, and a
            duplicate means something other than the pipeline wrote it.
    """
    if not path.is_file():
        return []
    rows: list[PipelineJournalRow] = []
    seen: set[PipelineStep] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = PipelineJournalRow.model_validate_json(line)
        except ValidationError as exc:
            raise ValueError(f"pipeline journal line {number} does not validate: {exc}") from exc
        if row.step in seen:
            raise ValueError(f"pipeline journal holds step {row.step.value!r} twice")
        seen.add(row.step)
        rows.append(row)
    return rows


#: Seconds a journal mutation waits for a sibling process's lock before
#: raising. A pipeline step can run for minutes, but two runs of the
#: same checkpoint racing the same journal file is an operator mistake
#: to surface promptly, not a contention pattern to wait out.
_JOURNAL_LOCK_TIMEOUT_SECONDS: Final[float] = 10.0


def _write_journal_rows(path: Path, rows: Iterable[PipelineJournalRow]) -> None:
    """Replace *path* with *rows*, via a temp file, fsync and rename.

    A bare ``write_text`` (or an ``open(..., "a")`` append) can leave a
    torn last line if the process dies mid-write, and the journal is the
    pipeline's only record of which steps are safe to skip on resume: a
    torn line would either silently drop a completed step or corrupt the
    next parse. Writing the whole file to a sibling temp path first and
    renaming it over the target makes the replace atomic on POSIX -- the
    journal is either the old content or the new, never a mix.

    Args:
        path: The journal file.
        rows: Every row the journal should hold, in order.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    text = "".join(row.model_dump_json() + "\n" for row in rows)
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def _append_journal(path: Path, row: PipelineJournalRow) -> None:
    """Append *row* to the journal at *path*, atomically and under a lock.

    The lock serializes the read-modify-write against a concurrent run
    of the same checkpoint; without it, two processes could each read
    the journal before either writes, and the second write would lose
    the first's row.

    Raises:
        portalock.LockTimeout: When a sibling process holds the lock
            past :data:`_JOURNAL_LOCK_TIMEOUT_SECONDS`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with portalock.acquire(path, timeout=_JOURNAL_LOCK_TIMEOUT_SECONDS):
        rows = read_journal(path)
        rows.append(row)
        _write_journal_rows(path, rows)


def discard_journal_from(path: Path, step: PipelineStep) -> tuple[PipelineStep, ...]:
    """Drop the rows of *step* and every later step, so the next run redoes them.

    Gate receipts expire, so an advance refused on a stale receipt needs
    the receipts step again even though the journal holds it. Every step
    is safe to repeat, so redoing the later ones only re-reads the record.
    The rewrite is atomic and lock-guarded for the same reason the append
    is: a partial rewrite, or one racing a concurrent run's append, would
    leave the journal disagreeing with what either process believes ran.

    Args:
        path: The journal file.
        step: The first step to redo.

    Returns:
        The steps whose rows were dropped.

    Raises:
        ValueError: When the journal does not validate.
        portalock.LockTimeout: When a sibling process holds the lock
            past :data:`_JOURNAL_LOCK_TIMEOUT_SECONDS`.
    """
    with portalock.acquire(path, timeout=_JOURNAL_LOCK_TIMEOUT_SECONDS):
        rows = read_journal(path)
        cut = PIPELINE_STEPS.index(step)
        kept = [row for row in rows if PIPELINE_STEPS.index(row.step) < cut]
        if len(kept) == len(rows):
            return ()
        _write_journal_rows(path, kept)
        return tuple(row.step for row in rows if PIPELINE_STEPS.index(row.step) >= cut)


def _write_json(path: Path, payload: object) -> None:
    """Write *payload* to *path* as indented JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@dataclass
class _PipelineRun:
    """One invocation's walk over the steps the journal does not hold."""

    options: PipelineOptions
    layout: PipelineLayout
    host: PipelineHost
    rpc: ReleaseRpc
    journaled: dict[PipelineStep, PipelineJournalRow]
    now: Callable[[], datetime]
    advance: TrainAdvanceRecord | None = None
    facts: dict[str, str] = field(default_factory=dict)

    @property
    def version(self) -> str:
        return self.layout.version

    @property
    def key(self) -> str:
        return release_key(self.version)

    @property
    def label(self) -> str:
        return rung_label(self.version)

    @property
    def source(self) -> str:
        return self.facts["source_sha"]

    def execute(self) -> PipelineResult:
        """Run every step the journal does not hold, in order."""
        handlers: dict[PipelineStep, Callable[[PipelineStep], dict[str, str]]] = {
            PipelineStep.PIN_SOURCE: self._pin_source,
            PipelineStep.BUILD_RECEIPTS: self._build_receipts,
            PipelineStep.TAG: self._tag,
            PipelineStep.PUBLISH_WAIT: self._publish_wait,
            PipelineStep.CREATE: self._create,
            PipelineStep.CANDIDATE: self._candidate,
            PipelineStep.RECEIPTS: self._receipts,
            PipelineStep.READINESS: self._readiness,
            PipelineStep.APPROVE: self._approve,
            PipelineStep.PUBLISH: self._publish,
            PipelineStep.RECONCILE: self._reconcile,
            PipelineStep.OBSERVE: self._observe,
            PipelineStep.ADVANCE: self._advance,
            PipelineStep.REPIN: self._repin,
            PipelineStep.EVIDENCE: self._evidence,
        }
        outcomes: list[StepOutcome] = []
        pinned = self.journaled.get(PipelineStep.PIN_SOURCE)
        if pinned is not None:
            self.facts.update(pinned.facts)
        for step in PIPELINE_STEPS:
            row = self.journaled.get(step)
            if row is not None:
                outcomes.append(StepOutcome(step, "resumed", row.facts))
                continue
            try:
                if pinned is not None:
                    self._require_pinned_head(step)
                facts = handlers[step](step)
            except PipelineRefusal as refusal:
                refusal.outcomes = tuple(outcomes)
                logger.warning(
                    f"execute version={self.version!r} step={step.value} "
                    f"code={refusal.code.value} detail={refusal.detail!r}"
                )
                raise
            row = PipelineJournalRow(
                version=self.version, step=step, completed_at=self.now(), facts=facts
            )
            _append_journal(self.layout.journal, row)
            if step is PipelineStep.PIN_SOURCE:
                self.facts.update(facts)
                pinned = row
            outcomes.append(StepOutcome(step, "ran", facts))
            logger.info(f"execute version={self.version!r} step={step.value} disposition=ran")
        return PipelineResult(version=self.version, outcomes=tuple(outcomes), advance=self.advance)

    # ---- shared checks -------------------------------------------------------

    def _require_clean_tree(self, step: PipelineStep) -> None:
        dirty = self.host.dirty_paths()
        if dirty:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.DIRTY_RELEASE_TREE,
                detail=f"{len(dirty)} uncommitted path(s), first {dirty[0]!r}",
                remedy="the readiness sweep reds tree_cleanliness on a dirty checkout; "
                "commit or stash them, then rerun",
            )

    def _require_pinned_head(self, step: PipelineStep) -> None:
        if step is PipelineStep.EVIDENCE:
            return
        head = self.host.head()
        if head != self.source:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.SOURCE_MOVED,
                detail=f"HEAD is {head[:12]}, the pipeline pinned {self.source[:12]}",
                remedy=f"check out {self.source[:12]} again (git checkout --detach "
                f"{self.source}) and rerun",
            )

    def _call(self, step: PipelineStep, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return self.rpc(method, params)
        except PipelineRpcError as exc:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.DAEMON_REFUSED,
                detail=str(exc),
                remedy=f"resolve the {method} refusal it names, then rerun",
            ) from exc

    def _record(self, step: PipelineStep) -> dict[str, Any] | None:
        record = self._call(step, "release.show", {"version": self.version}).get("record")
        return record if isinstance(record, dict) else None

    def _require_record(self, step: PipelineStep, *, at_least: ReleaseStatus) -> dict[str, Any]:
        record = self._record(step)
        if record is None:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.RECORD_STATUS_UNEXPECTED,
                detail=f"no record is stored for {self.key}",
                remedy="rerun with --redo create",
            )
        rank = _STATUS_RANK.get(ReleaseStatus(record["status"]))
        if rank is None or rank < _STATUS_RANK[at_least]:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.RECORD_STATUS_UNEXPECTED,
                detail=f"{self.key} stands at {record['status']}, the step needs "
                f"{at_least.value} or later",
                remedy=f"inspect it with `eawf release show {self.version}`; a recovering or "
                "timed-out record needs `eawf release retry` or `eawf release burn`",
            )
        return record

    def _staged_document(self, step: PipelineStep, name: str, redo: PipelineStep) -> Any:
        path = self.layout.staged / name
        if not path.is_file():
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.EVIDENCE_MISSING,
                detail=f"the staged {name} is gone",
                remedy=f"rerun with --redo {redo.value}",
            )
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _at_least(record: Mapping[str, Any], status: ReleaseStatus) -> bool:
        rank = _STATUS_RANK.get(ReleaseStatus(record["status"]))
        return rank is not None and rank >= _STATUS_RANK[status]

    # ---- steps ---------------------------------------------------------------

    def _pin_source(self, step: PipelineStep) -> dict[str, str]:
        self._require_clean_tree(step)
        head = self.host.head()
        main = self.host.remote_main()
        if head != main:
            remote = self.options.remote
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.SOURCE_NOT_ON_MAIN,
                detail=f"HEAD {head[:12]} is not {remote}/main {main[:12]}; the tag is made at "
                "HEAD and the ancestry row needs it on main",
                remedy=f"run from a clean checkout of the merge: git fetch {remote} && "
                f"git checkout --detach {remote}/main",
            )
        missing = [name for name in REQUIRED_PROGRAMS if self.host.which(name) is None]
        if missing:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.PROOF_TOOLCHAIN_MISSING,
                detail=f"not on PATH: {', '.join(missing)}",
                remedy="install them; the gate proofs run uv and uvx",
            )
        run_date = self.now().date().isoformat()
        return {
            "source_sha": head,
            "phase": self.options.phase_id,
            "evidence_dir": f"{EVIDENCE_DIRNAME}/{run_date}-{self.label}-publication",
        }

    def _build_receipts_current(self) -> bool:
        try:
            build = read_build_receipt(self.layout.repo_root)
        except ValueError:
            return False
        present = all(
            (self.layout.build_receipts / name).is_file() for name in RECEIPT_FILENAMES.values()
        )
        return present and build is not None and build.source_sha == self.source

    def _build_receipts(self, step: PipelineStep) -> dict[str, str]:
        if self._build_receipts_current():
            return {"reused": "true"}
        channel = _DRY_RUN_CHANNEL[channel_for_version(self.version)]
        raw = self.layout.downloads / "build"
        shutil.rmtree(raw, ignore_errors=True)
        remedy = "check the release.yaml dry run on the forge, then rerun"
        try:
            self.host.run_build_receipts(channel=channel, source=self.source, dest=raw)
            missing = flatten_artifacts(raw, self.layout.build_receipts, RECEIPT_FILENAMES.values())
        except (PipelineHostError, ValueError) as exc:
            raise PipelineRefusal(
                step, PipelineRefusalCode.BUILD_RECEIPTS_UNAVAILABLE, detail=str(exc), remedy=remedy
            ) from exc
        if missing or not self._build_receipts_current():
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.BUILD_RECEIPTS_UNAVAILABLE,
                detail=f"the dry run left no receipts for the pinned source (missing {missing})",
                remedy=remedy,
            )
        return {"channel": channel}

    def _tag(self, step: PipelineStep) -> dict[str, str]:
        tag = f"v{self.version}"
        pushed = self.host.remote_tag(tag)
        if pushed is not None:
            if pushed != self.source:
                raise PipelineRefusal(
                    step,
                    PipelineRefusalCode.TAG_CONFLICT,
                    detail=f"{tag} already points at {pushed[:12]}, not {self.source[:12]}",
                    remedy="a published version is never re-tagged; burn it and release the "
                    "next version",
                )
            return {"tag": tag, "pushed": "earlier"}
        self._require_clean_tree(step)
        blocker = self.host.preflight(self.version, source=self.source)
        if blocker is not None:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.RELEASE_NOT_READY,
                detail=blocker,
                remedy=f"run `eawf release preflight {self.version}` for the full sweep",
            )
        if not self.options.publish:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.PUBLISH_FLAG_MISSING,
                detail=f"pushing {tag} publishes {self.version} to every registry",
                remedy="rerun with --publish once the operator has decided to publish",
            )
        try:
            self.host.push_tag(tag, revision=self.source)
        except PipelineHostError as exc:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.TAG_CONFLICT,
                detail=str(exc),
                remedy="fix the tag push, then rerun; a tag already on the remote is adopted",
            ) from exc
        return {"tag": tag, "pushed": "now"}

    def _publication_problems(self, targets: tuple[str, ...]) -> list[str]:
        spellings = {self.version, semver_equivalent(self.version)}
        try:
            receipts = read_receipts(self.layout.publication_receipts, targets)
        except ValueError as exc:
            return [str(exc)]
        problems = [f"{target}: no receipt" for target in targets if target not in receipts]
        for target, receipt in sorted(receipts.items()):
            if reported_status(receipt) is not ReleaseTargetStatus.REPORTED_SUCCESS:
                problems.append(f"{target}: job concluded {receipt.job_conclusion}")
            if receipt.version not in spellings:
                problems.append(f"{target}: receipt is for {receipt.version}")
        return problems

    def _publish_wait(self, step: PipelineStep) -> dict[str, str]:
        try:
            targets = self.host.target_ids(self.version)
        except PipelineHostError as exc:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.PUBLICATION_RECEIPT_INVALID,
                detail=str(exc),
                remedy=f"author or repair the {self.version} checkpoint configuration",
            ) from exc
        if not self._publication_problems(targets):
            return {"targets": ",".join(targets), "reused": "true"}
        raw = self.layout.downloads / "publication"
        shutil.rmtree(raw, ignore_errors=True)
        try:
            self.host.wait_publication(tag=f"v{self.version}", dest=raw)
            flatten_artifacts(
                raw,
                self.layout.publication_receipts,
                [receipt_filename(target) for target in targets],
            )
        except (PipelineHostError, ValueError) as exc:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.PUBLICATION_RUN_FAILED,
                detail=str(exc),
                remedy="check the publish runs of the tag on the forge, then rerun",
            ) from exc
        problems = self._publication_problems(targets)
        if problems:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.PUBLICATION_RECEIPT_INVALID,
                detail="; ".join(problems),
                remedy="a failed leg is retried with `eawf release retry` once the record is "
                "publishing; otherwise fix the run and rerun",
            )
        return {"targets": ",".join(targets)}

    def _create(self, step: PipelineStep) -> dict[str, str]:
        record = self._record(step)
        if record is not None:
            return {"record": str(record["key"]), "reused": "true"}
        reply = self._call(
            step,
            "release.create",
            {"version": self.version, "membership_refs": list(self.options.membership_refs)},
        )
        return {"record": str((reply.get("release") or {}).get("key"))}

    def _candidate(self, step: PipelineStep) -> dict[str, str]:
        record = self._require_record(step, at_least=ReleaseStatus.DRAFT)
        manifest_path = self.layout.staged / f"{self.label}-manifest.json"
        if record["status"] != ReleaseStatus.DRAFT.value:
            if record.get("source_sha") != self.source:
                raise PipelineRefusal(
                    step,
                    PipelineRefusalCode.RECORD_STATUS_UNEXPECTED,
                    detail=f"{self.key} is pinned to {record.get('source_sha')}, not the source",
                    remedy="a pinned candidate never moves source; burn or cancel it first",
                )
            self._staged_document(step, manifest_path.name, PipelineStep.CANDIDATE)
            return {"manifest_digest": str(record.get("manifest_digest")), "reused": "true"}
        reply = self._call(
            step,
            "release.candidate",
            {
                "version": self.version,
                "receipts_dir": str(self.layout.publication_receipts),
                "source_sha": self.source,
                "manifest_ref": None,
            },
        )
        _write_json(manifest_path, reply.get("manifest"))
        _write_json(self.layout.staged / f"{self.label}-candidate.json", reply.get("release"))
        return {"manifest_digest": str(reply.get("manifest_digest"))}

    def _receipts(self, step: PipelineStep) -> dict[str, str]:
        self._require_record(step, at_least=ReleaseStatus.CANDIDATE)
        self._require_clean_tree(step)
        try:
            reply = self.host.prove_gates(self.version)
        except PipelineHostError as exc:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.GATE_RECEIPTS_REFUSED,
                detail=str(exc),
                remedy="fix the proof run, then rerun",
            ) from exc
        _write_json(self.layout.staged / "receipts.json", reply)
        refused = [
            str(row.get("gate")) for row in reply.get("refused") or () if isinstance(row, dict)
        ]
        if refused:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.GATE_RECEIPTS_REFUSED,
                detail=f"gates without a receipt: {', '.join(refused)}",
                remedy="fix each gate and rerun; the newest receipt per gate wins",
            )
        if reply.get("source_sha") != self.source:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.RECORD_STATUS_UNEXPECTED,
                detail=f"receipts were proven at {reply.get('source_sha')}, not the source",
                remedy=f"inspect the record with `eawf release show {self.version}`",
            )
        return {"receipts": str(len(reply.get("receipts") or ()))}

    def _readiness(self, step: PipelineStep) -> dict[str, str]:
        record = self._require_record(step, at_least=ReleaseStatus.CANDIDATE)
        if self._at_least(record, ReleaseStatus.APPROVED):
            return {"reused": "true"}
        self._require_clean_tree(step)
        reply = self._call(
            step,
            "release.compute_readiness",
            {
                "version": self.version,
                "observed_revision": self.source,
                "waiver_count": 0,
                "release": record,
            },
        )
        readiness = reply.get("readiness") or {}
        _write_json(self.layout.staged / "readiness-reply.json", reply)
        _write_json(self.layout.staged / "readiness.json", readiness)
        if readiness.get("ready") is not True or reply.get("first_red") is not None:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.RELEASE_NOT_READY,
                detail=f"first red signal {reply.get('first_red')}",
                remedy="fix the signal; an expired gate receipt needs --redo receipts",
            )
        return {"ready": "true"}

    def _approve(self, step: PipelineStep) -> dict[str, str]:
        record = self._require_record(step, at_least=ReleaseStatus.CANDIDATE)
        approved_path = self.layout.staged / f"{self.label}-approved.json"
        if self._at_least(record, ReleaseStatus.APPROVED):
            if not approved_path.is_file():
                _write_json(approved_path, record)
            return {"status": str(record["status"]), "reused": "true"}
        readiness = self._staged_document(step, "readiness.json", PipelineStep.READINESS)
        reply = self._call(
            step,
            "release.approve",
            {
                "release": record,
                "readiness": readiness,
                "approval_ref": f"repo:{self.facts['evidence_dir']}/receipts.json",
            },
        )
        approved = reply.get("release") or {}
        _write_json(approved_path, approved)
        return {"status": str(approved.get("status"))}

    def _publish(self, step: PipelineStep) -> dict[str, str]:
        record = self._require_record(step, at_least=ReleaseStatus.APPROVED)
        if self._at_least(record, ReleaseStatus.PUBLISHING):
            return {"status": str(record["status"]), "reused": "true"}
        digest = record.get("manifest_digest")
        reply = self._call(
            step,
            "release.publish",
            {
                "release": record,
                "expected_revision": record.get("revision", 0),
                "idempotency_key": f"{self.label}-publish-1",
                "approved_manifest_digest": digest,
                "proof_digest": digest,
                "observed_revision": self.source,
                "waiver_count": 0,
            },
        )
        return {"operation": str(reply.get("operation_ref"))}

    def _reconcile(self, step: PipelineStep) -> dict[str, str]:
        record = self._require_record(step, at_least=ReleaseStatus.PUBLISHING)
        settled: list[str] = []
        for target in sorted(record.get("target_statuses") or {}):
            status = record["target_statuses"][target]
            if status in _REPORTED:
                continue
            if status in _FAILED:
                raise PipelineRefusal(
                    step,
                    PipelineRefusalCode.PUBLICATION_LEG_FAILED,
                    detail=f"leg {target} stands at {status}",
                    remedy=f"retry it with `eawf release retry {self.key} --target {target}`, "
                    "or burn the version",
                )
            receipt_path = self.layout.publication_receipts / receipt_filename(target)
            if not receipt_path.is_file():
                raise PipelineRefusal(
                    step,
                    PipelineRefusalCode.PUBLICATION_RECEIPT_INVALID,
                    detail=f"no publication receipt for {target}",
                    remedy="rerun with --redo publish_wait",
                )
            reply = self._call(
                step,
                "release.reconcile",
                {
                    "release": record,
                    "expected_revision": record.get("revision", 0),
                    "idempotency_key": f"{self.label}-reconcile-{target}-r{record.get('revision')}",
                    "target_id": target,
                    "effect_receipt_ref": None,
                    "receipt": json.loads(receipt_path.read_text(encoding="utf-8")),
                },
            )
            record = reply.get("release") or record
            settled.append(target)
        return {"reconciled": ",".join(settled)}

    def _observe_target(self, step: PipelineStep, target: str, manifest: object) -> dict[str, Any]:
        attempts = self.options.observe_attempts
        for attempt in range(1, attempts + 1):
            record = self._require_record(step, at_least=ReleaseStatus.PUBLISHING)
            status = (record.get("target_statuses") or {}).get(target)
            if status == ReleaseTargetStatus.OBSERVED_SUCCESS.value:
                return record
            if status in _FAILED:
                raise PipelineRefusal(
                    step,
                    PipelineRefusalCode.PUBLICATION_LEG_FAILED,
                    detail=f"leg {target} stands at {status}, release {record['status']}",
                    remedy=f"recover with `eawf release retry {self.key} --target {target}` or "
                    "`eawf release burn`",
                )
            revision = record.get("revision", 0)
            try:
                reply = self.rpc(
                    "release.observe_target",
                    {
                        "release": record,
                        "expected_revision": revision,
                        "idempotency_key": f"{self.label}-observe-{target}-r{revision}-a{attempt}",
                        "target_id": target,
                        "manifest": manifest,
                        "effect_receipt_ref": None,
                    },
                )
            except PipelineRpcError as exc:
                if PipelineRefusalCode.OBSERVATION_INCONCLUSIVE.value not in str(exc):
                    raise PipelineRefusal(
                        step,
                        PipelineRefusalCode.DAEMON_REFUSED,
                        detail=str(exc),
                        remedy="resolve the release.observe_target refusal, then rerun",
                    ) from exc
                logger.info(f"_observe_target target={target} attempt={attempt} lagging=True")
                if attempt < attempts:
                    self.host.sleep(self.options.observe_backoff_seconds)
                continue
            observed = reply.get("release") or {}
            if (observed.get("target_statuses") or {}).get(target) == (
                ReleaseTargetStatus.OBSERVED_SUCCESS.value
            ):
                return observed
        raise PipelineRefusal(
            step,
            PipelineRefusalCode.OBSERVATION_INCONCLUSIVE,
            detail=f"{target} did not read back after {attempts} attempt(s)",
            remedy="the registry may still lag; rerun later, never force it",
        )

    def _observe(self, step: PipelineStep) -> dict[str, str]:
        record = self._require_record(step, at_least=ReleaseStatus.PUBLISHING)
        if not self._at_least(record, ReleaseStatus.BAKED):
            manifest = self._staged_document(
                step, f"{self.label}-manifest.json", PipelineStep.CANDIDATE
            )
            for target in sorted(record.get("target_statuses") or {}):
                self._observe_target(step, target, manifest)
            record = self._require_record(step, at_least=ReleaseStatus.PUBLISHING)
        if not self._at_least(record, ReleaseStatus.BAKED):
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.RECORD_STATUS_UNEXPECTED,
                detail=f"every leg read back but {self.key} stands at {record['status']}",
                remedy=f"inspect it with `eawf release show {self.version}`",
            )
        _write_json(self.layout.staged / f"{self.label}-baked.json", record)
        return {"status": str(record["status"])}

    def _advance(self, step: PipelineStep) -> dict[str, str]:
        shown = self._call(step, "release.show", {"version": None})
        open_key = (shown.get("checkpoint") or {}).get("release_key")
        if open_key != self.key:
            return {"opened": str(open_key), "reused": "true"}
        reply = self._call(step, "release.advance_train", {"release_key": self.key})
        try:
            self.advance = TrainAdvanceRecord.model_validate(reply.get("advance"))
        except ValidationError as exc:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.DAEMON_REFUSED,
                detail=f"the advance reply carries no valid train-advance row: {exc}",
                remedy="inspect the train with `eawf release train show`",
            ) from exc
        return {
            "closed": self.advance.closed_key,
            "opened": self.advance.opened_key,
            "train_revision": str(self.advance.train_revision),
        }

    def _repin(self, step: PipelineStep) -> dict[str, str]:
        pins = self.host.phase_wave_pins(self.options.phase_id)
        target_ref = f"refs/remotes/{self.options.remote}/main"
        try:
            rows = resolve_trailer_repins(
                pins, target_ref=target_ref, repo_root=self.host.repo_root
            )
        except TrailerRepinError as exc:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.TRAILER_REPIN_UNDECIDED,
                detail=str(exc),
                remedy=f"fetch {self.options.remote} and rerun",
            ) from exc
        undecided = [f"{row.wave_id}={row.outcome}" for row in rows if row.new_commit is None]
        if undecided:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.TRAILER_REPIN_UNDECIDED,
                detail=f"no single landed commit for {', '.join(undecided)}",
                remedy="repin those waves by hand with `eawf wave close --commit`",
            )
        moved = [row.wave_id for row in rows if row.outcome != "unchanged"]
        if moved:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.TRAILER_REPIN_PENDING,
                detail=f"main rewrote the commits of {', '.join(moved)}",
                remedy="apply the commits.repin repair with `eawf doctor --fix`, then rerun",
            )
        return {"pins": str(len(rows))}

    def _evidence(self, step: PipelineStep) -> dict[str, str]:
        staged = sorted(self.layout.staged.glob("*.json"))
        if not staged:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.EVIDENCE_MISSING,
                detail="nothing is staged",
                remedy="rerun with --redo candidate",
            )
        evidence_dir = self.facts["evidence_dir"]
        dest = self.layout.repo_root / evidence_dir
        dest.mkdir(parents=True, exist_ok=True)
        for path in staged:
            shutil.copy2(path, dest / path.name)
        rel_paths = [f"{evidence_dir}/{path.name}" for path in staged]
        try:
            findings = baseline_evidence(self.layout.repo_root, rel_paths)
        except (ImportError, OSError, ValueError) as exc:
            raise PipelineRefusal(
                step,
                PipelineRefusalCode.EVIDENCE_BASELINE_FAILED,
                detail=str(exc),
                remedy="baseline only the new evidence files by hand, bump the Baseline-hash "
                "comment, then rerun",
            ) from exc
        return {"evidence_dir": evidence_dir, "files": str(len(staged)), "baselined": str(findings)}


def run_post_merge_pipeline(
    options: PipelineOptions,
    *,
    host: PipelineHost,
    rpc: ReleaseRpc,
    redo: PipelineStep | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PipelineResult:
    """Walk a merged phase's checkpoint from tag to BAKED and the train advance.

    Resumes at the first step the journal does not hold; *redo* first
    drops that step's row and every later one.

    Args:
        options: The operator's request.
        host: Git, the forge and the isolated proofs.
        rpc: The daemon's ``release.*`` methods.
        redo: The first step to run again, or ``None`` to resume.
        now: The clock journal rows are stamped with.

    Returns:
        The completed walk.

    Raises:
        PipelineRefusal: When a step cannot proceed; it names the step,
            the reason and the next action, and journals nothing for it.
        ValueError: When the version is not a train version or the
            journal does not validate.
    """
    version = normalize_version(options.version)
    layout = PipelineLayout(repo_root=host.repo_root, version=version)
    if redo is not None:
        dropped = discard_journal_from(layout.journal, redo)
        logger.info(
            f"run_post_merge_pipeline version={version!r} dropped={[s.value for s in dropped]}"
        )
    journaled = {row.step: row for row in read_journal(layout.journal)}
    run = _PipelineRun(
        options=options, layout=layout, host=host, rpc=rpc, journaled=journaled, now=now
    )
    return run.execute()


__all__ = [
    "EVIDENCE_DIRNAME",
    "PIPELINE_DIRNAME",
    "PIPELINE_STEPS",
    "PUBLISH_WORKFLOWS",
    "REQUIRED_PROGRAMS",
    "PipelineHost",
    "PipelineHostError",
    "PipelineJournalRow",
    "PipelineLayout",
    "PipelineOptions",
    "PipelineRefusal",
    "PipelineRefusalCode",
    "PipelineResult",
    "PipelineRpcError",
    "PipelineStep",
    "ReleaseRpc",
    "StepOutcome",
    "discard_journal_from",
    "read_journal",
    "run_post_merge_pipeline",
    "rung_label",
]
