"""Typed ``MathClaim`` + ``MathExplainer`` doc-type for verification-grounded math.

Agents are strong at olympiad-style math and weak at research-level
conceptual math, and the dominant failure is a fluent, confident, *invalid*
derivation rather than a refusal. The defence — the same discipline that lets
a non-expert reader trust a math explanation they cannot verify themselves —
is one per-claim contract: every math claim carries **four facets**

1. an *intuition* (prose that explains, not merely asserts, the result),
2. a runnable, CI-checked *example* — a :class:`~eawf.kernel.spec.common.GateSpec`
   hosting a CAS / property-test / SMT / units / interval verifier,
3. an *assumptions / regime-of-validity* statement (where the claim breaks),
4. a canonical *citation* — an :class:`~eawf.kernel.spec.common.EvidenceRef`.

A :class:`MathClaim` is *constructible* only when all four facets are present
(Pydantic enforces this at ingestion: a missing facet raises
:class:`pydantic.ValidationError`). A :class:`MathExplainer` wrapping a set of
claims is *promotable* only when, additionally, every claim's gate is actually
runnable and its citation actually resolves — the same shape as the
``IntentBrief.evidence_refs`` EviBound contract, where ingestion succeeds but
the promotion gate fails until the evidence is grounded.

The ``verifier`` + ``assurance`` hints record *which* check ran and *how
strong* it is. Per the brief's refute-before-you-certify principle, most
verifiers (CAS, high-precision numeric, property test, units, SMT-sat,
reference-DB lookup) only *refute* — they kill false claims without proving
true ones — and only validated-interval arithmetic and a kernel-checked proof
*certify*. The ``assurance`` Literal is the typed home for that distinction so
a downstream lint / judge can record whether a passing gate proves the claim
or merely fails to refute it.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from eawf.kernel.spec.common import EvidenceRef, GateSpec, _StrictModel

# Assurance level of a math verifier — the typed refute/certify split.
#
#   "refute"  -> the verifier can only kill a false claim (necessary, not
#                sufficient): CAS identity, high-precision numeric cross-check,
#                property-based test, units check, SMT-sat counterexample hunt,
#                reference-DB lookup. A passing gate *fails to refute*; it does
#                not prove.
#   "certify" -> the verifier proves the claim within its scope: validated /
#                interval (ball) arithmetic for a bound, or a kernel-checked
#                proof (Lean / Isabelle / Coq). A passing gate is a guaranteed
#                yes.
MathAssurance = Literal["refute", "certify"]


class MathClaim(_StrictModel):
    """One verification-grounded math claim carrying all four facets.

    Strict-mode (``ConfigDict(extra="forbid")`` via :class:`_StrictModel`):
    an unknown key fails at ingestion. Every facet is a *required* field so a
    claim missing its intuition, runnable example, regime, or citation raises
    :class:`pydantic.ValidationError` rather than silently shipping an
    un-grounded assertion.

    Attributes:
        statement: The math claim itself — the identity / invariant / bound /
            derived-quantity result being asserted. Bounded at 500 characters
            so it stays a single scannable claim, not a derivation.
        intuition: Facet (a) — prose that *explains* why the statement holds
            (the analogy, the shape of the argument), targeting a reader who
            cannot follow the formal proof. Bounded at 2000 characters; the
            ``min_length=1`` floor forbids an empty intuition (the facet that
            substitutes a citation for the conceptually-hard step is exactly
            the non-expert trap this contract defends against).
        example_gate: Facet (b) — the runnable, CI-checked example. A
            :class:`GateSpec` whose ``kind`` hosts the verifier (a
            ``command_exit_zero`` gate shelling a CAS / property / SMT / units
            / interval check). The gate is what makes the claim *trustable*
            rather than merely *asserted*.
        regime: Facet (c), the headline — a one-line statement of the
            regime/domain in which the claim is valid (e.g. "real, positive
            volatility; correlation in (-1, 1)"). Bounded at 500 characters.
        assumptions: Facet (c), the detail — the explicit assumptions the
            claim rests on, one per entry. Bounded list (max 20) of bounded
            strings (max 500) so the regime-of-validity stays enumerable.
            May be empty when the ``regime`` headline fully states it.
        citation: Facet (d) — the canonical citation backing the claim, as a
            typed :class:`EvidenceRef` (audit / artifact / decision /
            store-record URN or external URL).
        verifier: Thin hint naming *which* check the ``example_gate`` runs
            (e.g. ``"sympy+mpmath"``, ``"hypothesis"``, ``"z3"``,
            ``"python-flint-interval"``, ``"lean-mathlib"``). Lets the lint
            and judge record the check family without parsing the gate argv.
        assurance: Thin hint — ``"refute"`` (the verifier only kills false
            claims) or ``"certify"`` (it proves the claim within precision).
            Most verifiers are ``refute``; reserve ``certify`` for interval
            arithmetic and proof assistants.
    """

    statement: Annotated[str, Field(min_length=1, max_length=500)]
    intuition: Annotated[str, Field(min_length=1, max_length=2000)]
    example_gate: GateSpec
    regime: Annotated[str, Field(min_length=1, max_length=500)]
    assumptions: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=20
    )
    citation: EvidenceRef
    verifier: Annotated[str, Field(min_length=1, max_length=120)]
    assurance: MathAssurance

    def gate_is_runnable(self) -> bool:
        """Return whether :attr:`example_gate` is actually runnable.

        A gate is runnable when it hosts a ``command_exit_zero`` check that
        carries a non-empty ``argv`` vector the gate-runner can execute. A
        gate of any other kind, or an argv-bearing kind whose ``args['argv']``
        is missing or empty, is *not* runnable — the example would be a
        cosmetic pin (the dead-snippet-gate failure mode the brief documents).
        """
        if self.example_gate.kind != "command_exit_zero":
            return False
        argv = self.example_gate.args.get("argv")
        return isinstance(argv, list) and len(argv) > 0

    def citation_resolves(self) -> bool:
        """Return whether :attr:`citation` carries a non-empty reference.

        The structural resolution check this layer can make without I/O: the
        citation's ``ref`` is a non-blank string. Deep resolution (does the
        file:line / URN / URL actually exist and entail the claim) is the
        gate-runner's / L3-judge's job, not the spec layer's.
        """
        return bool(self.citation.ref.strip())

    def is_grounded(self) -> bool:
        """Return whether the claim's gate is runnable AND its citation resolves.

        The per-claim half of the promotion contract: a grounded claim has a
        runnable example *and* a resolving citation (facets (b) and (d)
        verified, not merely present). Facets (a) intuition and (c) regime are
        guaranteed present by the model's required fields.
        """
        return self.gate_is_runnable() and self.citation_resolves()


class MathExplainer(_StrictModel):
    """A verification-grounded math-explainer doc-type (artifact kind).

    Wraps an ordered set of :class:`MathClaim` rows under a bounded title +
    slug. Maps onto :attr:`~eawf.kernel.state.enums.ArtifactKind.MATH_EXPLAINER`
    when promoted to a tracked artifact.

    Promotability mirrors the ``IntentBrief.evidence_refs`` EviBound
    invariant: ingestion succeeds for a draft whose claims are not yet
    grounded, but :meth:`is_promotable` is ``False`` until *every* claim's
    gate is runnable and its citation resolves. Construction enforces only
    that the explainer carries at least one claim (an explainer with no claims
    has nothing to ground).

    Attributes:
        title: Bounded imperative noun-phrase label (<= 72 chars, no trailing
            period) per the entity-title convention.
        slug: Stable repo-relative-safe slug stem (``[a-z0-9][a-z0-9._-]*``)
            used for the artifact path / id.
        claims: Ordered, non-empty list of :class:`MathClaim` rows. Each is
            constructible only with all four facets present; promotion further
            requires each to be grounded.
    """

    title: Annotated[str, Field(min_length=1, max_length=72)]
    slug: Annotated[str, Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")]
    claims: list[MathClaim] = Field(min_length=1)

    def ungrounded_claim_indexes(self) -> list[int]:
        """Return the positions of claims that are not yet grounded.

        A claim is ungrounded when its gate is not runnable or its citation
        does not resolve (see :meth:`MathClaim.is_grounded`). The returned
        zero-based indexes let a promotion surface name *which* claims block
        promotion rather than failing opaquely.
        """
        return [i for i, claim in enumerate(self.claims) if not claim.is_grounded()]

    def is_promotable(self) -> bool:
        """Return whether every claim is grounded (gate runnable + citation resolves).

        The explainer-level half of the contract: promotable iff
        :meth:`ungrounded_claim_indexes` is empty. A draft with an
        un-runnable gate or an empty citation on any claim is constructible
        but not promotable, exactly like an ``IntentBrief`` whose
        ``evidence_refs`` are not yet sourced.
        """
        return not self.ungrounded_claim_indexes()


__all__ = [
    "MathAssurance",
    "MathClaim",
    "MathExplainer",
]
