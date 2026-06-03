"""Calibration anchors for the Layer-3 LLM clarity judge.

Layer 3 of the doc-clarity enforcement stack (see
``.ea/local/research/2026-05-29-doc-clarity.md``) is an LLM-as-judge that
scores newcomer-understandability where a regex cannot — audience-fit and
genuine motivation. An LLM judge is only as reliable as its calibration:
without worked anchors it drifts, scoring the same artifact differently
across runs. This module is the **calibration set** — the fixed positive
and negative examples the judge prompt embeds so every juror anchors its
0-2 scale on the same worked cases.

The anchors are deliberately drawn from the two extremes the doc-clarity
findings name:

- **positive** — the best-quality classes: a motivation-first code
  docstring and a five-of-five PR summary bullet. These score 2 on every
  dimension and are the "this is what good looks like" anchor.
- **negative** — the worst-quality classes: a jargon-dense PR bullet
  (undefined internal codes, inline path/link soup, no motivation) and a
  ``description == title`` entity description (pure restatement, zero
  motivation). The ``description == title`` anchor is the worked judgment
  the brief calls out: ``why`` and ``not_a_title_duplicate`` both score 0,
  and because those two dimensions are blocking for the description
  surface the aggregate verdict is ``fail``.

Zero new wire types. Each anchor is a frozen dataclass — local plumbing,
not a Pydantic model and not a schema surface — and every per-dimension
expected score keys on the same :data:`~eawf.platform.profiles.clarity.NEWCOMER_TEST_DIMENSIONS`
the deterministic lints and the judge prompt share. The container exists
only so the judge prompt can iterate worked examples and a round-trip test
can assert the negative anchor reduces to ``fail``; it never crosses a
store, an envelope, or ``state.json``.
"""

from __future__ import annotations

from dataclasses import dataclass

from eawf.platform.profiles.clarity import NEWCOMER_TEST_DIMENSIONS

#: The closed set of dimension keys every anchor must score, derived from
#: the shared :data:`~eawf.platform.profiles.clarity.NEWCOMER_TEST_DIMENSIONS`
#: so an anchor and the judge prompt can never disagree on what the six
#: dimensions are. A missing or extra key on an anchor is a construction
#: error (:func:`_validate_anchor_keys`), not a silently mis-calibrated
#: example.
ANCHOR_DIMENSION_KEYS: tuple[str, ...] = tuple(dim.key for dim in NEWCOMER_TEST_DIMENSIONS)

#: The top of the anchored 0-2 scale (a 3-point scale is the reliable
#: ceiling for an LLM judge per the doc-clarity Layer-3 design). A
#: dimension scored at this value fully satisfies that dimension.
ANCHOR_SCORE_MAX: int = 2


@dataclass(frozen=True)
class ClarityAnchor:
    """One worked calibration example for the clarity judge.

    A frozen dataclass — local calibration plumbing, never a wire type. It
    pairs a sample artifact with the per-dimension scores a well-calibrated
    judge should assign, so the judge prompt can show the example *and* its
    expected scoring, and a test can assert the negative anchors reduce to
    the verdict the brief specifies.

    Attributes:
        anchor_id: Stable id for the anchor (snake_case), surfaced in the
            judge prompt so a juror can refer to the worked example.
        surface: Which prose surface the sample is drawn from — one of
            ``"docstring"`` / ``"pr_bullet"`` / ``"entity_description"``.
            The ``"entity_description"`` surface is the one where the
            blocking dimensions (``why_present`` /
            ``not_a_title_duplicate``) bite.
        polarity: ``"positive"`` for a best-class example (scores all 2s)
            or ``"negative"`` for a worst-class example (scores 0 on the
            failing dimensions).
        sample: The verbatim artifact text the judge reads.
        scores: Expected per-dimension score, keyed by the dimension
            ``key`` from :data:`ANCHOR_DIMENSION_KEYS`. Every dimension key
            must be present exactly once; each value is in
            ``0..ANCHOR_SCORE_MAX``.
        rationale: One-line plain-language explanation of why the anchor
            scores the way it does, rendered beside the example in the
            judge prompt.
    """

    anchor_id: str
    surface: str
    polarity: str
    sample: str
    scores: dict[str, int]
    rationale: str

    def __post_init__(self) -> None:
        """Validate the anchor scores key the canonical dimension set.

        Raises:
            ValueError: When :attr:`scores` does not key exactly the
                :data:`ANCHOR_DIMENSION_KEYS`, when any score is outside
                ``0..ANCHOR_SCORE_MAX``, or when :attr:`polarity` /
                :attr:`surface` is not a recognized value.
        """
        _validate_anchor_keys(self.anchor_id, self.scores)
        for key, value in self.scores.items():
            if not 0 <= value <= ANCHOR_SCORE_MAX:
                raise ValueError(
                    f"anchor {self.anchor_id!r} dimension {key!r} score {value} "
                    f"outside 0..{ANCHOR_SCORE_MAX}"
                )
        if self.polarity not in ("positive", "negative"):
            raise ValueError(f"anchor {self.anchor_id!r} polarity must be positive/negative")
        if self.surface not in ("docstring", "pr_bullet", "entity_description"):
            raise ValueError(f"anchor {self.anchor_id!r} surface {self.surface!r} not recognized")


def _validate_anchor_keys(anchor_id: str, scores: dict[str, int]) -> None:
    """Raise when *scores* does not key exactly the canonical dimensions.

    Args:
        anchor_id: The anchor id, interpolated into the error message.
        scores: The candidate per-dimension score map.

    Raises:
        ValueError: When a canonical dimension key is missing from *scores*
            or *scores* carries a key that is not a canonical dimension.
    """
    expected = set(ANCHOR_DIMENSION_KEYS)
    got = set(scores)
    missing = expected - got
    extra = got - expected
    if missing:
        raise ValueError(f"anchor {anchor_id!r} missing dimension scores: {sorted(missing)}")
    if extra:
        raise ValueError(f"anchor {anchor_id!r} carries unknown dimensions: {sorted(extra)}")


