"""Tests: the arm form transmits every field into fleet.drive (P30-I17-W02).

The FA1 arm modal derived the EU / USD / waves caps, the hard-halt toggle, and
the ``kclean`` K threshold, then DROPPED them before the ``fleet.drive`` RPC --
so a budget-tier run was always uncapped and the scope option was cosmetic.
These assertions confirm the W02 fix: every arm-form field now reaches the RPC
params, and the scope option narrows the claimed frontier.

- C1: :func:`build_arm_spec` derives the DL-4 EU / USD / waves spend caps from
  the budget tier (``unbounded`` leaves them ``None``) + the ``kclean_k``.
- C2: :func:`issue_drive` folds EVERY field into the ``fleet.drive`` params --
  the spend caps, hard_halt, kclean_k, convergence, concurrency, and the
  scope-filtered frontier.
- C3: :func:`scope_frontier` narrows the frontier by the scope option (this
  iter / this phase / cross-repo), anchored on the frontier head.
"""

from __future__ import annotations

from typing import ClassVar

from eawf.surfaces.tui.screens.overlays.arm import (
    ArmSpec,
    build_arm_spec,
    issue_drive,
    scope_frontier,
)


def _spec(**overrides: object) -> ArmSpec:
    base = build_arm_spec(
        scope="cross-repo",
        budget="standard",
        concurrency_option="2 lanes",
        risk_policy="auto-close, fork on fail",
        convergence_option="K-clean rounds",
    )
    return base.model_copy(update=overrides)


class _RecordingClient:
    """Records the (method, params) of each ``fleet.drive`` call."""

    calls: ClassVar[list[tuple[str, dict[str, object]]]] = []

    def __init__(self, *_a: object, **_k: object) -> None:
        return None

    def __enter__(self) -> _RecordingClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def call(self, method: str, params: dict[str, object]) -> dict[str, object]:
        type(self).calls.append((method, dict(params)))
        return {"handle_id": "fleet-run-x", "run_state": "draining", "backgrounded": True}


# ---- C1: build_arm_spec derives the budget caps + kclean_k -------------------


def test_build_arm_spec_unbounded_leaves_caps_none() -> None:
    """C1: the ``unbounded`` budget tier leaves every spend cap ``None``."""
    spec = build_arm_spec(
        scope="this iter",
        budget="unbounded",
        concurrency_option="1 lane",
        risk_policy="auto-close, fork on fail",
        convergence_option="drain to empty",
    )
    assert spec.eu_cap is None
    assert spec.usd_cap is None
    assert spec.waves_cap is None
    assert spec.kclean_k == 2


def test_build_arm_spec_strict_tier_derives_tight_caps() -> None:
    """C1: the ``strict`` tier derives the tightest armed EU / USD / waves caps."""
    spec = build_arm_spec(
        scope="this iter",
        budget="strict",
        concurrency_option="1 lane",
        risk_policy="auto-close, fork on fail",
        convergence_option="drain to empty",
    )
    assert spec.eu_cap == 4.0
    assert spec.usd_cap == 8.0
    assert spec.waves_cap == 4


def test_build_arm_spec_tiers_tighten_monotonically() -> None:
    """C1: lenient -> standard -> strict tighten every armed cap axis."""
    tiers = [
        build_arm_spec(
            scope="this iter",
            budget=b,
            concurrency_option="1 lane",
            risk_policy="auto-close, fork on fail",
            convergence_option="drain to empty",
        )
        for b in ("lenient", "standard", "strict")
    ]
    eu = [t.eu_cap for t in tiers]
    usd = [t.usd_cap for t in tiers]
    waves = [t.waves_cap for t in tiers]
    assert eu == sorted(eu, reverse=True) and all(v is not None for v in eu)
    assert usd == sorted(usd, reverse=True) and all(v is not None for v in usd)
    assert waves == sorted(waves, reverse=True) and all(v is not None for v in waves)


# ---- C2: issue_drive folds every field into the RPC params -------------------


