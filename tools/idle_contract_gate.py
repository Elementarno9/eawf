"""Deterministic idle-contract gate for the band-scoped spec-jury QC gate.

This repo has a history of building a verifier and then leaving it IDLE
forever -- a dead gate that never runs (tracked as the B091 idle-verifier
regression). The spec-jury close gate is the latest such verifier: W05/W06
wired the producer
(:func:`eawf.workflow.dispatch.spec_jury.produce_spec_jury_verdict`) and the
band-conditional resolver
(:func:`eawf.workflow.verify.readiness.resolve_wave_verify_block`), and the
shipped ``quality`` profile turns it on for a non-empty UI/UX band. This gate
makes "wired + band-scoped, not idle, not global" a CHECKED invariant rather
than a hope.

Two independent contracts are asserted, in precedence order:

- **not-idle** -- the producer is importable AND at least one shipped profile
  enables it via a non-empty :attr:`~eawf.platform.profiles.models.VerifyBlock.uiux_bands`
  with :attr:`~eawf.platform.profiles.models.VerifyBlock.enforce` true. A
  producer that no profile wires on is idle-forever; this contract fails on
  exactly that.
- **band-scoped (not global)** -- for that band-enabling profile,
  :func:`resolve_wave_verify_block` resolves to ``enforce=True`` for a UI-scope
  probe wave (``file_scopes`` under ``src/eawf/surfaces/tui/`` or
  ``.../render/``) AND to ``enforce=False`` for a non-UI probe wave (e.g.
  ``src/eawf/kernel/...``). A profile that flips enforcement on fleet-wide
  fails this contract because it would gate every wave, not just the band.

Both probe waves are pure in-process objects -- the gate never mutates state,
never writes a file, never runs a mutating ``eawf`` command.

The checks are injectable: :func:`check_idle_contract` takes the candidate
profile list and the resolver as parameters (defaulting to the shipped
profiles + the real resolver) so the failure modes are testable without
editing shipped profiles. :func:`check_idle_contract` returns a typed
:class:`GateResult` and the thin :func:`main` CLI maps it onto an exit code.

Invocation:

    python3 tools/idle_contract_gate.py

Exit codes:
- ``0`` -- the producer is importable, wired on for a non-empty band, and that
  band profile resolves band-scoped (not global).
- ``1`` -- a contract failed (the failure is named on stderr).
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import Wave
from eawf.platform.profiles.loader import list_profiles, load_profile
from eawf.platform.profiles.models import ProfileBody, VerifyBlock
from eawf.workflow.dispatch.spec_jury import produce_spec_jury_verdict  # noqa: F401
from eawf.workflow.verify.readiness import resolve_wave_verify_block

#: A resolver with the shape of
#: :func:`eawf.workflow.verify.readiness.resolve_wave_verify_block`. Injected so
#: the global-flip failure mode is testable with a stub resolver that returns
#: an always-enforcing block.
type ResolveFn = Callable[[VerifyBlock | None, Wave], VerifyBlock | None]

#: Probe file scope that IS UI surface (per
#: :func:`eawf.kernel.spec.heuristics.is_ui_scope`). A band profile MUST resolve
#: to ``enforce=True`` for a wave touching this scope.
_UI_SCOPE = "src/eawf/surfaces/tui/app.py"

#: Probe file scope that is NOT UI surface. A band-scoped (not global) profile
#: MUST resolve to ``enforce=False`` for a wave touching this scope.
_NON_UI_SCOPE = "src/eawf/kernel/state/models.py"

_PROBE_OPENED_AT = datetime(2026, 1, 1, tzinfo=UTC)


class GateFailure(StrEnum):
    """The mutually exclusive ways the idle-contract gate can fail.

    The order encodes precedence: an absent producer wiring (idle) is reported
    before a band/global resolution defect, so a single run names the more
    fundamental problem first.
    """

    PRODUCER_IDLE = "producer_idle"
    BAND_ENFORCES_GLOBALLY = "band_enforces_globally"


@dataclass(frozen=True, slots=True)
class GateResult:
    """Typed outcome of one idle-contract check.

    Attributes:
        passed: Whether both the not-idle and band-scoped contracts held.
        failure: The failure kind when ``passed`` is ``False``; ``None`` on a
            pass.
        message: A human-readable line; on failure it names the violated
            contract and the offending profile.
    """

    passed: bool
    failure: GateFailure | None
    message: str


def _make_probe_wave(*, scope: str) -> Wave:
    """Build a pure in-process probe :class:`Wave` whose only varying axis is *scope*.

    The wave is never persisted and never mutated; it exists only so
    :func:`resolve_wave_verify_block` can be asked how it bands a given file
    scope. The id / title are deliberately neutral (no ``uiux_bands`` token
    substring) so band membership is decided by the structural ``file_scopes``
    arm alone -- the gate can then assert the UI / non-UI split unambiguously.

    Args:
        scope: The single repo-relative file scope the probe wave declares.

    Returns:
        A validated :class:`Wave` with ``file_scopes=[scope]``.
    """
    return Wave(
        id="P00-I01-W01",
        iter_id="P00-I01",
        title="idle-contract probe wave",
        status=WaveStatus.PENDING,
        file_scopes=[scope],
        opened_at=_PROBE_OPENED_AT,
    )


def _band_enabling_profiles(profiles: Sequence[ProfileBody]) -> list[ProfileBody]:
    """Return the profiles that wire the spec-jury producer on for a real band.

    A profile wires the producer on when its ``verify`` block declares a
    non-empty :attr:`~eawf.platform.profiles.models.VerifyBlock.uiux_bands`
    AND :attr:`~eawf.platform.profiles.models.VerifyBlock.enforce` is true: the
    band list is the structural opt-in and ``enforce`` is the gating bit the
    resolver narrows per wave. A profile with an empty band list (or
    ``enforce=False``, or no verify block at all) leaves the producer idle.

    Args:
        profiles: The candidate profile bodies to scan.

    Returns:
        The subset of *profiles* whose verify block enables a non-empty band
        with enforcement on.
    """
    return [
        profile
        for profile in profiles
        if profile.verify is not None and profile.verify.enforce and profile.verify.uiux_bands
    ]


def _load_shipped_profiles() -> list[ProfileBody]:
    """Load every shipped (built-in) profile body.

    Returns:
        The validated :class:`ProfileBody` for each id from
        :func:`eawf.platform.profiles.loader.list_profiles`, in id order.
    """
    return [load_profile(profile_id) for profile_id in list_profiles()]


def check_idle_contract(
    *,
    profiles: Sequence[ProfileBody] | None = None,
    resolve_fn: ResolveFn = resolve_wave_verify_block,
) -> GateResult:
    """Assert the spec-jury producer is wired on for a band and resolves band-scoped.

    The two contracts are checked in precedence order:

    1. **not-idle** -- at least one of *profiles* enables the producer via a
       non-empty ``uiux_bands`` with ``enforce=True`` (see
       :func:`_band_enabling_profiles`). The producer importability is proven
       by this module importing
       :func:`eawf.workflow.dispatch.spec_jury.produce_spec_jury_verdict` at
       module load. When no profile wires it on, the producer is idle-forever
       and the gate fails :attr:`GateFailure.PRODUCER_IDLE`.
    2. **band-scoped** -- for a band-enabling profile, *resolve_fn* resolves to
       ``enforce=True`` for a UI-scope probe wave AND ``enforce=False`` for a
       non-UI probe wave. A profile that resolves to ``enforce=True`` for the
       non-UI probe enforces fleet-wide (it would gate every wave) and the gate
       fails :attr:`GateFailure.BAND_ENFORCES_GLOBALLY`.

    Args:
        profiles: Candidate profile bodies. ``None`` loads the shipped
            built-in profiles via :func:`_load_shipped_profiles`. Tests inject
            a synthetic list to exercise the idle / global failure modes
            without editing shipped profiles.
        resolve_fn: The band-conditional resolver under test. Defaults to
            :func:`eawf.workflow.verify.readiness.resolve_wave_verify_block`;
            tests inject a stub to force the global-flip path.

    Returns:
        A :class:`GateResult` whose ``passed`` is ``True`` only when the
        producer is wired on for a non-empty band AND that band profile
        resolves band-scoped (UI enforces, non-UI does not); otherwise
        ``failure`` names the first violated contract.
    """
    candidate = list(profiles) if profiles is not None else _load_shipped_profiles()

    band_profiles = _band_enabling_profiles(candidate)
    if not band_profiles:
        return GateResult(
            passed=False,
            failure=GateFailure.PRODUCER_IDLE,
            message=(
                "spec-jury producer is idle: no shipped profile enables a verify band "
                "(a non-empty 'uiux_bands' with 'enforce: true'); the producer "
                "'produce_spec_jury_verdict' is importable but never wired on"
            ),
        )

    ui_wave = _make_probe_wave(scope=_UI_SCOPE)
    non_ui_wave = _make_probe_wave(scope=_NON_UI_SCOPE)
    for profile in band_profiles:
        ui_resolved = resolve_fn(profile.verify, ui_wave)
        non_ui_resolved = resolve_fn(profile.verify, non_ui_wave)
        ui_enforces = ui_resolved is not None and ui_resolved.enforce
        non_ui_enforces = non_ui_resolved is not None and non_ui_resolved.enforce
        if not ui_enforces or non_ui_enforces:
            return GateResult(
                passed=False,
                failure=GateFailure.BAND_ENFORCES_GLOBALLY,
                message=(
                    f"band profile {profile.name!r} enforces globally, not band-scoped: "
                    f"ui_scope enforce={ui_enforces} (expected True), "
                    f"non_ui_scope enforce={non_ui_enforces} (expected False); "
                    "a band profile must gate only UI/UX waves, never the whole fleet"
                ),
            )

    band_names = ", ".join(sorted(profile.name for profile in band_profiles))
    return GateResult(
        passed=True,
        failure=None,
        message=(
            f"idle-contract gate: ok (spec-jury producer wired on by [{band_names}]; "
            "band resolves enforce=True for UI, enforce=False for non-UI)"
        ),
    )


def main(argv: list[str]) -> int:
    del argv  # the gate reads no arguments; the shipped tree is the input
    result = check_idle_contract()
    if result.passed:
        print(result.message)
        return 0
    print(result.message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
