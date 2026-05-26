"""Derived close-readiness view for v0.4 verify spine (W06).

The :func:`compute` function returns a :class:`CloseReadiness` projection
of three inputs:

* typed :class:`~eawf.kernel.spec.common.CriterionSpec` /
  :class:`~eawf.kernel.spec.common.GateSpec` definitions attached to
  the scope (none today; W03 landed the models, later waves migrate
  callers — :func:`_load_criterion_specs` /
  :func:`_load_gate_specs` give tests + future call sites a clean
  injection point);
* the legacy :attr:`~eawf.kernel.state.models.Wave.success_criteria`
  string list (still load-bearing in v0.4.0);
* :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord` rows in
  ``<store_dir>/evidence.jsonl`` whose ``scope_id`` matches the wave's
  derived SHA (see :func:`eawf.workflow.lifecycle.wave_sha.derive_wave_sha`).

``compute`` is **pure read-only**: it does not mutate *state*, and the
function is idempotent on its inputs (calling it twice on the same
tuple returns equal results). The three wave-close seams attach it as
an **advisory** call — warnings flow but no close path blocks. W19
(later wave) flips that behaviour behind ``profile.verify.enforce``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import orjson

from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State, Wave
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.workflow.lifecycle.wave_sha import derive_wave_sha
from eawf.workflow.verify.models import (
    CloseReadiness,
    CriterionView,
    GateResult,
)

logger = logging.getLogger(__name__)


def _load_criterion_specs(scope_id: str, state: State) -> list[CriterionSpec]:
    """Return typed CriterionSpec rows attached to *scope_id*.

    Today this returns ``[]`` for every scope because no v0.4.0 state
    field carries typed specs yet (W03 landed the model; W08 + W11
    introduce the storage + attachment). The helper exists so tests
    can monkeypatch it to feed synthetic specs into :func:`compute`
    and so the migration that wires real storage is one-import-site
    later.

    Args:
        scope_id: Wave / iter / phase URN. Reserved for the future
            multi-scope attachment story (today only waves carry
            criteria).
        state: Validated state model. Reserved likewise.

    Returns:
        Empty list in v0.4.0. Future waves attach real specs here.
    """
    del scope_id, state  # unused until W08+W11 migrate the storage
    return []


def _load_gate_specs(scope_id: str, state: State) -> list[GateSpec]:
    """Return typed GateSpec rows attached to *scope_id*.

    Symmetric to :func:`_load_criterion_specs` — returns ``[]`` today
    because no state field carries gates yet. Tests monkeypatch this
    helper to exercise the spec branch of :func:`compute`.

    Args:
        scope_id: Wave / iter / phase URN. Reserved.
        state: Validated state model. Reserved.

    Returns:
        Empty list in v0.4.0.
    """
    del scope_id, state  # unused until W08+W11 migrate the storage
    return []


def _read_evidence_rows(store_dir: Path) -> list[EvidenceRecord]:
    """Decode + validate every row in ``<store_dir>/evidence.jsonl``.

    Returns an empty list when the file does not exist (the v0.4
    evidence store is created lazily on first ``evidence.append``).
    Malformed rows are skipped with a debug log — a malformed
    evidence row is an advisory failure, not a close blocker (W06's
    whole point).

    Args:
        store_dir: ``<state_dir>/store/`` (resolved by callers via
            :func:`eawf.kernel.store.paths.store_dir`).

    Returns:
        List of validated :class:`EvidenceRecord` rows in file order.
    """
    evidence_path = store_dir / f"{StoreKind.EVIDENCE.value}.jsonl"
    if not evidence_path.exists():
        return []
    rows: list[EvidenceRecord] = []
    try:
        raw = evidence_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug(f"_read_evidence_rows status=os-error path={evidence_path!s} err={exc!s}")
        return []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            envelope = Envelope.model_validate_json(line)
            if envelope.kind != StoreKind.EVIDENCE:
                continue
            record = EvidenceRecord.model_validate(envelope.payload)
        except (orjson.JSONDecodeError, ValueError) as exc:
            logger.debug(f"_read_evidence_rows status=row-skip err={exc!s}")
            continue
        rows.append(record)
    return rows


def _filter_evidence_for_scope(
    rows: list[EvidenceRecord], *, scope_id: str
) -> list[EvidenceRecord]:
    """Return the evidence rows whose ``scope_id`` matches *scope_id*.

    SHA-bound freshness is the W08 contract (compile-gate); W06 keeps
    the filter scope-id-equal so the readiness view is deterministic
    on the rows currently visible.

    Args:
        rows: All evidence rows read off disk.
        scope_id: Wave id we are computing readiness for.

    Returns:
        Filtered rows in original order.
    """
    return [row for row in rows if row.scope_id == scope_id]


def _is_stale_waiver(row: EvidenceRecord, *, current_sha: str | None) -> bool:
    """Return ``True`` when *row* is a waiver against an outdated wave SHA.

    Waivers (W11) are SHA-bound: each waiver row carries
    ``metrics["wave_sha"]`` at attest-time. Once the wave SHA advances
    (e.g. an executor adds a new commit), prior waivers are stale and
    MUST NOT count toward the readiness rollup — the gate needs a
    fresh attestation against the post-advance code.

    The check fires only for ``status="waived"`` rows; non-waiver
    rows are scope-id-bound (W06 contract) and freshness is the W08
    compile-gate's responsibility.

    Args:
        row: One scope-filtered :class:`EvidenceRecord`.
        current_sha: Current wave SHA derived via
            :func:`eawf.workflow.lifecycle.wave_sha.derive_wave_sha`.
            ``None`` (git unavailable, no commit yet) is treated as
            "freshness check skipped" — the waiver is NOT considered
            stale when the SHA cannot be derived.

    Returns:
        ``True`` iff *row* is a waiver whose stamped ``wave_sha``
        disagrees with *current_sha*.
    """
    if row.status != "waived":
        return False
    if current_sha is None:
        return False
    if row.metrics is None:
        return False
    stamped = row.metrics.get("wave_sha")
    if stamped is None:
        return False
    return stamped != current_sha


def _gate_status_from_evidence(gate: GateSpec, evidence: list[EvidenceRecord]) -> tuple[str, bool]:
    """Return ``(status, was_waived)`` for *gate* given its evidence rows.

    Rolls up evidence per gate by looking at the most recent row whose
    ``refs`` list mentions the gate id. The most recent row wins so
    repeat runs converge. Returns ``("blocked", False)`` when no
    evidence row references the gate.

    The caller is responsible for filtering out SHA-stale waiver rows
    via :func:`_is_stale_waiver` BEFORE calling this helper; that
    keeps the per-gate roll-up free of branching on the freshness
    contract.

    Args:
        gate: Gate spec to score.
        evidence: Scope-filtered evidence rows with stale waivers
            already removed.

    Returns:
        Tuple of (rolled-up gate status, whether the gate was waived).
    """
    relevant = [row for row in evidence if gate.id in row.refs]
    if not relevant:
        return ("blocked", False)
    latest = relevant[-1]
    if latest.status == "waived":
        return ("pass", True)
    if latest.status == "pass":
        return ("pass", False)
    if latest.status == "fail":
        return ("fail", False)
    # "blocked" — pass-through.
    return ("blocked", False)


def _criterion_status_from_gates(
    criterion: CriterionSpec,
    gate_results: list[GateResult],
    *,
    waived_gate_ids: list[str] | None = None,
) -> str:
    """Roll up *criterion* status from its per-gate :class:`GateResult` list.

    Algorithm:

    * If the criterion has an explicit ``waiver_reason``, the rolled-up
      status is ``waived`` regardless of gates.
    * If *waived_gate_ids* covers every gate on *criterion*, the rolled-
      up status is ``waived`` (W11: a fully-waived criterion surfaces
      its waiver explicitly so renderers can flag operator overrides).
    * Any required ``fail`` => ``fail``.
    * Any required ``blocked`` (and no fails) => ``blocked``.
    * Empty required-gate set with ``required=True`` criterion =>
      ``pending`` (no evidence to score yet).
    * Otherwise => ``pass``.

    Args:
        criterion: The criterion under evaluation.
        gate_results: Per-gate results scored against the criterion.
        waived_gate_ids: Gate ids the W11 waiver path cleared (i.e.
            rows whose status was ``waived`` rather than ``pass``).
            ``None`` is treated as an empty list.

    Returns:
        One of ``pass`` / ``fail`` / ``blocked`` / ``pending`` /
        ``waived`` — see :data:`~eawf.workflow.verify.models.CriterionStatus`.
    """
    if criterion.waiver_reason:
        return "waived"
    if not gate_results:
        return "pending" if criterion.required else "pass"
    waived_set = set(waived_gate_ids or [])
    if waived_set and all(r.gate_id in waived_set for r in gate_results):
        return "waived"
    required_results = [r for r in gate_results if r.status != "pass"]
    if any(r.status == "fail" for r in required_results):
        return "fail"
    if any(r.status == "blocked" for r in required_results):
        return "blocked"
    return "pass"


def _build_spec_views(
    criterion_specs: list[CriterionSpec],
    gate_specs: list[GateSpec],
    evidence: list[EvidenceRecord],
) -> tuple[list[CriterionView], list[str]]:
    """Convert typed CriterionSpec / GateSpec into :class:`CriterionView` rows.

    Returns the per-criterion views plus the list of waived gate ids
    (collected across all criteria so :class:`CloseReadiness` can
    surface them at the top level).

    Args:
        criterion_specs: Typed criterion rows attached to the scope.
        gate_specs: Typed gate rows attached to the scope.
        evidence: Already scope-filtered evidence rows. The caller is
            responsible for filtering out SHA-stale waiver rows BEFORE
            invoking this helper (see :func:`_is_stale_waiver`).

    Returns:
        ``(views, waived_gate_ids)``.
    """
    gates_by_criterion: dict[str, list[GateSpec]] = {}
    for gate in gate_specs:
        gates_by_criterion.setdefault(gate.criterion_id, []).append(gate)

    views: list[CriterionView] = []
    waived: list[str] = []
    for criterion in criterion_specs:
        gates = gates_by_criterion.get(criterion.id, [])
        gate_results: list[GateResult] = []
        per_criterion_waived: list[str] = []
        for gate in gates:
            status, was_waived = _gate_status_from_evidence(gate, evidence)
            gate_results.append(
                GateResult(
                    gate_id=gate.id,
                    # status literal is closed; cast not needed because
                    # _gate_status_from_evidence returns one of the four
                    # GateStatus values by construction.
                    status=status,  # type: ignore[arg-type]
                )
            )
            if was_waived:
                waived.append(gate.id)
                per_criterion_waived.append(gate.id)
        criterion_status = _criterion_status_from_gates(
            criterion,
            gate_results,
            waived_gate_ids=per_criterion_waived,
        )
        views.append(
            CriterionView(
                id=criterion.id,
                source="spec",
                # Same rationale as the gate status cast above.
                status=criterion_status,  # type: ignore[arg-type]
                gate_results=gate_results,
            )
        )
    return views, waived


def _build_legacy_views(wave: Wave) -> tuple[list[CriterionView], list[str]]:
    """Convert ``Wave.success_criteria`` strings into legacy :class:`CriterionView`.

    Each string becomes one view with ``source="legacy"``, ``status="pass"``,
    and ``gate_results=None``. Returns the per-criterion views plus a
    list of advisory warning strings — one per criterion so the
    readiness rollup can tally the legacy-not-gated count without
    re-walking the views.

    Args:
        wave: The closing wave whose legacy strings we project.

    Returns:
        ``(views, warnings)``.
    """
    views: list[CriterionView] = []
    warnings: list[str] = []
    for index, _text in enumerate(wave.success_criteria, start=1):
        criterion_id = f"CR-{index:02d}"
        views.append(
            CriterionView(
                id=criterion_id,
                source="legacy",
                status="pass",
                gate_results=None,
            )
        )
        warnings.append(f"legacy criterion {criterion_id!r} not gated")
    return views, warnings


def compute(
    scope_id: str,
    *,
    state: State,
    store_dir: Path,
    repo_root: Path,
) -> CloseReadiness:
    """Return the close-readiness projection for *scope_id*.

    Pure read-only — does not mutate *state* nor write any file. Reads
    the typed CriterionSpec / GateSpec attachments (via
    :func:`_load_criterion_specs` / :func:`_load_gate_specs`), the
    legacy :attr:`~eawf.kernel.state.models.Wave.success_criteria`
    list, and the SHA-bound EvidenceRecord rows under *store_dir*.

    Args:
        scope_id: Wave id (today; iter / phase scopes land later).
        state: Validated state model. Read-only.
        store_dir: ``<state_dir>/store/`` — the JSONL store root the
            EvidenceRecord rows live under.
        repo_root: Repository root, forwarded to
            :func:`eawf.workflow.lifecycle.wave_sha.derive_wave_sha` so
            the SHA-bound freshness lookup runs in the right git tree.

    Returns:
        A :class:`CloseReadiness` view. Empty waves (no typed specs +
        no legacy criteria) return ``ready=True`` with empty
        ``criteria``; legacy-only waves return ``ready=True`` plus one
        warning per legacy criterion.

    Raises:
        KeyError: When *scope_id* is not a known wave id in *state*.
            (Iter / phase scopes will resolve to their own loader once
            attached in a later wave; today only waves are scored.)
    """
    wave = state.waves.get(scope_id)
    if wave is None:
        raise KeyError(f"unknown wave: {scope_id!r}")

    # SHA derive feeds both the W06 advisory log + the W11 SHA-bound
    # waiver freshness filter: a waiver row whose stamped
    # ``metrics["wave_sha"]`` no longer matches the current wave SHA
    # is considered stale and dropped before per-gate roll-up so the
    # readiness view reflects only fresh operator attestations. The
    # compile-gate (W08) flips the same SHA into a full hard filter
    # for non-waiver evidence.
    sha = derive_wave_sha(scope_id, repo_root=repo_root)
    logger.debug(f"compute wave={scope_id!r} sha={sha!r}")

    criterion_specs = _load_criterion_specs(scope_id, state)
    gate_specs = _load_gate_specs(scope_id, state)

    evidence_rows = _read_evidence_rows(store_dir)
    scope_evidence = _filter_evidence_for_scope(evidence_rows, scope_id=scope_id)
    fresh_evidence = [row for row in scope_evidence if not _is_stale_waiver(row, current_sha=sha)]

    spec_views, waived_gate_ids = _build_spec_views(criterion_specs, gate_specs, fresh_evidence)
    legacy_views, legacy_warnings = _build_legacy_views(wave)

    criteria: list[CriterionView] = [*spec_views, *legacy_views]
    warnings = list(legacy_warnings)
    if not spec_views and not legacy_views:
        # Empty waves cannot be meaningfully blocking — flag the
        # advisory so operators notice the gap without raising.
        warnings.append("no criteria attached to wave")

    ready = _is_ready(criteria)
    return CloseReadiness(
        ready=ready,
        criteria=criteria,
        warnings=warnings,
        waived_gate_ids=waived_gate_ids,
    )


def _is_ready(criteria: list[CriterionView]) -> bool:
    """Return ``True`` iff every criterion has status in ``{pass, waived}``.

    Pure helper so :func:`compute` reads top-down. A criterion with
    ``status="pending"`` or ``"blocked"`` or ``"fail"`` flips the
    aggregate to ``ready=False``.

    Args:
        criteria: All :class:`CriterionView` rows for the scope.

    Returns:
        ``True`` when every criterion is pass/waived (or the list is
        empty — an empty scope is trivially ready by definition).
    """
    return all(view.status in ("pass", "waived") for view in criteria)


__all__ = [
    "compute",
]