def test_issue_drive_transmits_every_field(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """C2: issue_drive sends the caps + hard_halt + kclean_k + convergence."""
    _RecordingClient.calls = []
    from eawf.surfaces.cli import _daemon_client as dc

    monkeypatch.setattr(dc, "DaemonClient", _RecordingClient)
    spec = build_arm_spec(
        scope="cross-repo",
        budget="standard",
        concurrency_option="4 lanes",
        risk_policy="auto-close, hard-halt on fail",
        convergence_option="K-clean rounds",
    )
    line = issue_drive(spec, ["P30-I17-W01", "P30-I17-W02"], daemon_available=True)
    assert "draining" in line
    assert len(_RecordingClient.calls) == 1
    method, params = _RecordingClient.calls[0]
    assert method == "fleet.drive"
    # Every derived field reached the RPC -- none was dropped (the W02 fix).
    assert params["concurrency"] == 4
    assert params["convergence"] == "kclean"
    assert params["kclean_k"] == 2
    assert params["hard_halt"] is True
    assert params["eu_cap"] == 16.0
    assert params["usd_cap"] == 32.0
    assert params["waves_cap"] == 12
    assert params["frontier"] == ["P30-I17-W01", "P30-I17-W02"]


def test_issue_drive_unbounded_omits_cap_keys(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """C2: an unbounded tier omits the spend-cap keys (strict DriveParams)."""
    _RecordingClient.calls = []
    from eawf.surfaces.cli import _daemon_client as dc

    monkeypatch.setattr(dc, "DaemonClient", _RecordingClient)
    spec = build_arm_spec(
        scope="cross-repo",
        budget="unbounded",
        concurrency_option="1 lane",
        risk_policy="fork all, hard-halt on fail",
        convergence_option="drain to empty",
    )
    issue_drive(spec, ["P30-I17-W01"], daemon_available=True)
    _method, params = _RecordingClient.calls[0]
    assert "eu_cap" not in params
    assert "usd_cap" not in params
    assert "waves_cap" not in params
    assert params["hard_halt"] is True
    assert params["convergence"] == "drain"


def test_issue_drive_validates_against_drive_params(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """C2: the params issue_drive sends validate against the real DriveParams.

    The arm form and the daemon must agree on the wire shape: the params dict
    issue_drive builds parses cleanly through the strict
    :class:`~eawf.runtime.daemon.methods.fleet.DriveParams` (extra=forbid), so
    a budget-capped run reaches the DL-4 teeth instead of being dropped.
    """
    _RecordingClient.calls = []
    from eawf.runtime.daemon.methods.fleet import DriveParams
    from eawf.surfaces.cli import _daemon_client as dc

    monkeypatch.setattr(dc, "DaemonClient", _RecordingClient)
    spec = build_arm_spec(
        scope="cross-repo",
        budget="strict",
        concurrency_option="2 lanes",
        risk_policy="auto-close, hard-halt on fail",
        convergence_option="K-clean rounds",
    )
    issue_drive(spec, ["P30-I17-W01", "P30-I17-W02"], daemon_available=True)
    _method, params = _RecordingClient.calls[0]
    parsed = DriveParams.model_validate(params)
    assert parsed.eu_cap == 4.0
    assert parsed.usd_cap == 8.0
    assert parsed.waves_cap == 4
    assert parsed.hard_halt is True
    assert parsed.kclean_k == 2
    assert parsed.convergence == "kclean"


# ---- C3: scope_frontier narrows the claimed frontier -------------------------


def test_scope_frontier_this_iter_keeps_only_head_iter() -> None:
    """C3: ``this iter`` keeps only the waves sharing the head wave's iter."""
    frontier = ["P30-I17-W01", "P30-I17-W02", "P30-I18-W01", "P29-I04-W03"]
    kept = scope_frontier("this iter", frontier)
    assert kept == ["P30-I17-W01", "P30-I17-W02"]


def test_scope_frontier_this_phase_keeps_only_head_phase() -> None:
    """C3: ``this phase`` keeps the waves sharing the head wave's phase."""
    frontier = ["P30-I17-W01", "P30-I18-W01", "P29-I04-W03"]
    kept = scope_frontier("this phase", frontier)
    assert kept == ["P30-I17-W01", "P30-I18-W01"]


def test_scope_frontier_cross_repo_keeps_whole_frontier() -> None:
    """C3: ``cross-repo`` keeps the whole frontier unchanged."""
    frontier = ["P30-I17-W01", "P30-I18-W01", "P29-I04-W03"]
    assert scope_frontier("cross-repo", frontier) == frontier


def test_scope_frontier_empty_is_empty() -> None:
    """C3: an empty frontier scopes to empty regardless of the option."""
    assert scope_frontier("this iter", []) == []


def test_issue_drive_scope_narrows_claimed_frontier(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """C3: the scope option changes the claimed frontier the RPC receives.

    A ``this iter`` arm over a mixed-iter frontier sends only the head iter's
    waves -- the scope option is load-bearing, not cosmetic.
    """
    _RecordingClient.calls = []
    from eawf.surfaces.cli import _daemon_client as dc

    monkeypatch.setattr(dc, "DaemonClient", _RecordingClient)
    spec = build_arm_spec(
        scope="this iter",
        budget="unbounded",
        concurrency_option="1 lane",
        risk_policy="auto-close, fork on fail",
        convergence_option="drain to empty",
    )
    issue_drive(
        spec,
        ["P30-I17-W01", "P30-I17-W02", "P30-I18-W01"],
        daemon_available=True,
    )
    _method, params = _RecordingClient.calls[0]
    assert params["frontier"] == ["P30-I17-W01", "P30-I17-W02"]
