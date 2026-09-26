"""The largest supported state: this repository's own corpus, at cutover.

The other nine fixtures answer "does the importer handle this shape". This
one answers "how big a corpus does the cutover support", and the only
honest answer available is the biggest real one in existence, which is the
corpus this repository runs on. It is staged by copying -- never by
pointing the importer at the live tree, which has a writer.

What is copied is the **committed** surface of ``.ea/``, not the whole
directory. A migration operates on the state a clone can reproduce, and
the declaration of what that is already exists: the commit policy table
at :data:`~eawf.kernel.store.commit_policy.EA_PATH_CLASSES`. Selecting
through :func:`~eawf.kernel.store.commit_policy.classify_path` rather
than through a hand-written exclusion list is what keeps the fixture and
the policy from disagreeing -- a file whose policy changes moves the
fixture with it, in the same commit, without anyone remembering to.
The bytes come from git objects at HEAD rather than from the working
tree, so the staging depends on the revision alone and never races the
daemon writing the live document.

The firehose is the case that makes this load-bearing.
``.ea/store/event.jsonl`` is raw agent stdout: gitignored, unbounded, and
full of this machine's absolute paths. Staging it measured a file no
clone has, put megabytes of un-migrated text in the denominator of the
growth multiplier, and failed the cutover's own scrub gate on bytes that
were never part of the corpus.

The corpus is not committed a second time. It is already in the
repository at ``.ea/``, it is 6.9 MB, and the repository's own
``check-added-large-files`` hook caps a committed file at 1 MB, so a
frozen copy could not be committed even if duplicating it were a good
idea. What is committed instead is the **pin**: the order of magnitude
the corpus has to be at, the ceiling the published tree may not exceed
relative to its source, the wall-clock budget the apply has to finish
inside, and the size the residual document has to stay under. Those four
numbers are the contract; the corpus behind them grows, and the contract
is written so that growth does not silently loosen it.

The pin is a file rather than constants in the test for the same reason
the disposability declaration is a file: it is a statement about the
data, and it has to be editable -- and reviewable -- without touching the
code that reads it.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.migration.epoch2.canary import OPT_IN_DECLARATION_FILENAME
from eawf.kernel.migration.epoch2.snapshot import (
    LIVE_CONFIG_LOCATOR,
    LIVE_STATE_LOCATOR,
    LIVE_STORE_LOCATOR,
    StagedSource,
    committed_sources,
    is_committed,
    repository_revision,
    require_committed,
    stage_committed_corpus,
)
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.paths import store_path
from eawf.platform.registry import Registry, WorkspaceRecord, create_workspace
from eawf.runtime.worktree import worktree_registry_lock
from eawf.runtime.worktree.reconcile import reconcile_stale_rows
from tests.integration.kernel.migration._corpus_shapes import (
    DEFAULT_TRACK_KEY,
    PROJECT_KEY,
    WORKSPACE_KEY,
)

logger = logging.getLogger(__name__)


#: The pin filename inside the live-cutover fixture directory.
PIN_FILENAME: Final = "corpus-pin.json"


class CorpusMagnitude(StrEnum):
    """The order of magnitude a corpus's row count falls in.

    Coarse on purpose. The question a consumer asks is "was this measured
    over a corpus at least as big as mine", which is an ordering
    question, and an order of magnitude is the finest resolution at which
    that ordering stays stable as a live corpus grows.

    Distinct from
    :class:`~eawf.kernel.spec.measured_contract.ScaleBand`, which classes
    the environment a measurement ran in (toy, dev, production, fleet)
    rather than how much data it ran over. One corpus magnitude can be
    reached in either environment, so the two orderings never substitute
    for one another.
    """

    UNITS = "units"
    TENS = "tens"
    HUNDREDS = "hundreds"
    THOUSANDS = "thousands"
    TENS_OF_THOUSANDS = "tens_of_thousands"


#: The lower bound of each magnitude, in rows. A count at or above the
#: last entry's bound is at that magnitude.
MAGNITUDE_FLOORS: Final[tuple[tuple[CorpusMagnitude, int], ...]] = (
    (CorpusMagnitude.UNITS, 1),
    (CorpusMagnitude.TENS, 10),
    (CorpusMagnitude.HUNDREDS, 100),
    (CorpusMagnitude.THOUSANDS, 1_000),
    (CorpusMagnitude.TENS_OF_THOUSANDS, 10_000),
)


def magnitude_for(rows: int) -> CorpusMagnitude:
    """Return the corpus magnitude a row count falls in.

    Args:
        rows: How many rows the corpus holds.

    Returns:
        The magnitude. A count below one row is reported as ``UNITS``
        rather than raising: an empty corpus is a real shape, and the
        empty-repository fixture is where it is rehearsed.

    Raises:
        ValueError: When ``rows`` is negative, which no census produces.
    """
    if rows < 0:
        raise ValueError(f"a corpus cannot hold {rows} rows")
    magnitude = CorpusMagnitude.UNITS
    for candidate, floor in MAGNITUDE_FLOORS:
        if rows >= floor:
            magnitude = candidate
    return magnitude


class LiveCorpusPin(BaseModel):
    """The committed contract the live corpus has to keep satisfying.

    Attributes:
        declared_magnitude: The order of magnitude the corpus is asserted
            at. The rehearsal refuses a corpus below it, because a
            measurement taken over a smaller population would not back
            the claim the magnitude makes.
        multiplier_ceiling: The largest factor by which the published
            epoch-2 generation may exceed the epoch-1 corpus it was built
            from. An importer change that inflates the tree past this
            reds here rather than at the flag day.
        apply_timeout_budget_s: The wall clock one apply has to finish
            inside.
        residual_document_ceiling_bytes: The size the generation's own
            document has to stay under once the terminal records have
            moved to their ledgers. This is the number that decides
            whether a reader pays for history it is not looking at.
        observed_at_revision: The revision the numbers below were
            measured at, so a later reader can reproduce them.
        observed_rows: The row count observed then.
        observed_multiplier: The multiplier observed then.
        observed_apply_s: The apply wall clock observed then.
        observed_residual_bytes: The residual document size observed then.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    declared_magnitude: CorpusMagnitude
    multiplier_ceiling: Annotated[float, Field(gt=1.0)]
    apply_timeout_budget_s: Annotated[float, Field(gt=0.0)]
    residual_document_ceiling_bytes: Annotated[int, Field(gt=0)]
    observed_at_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{7,40}$")]
    observed_rows: Annotated[int, Field(ge=1)]
    observed_multiplier: Annotated[float, Field(gt=0.0)]
    observed_apply_s: Annotated[float, Field(gt=0.0)]
    observed_residual_bytes: Annotated[int, Field(ge=1)]

    @classmethod
    def load(cls, path: Path) -> LiveCorpusPin:
        """Read and validate the pin at ``path``.

        Args:
            path: Location of the pin document.

        Returns:
            The validated pin.

        Raises:
            FileNotFoundError: When the pin is absent.
            ValidationError: When a field is missing or mis-shaped.
        """
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))


