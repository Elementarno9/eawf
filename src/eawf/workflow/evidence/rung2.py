"""EviBound rung-2 in-process NLI scorer (Keystone-B escalation rung).

This module is the rung-2 layer of the EviBound evidence chain. It sits
directly above the W07 reference resolver
(:func:`eawf.workflow.evidence.resolve.resolve`) and the W08 rung-1
deterministic gate (:func:`eawf.workflow.evidence.evibound.run_rung1_gate`):
rung-1 answers *does the evidence reference resolve* with a deterministic
bit; rung-2 answers *does the resolved evidence actually entail the
claim* with an in-process natural-language-inference (NLI) score.

Why in-process and not a spawned jury
-------------------------------------
Rung-2 is the cheap, deterministic escalation BELOW the rung-3 spawned
jury. It runs **in-process** — no subagent spawn, no sandbox jail, no
network egress (see :class:`EntailmentScorer` contract). That keeps the
per-claim cost flat (a function call, not a metered model spawn) so the
common case (a text claim whose evidence plainly entails it) clears
without paying for the jury. Only claims rung-2 cannot confidently
entail escalate upward.

Refute-first bias
-----------------
The scorer is **refute-first**: when entailment is uncertain it biases
toward refuting / escalating rather than passing. Concretely
:func:`score_claim` maps an entailment probability ``p`` to one of three
:class:`Rung2Verdict` outcomes against two thresholds
(:data:`ENTAIL_THRESHOLD` / :data:`REFUTE_THRESHOLD`):

* ``p >= ENTAIL_THRESHOLD`` -> ``ENTAILED`` (the only passing verdict).
* ``p <= REFUTE_THRESHOLD`` -> ``REFUTED`` (a confident contradiction).
* otherwise -> ``ESCALATE`` (the uncertain middle band; rung-2 declines
  to pass and hands the claim to the rung-3 jury).

The uncertain band collapses into ESCALATE, not ENTAILED — an unsure
rung-2 never silently certifies. That is the refute-first contract: the
burden of proof is on entailment, and the default for the grey zone is
*do not pass*.

Numeric claims are forced to rung-1
-----------------------------------
NLI is an in-distribution model for *prose* entailment; a numeric claim
("latency dropped 40%", "coverage >= 0.9") is a deterministic assertion
about a measured value, not a textual-entailment judgement. Routing a
numeric claim through an NLI scorer would ask the wrong question. So
:func:`route_claim_to_rung` classifies any claim whose text carries a
numeric / comparison assertion (see :func:`looks_numeric`) as
:data:`ClaimRung.RUNG1` — it belongs to the W08 deterministic gate, not
to this rung. Only text-shaped claims route to :data:`ClaimRung.RUNG2`.

Documented escalation threshold (the <0.7 contingency)
------------------------------------------------------
:data:`ENTAIL_THRESHOLD` is set to ``0.7``. This encodes a decision, not
a tuned hyperparameter: rung-2 ASSUMES the entailment model is
in-distribution on the claim corpus. The known risk (recorded here as a
documented assumption, NOT a hidden one) is that a terse / jargon-dense
corpus pushes a real NLI model's calibrated scores below this floor. If
a real-model spike measures sustained entailment probabilities below
``ENTAIL_THRESHOLD`` on a representative terse corpus, the correct move
is NOT to lower the threshold (that re-admits the uncertain band rung-2
exists to refuse) but to MOVE THIS RUNG BEHIND THE JURY FLOOR in a later
iter (I03): rung-2 becomes advisory and the rung-3 jury becomes the
gating verdict for text claims. The :class:`EntailmentScorer` Protocol +
:func:`load_default_scorer` factory are the clean seam that makes that
move a configuration change (swap the scorer / re-route the rung) rather
than a rewrite. See :data:`RUNG2_ESCALATION_NOTE` for the operator-facing
statement of this contingency.

Pluggable scorer seam (the dependency-hygiene contract)
-------------------------------------------------------
The actual entailment score comes from an :class:`EntailmentScorer`
implementation. ``eawf`` is a lightweight workflow tool, so the DEFAULT
implementation is :class:`LexicalEntailmentScorer` — a zero-dependency
lexical-overlap heuristic that needs no model download and runs in this
sandbox. A heavier optional model (an AlignScore / DeBERTa-MNLI class
checkpoint) plugs in behind the same Protocol via
:func:`load_default_scorer`, which **lazily imports** the optional
backend and **gracefully degrades** to the lexical scorer when the
optional dependency is not installed. No heavy ML runtime dependency is
declared as a hard requirement; the optional backend is an opt-in extra
(documented at :data:`OPTIONAL_MODEL_EXTRA`). Tests inject a fake scorer
through the same Protocol and never download or run a real model.
"""

