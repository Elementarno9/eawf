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
idea. What is committed instead is the **pin**: the scale band the
corpus has to be in, the ceiling the published tree may not exceed
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
import shutil
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.store.commit_policy import CommitPolicy, classify_path
from tests.integration.kernel.migration._corpus_builders import (
    CONFIG_DIRNAME,
    CONFIG_FILENAME,
    DOCUMENT_FILENAME,
    REGISTRY_FILENAME,
    STORE_DIRNAME,
    TELEMETRY_BODY,
    TELEMETRY_FILENAME,
)
from tests.integration.kernel.migration._corpus_shapes import default_registry

logger = logging.getLogger(__name__)


#: Where the live corpus is read from, relative to the repository root.
LIVE_STATE_LOCATOR: Final = ".ea/state.json"
LIVE_STORE_LOCATOR: Final = ".ea/store"
LIVE_CONFIG_LOCATOR: Final = ".ea/config.yaml"

#: The pin filename inside the live-cutover fixture directory.
PIN_FILENAME: Final = "corpus-pin.json"


class ScaleBand(StrEnum):
    """The order-of-magnitude band a corpus's row count falls in.

    Coarse on purpose. The question a consumer asks is "was this measured
    over a corpus at least as big as mine", which is an ordering
    question, and an order of magnitude is the finest resolution at which
    that ordering stays stable as a live corpus grows.
    """

    UNITS = "units"
    TENS = "tens"
    HUNDREDS = "hundreds"
    THOUSANDS = "thousands"
    TENS_OF_THOUSANDS = "tens_of_thousands"


#: The lower bound of each band, in rows. A count at or above the last
#: entry's bound is in that band.
BAND_FLOORS: Final[tuple[tuple[ScaleBand, int], ...]] = (
    (ScaleBand.UNITS, 1),
    (ScaleBand.TENS, 10),
    (ScaleBand.HUNDREDS, 100),
    (ScaleBand.THOUSANDS, 1_000),
    (ScaleBand.TENS_OF_THOUSANDS, 10_000),
)


def band_for(rows: int) -> ScaleBand:
    """Return the scale band a row count falls in.

    Args:
        rows: How many rows the corpus holds.

    Returns:
        The band. A count below one row is reported as ``UNITS`` rather
        than raising: an empty corpus is a real shape, and the
        empty-repository fixture is where it is rehearsed.

    Raises:
        ValueError: When ``rows`` is negative, which no census produces.
    """
    if rows < 0:
        raise ValueError(f"a corpus cannot hold {rows} rows")
    band = ScaleBand.UNITS
    for candidate, floor in BAND_FLOORS:
        if rows >= floor:
            band = candidate
    return band


class LiveCorpusPin(BaseModel):
    """The committed contract the live corpus has to keep satisfying.

    Attributes:
        declared_band: The scale band the corpus is asserted at. The
            rehearsal refuses a corpus below it, because a measurement
            taken over a smaller population would not back the claim the
            band makes.
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

    declared_band: ScaleBand
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


class StagedSource(BaseModel):
    """One live file the snapshot is assembled from.

    Attributes:
        locator: The file's repo-relative path, which is the string the
            commit policy classifies.
        destination: Where the file lands inside the staged snapshot,
            relative to the snapshot root.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    locator: Annotated[str, Field(min_length=1)]
    destination: Annotated[str, Field(min_length=1)]


def is_committed(locator: str) -> bool:
    """Report whether version control carries one path under ``.ea/``.

    Args:
        locator: A repo-relative, forward-slash path.

    Returns:
        ``True`` when the commit policy declares the path committed.

    Raises:
        UndeclaredPathError: When no policy row matches, which means the
            tree grew a file family nobody declared.
    """
    return classify_path(locator).policy is CommitPolicy.COMMITTED