def stage_live_corpus(*, repo_root: Path, destination: Path) -> Path:
    """Stage the committed corpus the way the operator verb does.

    A thin call into
    :func:`~eawf.kernel.migration.epoch2.snapshot.stage_committed_corpus`,
    which ``eawf migrate epoch2 --stage-to`` also calls, so the rehearsal
    measures the exact snapshot an operator would stage rather than a
    test-only copy of it. The registry is sealed under the rehearsal's own
    addressing slots.

    Args:
        repo_root: Any directory inside the repository whose committed
            ``.ea`` tree is the corpus.
        destination: Where to assemble the snapshot.

    Returns:
        ``destination``, now laid out as a snapshot root.

    Raises:
        MigrationSourceUnreadableError: When HEAD tracks no committed
            corpus, or ``repo_root`` is not in a git repository.
        MigrationStagingRefusedError: When ``destination`` is not empty.
    """
    stage_committed_corpus(
        repo_root=repo_root,
        destination=destination,
        workspace_key=WORKSPACE_KEY,
        project_key=PROJECT_KEY,
    )
    return destination


def corpus_bytes(root: Path) -> int:
    """Return the total size of every file in a staged snapshot."""
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


# ---------------------------------------------------------------------------
# The pinned clone.
#
# Staging from git objects answers "what does the importer make of this
# corpus". It does not answer "can this repository be cut over", because a
# cutover also writes into the repository: it fences the tree, proves nobody
# holds it, pins a restore point and swaps authority. That half can only be
# rehearsed on a repository nobody else is using, so it runs on a clone,
# checked out at one frozen revision, under the addressing the live cut uses.
# ---------------------------------------------------------------------------

#: The workspace this repository is registered under at the live cut.
LIVE_WORKSPACE_KEY: Final = "EAWF"

#: The project and repository the live corpus is addressed to -- the
#: project code the repository's own document already carries.
LIVE_PROJECT_KEY: Final = "EAWF"
LIVE_REPOSITORY_KEY: Final = "EAWF"