from __future__ import annotations

import importlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

from eawf.kernel.store.kinds.evidence import EvidenceRecord, EvidenceStatus, mint_evidence_id

logger = logging.getLogger(__name__)

#: Entailment-probability floor at or above which rung-2 PASSES a claim
#: (verdict ``ENTAILED``). A decision, not a tuned constant — see the
#: module docstring's escalation-threshold section + :data:`RUNG2_ESCALATION_NOTE`.
ENTAIL_THRESHOLD: Final[float] = 0.7

#: Entailment-probability ceiling at or below which rung-2 treats the
#: claim as a confident contradiction (verdict ``REFUTED``). The
#: ``(REFUTE_THRESHOLD, ENTAIL_THRESHOLD)`` open interval is the
#: uncertain band that collapses to ``ESCALATE`` under the refute-first
#: contract.
REFUTE_THRESHOLD: Final[float] = 0.3

#: The opt-in extras group an operator installs to make
#: :func:`load_default_scorer` pick up a real NLI model backend
#: (``pip install eawf[nli]``). NOT a hard runtime dependency: the
#: lexical default ships in-tree and the factory degrades to it when the
#: extra is absent. Documented here so the seam is discoverable without
#: reading the factory body.
OPTIONAL_MODEL_EXTRA: Final[str] = "nli"

#: Operator-facing statement of the documented <0.7 escalation
#: contingency. Surfaced (e.g. in a doctor / verify report) so the
#: assumption rung-2 makes is visible rather than buried in code.
RUNG2_ESCALATION_NOTE: Final[str] = (
    "rung-2 NLI assumes the entailment model is in-distribution on the claim corpus. "
    "If a real-model spike scores sustained entailment below "
    f"{ENTAIL_THRESHOLD} on a terse corpus, move rung-2 behind the rung-3 jury floor "
    "(iter I03) rather than lowering the threshold."
)

#: Matches a numeric / comparison assertion in claim text: a bare or
#: signed decimal, a percentage, or a comparison operator. Used by
#: :func:`looks_numeric` to force numeric claims onto rung-1. The number
#: alternative requires the digit run NOT be glued to a leading letter
#: (so an identifier like ``v2`` / ``utf8`` does not read as numeric)
#: while still catching ``40%``, ``>= 0.9``, ``3.5x``.
_NUMERIC_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z])[<>]=?|(?<![A-Za-z])\d+(?:\.\d+)?\s*%?",
)

#: Tokenizer for the lexical-overlap heuristic: runs of word characters,
#: lower-cased by the caller. Punctuation is dropped so "fast." and
#: "fast" tokenize identically.
_WORD_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")

#: Lexical stop-words excluded from the overlap denominator so a claim's
#: content words drive the score rather than its function words.
_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "will",
        "with",
    }
)


class ClaimRung(StrEnum):
    """The EviBound rung a claim routes to.

    ``RUNG1`` is the W08 deterministic gate (numeric / measured
    assertions); ``RUNG2`` is this in-process NLI scorer (text
    entailment). The router never returns rung-3 directly — escalation to
    the jury is a *verdict* of rung-2 (:attr:`Rung2Verdict.ESCALATE`),
    not a routing decision.
    """

    RUNG1 = "rung1"
    RUNG2 = "rung2"


class Rung2Verdict(StrEnum):
    """The three-way outcome of scoring one text claim at rung-2.

    Distinct from the binary :class:`~eawf.kernel.store.kinds.evidence.EvidenceStatus`
    because rung-2's whole purpose is the third (``ESCALATE``) state: a
    binary pass/fail would force the uncertain band to pick a side, which
    is exactly the silent-certification failure the refute-first contract
    forbids. :func:`verdict_to_status` maps the three-way verdict onto the
    persisted binary status when a row is written.
    """

    #: Entailment probability cleared :data:`ENTAIL_THRESHOLD`; rung-2
    #: certifies the claim. The only PASSING verdict.
    ENTAILED = "entailed"
    #: Entailment probability fell to / below :data:`REFUTE_THRESHOLD`; a
    #: confident contradiction. Rung-2 refutes the claim outright.
    REFUTED = "refuted"
    #: Entailment probability landed in the uncertain band; rung-2
    #: declines to pass and hands the claim to the rung-3 jury.
    ESCALATE = "escalate"