def require_committed(locator: str) -> str:
    """Return a locator the snapshot layout needs, once the policy admits it.

    Args:
        locator: A repo-relative path the fixed part of the layout maps.

    Returns:
        The locator unchanged.

    Raises:
        ValueError: When the policy declares the path uncommitted. The
            layout and the policy then disagree about what a
            repository's reproducible state is, and that is a defect in
            one of the two rather than something to route around.
        UndeclaredPathError: When no policy row matches the path.
    """
    if not is_committed(locator):
        raise ValueError(f"{locator} is declared uncommitted, so the snapshot cannot stage it")
    return locator


def committed_sources(repo_root: Path) -> tuple[StagedSource, ...]:
    """Return every live file the snapshot stages, committed surface only.

    The document and the layered config sit at fixed destinations the
    snapshot layout requires, so an uncommitted classification for either
    is raised rather than filtered: a corpus missing its document is not
    a smaller corpus, it is a broken one. The ledger set is discovered
    and filtered, because which ledgers exist is a property of the tree.

    Args:
        repo_root: The repository whose ``.ea`` tree is the corpus.

    Returns:
        The document, the config, then each committed store ledger sorted
        by filename.

    Raises:
        FileNotFoundError: When the repository carries no live store.
        ValueError: When a locator the layout requires is uncommitted.
    """
    store_root = repo_root / LIVE_STORE_LOCATOR
    if not store_root.is_dir():
        raise FileNotFoundError(f"{LIVE_STORE_LOCATOR} is absent, so there is no live corpus")
    ledgers = tuple(
        StagedSource(locator=locator, destination=f"{STORE_DIRNAME}/{path.name}")
        for path, locator in (
            (path, f"{LIVE_STORE_LOCATOR}/{path.name}")
            for path in sorted(store_root.glob("*.jsonl"))
        )
        if is_committed(locator)
    )
    return (
        StagedSource(locator=require_committed(LIVE_STATE_LOCATOR), destination=DOCUMENT_FILENAME),
        StagedSource(
            locator=require_committed(LIVE_CONFIG_LOCATOR),
            destination=f"{CONFIG_DIRNAME}/{CONFIG_FILENAME}",
        ),
        *ledgers,
    )


def stage_live_corpus(*, repo_root: Path, destination: Path) -> Path:
    """Copy the live corpus into a snapshot tree the read barrier can pin.

    The copy is the whole point. The live tree has a canonical writer, so
    a read barrier over it could only ever be advisory; a barrier over an
    assembled copy is enforceable, and nothing here ever opens the live
    tree for writing.

    The registry and the telemetry surface are synthesised rather than
    copied. The live ``.ea/telemetry.db`` is declared uncommitted because
    it embeds this machine's absolute paths, so the snapshot carries the
    builders' neutral body in its place.

    Args:
        repo_root: The repository whose ``.ea`` tree is the corpus.
        destination: Where to assemble the snapshot. Created when absent.

    Returns:
        ``destination``, now laid out as a snapshot root.

    Raises:
        FileNotFoundError: When the repository carries no live corpus.
        ValueError: When a locator the layout requires is uncommitted.
    """
    (destination / STORE_DIRNAME).mkdir(parents=True, exist_ok=True)
    (destination / CONFIG_DIRNAME).mkdir(parents=True, exist_ok=True)
    for source in committed_sources(repo_root):
        shutil.copyfile(repo_root / source.locator, destination / source.destination)
    _write_json(destination / REGISTRY_FILENAME, default_registry().document())
    _write_json(destination / TELEMETRY_FILENAME, TELEMETRY_BODY)
    return destination


def corpus_bytes(root: Path) -> int:
    """Return the total size of every file in a staged snapshot."""
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _write_json(path: Path, payload: object) -> None:
    """Write ``payload`` as sorted, newline-terminated JSON."""
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")


__all__ = [
    "BAND_FLOORS",
    "LIVE_CONFIG_LOCATOR",
    "LIVE_STATE_LOCATOR",
    "LIVE_STORE_LOCATOR",
    "PIN_FILENAME",
    "LiveCorpusPin",
    "ScaleBand",
    "StagedSource",
    "band_for",
    "committed_sources",
    "corpus_bytes",
    "is_committed",
    "require_committed",
    "stage_live_corpus",
]
