"""Unit tests for :mod:`eawf.workflow.evidence.rung4` (EviBound rung-4).

Covers the final EviBound rung and the closed verdict vocabulary:

* :class:`CriterionVerdict` — the closed StrEnum with exactly the five
  values ``certified | supported | refuted | unresolved | attested`` and
  no others.
* :func:`render_attested_verdict` — the rung-4 attested render. Asserts
  it is RENDER-ONLY (no gate runner / scorer is taken or invoked), that
  it is the LOWEST assurance rung (the verdict is ``ATTESTED``), and the
  error paths (a non-attested criterion and a non-attesting producer
  fail fast).
* :func:`dominant_verdict` — the refute-first combine reduction. Asserts
  ``REFUTED`` and ``UNRESOLVED`` dominate the positive / attested
  verdicts so an uncertain or contradicted criterion is never reported
  as certified on the strength of a co-resident pass, plus the
  empty-list boundary.
* :func:`verdict_to_status` — each verdict collapses to the right binary
  persisted status.

The attested render takes no runner and no subprocess, so these tests
need no git worktree, no allowlisted argv, and no model.
"""

from __future__ import annotations

import pytest

from eawf.kernel.spec.common import CriterionSpec
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.workflow.evidence.rung4 import (
    CriterionVerdict,
    dominant_verdict,
    render_attested_verdict,
    verdict_to_status,
)

_SCOPE = "urn:eawf:v1:wave:owner/P29-I01-W10"


def _attested_criterion(criterion_id: str = "CR-1") -> CriterionSpec:
    """Build an attested CriterionSpec (the rung-4 input)."""
    return CriterionSpec(
        id=criterion_id,
        text="operator confirms the rollout looks healthy",
        kind="judgement",
        acceptance_style="binary",
        evidence_kind="attested",
    )


def _criterion(evidence_kind: str, criterion_id: str = "CR-1") -> CriterionSpec:
    """Build a CriterionSpec with an arbitrary evidence flavor."""
    return CriterionSpec(
        id=criterion_id,
        text="some criterion",
        kind="behavioral",
        acceptance_style="binary",
        evidence_kind=evidence_kind,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# CriterionVerdict — the closed vocabulary (exactly five values).
# --------------------------------------------------------------------------- #


def test_criterion_verdict_has_exactly_five_members() -> None:
    """The closed verdict StrEnum carries exactly the five named values."""
    assert [member.value for member in CriterionVerdict] == [
        "certified",
        "supported",
        "refuted",
        "unresolved",
        "attested",
    ]


@pytest.mark.parametrize(
    ("member", "value"),
    [
        (CriterionVerdict.CERTIFIED, "certified"),
        (CriterionVerdict.SUPPORTED, "supported"),
        (CriterionVerdict.REFUTED, "refuted"),
        (CriterionVerdict.UNRESOLVED, "unresolved"),
        (CriterionVerdict.ATTESTED, "attested"),
    ],
)
def test_criterion_verdict_value_strings(member: CriterionVerdict, value: str) -> None:
    """Each member's wire value is the exact lowercase verdict string."""
    assert member.value == value
    assert member == value  # StrEnum compares equal to its string value.


# --------------------------------------------------------------------------- #
# verdict_to_status — closed verdict -> persisted binary status.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("verdict", "status"),
    [
        (CriterionVerdict.CERTIFIED, "pass"),
        (CriterionVerdict.SUPPORTED, "pass"),
        (CriterionVerdict.REFUTED, "fail"),
        (CriterionVerdict.UNRESOLVED, "blocked"),
        (CriterionVerdict.ATTESTED, "pass"),
    ],
)
def test_verdict_to_status(verdict: CriterionVerdict, status: str) -> None:
    """Each verdict collapses to the documented binary EvidenceStatus."""
    assert verdict_to_status(verdict) == status


def test_verdict_to_status_covers_every_verdict() -> None:
    """The status map is total over the closed verdict vocabulary."""
    for verdict in CriterionVerdict:
        assert verdict_to_status(verdict) in ("pass", "fail", "blocked", "waived")


# --------------------------------------------------------------------------- #
# dominant_verdict — refute-first combine reduction.
# --------------------------------------------------------------------------- #


def test_dominant_verdict_single() -> None:
    """A single-element reduction returns that element."""
    assert dominant_verdict([CriterionVerdict.CERTIFIED]) is CriterionVerdict.CERTIFIED


def test_dominant_verdict_refuted_dominates_certified() -> None:
    """A confident REFUTED wins over a co-resident CERTIFIED (refute-first)."""
    assert (
        dominant_verdict([CriterionVerdict.CERTIFIED, CriterionVerdict.REFUTED])
        is CriterionVerdict.REFUTED
    )


def test_dominant_verdict_unresolved_dominates_certified() -> None:
    """An UNRESOLVED criterion is NOT certified on a co-resident pass."""
    assert (
        dominant_verdict([CriterionVerdict.CERTIFIED, CriterionVerdict.UNRESOLVED])
        is CriterionVerdict.UNRESOLVED
    )


def test_dominant_verdict_unresolved_dominates_attested() -> None:
    """UNRESOLVED dominates the lowest-assurance ATTESTED verdict."""
    assert (
        dominant_verdict([CriterionVerdict.ATTESTED, CriterionVerdict.UNRESOLVED])
        is CriterionVerdict.UNRESOLVED
    )


def test_dominant_verdict_refuted_beats_unresolved() -> None:
    """A confident contradiction outranks the merely-uncertain verdict."""
    assert (
        dominant_verdict([CriterionVerdict.UNRESOLVED, CriterionVerdict.REFUTED])
        is CriterionVerdict.REFUTED
    )


