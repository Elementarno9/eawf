"""Why five closes ran no gate at all, and the pin that keeps them running.

Five waves closed in one iteration with ``status=closed``, a non-empty
``required_gate_ids``, and zero receipts. The cause is named here so it cannot
recur unnoticed: the enforcing close gate took a **risk-weighted early return**
before it scored anything. A whole-fleet ``verify.enforce`` profile (one
declaring no ``uiux_bands``) returned an empty evidence list for every wave
whose :func:`~eawf.workflow.dispatch.verdict.verdict_requirement` was not
``"always"`` -- that is, every wave below the ``L`` effort bucket that is not
judgment-roled and not security-scoped. The return carried a ``logger.debug``
line, and the daemon logs at INFO, so the skip left no trace at all.

The discriminator is exact on the observed population: the three waves that
recorded full receipts were ``L``-bucket (``always``), and the five that
recorded none were ``M`` and ``XS`` (``sampled`` / ``skip``). A daemon restart
was observed in the same window and is NOT the cause -- it does not partition
the population, and the tests below red on the risk band with no process death
anywhere in them.

What the band is legitimately for is the expensive tier: a mechanical wave
earns no fresh auditor and no cross-vendor jury. It was never meant to excuse
the wave's own deterministic gates, which are the cheap rung of the same ladder
and the falsifiers the wave itself declared. So the repair narrows the tier
rather than skipping the pass, and the tests here pin both halves: a mechanical
wave's gates run and receipt, and its jury stays unreached.

Every fixture tree is built under ``tmp_path``; nothing here reads or writes the
repository's own ``.ea/``.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import Wave
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.methods.state_close import (
    CLOSE_GATE_TIER_SKIP,
    resolve_close_gate_tier,
)
from eawf.workflow.dispatch.verdict import verdict_requirement
from eawf.workflow.verify.gate_receipts import RECEIPT_MARKER
from tests.integration.workflow.verify._close_gate_helpers import (
    criterion,
    enforce_verify_block,
    file_exists_gate,
    live_tree,
    score_close,
    wave,
)

pytestmark = pytest.mark.integration

#: The effort buckets of the five waves that closed with zero receipts, and of
#: the three that closed with full ones. The split is the whole finding.
RECEIPTLESS_BUCKETS = ("M", "XS")
RECEIPTED_BUCKETS = ("L",)


def _two_gate_wave(effort_bucket: str) -> Wave:
    """Return a wave with two required blocking deterministic gates."""
    return wave(
        criteria=[
            criterion("CR-01", gate_ids=["G-01"]),
            criterion("CR-02", gate_ids=["G-02"]),
        ],
        gates=[
            file_exists_gate("G-01", criterion_id="CR-01"),
            file_exists_gate("G-02", criterion_id="CR-02"),
        ],
        effort_bucket=effort_bucket,
    )


def _receipted_gate_ids(state_path: Path) -> set[str]:
    """Return the gate ids the evidence store says this close executed.

    Reads the raw JSONL rather than a library helper: the property under test is
    that the persisted rows themselves answer "which gates ran".

    Args:
        state_path: Path to the fixture's ``state.json``.

    Returns:
        The gate ids carried on gate-execution receipt rows.
    """
    path = store_path(state_path, StoreKind.EVIDENCE)
    if not path.is_file():
        return set()
    found: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        envelope = Envelope.model_validate_json(line)
        if envelope.kind is not StoreKind.EVIDENCE:
            continue
        record = EvidenceRecord.model_validate(envelope.payload)
        metrics = record.metrics or {}
        if metrics.get("receipt") == RECEIPT_MARKER:
            found.add(str(metrics["gate_id"]))
    return found


# --- the named cause -------------------------------------------------------


@pytest.mark.parametrize("effort_bucket", RECEIPTLESS_BUCKETS)
def test_the_receiptless_buckets_are_exactly_the_mechanical_ones(effort_bucket: str) -> None:
    """Each bucket that recorded no receipt classifies below ``always``."""
    assert verdict_requirement(_two_gate_wave(effort_bucket)) != "always"


@pytest.mark.parametrize("effort_bucket", RECEIPTED_BUCKETS)
def test_the_receipted_buckets_are_exactly_the_always_ones(effort_bucket: str) -> None:
    """Each bucket that recorded full receipts classifies as ``always``."""
    assert verdict_requirement(_two_gate_wave(effort_bucket)) == "always"


@pytest.mark.parametrize("effort_bucket", RECEIPTLESS_BUCKETS)
def test_a_mechanical_wave_resolves_to_the_deterministic_tier_not_a_skip(
    effort_bucket: str,
) -> None:
    """The named cause, stated as the resolver contract.

    The early return this replaces answered "mechanical" with "score nothing".
    The resolver must answer it with "score the deterministic tier", because
    that is the tier holding the wave's own declared falsifiers.
    """
    resolved = resolve_close_gate_tier(
        "all",
        wave=_two_gate_wave(effort_bucket),
        uiux_bands=[],
    )

    assert resolved == "deterministic"
    assert resolved != CLOSE_GATE_TIER_SKIP


@pytest.mark.parametrize("effort_bucket", RECEIPTLESS_BUCKETS)
def test_a_mechanical_wave_still_earns_no_verdict_tier_pass(effort_bucket: str) -> None:
    """The band keeps its real job: no auditor, no jury, for a mechanical wave."""
    assert (
        resolve_close_gate_tier(
            "verdict",
            wave=_two_gate_wave(effort_bucket),
            uiux_bands=[],
        )
        == CLOSE_GATE_TIER_SKIP
    )


@pytest.mark.parametrize("effort_bucket", RECEIPTED_BUCKETS)
def test_an_always_wave_keeps_every_requested_tier(effort_bucket: str) -> None:
    """A high-risk wave's tier is never narrowed; it earns the whole ladder."""
    high_risk = _two_gate_wave(effort_bucket)
    for requested in ("all", "deterministic", "verdict"):
        assert resolve_close_gate_tier(requested, wave=high_risk, uiux_bands=[]) == requested