@runtime_checkable
class EntailmentScorer(Protocol):
    """In-process entailment scorer — the pluggable rung-2 backend.

    An implementation maps ``(premise, hypothesis)`` pairs to entailment
    probabilities in ``[0.0, 1.0]`` where ``premise`` is the resolved
    evidence text and ``hypothesis`` is the claim text. Higher means the
    premise more strongly entails the claim.

    Contract (the in-process / no-egress guarantee):

    * **In-process** — an implementation MUST compute the score in the
      calling process. No subagent spawn, no sandbox jail, no network
      egress. A backend that needs a remote API is out of scope for
      rung-2 (that is the rung-3 jury's territory).
    * **Batchable** — :meth:`score_batch` scores a whole list of pairs in
      one call so a model-backed implementation can vectorise; the
      default lexical scorer simply maps over the list.
    * **Total** — every pair yields a probability; an implementation must
      not raise on ordinary prose input. Degenerate input (empty
      hypothesis) is the caller's concern, handled before scoring.
    """

    def score_batch(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Return one entailment probability in ``[0, 1]`` per ``(premise, hypothesis)`` pair."""
        ...


@dataclass(frozen=True)
class LexicalEntailmentScorer:
    """Zero-dependency lexical-overlap entailment heuristic (the default backend).

    The score is the fraction of the hypothesis's content tokens (stop-words
    removed, lower-cased) that also appear in the premise. It is a
    deliberately conservative stand-in for a real NLI model: it captures the
    "the evidence text literally mentions what the claim asserts" signal
    without any model download, so it runs in any environment ``eawf`` runs
    in. A real AlignScore / DeBERTa-MNLI backend plugs in behind the same
    :class:`EntailmentScorer` Protocol via :func:`load_default_scorer`.

    The heuristic is intentionally lossy — it cannot detect negation or
    paraphrase — which is WHY rung-2 is refute-first and escalates the
    uncertain band rather than trusting a high lexical overlap as proof.

    Attributes:
        empty_hypothesis_score: Probability returned when the hypothesis
            has no content tokens after stop-word removal. Defaults to
            ``0.0`` so a contentless claim refutes / escalates rather than
            trivially "entailing" against any premise.
    """

    empty_hypothesis_score: float = 0.0

    def score_batch(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Score each pair by content-token overlap of hypothesis into premise.

        Args:
            pairs: ``(premise, hypothesis)`` text pairs.

        Returns:
            One probability in ``[0, 1]`` per pair, in input order.
        """
        return [self._score_one(premise, hypothesis) for premise, hypothesis in pairs]

    def _score_one(self, premise: str, hypothesis: str) -> float:
        """Return the fraction of *hypothesis* content tokens present in *premise*."""
        hyp_tokens = _content_tokens(hypothesis)
        if not hyp_tokens:
            return self.empty_hypothesis_score
        prem_tokens = _content_tokens(premise)
        overlap = sum(1 for tok in hyp_tokens if tok in prem_tokens)
        return overlap / len(hyp_tokens)


@dataclass(frozen=True)
class Rung2ClaimResult:
    """Typed outcome of scoring one text claim at rung-2.

    Attributes:
        claim: The claim text that was scored (the NLI hypothesis).
        probability: The entailment probability the
            :class:`EntailmentScorer` returned, in ``[0, 1]``.
        verdict: The three-way :class:`Rung2Verdict` the probability maps
            to under the refute-first thresholds.
        reason: Short human-readable explanation of the verdict.
    """

    claim: str
    probability: float
    verdict: Rung2Verdict
    reason: str = ""


def _content_tokens(text: str) -> set[str]:
    """Return the lower-cased non-stop-word tokens of *text*."""
    return {tok for tok in _WORD_RE.findall(text.lower()) if tok not in _STOPWORDS}


