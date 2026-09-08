"""One canonical name per release concept, with the retired spelling gone.

The release subsystem grew four pairs of names a reader could not tell
apart: two receipt families both spelled ``receipts``, the CI receipt
producer beside the readiness probe registry, and two modules one letter
apart holding "what a read-back found" and "what it settles". A near
collision is not a style complaint here -- ``read the receipt`` at a call
site named two different artifacts, so a reader had to open the import
line to learn which subsystem the code was in.

Each pair is now one canonical name. The retired spelling is *deleted*
rather than aliased: an alias kept for compatibility would leave both
spellings live, which is the state this test exists to prevent. So each
row asserts three things -- the canonical name resolves, the retired
module is not importable, and the retired spelling appears nowhere in
the shipped source.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parents[4] / "src" / "eawf"

RELEASE_PACKAGE = SOURCE_ROOT / "workflow" / "release"


@dataclass(frozen=True)
class ConceptRename:
    """One retired spelling and the canonical name that replaced it.

    Attributes:
        retired: The spelling that must be absent from the source tree.
        canonical: The single name the concept now carries.
        module: Dotted path holding :attr:`canonical`, or the canonical
            module itself when the rename was a module rename.
        attribute: Attribute the canonical name resolves to inside
            :attr:`module`, or ``None`` for a module rename.
    """

    retired: str
    canonical: str
    module: str
    attribute: str | None = None


#: The four pairs. Ordered receipt, producer, observation -- the three
#: families the collisions fell into.
RENAMES: tuple[ConceptRename, ...] = (
    ConceptRename(
        retired="eawf.workflow.release.receipts",
        canonical="eawf.workflow.release.pipeline_receipts",
        module="eawf.workflow.release.pipeline_receipts",
    ),
    ConceptRename(
        retired="eawf.workflow.release.producers",
        canonical="eawf.workflow.release.signal_probes",
        module="eawf.workflow.release.signal_probes",
    ),
    ConceptRename(
        retired="eawf.workflow.release.observe",
        canonical="eawf.workflow.release.settlement",
        module="eawf.workflow.release.settlement",
    ),
    ConceptRename(
        retired="observe_publication",
        canonical="collect_observation",
        module="eawf.workflow.release.adapters",
        attribute="collect_observation",
    ),
)


def _shipped_sources() -> list[Path]:
    """Return every Python module shipped under ``src/eawf/``."""
    return sorted(p for p in SOURCE_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _occurrences(spelling: str) -> list[str]:
    """Return ``path:line`` for every use of *spelling* in the source tree.

    The spelling is matched on a trailing word boundary so a canonical
    name that merely *contains* a retired one (``observe_target`` after
    ``observe``) is not reported, while a dotted use of the retired
    module (``...release.observe.observe_target``) still is.

    Args:
        spelling: The retired spelling to hunt for.

    Returns:
        One ``path:line`` row per occurrence, repo-relative.
    """
    pattern = re.compile(rf"{re.escape(spelling)}\b")
    hits: list[str] = []
    for path in _shipped_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(SOURCE_ROOT.parent.parent)}:{number}")
    return hits


@pytest.mark.parametrize("rename", RENAMES, ids=lambda rename: rename.retired)
def test_release_concept_names_are_canonical(rename: ConceptRename) -> None:
    """The canonical name resolves and the retired spelling is absent."""
    module = importlib.import_module(rename.module)
    if rename.attribute is None:
        assert module.__name__ == rename.canonical
    else:
        assert getattr(module, rename.attribute) is not None
    assert _occurrences(rename.retired) == []


@pytest.mark.parametrize("rename", RENAMES, ids=lambda rename: rename.retired)
def test_release_concept_names_keep_no_adapter_shim(rename: ConceptRename) -> None:
    """No retired spelling survives as an importable alias."""
    if rename.attribute is None:
        assert importlib.util.find_spec(rename.retired) is None
        assert not (RELEASE_PACKAGE / f"{rename.retired.rsplit('.', 1)[1]}.py").exists()
    else:
        assert not hasattr(importlib.import_module(rename.module), rename.retired)


def test_release_concept_names_cover_four_pairs() -> None:
    """Exactly four pairs are claimed, each with a distinct canonical name."""
    assert len(RENAMES) == 4
    assert len({rename.canonical for rename in RENAMES}) == 4
    assert len({rename.retired for rename in RENAMES}) == 4


def test_occurrences_reports_a_spelling_the_source_still_carries() -> None:
    """The absence check can fail: a live spelling is reported, with lines."""
    hits = _occurrences("collect_observation")
    assert hits, "the canonical spelling must be findable, or the scan proves nothing"
    assert all(row.startswith("src/eawf/") for row in hits)


def test_occurrences_of_an_unused_spelling_is_empty() -> None:
    """A spelling nothing uses reports no rows (the empty boundary)."""
    assert _occurrences("observe_publication_that_never_existed") == []


def test_occurrences_does_not_match_a_longer_identifier() -> None:
    """A retired prefix inside a longer name is not a false positive."""
    assert _occurrences("collect_observatio") == []