def _all_max() -> dict[str, int]:
    """Return a per-dimension score map with every dimension at the max anchor."""
    return dict.fromkeys(ANCHOR_DIMENSION_KEYS, ANCHOR_SCORE_MAX)


#: Positive anchor — a motivation-first code docstring. Code docstrings are
#: the best-quality class per the doc-clarity findings: they lead with the
#: motivation, define terms inline, and read cleanly to a newcomer. Scores
#: a full 2 on every dimension.
_POSITIVE_DOCSTRING = ClarityAnchor(
    anchor_id="positive_docstring",
    surface="docstring",
    polarity="positive",
    sample=(
        "Return the freshest auditor verdict for a wave, or None when none exists.\n"
        "\n"
        "The close gate honours the last verdict an independent auditor wrote, so\n"
        "a stale earlier attempt never blocks close. Rows arrive sorted oldest\n"
        "first, so the final row is the freshest attempt."
    ),
    scores=_all_max(),
    rationale="motivation-first, terms defined inline, scannable — the best class",
)

#: Positive anchor — a five-of-five PR summary bullet. A strong PR summary
#: states what changed, why it changed, and what it unblocks, in plain
#: language with the internal code glossed on first use.
_POSITIVE_PR_BULLET = ClarityAnchor(
    anchor_id="positive_pr_bullet",
    surface="pr_bullet",
    polarity="positive",
    sample=(
        "Add a close-time gate that requires an independent auditor verdict before "
        "a high-risk wave (the smallest single-agent unit of work) may close, so a "
        "vendor-correlated blind spot cannot sail a regression past review."
    ),
    scores=_all_max(),
    rationale="what + why + what-it-unblocks, plain language, code glossed on first use",
)

#: Negative anchor — a jargon-dense PR bullet. The worst PR-prose class:
#: undefined internal codes, inline ``path:line`` and link soup, and no
#: motivation. Fails audience-fit, jargon, why, and reference-hygiene; the
#: scannability of a single bullet survives.
_NEGATIVE_JARGON_BULLET = ClarityAnchor(
    anchor_id="negative_jargon_bullet",
    surface="pr_bullet",
    polarity="negative",
    sample=(
        "Fixed the W65 perf-ceiling bump by editing src/eawf/observability/perf.py:142 "
        "and the budget table in src/eawf/platform/profiles/models.py:112, see "
        "https://example.org/jitter — SWITCH_MANUAL still flaky."
    ),
    scores={
        "audience_fit": 0,
        "jargon_defined": 0,
        "why_present": 0,
        "scannable": 1,
        "reference_hygiene": 0,
        "not_a_title_duplicate": ANCHOR_SCORE_MAX,
    },
    rationale="undefined codes, inline path/link soup, no motivation — the worst PR class",
)

#: Negative anchor — a ``description == title`` entity description. The
#: worked judgment from the doc-clarity brief: the description merely
#: restates the title, so ``why`` (the motivation is absent) and
#: ``not_a_title_duplicate`` (it is a pure restatement) both score 0. Both
#: dimensions are blocking for the description surface, so the aggregate
#: verdict is ``fail`` even though the prose is otherwise short and
#: scannable.
_NEGATIVE_DESCRIPTION_EQUALS_TITLE = ClarityAnchor(
    anchor_id="negative_description_equals_title",
    surface="entity_description",
    polarity="negative",
    sample="Add bounded title to entities. Adds a bounded title to every entity.",
    scores={
        "audience_fit": 1,
        "jargon_defined": ANCHOR_SCORE_MAX,
        "why_present": 0,
        "scannable": ANCHOR_SCORE_MAX,
        "reference_hygiene": ANCHOR_SCORE_MAX,
        "not_a_title_duplicate": 0,
    },
    rationale="pure title restatement: why=0 + not-a-duplicate=0, both blocking → fail",
)


#: The full calibration set, positives first then negatives, in the order
#: the judge prompt renders them. Frozen tuple of frozen dataclasses — a
#: stable, importable calibration anchor with zero wire-type surface.
CALIBRATION_ANCHORS: tuple[ClarityAnchor, ...] = (
    _POSITIVE_DOCSTRING,
    _POSITIVE_PR_BULLET,
    _NEGATIVE_JARGON_BULLET,
    _NEGATIVE_DESCRIPTION_EQUALS_TITLE,
)

#: The worked negative anchor the round-trip test pins: a description that
#: restates its title must reduce to ``fail`` on the description surface.
#: Exported by name so the test references the exact brief example rather
#: than re-deriving it.
DESCRIPTION_EQUALS_TITLE_ANCHOR: ClarityAnchor = _NEGATIVE_DESCRIPTION_EQUALS_TITLE


def positive_anchors() -> tuple[ClarityAnchor, ...]:
    """Return the positive (best-class) calibration anchors, in render order."""
    return tuple(a for a in CALIBRATION_ANCHORS if a.polarity == "positive")


def negative_anchors() -> tuple[ClarityAnchor, ...]:
    """Return the negative (worst-class) calibration anchors, in render order."""
    return tuple(a for a in CALIBRATION_ANCHORS if a.polarity == "negative")


__all__ = [
    "ANCHOR_DIMENSION_KEYS",
    "ANCHOR_SCORE_MAX",
    "CALIBRATION_ANCHORS",
    "DESCRIPTION_EQUALS_TITLE_ANCHOR",
    "ClarityAnchor",
    "negative_anchors",
    "positive_anchors",
]