def looks_numeric(claim: str) -> bool:
    """Return True when *claim* carries a numeric / comparison assertion.

    A claim like ``"latency dropped 40%"`` or ``"coverage >= 0.9"`` is a
    deterministic assertion about a measured value, not a prose-entailment
    judgement, so it belongs to the rung-1 deterministic gate rather than
    the rung-2 NLI scorer. The detector matches a bare / signed decimal, a
    percentage, or a comparison operator (``<``, ``>``, ``<=``, ``>=``)
    while NOT treating a digit glued to a leading letter (an identifier
    like ``v2``) as numeric.

    Args:
        claim: The claim text to classify.

    Returns:
        ``True`` if the claim reads as a numeric / comparison assertion.
    """
    return _NUMERIC_RE.search(claim) is not None


def route_claim_to_rung(claim: str) -> ClaimRung:
    """Route *claim* to rung-1 (numeric) or rung-2 (text).

    Numeric / comparison claims are forced to :attr:`ClaimRung.RUNG1` —
    NLI is text-only (see the module docstring). Everything else routes to
    :attr:`ClaimRung.RUNG2` for in-process entailment scoring.

    Args:
        claim: The claim text to route.

    Returns:
        :attr:`ClaimRung.RUNG1` for a numeric claim, else
        :attr:`ClaimRung.RUNG2`.

    Raises:
        ValueError: When *claim* is empty / whitespace-only — an empty
            claim cannot be routed to any rung.
    """
    if not claim.strip():
        raise ValueError("claim must be non-empty")
    return ClaimRung.RUNG1 if looks_numeric(claim) else ClaimRung.RUNG2


def classify_probability(probability: float) -> tuple[Rung2Verdict, str]:
    """Map an entailment *probability* to a refute-first :class:`Rung2Verdict`.

    The mapping is the heart of the refute-first contract:

    * ``probability >= ENTAIL_THRESHOLD`` -> ``ENTAILED`` (pass).
    * ``probability <= REFUTE_THRESHOLD`` -> ``REFUTED`` (confident
      contradiction).
    * otherwise -> ``ESCALATE`` (uncertain band; do NOT pass — hand to
      the rung-3 jury).

    The uncertain middle band resolves to ``ESCALATE`` rather than
    ``ENTAILED`` so an unsure rung-2 never silently certifies.

    Args:
        probability: Entailment probability in ``[0, 1]``.

    Returns:
        A ``(verdict, reason)`` pair.

    Raises:
        ValueError: When *probability* is outside ``[0, 1]``.
    """
    if not 0.0 <= probability <= 1.0:
        raise ValueError(f"entailment probability out of range: {probability!r}")
    if probability >= ENTAIL_THRESHOLD:
        return Rung2Verdict.ENTAILED, (
            f"entailment probability {probability:.3f} >= {ENTAIL_THRESHOLD} threshold"
        )
    if probability <= REFUTE_THRESHOLD:
        return Rung2Verdict.REFUTED, (
            f"entailment probability {probability:.3f} <= {REFUTE_THRESHOLD} refute floor"
        )
    return Rung2Verdict.ESCALATE, (
        f"entailment probability {probability:.3f} in uncertain band "
        f"({REFUTE_THRESHOLD}, {ENTAIL_THRESHOLD}) -> escalate to jury (refute-first)"
    )


def verdict_to_status(verdict: Rung2Verdict) -> EvidenceStatus:
    """Map a three-way :class:`Rung2Verdict` onto the persisted binary status.

    ``ENTAILED`` -> ``"pass"`` (certified). ``REFUTED`` -> ``"fail"`` (a
    confident contradiction is a failing gate). ``ESCALATE`` ->
    ``"blocked"`` — rung-2 did not reach a pass/fail bit and the claim is
    blocked on the rung-3 jury, which is exactly the ``"blocked"``
    semantics the verify spine uses for "a later rung owns this".

    Args:
        verdict: The rung-2 three-way verdict.

    Returns:
        The :class:`~eawf.kernel.store.kinds.evidence.EvidenceStatus` to
        stamp on the persisted row.
    """
    if verdict is Rung2Verdict.ENTAILED:
        return "pass"
    if verdict is Rung2Verdict.REFUTED:
        return "fail"
    return "blocked"