def test_dominant_verdict_certified_beats_supported_and_attested() -> None:
    """Among non-dominating verdicts the higher assurance one wins."""
    assert (
        dominant_verdict(
            [
                CriterionVerdict.ATTESTED,
                CriterionVerdict.SUPPORTED,
                CriterionVerdict.CERTIFIED,
            ]
        )
        is CriterionVerdict.CERTIFIED
    )


def test_dominant_verdict_supported_beats_attested() -> None:
    """SUPPORTED (entailment) outranks the attested floor."""
    assert (
        dominant_verdict([CriterionVerdict.ATTESTED, CriterionVerdict.SUPPORTED])
        is CriterionVerdict.SUPPORTED
    )


def test_dominant_verdict_is_order_independent() -> None:
    """The reduction does not depend on input ordering."""
    bag = [
        CriterionVerdict.ATTESTED,
        CriterionVerdict.CERTIFIED,
        CriterionVerdict.REFUTED,
        CriterionVerdict.SUPPORTED,
        CriterionVerdict.UNRESOLVED,
    ]
    assert dominant_verdict(bag) is CriterionVerdict.REFUTED
    assert dominant_verdict(list(reversed(bag))) is CriterionVerdict.REFUTED


def test_dominant_verdict_empty_raises() -> None:
    """Reducing an empty verdict list fails fast — no defined answer."""
    with pytest.raises(ValueError, match="empty verdict list"):
        dominant_verdict([])


# --------------------------------------------------------------------------- #
# render_attested_verdict — rung-4 attested render (render-only, lowest rung).
# --------------------------------------------------------------------------- #


def test_render_attested_verdict_shape() -> None:
    """The rendered row carries the attested kind, pass status, and criterion ref."""
    criterion = _attested_criterion("CR-7")
    record = render_attested_verdict(criterion, scope_id=_SCOPE)
    assert isinstance(record, EvidenceRecord)
    assert record.evidence_kind == "attested"
    assert record.status == "pass"
    assert record.scope_id == _SCOPE
    assert record.refs == ["CR-7"]
    assert record.id.startswith("EV-")


def test_render_attested_verdict_is_lowest_assurance() -> None:
    """The attested render reports the LOWEST-assurance ATTESTED verdict.

    The verdict the rung renders is ATTESTED, and its persisted status
    matches the verdict->status map for ATTESTED — i.e. the row's
    assurance lives in the verdict, not in a richer status.
    """
    record = render_attested_verdict(_attested_criterion(), scope_id=_SCOPE)
    assert record.status == verdict_to_status(CriterionVerdict.ATTESTED)
    assert CriterionVerdict.ATTESTED.value in record.summary


def test_render_attested_verdict_summary_marks_render_only() -> None:
    """The summary states no automated check ran (render-only contract)."""
    record = render_attested_verdict(_attested_criterion(), scope_id=_SCOPE)
    assert "render-only" in record.summary
    assert "no automated check" in record.summary


def test_render_attested_verdict_default_producer_is_human() -> None:
    """An operator attestation is the default producer."""
    record = render_attested_verdict(_attested_criterion(), scope_id=_SCOPE)
    assert record.produced_by == "human"
    assert "human" in record.summary


def test_render_attested_verdict_agent_producer() -> None:
    """A subagent may attest; the producer + summary reflect it."""
    record = render_attested_verdict(_attested_criterion(), scope_id=_SCOPE, attested_by="agent")
    assert record.produced_by == "agent"
    assert "agent" in record.summary


def test_render_attested_verdict_folds_note() -> None:
    """An operator note is folded into the row summary."""
    record = render_attested_verdict(
        _attested_criterion(), scope_id=_SCOPE, note="rollout looked healthy on the canary"
    )
    assert "rollout looked healthy on the canary" in record.summary


def test_render_attested_verdict_takes_no_runner() -> None:
    """The render is pure: it accepts only the criterion + attestation inputs.

    Rung-4 runs NO automated check, so unlike :func:`run_rung1_gate`
    (which takes a ``runner_cwd``) or :func:`run_rung2_gate` (which takes
    a ``scorer``), the attested render exposes no runner / scorer
    parameter. This pins the no-gate-execution contract at the signature
    level: a future refactor cannot quietly thread a subprocess runner
    through the attested floor.
    """
    import inspect

    params = set(inspect.signature(render_attested_verdict).parameters)
    assert params == {"criterion", "scope_id", "attested_by", "note"}
    assert "runner_cwd" not in params
    assert "scorer" not in params


def test_render_attested_verdict_rejects_non_attested_criterion() -> None:
    """A deterministic criterion cannot be attested — that masks a missing check."""
    with pytest.raises(ValueError, match="requires an attested criterion"):
        render_attested_verdict(_criterion("deterministic"), scope_id=_SCOPE)


def test_render_attested_verdict_rejects_jury_criterion() -> None:
    """A jury criterion cannot be short-circuited to an attestation."""
    with pytest.raises(ValueError, match="requires an attested criterion"):
        render_attested_verdict(_criterion("jury"), scope_id=_SCOPE)


def test_render_attested_verdict_rejects_tool_producer() -> None:
    """A deterministic checker cannot attest — an attestation is a judgement."""
    with pytest.raises(ValueError, match="must be 'human' or 'agent'"):
        render_attested_verdict(_attested_criterion(), scope_id=_SCOPE, attested_by="tool")


def test_render_attested_verdict_rejects_canary_producer() -> None:
    """A synthetic seed cannot attest."""
    with pytest.raises(ValueError, match="must be 'human' or 'agent'"):
        render_attested_verdict(_attested_criterion(), scope_id=_SCOPE, attested_by="canary")
