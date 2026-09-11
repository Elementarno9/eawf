"""The ten fixtures the v0.7 migration signal is computed over, and how to stage each.

Eight are built from typed builders, one is frozen out of this project's
own history, and one is the live corpus assembled at run time. They are
declared in one table because the rehearsal is a claim about the *set*:
"the importer handles the required fixtures" is only falsifiable if the
required fixtures are enumerated somewhere a test can iterate.

Staging is uniform. Whatever a corpus is, it reaches the rehearsal as a
throwaway copy under a temporary root, so no leg can reach back into a
committed fixture or into the repository's own tree.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Final

from tests.integration.kernel.migration._corpus_shapes import (
    MISSING_ITER_ID,
    STRUCTURAL_CORPORA,
    UNKNOWN_EXTENSION_FIELD,
)
from tests.integration.kernel.migration._live_corpus import stage_live_corpus
from tests.integration.kernel.migration._rehearsal import (
    BUILT_CORPUS_ROOT,
    HISTORICAL_CORPUS_ROOT,
    GoldenKind,
    RehearsalDisposition,
    RehearsalFixture,
)

logger = logging.getLogger(__name__)


#: The fixture name of the frozen historical corpus.
HISTORICAL_FIXTURE_NAME: Final = "p30-i26-history"

#: The fixture name of the live corpus. It carries the word ``largest``
#: because the criterion it discharges selects on it.
LARGEST_FIXTURE_NAME: Final = "largest-supported-state"

#: The snapshot subdirectory every committed corpus keeps its tree in.
SNAPSHOT_DIRNAME: Final = "snapshot"

#: The two corpora that are expected to be refused rather than imported,
#: and what each refusal has to say. The codes are the ones the importer
#: actually raises; the names are what an operator needs in order to find
#: the offending row in the source.
_CORRUPT_REFERENCE = RehearsalFixture(
    name="corrupt-reference",
    summary="a wave whose iter reference names an iter the corpus does not hold",
    disposition=RehearsalDisposition.REFUSES,
    refusal_code="migration_fabrication_detected",
    refusal_names=("waves/P01-I01-W02.batch_ref", MISSING_ITER_ID),
)

_UNKNOWN_EXTENSION_FIELD = RehearsalFixture(
    name="unknown-extension-field",
    summary="a document carrying a top-level key no disposition row declares",
    disposition=RehearsalDisposition.REFUSES,
    refusal_code="migration_collection_unknown",
    refusal_names=(UNKNOWN_EXTENSION_FIELD,),
)

_REFUSING_FIXTURES: Final[dict[str, RehearsalFixture]] = {
    fixture.name: fixture for fixture in (_CORRUPT_REFERENCE, _UNKNOWN_EXTENSION_FIELD)
}


def _built_fixture(name: str, summary: str) -> RehearsalFixture:
    """Return the declaration for one built corpus.

    Args:
        name: The corpus name, which is also its directory and test id.
        summary: What it exercises.

    Returns:
        Its refusing declaration when it is one of the two negatives,
        otherwise an importing one.
    """
    refusing = _REFUSING_FIXTURES.get(name)
    if refusing is not None:
        return refusing
    return RehearsalFixture(name=name, summary=summary, disposition=RehearsalDisposition.IMPORTS)


HISTORICAL_FIXTURE: Final = RehearsalFixture(
    name=HISTORICAL_FIXTURE_NAME,
    summary="the P30/I26 subtree frozen out of this project's own history",
    disposition=RehearsalDisposition.IMPORTS,
)

LARGEST_FIXTURE: Final = RehearsalFixture(
    name=LARGEST_FIXTURE_NAME,
    summary="the live corpus at cutover, which is the largest state the cutover supports",
    disposition=RehearsalDisposition.IMPORTS,
    golden_kind=GoldenKind.SHAPE,
)


#: Every fixture the rehearsal runs, in the order it runs them.
REHEARSAL_FIXTURES: Final[tuple[RehearsalFixture, ...]] = (
    *(_built_fixture(corpus.name, corpus.summary) for corpus in STRUCTURAL_CORPORA),
    HISTORICAL_FIXTURE,
    LARGEST_FIXTURE,
)

REHEARSAL_FIXTURE_INDEX: Final[dict[str, RehearsalFixture]] = {
    fixture.name: fixture for fixture in REHEARSAL_FIXTURES
}

#: The fixtures expected to import, which are the ones with four legs.
IMPORTING_FIXTURES: Final[tuple[RehearsalFixture, ...]] = tuple(
    fixture for fixture in REHEARSAL_FIXTURES if fixture.disposition is RehearsalDisposition.IMPORTS
)

#: The fixtures expected to be refused.
REFUSING_FIXTURES: Final[tuple[RehearsalFixture, ...]] = tuple(
    fixture for fixture in REHEARSAL_FIXTURES if fixture.disposition is RehearsalDisposition.REFUSES
)


def committed_snapshot(name: str) -> Path:
    """Return the committed snapshot tree of one fixture.

    Args:
        name: The fixture name.

    Returns:
        The directory the corpus is committed in.

    Raises:
        KeyError: When the fixture has no committed tree, which is only
            the live corpus.
    """
    if name == HISTORICAL_FIXTURE_NAME:
        return HISTORICAL_CORPUS_ROOT / SNAPSHOT_DIRNAME
    if name == LARGEST_FIXTURE_NAME:
        raise KeyError(name)
    return BUILT_CORPUS_ROOT / name / SNAPSHOT_DIRNAME


def stage_corpus(*, fixture: RehearsalFixture, root: Path, repo_root: Path) -> Path:
    """Assemble a throwaway copy of one fixture's corpus under ``root``.

    Args:
        fixture: Which corpus to stage.
        root: The temporary directory to stage into.
        repo_root: The repository, needed only by the live corpus.

    Returns:
        The staged snapshot root.

    Raises:
        FileNotFoundError: When a committed corpus is missing from the
            tree, which means the fixture set is incomplete.
    """
    staged = root / "staged"
    if fixture.name == LARGEST_FIXTURE_NAME:
        return stage_live_corpus(repo_root=repo_root, destination=staged)
    source = committed_snapshot(fixture.name)
    if not source.is_dir():
        raise FileNotFoundError(f"{fixture.name} has no committed corpus at {source}")
    shutil.copytree(source, staged)
    return staged


__all__ = [
    "HISTORICAL_FIXTURE",
    "HISTORICAL_FIXTURE_NAME",
    "IMPORTING_FIXTURES",
    "LARGEST_FIXTURE",
    "LARGEST_FIXTURE_NAME",
    "REFUSING_FIXTURES",
    "REHEARSAL_FIXTURES",
    "REHEARSAL_FIXTURE_INDEX",
    "SNAPSHOT_DIRNAME",
    "committed_snapshot",
    "stage_corpus",
]