def score_claim(
    claim: str,
    evidence_text: str,
    *,
    scorer: EntailmentScorer,
) -> Rung2ClaimResult:
    """Score one text *claim* against *evidence_text* through *scorer* (refute-first).

    The single-claim convenience wrapper over :func:`score_claims`. The
    claim is the NLI hypothesis; the resolved evidence is the premise.

    Args:
        claim: The claim text (NLI hypothesis).
        evidence_text: The resolved evidence text (NLI premise).
        scorer: The in-process :class:`EntailmentScorer` backend.

    Returns:
        A :class:`Rung2ClaimResult` carrying the probability + refute-first
        verdict.

    Raises:
        ValueError: When *claim* is empty / whitespace-only.
    """
    return score_claims([(claim, evidence_text)], scorer=scorer)[0]


def score_claims(
    pairs: list[tuple[str, str]],
    *,
    scorer: EntailmentScorer,
) -> list[Rung2ClaimResult]:
    """Batch-score ``(claim, evidence_text)`` *pairs* through *scorer* (refute-first).

    Routes the whole batch through :meth:`EntailmentScorer.score_batch` in
    one call (so a model-backed scorer can vectorise) and maps each
    probability to a refute-first :class:`Rung2Verdict`. The premise /
    hypothesis order handed to the scorer is ``(evidence_text, claim)`` —
    the evidence entails the claim, not the other way round.

    An empty *pairs* list returns an empty result list (the empty-batch
    boundary) without calling the scorer.

    Args:
        pairs: ``(claim, evidence_text)`` text pairs to score.
        scorer: The in-process :class:`EntailmentScorer` backend.

    Returns:
        One :class:`Rung2ClaimResult` per input pair, in input order.

    Raises:
        ValueError: When any claim in *pairs* is empty / whitespace-only.
    """
    if not pairs:
        return []
    for claim, _ in pairs:
        if not claim.strip():
            raise ValueError("claim must be non-empty")
    # Premise = evidence, hypothesis = claim: the evidence entails the claim.
    scorer_pairs = [(evidence_text, claim) for claim, evidence_text in pairs]
    probabilities = scorer.score_batch(scorer_pairs)
    results: list[Rung2ClaimResult] = []
    for (claim, _), probability in zip(pairs, probabilities, strict=True):
        verdict, reason = classify_probability(probability)
        results.append(
            Rung2ClaimResult(
                claim=claim,
                probability=probability,
                verdict=verdict,
                reason=reason,
            )
        )
    logger.debug(
        f"score_claims pairs={len(pairs)} "
        f"entailed={sum(1 for r in results if r.verdict is Rung2Verdict.ENTAILED)} "
        f"refuted={sum(1 for r in results if r.verdict is Rung2Verdict.REFUTED)} "
        f"escalate={sum(1 for r in results if r.verdict is Rung2Verdict.ESCALATE)}"
    )
    return results


def run_rung2_gate(
    claim: str,
    evidence_text: str,
    *,
    scope_id: str,
    scorer: EntailmentScorer,
) -> EvidenceRecord:
    """Score *claim* against *evidence_text* and emit a typed evidence row.

    The rung-2 analogue of
    :func:`eawf.workflow.evidence.evibound.run_rung1_gate`: it scores one
    text claim through the in-process NLI *scorer* and returns an
    :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord` whose
    ``status`` mirrors the refute-first verdict (``ENTAILED`` -> ``pass``
    certifies; ``REFUTED`` -> ``fail``; ``ESCALATE`` -> ``blocked``, the
    rung-3 jury owns it). The row's ``evidence_kind`` is ``"jury"`` —
    rung-2 is the in-process member of the jury escalation family, not a
    deterministic gate — and its ``metrics`` carries the entailment
    probability so a downstream consumer can re-threshold or audit the
    call without re-running the scorer.

    The caller is responsible for the routing precondition: only a text
    claim (``route_claim_to_rung(claim) is ClaimRung.RUNG2``) belongs
    here. A numeric claim must go to the W08 rung-1 deterministic gate.

    Args:
        claim: The claim text (NLI hypothesis).
        evidence_text: The resolved evidence text (NLI premise).
        scope_id: URN of the scope the evidence backs.
        scorer: The in-process :class:`EntailmentScorer` backend.

    Returns:
        An :class:`EvidenceRecord` whose ``status`` mirrors the rung-2
        verdict.

    Raises:
        ValueError: When *claim* is empty / whitespace-only.
    """
    result = score_claim(claim, evidence_text, scorer=scorer)
    status = verdict_to_status(result.verdict)
    summary = f"rung-2 NLI {result.verdict.value} for claim ({result.reason})"
    record = EvidenceRecord(
        id=mint_evidence_id(),
        scope_id=scope_id,
        produced_by="tool",
        evidence_kind="jury",
        status=status,
        summary=summary[:500],
        metrics={"entailment_probability": result.probability},
        created_at=datetime.now(UTC),
    )
    logger.debug(
        f"run_rung2_gate verdict={result.verdict.value} status={status!r} "
        f"probability={result.probability:.3f}"
    )
    return record