def test_a_band_scoped_profile_keeps_every_requested_tier() -> None:
    """A non-empty band list is resolved upstream, so the tier is untouched."""
    mechanical = _two_gate_wave("XS")
    for requested in ("all", "deterministic", "verdict"):
        assert resolve_close_gate_tier(requested, wave=mechanical, uiux_bands=["tui"]) == requested


# --- the regression that reds on the named cause ---------------------------


@pytest.mark.parametrize("effort_bucket", RECEIPTLESS_BUCKETS)
def test_a_mechanical_close_executes_and_receipts_every_required_gate(
    effort_bucket: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mechanical wave's gates run, against the real production close gate.

    This is the test that reds on the named cause: restoring the risk-weighted
    early return makes the close gate return before the scoring pass, so no
    receipt is written and the assertion on the persisted set fails.
    """
    closing = _two_gate_wave(effort_bucket)
    state, state_path = live_tree(tmp_path, monkeypatch, closing=closing)
    enforce_verify_block(monkeypatch, uiux_bands=[])

    evidence = score_close(state, state_path=state_path, repo_root=tmp_path)

    assert _receipted_gate_ids(state_path) == {"G-01", "G-02"}
    assert {record.refs[0] for record in evidence} == {"G-01", "G-02"}


def test_a_full_close_records_a_receipt_for_every_required_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every required gate of a closing wave leaves a receipt naming its run.

    The receipt is the only durable answer to "which gate ran, and what did it
    exit", so a close that scores a pass without one has proved nothing a later
    reader can check.
    """
    closing = _two_gate_wave("M")
    state, state_path = live_tree(tmp_path, monkeypatch, closing=closing)
    enforce_verify_block(monkeypatch, uiux_bands=[])

    score_close(state, state_path=state_path, repo_root=tmp_path)

    path = store_path(state_path, StoreKind.EVIDENCE)
    receipts = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        record = EvidenceRecord.model_validate(Envelope.model_validate_json(line).payload)
        metrics = record.metrics or {}
        if metrics.get("receipt") == RECEIPT_MARKER:
            receipts[str(metrics["gate_id"])] = record

    assert set(receipts) == {gate.id for gate in closing.gates}
    for gate in closing.gates:
        metrics = receipts[gate.id].metrics or {}
        assert metrics["criterion_id"] == gate.criterion_id
        assert str(metrics["executed_at"]).startswith("20")
        assert receipts[gate.id].status == "pass"
        assert shlex.split(str(metrics["argv"])) == []


def test_a_mechanical_close_never_reaches_the_jury_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An un-gated criterion on a mechanical wave is dropped, not escalated.

    The tier narrowing is only safe if it cannot turn the band's saved spawn
    into a spawn after all. The wave below carries one gated criterion and one
    un-gated one; the un-gated one would reach the jury under ``tier="all"``,
    and the spawn factory is replaced by one that fails the test the moment a
    juror runtime is requested from it.
    """
    closing = wave(
        criteria=[
            criterion("CR-01", gate_ids=["G-01"]),
            criterion("CR-02", gate_ids=[], evidence_kind="jury"),
        ],
        gates=[file_exists_gate("G-01", criterion_id="CR-01")],
        effort_bucket="M",
    )
    state, state_path = live_tree(tmp_path, monkeypatch, closing=closing)
    enforce_verify_block(monkeypatch, uiux_bands=[])

    def _refusing_factory(*_args: object, **_kwargs: object) -> object:
        return lambda _runtime: pytest.fail("a mechanical wave must not convene a jury")

    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.state._jury_spawn_factory",
        _refusing_factory,
    )

    evidence = score_close(state, state_path=state_path, repo_root=tmp_path)

    assert [record.refs[0] for record in evidence] == ["G-01"]
    assert _receipted_gate_ids(state_path) == {"G-01"}


# --- boundary and error paths ----------------------------------------------


def test_resolve_close_gate_tier_on_an_unknown_tier_word_still_narrows() -> None:
    """An unrecognised tier is narrowed, never passed through unscored.

    Failing open here would hand a future caller's typo the exact behaviour
    this wave exists to remove.
    """
    assert resolve_close_gate_tier("", wave=_two_gate_wave("XS"), uiux_bands=[]) == "deterministic"


def test_resolve_close_gate_tier_rejects_a_non_wave() -> None:
    """A caller that passes something other than a wave fails fast."""
    with pytest.raises(AttributeError):
        resolve_close_gate_tier("all", wave=object(), uiux_bands=[])  # type: ignore[arg-type]


def test_a_close_with_no_gates_at_all_receipts_nothing_and_refuses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty boundary: a wave owing no deterministic proof closes clean."""
    closing = wave(
        criteria=[criterion("CR-01", gate_ids=[], evidence_kind="attested")],
        gates=[],
        effort_bucket="XS",
    )
    state, state_path = live_tree(tmp_path, monkeypatch, closing=closing)
    enforce_verify_block(monkeypatch, uiux_bands=[])

    assert score_close(state, state_path=state_path, repo_root=tmp_path) == []
    assert _receipted_gate_ids(state_path) == set()


def test_a_single_gate_close_receipts_exactly_that_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The single boundary: one gate in, one receipt out."""
    closing = wave(
        criteria=[criterion("CR-01", gate_ids=["G-01"])],
        gates=[file_exists_gate("G-01", criterion_id="CR-01")],
        effort_bucket="S",
    )
    state, state_path = live_tree(tmp_path, monkeypatch, closing=closing)
    enforce_verify_block(monkeypatch, uiux_bands=[])

    score_close(state, state_path=state_path, repo_root=tmp_path)

    assert _receipted_gate_ids(state_path) == {"G-01"}