#: The Track declared as owner of every record whose source names none.
LIVE_TRACK_KEY: Final = DEFAULT_TRACK_KEY


def clone_at_revision(*, repo_root: Path, destination: Path) -> str:
    """Clone the repository holding ``repo_root`` and pin it at its HEAD.

    The clone is detached at the full commit id rather than left on a
    branch, so nothing that moves a branch while the rehearsal runs can
    change which corpus it is rehearsing. User-wide git hooks are disabled
    for both calls: the clone is a scratch tree, and a hook that ran in it
    would make the rehearsal depend on the machine it runs on.

    Args:
        repo_root: Any directory inside the repository to clone.
        destination: Where the clone lands; it must not exist yet.

    Returns:
        The revision the clone is checked out at.

    Raises:
        MigrationSourceUnreadableError: ``repo_root`` is not in a git
            repository with a commit.
        subprocess.CalledProcessError: git refused the clone or checkout.
    """
    top, revision = repository_revision(repo_root)
    _git_quiet(["clone", "--quiet", "--no-checkout", str(top), str(destination)])
    _git_quiet(["-C", str(destination), "checkout", "--quiet", "--detach", revision])
    return revision


def _git_quiet(args: list[str]) -> None:
    """Run one git command with user-wide hooks disabled."""
    subprocess.run(
        ["git", "-c", f"core.hooksPath={os.devnull}", *args],
        check=True,
        capture_output=True,
    )


def quiesce_clone(clone: Path, *, booted_at: datetime) -> int:
    """Retire every holder the clone inherited from the committed document.

    A committed document still names the sessions and worktrees that were
    live when it was committed, and a fresh clone has none of those
    checkouts. This runs exactly what the daemon's reconcile verb runs --
    the worktree registry lock around the stale-row retirement -- with the
    daemon's boot instant, so a session that predates it retires too.

    Args:
        clone: The clone's repository root.
        booted_at: The boot instant the retirement measures sessions
            against.

    Returns:
        How many rows were retired.
    """
    state = clone / ".ea" / "state.json"
    with worktree_registry_lock(clone):
        result = reconcile_stale_rows(
            state,
            store_path(state, StoreKind.EVENT),
            repo_root=clone,
            booted_at=booted_at,
            dry_run=False,
        )
    return len(result.rows)


def write_opt_in(ea_root: Path, *, backup_ts: str, backup_digest: str) -> Path:
    """Opt ``ea_root`` into the cutover against one verified backup.

    Args:
        ea_root: The tree to opt in.
        backup_ts: The snapshot ``eawf backup create`` reported.
        backup_digest: The digest it reported for that snapshot.

    Returns:
        The declaration's path.
    """
    path = ea_root / OPT_IN_DECLARATION_FILENAME
    declaration = {
        "opt_in": True,
        "declared_by": "rehearsal",
        "purpose": "rehearse this repository's epoch-2 cutover on a pinned clone",
        "backup_ts": backup_ts,
        "backup_digest": backup_digest,
    }
    path.write_text(json.dumps(declaration, sort_keys=True) + "\n", encoding="utf-8")
    return path


def register_live_workspace(path: Path) -> Path:
    """Write a registry holding the one workspace the live cut resolves.

    Args:
        path: Where to write the registry.

    Returns:
        ``path``.
    """
    registry = create_workspace(
        Registry(),
        record=WorkspaceRecord(
            key=LIVE_WORKSPACE_KEY,
            title="eawf",
            member_project_codes=frozenset({LIVE_PROJECT_KEY}),
            home_project_code=LIVE_PROJECT_KEY,
        ),
    )
    path.write_text(registry.model_dump_json(indent=2), encoding="utf-8")
    return path


__all__ = [
    "LIVE_CONFIG_LOCATOR",
    "LIVE_PROJECT_KEY",
    "LIVE_REPOSITORY_KEY",
    "LIVE_STATE_LOCATOR",
    "LIVE_STORE_LOCATOR",
    "LIVE_TRACK_KEY",
    "LIVE_WORKSPACE_KEY",
    "MAGNITUDE_FLOORS",
    "PIN_FILENAME",
    "CorpusMagnitude",
    "LiveCorpusPin",
    "StagedSource",
    "clone_at_revision",
    "committed_sources",
    "corpus_bytes",
    "is_committed",
    "magnitude_for",
    "quiesce_clone",
    "register_live_workspace",
    "require_committed",
    "stage_live_corpus",
    "write_opt_in",
]