def load_default_scorer() -> EntailmentScorer:
    """Return the rung-2 entailment scorer, preferring an optional model backend.

    The pluggable seam (the dependency-hygiene contract): try to lazily
    import an optional NLI-model backend, and fall back to the in-tree
    :class:`LexicalEntailmentScorer` when that backend is not installed.
    The lazy import means importing this module never pulls a heavy ML
    dependency; the optional backend is an opt-in extra (see
    :data:`OPTIONAL_MODEL_EXTRA`). The fallback means rung-2 always has a
    working in-process scorer even in a minimal install.

    The optional backend module
    (``eawf.workflow.evidence.rung2_model.ModelEntailmentScorer``) is NOT
    shipped in this wave — only the seam is. The import is resolved
    dynamically (:func:`importlib.import_module`) precisely so the
    not-yet-existing module is a runtime miss, not a static type error:
    until it exists this factory always returns the lexical scorer, which
    is the intended graceful-degradation path, not a bug. A backend that
    is present but does not satisfy the :class:`EntailmentScorer` Protocol
    is also rejected (degrade to lexical) rather than handed back.

    Returns:
        An :class:`EntailmentScorer`: the optional model backend when
        importable AND Protocol-conformant, else
        :class:`LexicalEntailmentScorer`.
    """
    # Dynamic, guarded import: the optional model backend lives in a
    # sibling module shipped only with the ``[nli]`` extra. Resolving it
    # via ``import_module`` keeps module import dependency-free AND keeps
    # the not-yet-shipped target out of the static import graph.
    try:
        module = importlib.import_module("eawf.workflow.evidence.rung2_model")
        scorer = module.ModelEntailmentScorer()
    except ImportError, AttributeError:
        logger.debug(
            "load_default_scorer optional model backend absent; "
            f"degrading to lexical (install eawf[{OPTIONAL_MODEL_EXTRA}] for the model)"
        )
        return LexicalEntailmentScorer()
    if not isinstance(scorer, EntailmentScorer):
        logger.debug(
            "load_default_scorer optional backend does not satisfy EntailmentScorer; "
            "degrading to lexical"
        )
        return LexicalEntailmentScorer()
    return scorer


@dataclass(frozen=True)
class Rung2Config:
    """Frozen rung-2 configuration snapshot for surfacing the documented contract.

    A read-only value object a doctor / verify report can render to make
    the rung-2 thresholds + the <0.7 escalation contingency visible to an
    operator rather than buried in source. Construct via
    :meth:`current` to capture the module's live constants.

    Attributes:
        entail_threshold: The pass floor (:data:`ENTAIL_THRESHOLD`).
        refute_threshold: The refute ceiling (:data:`REFUTE_THRESHOLD`).
        escalation_note: The operator-facing <0.7 contingency
            (:data:`RUNG2_ESCALATION_NOTE`).
        optional_model_extra: The opt-in extras group for the real model
            backend (:data:`OPTIONAL_MODEL_EXTRA`).
    """

    entail_threshold: float = ENTAIL_THRESHOLD
    refute_threshold: float = REFUTE_THRESHOLD
    escalation_note: str = RUNG2_ESCALATION_NOTE
    optional_model_extra: str = OPTIONAL_MODEL_EXTRA

    @classmethod
    def current(cls) -> Rung2Config:
        """Return the live rung-2 configuration snapshot."""
        return cls()


__all__ = [
    "ENTAIL_THRESHOLD",
    "OPTIONAL_MODEL_EXTRA",
    "REFUTE_THRESHOLD",
    "RUNG2_ESCALATION_NOTE",
    "ClaimRung",
    "EntailmentScorer",
    "LexicalEntailmentScorer",
    "Rung2ClaimResult",
    "Rung2Config",
    "Rung2Verdict",
    "classify_probability",
    "load_default_scorer",
    "looks_numeric",
    "route_claim_to_rung",
    "run_rung2_gate",
    "score_claim",
    "score_claims",
    "verdict_to_status",
]
