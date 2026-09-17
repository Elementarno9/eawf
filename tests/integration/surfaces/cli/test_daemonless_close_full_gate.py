"""A daemonless close runs the full close gate under both profile shapes.

The daemon close gate branches on the merged profile's ``enforce`` flag: the
band resolver narrows a mechanical non-band wave to withdraw the jury and the
auditor, never the wave's own gates. Two daemonless doors still keyed on the
narrowed flag, so under a band-scoped profile (the shape of the ``quality``
profile this repository enables) a mechanical wave's failing gate ran in an
advisory lane and the close landed anyway:

* the CLI fallback close in :func:`eawf.surfaces.cli.commands.lifecycle_wave.wave_close_cmd`;
* the lock-free readiness pre-flight
  :func:`eawf.runtime.daemon.methods.state_close.compute_wave_close_readiness`.

A third door closed the failing gate under either shape: ``--no-runtime``
waived the pre-flight's gate failure as well as the missing runtime capture.

These tests drive the real ``eawf wave close`` under ``EAWF_DAEMONLESS=1`` and
the real readiness pre-flight against profile files written under
``tmp_path``. The ``pre_fix`` tests seed each old branch back in and show the
close then lands, so the refusal assertions are what the fix changed.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.spec.common import CriterionSpec, GateSpec, QualityDimension
from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus, StoreKind
from eawf.kernel.state.models import AgentSession, State
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.kernel.store.paths import store_path
from eawf.platform.profiles.models import VerifyBlock
from eawf.runtime.daemon.methods.state_close import compute_wave_close_readiness
from eawf.surfaces.cli._mutation import DAEMONLESS_WAIVER_EVENT_TYPE
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import lifecycle_wave
from eawf.workflow.lifecycle.transitions import LifecycleError
from eawf.workflow.verify import readiness as readiness_mod
from eawf.workflow.verify.models import CloseReadiness
from tests._session_helpers import seed_active_session_on_disk
from tests.conftest import make_claim_criterion

runner = CliRunner()

_WAVE_ID = "P01-I01-W01"
_FAILING_ARGV = ["git", "show", "no-such-ref-full-close-gate"]
_PASSING_ARGV = ["git", "rev-parse", "HEAD"]

#: The two enforcing profile shapes: one declaring no bands, and a band-scoped
#: one whose band the fixture wave misses, so the resolver narrows it.
PROFILE_SHAPES = pytest.mark.parametrize(
    "uiux_bands", [[], ["tui"]], ids=["whole-fleet", "band-scoped"]
)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Return a git-backed temp workspace whose closes run daemonless."""
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q", "-b", "main"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "test"], check=True)
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "seed"], check=True)
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    monkeypatch.setenv("EAWF_EVIDENCE_DIRECT_WRITE", "1")
    yield tmp_path


def _state_path(workspace: Path) -> Path:
    return workspace / ".ea" / "state.json"


def _load(workspace: Path) -> State:
    return State.model_validate(orjson.loads(_state_path(workspace).read_bytes()))


def _save(workspace: Path, state: State) -> None:
    _state_path(workspace).write_text(state.model_dump_json(), encoding="utf-8")


def _bootstrap(workspace: Path, *, argv: list[str], uiux_bands: list[str]) -> None:
    """Build one CLAIMED mechanical wave carrying one command gate, plus a profile.

    Args:
        workspace: The temp workspace root.
        argv: The ``command_exit_zero`` gate argv.
        uiux_bands: Band tokens for the enforcing profile; empty is whole-fleet.
    """
    for args in (
        ["project", "init", "QR", "--title", "Quant", "--domains", "quant"],
        ["phase", "open", "--auto", "--title", "P1"],
        ["iter", "open", "--phase", "P01", "--title", "I1"],
        [
            "wave",
            "plan",
            "P01-I01",
            "--id",
            _WAVE_ID,
            "--title",
            "close gate fixture",
            "--files",
            "src/",
            "--effort-bucket",
            "S",
        ],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.stdout
    state = _load(workspace)
    state.waves[_WAVE_ID].success_criteria = [make_claim_criterion()]
    _save(workspace, state)
    seed_active_session_on_disk(_state_path(workspace), session_id="S-1")
    result = runner.invoke(app, ["wave", "claim", _WAVE_ID, "--session", "S-1"])
    assert result.exit_code == 0, result.stdout

    state = _load(workspace)
    wave = state.waves[_WAVE_ID]
    wave.success_criteria = [
        CriterionSpec(
            id="CR-01",
            text="the wave's own command gate exits zero",
            kind="behavior",
            acceptance_style="binary",
            evidence_kind="deterministic",
            gate_ids=["GATE-01"],
            required=True,
            quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
            measurable_signal="command_exit_zero gate argv exits zero under the floor",
        )
    ]
    wave.gates = [
        GateSpec(
            id="GATE-01",
            criterion_id="CR-01",
            kind="command_exit_zero",
            args={"argv": argv},
            policy="block",
            cadence="every-wave",
        )
    ]
    operator = AgentSession(
        id="OP-1",
        role=AgentSessionRole.OPERATOR,
        runtime="cli",
        scope_id="QR",
        status=AgentSessionStatus.ACTIVE,
        started_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    state.agent_sessions[operator.id] = operator
    state.current.active_session_ids.insert(0, operator.id)
    _save(workspace, state)

    bands = "".join(f"    - {band}\n" for band in uiux_bands)
    band_block = f"  uiux_bands:\n{bands}" if uiux_bands else ""
    profile_dir = workspace / ".ea" / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (workspace / ".ea" / "config.yaml").write_text(
        "profiles:\n  enabled:\n    - enforcing\n", encoding="utf-8"
    )
    (profile_dir / "enforcing.yaml").write_text(
        f"name: enforcing\nverify:\n  enforce: true\n{band_block}  argv_allowlist:\n    - git\n",
        encoding="utf-8",
    )


def _assert_shape_narrows_as_declared(workspace: Path, *, uiux_bands: list[str]) -> None:
    """Guard: the band-scoped shape really narrows the fixture wave, the other does not."""
    state = _load(workspace)
    merged = readiness_mod.load_active_verify_block(
        _WAVE_ID, state, repo_root=workspace, config_root=workspace
    )
    assert merged is not None and merged.enforce
    narrowed = readiness_mod.resolve_wave_verify_block(merged, state.waves[_WAVE_ID])
    assert narrowed is not None
    assert narrowed.enforce is not bool(uiux_bands)


def _events(workspace: Path, event_type: str) -> list[dict[str, Any]]:
    path = store_path(_state_path(workspace), StoreKind.EVENT)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    rows = [orjson.loads(line) for line in lines if line.strip()]
    return [row for row in rows if row["payload"]["event_type"] == event_type]


def _seed_narrowed_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed the pre-fix branch: the close doors see the band-narrowed block."""
    real = readiness_mod.load_active_verify_block

    def _narrowed(scope_id: str, state: State, **kwargs: Any) -> VerifyBlock | None:
        return readiness_mod.resolve_wave_verify_block(
            real(scope_id, state, **kwargs), state.waves[scope_id]
        )

    monkeypatch.setattr(readiness_mod, "load_active_verify_block", _narrowed)


# --- the CLI fallback close ---------------------------------------------------


@PROFILE_SHAPES
@pytest.mark.parametrize("waiver", [[], ["--no-runtime"]], ids=["no-waiver", "no-runtime"])
def test_wave_close_cmd_refuses_failing_gate_under_both_profile_shapes(
    workspace: Path, uiux_bands: list[str], waiver: list[str]
) -> None:
    """A mechanical wave's failing gate refuses the daemonless close, waiver or not."""
    _bootstrap(workspace, argv=_FAILING_ARGV, uiux_bands=uiux_bands)
    _assert_shape_narrows_as_declared(workspace, uiux_bands=uiux_bands)

    result = runner.invoke(app, ["wave", "close", _WAVE_ID, "--outcome", "done", *waiver])

    assert result.exit_code != 0, result.stdout
    assert "readiness enforcement failed" in result.stdout
    assert "CR-01:fail" in result.stdout
    assert _load(workspace).waves[_WAVE_ID].status.value == "claimed"
    assert _events(workspace, "wave close") == []
    assert _events(workspace, DAEMONLESS_WAIVER_EVENT_TYPE) == []


@PROFILE_SHAPES
def test_wave_close_cmd_closes_passing_gate_under_both_profile_shapes(
    workspace: Path, uiux_bands: list[str]
) -> None:
    """Boundary: a passing gate closes, and ``--no-runtime`` still waives the runtime."""
    _bootstrap(workspace, argv=_PASSING_ARGV, uiux_bands=uiux_bands)

    result = runner.invoke(app, ["wave", "close", _WAVE_ID, "--outcome", "done", "--no-runtime"])

    assert result.exit_code == 0, result.stdout
    assert _load(workspace).waves[_WAVE_ID].status.value == "closed"
    closes = _events(workspace, "wave close")
    assert [row["payload"]["extras"]["close_mechanism"] for row in closes] == ["daemonless-waiver"]


@PROFILE_SHAPES
def test_wave_close_cmd_closes_failing_gate_under_explicit_gate_waiver(
    workspace: Path, uiux_bands: list[str]
) -> None:
    """The per-gate ``--waive`` is the override for a failing gate."""
    _bootstrap(workspace, argv=_FAILING_ARGV, uiux_bands=uiux_bands)
    # Waivers bind to the wave's commit; with none, every waiver reads as stale.
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            f"fix: land the wave\n\nEawf-Wave: {_WAVE_ID}",
        ],
        check=True,
    )

    result = runner.invoke(
        app,
        [
            "wave",
            "close",
            _WAVE_ID,
            "--outcome",
            "done",
            "--waive",
            "GATE-01",
            "--reason",
            "ref is pruned upstream; verified by hand",
            "--no-runtime",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert _load(workspace).waves[_WAVE_ID].status.value == "closed"


def test_wave_close_cmd_pre_fix_narrowed_branch_lets_failing_gate_close(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seeding the narrowed branch back in closes the band-scoped failing gate."""
    _bootstrap(workspace, argv=_FAILING_ARGV, uiux_bands=["tui"])
    _seed_narrowed_loader(monkeypatch)

    result = runner.invoke(app, ["wave", "close", _WAVE_ID, "--outcome", "done", "--no-runtime"])

    assert result.exit_code == 0, result.stdout
    assert _load(workspace).waves[_WAVE_ID].status.value == "closed"


@PROFILE_SHAPES
def test_wave_close_cmd_pre_fix_runtime_waiver_lets_failing_gate_close(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, uiux_bands: list[str]
) -> None:
    """Seeding the old ``--no-runtime`` swallow back in closes the failing gate."""
    _bootstrap(workspace, argv=_FAILING_ARGV, uiux_bands=uiux_bands)
    real: Callable[..., CloseReadiness | None] = lifecycle_wave._run_daemonless_close_preflight

    def _swallow_when_waived(state: State, *, waived: bool, **kwargs: Any) -> CloseReadiness | None:
        try:
            return real(state, waived=waived, **kwargs)
        except LifecycleError:
            if not waived:
                raise
            return None

    monkeypatch.setattr(lifecycle_wave, "_run_daemonless_close_preflight", _swallow_when_waived)

    result = runner.invoke(app, ["wave", "close", _WAVE_ID, "--outcome", "done", "--no-runtime"])

    assert result.exit_code == 0, result.stdout
    assert _load(workspace).waves[_WAVE_ID].status.value == "closed"


# --- the lock-free readiness pre-flight ----------------------------------------


def _close_mutation(wave_id: str = _WAVE_ID) -> Mutation:
    return Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id=wave_id,
        mutation_id="c" * 32,
        params={"wave_id": wave_id, "outcome": "done"},
    )


@PROFILE_SHAPES
def test_compute_wave_close_readiness_refuses_failing_gate_under_both_profile_shapes(
    workspace: Path, uiux_bands: list[str]
) -> None:
    """The daemon's readiness pre-flight enforces a mechanical wave's failing gate."""
    _bootstrap(workspace, argv=_FAILING_ARGV, uiux_bands=uiux_bands)

    with pytest.raises(LifecycleError, match=r"readiness enforcement failed.*CR-01:fail"):
        compute_wave_close_readiness(
            _load(workspace),
            _close_mutation(),
            state_path=_state_path(workspace),
            repo_root=workspace,
            defer_verdict_kinds=True,
        )


@PROFILE_SHAPES
def test_compute_wave_close_readiness_returns_view_for_passing_gate(
    workspace: Path, uiux_bands: list[str]
) -> None:
    """Boundary: a passing gate yields a ready enforcing view, not ``None``."""
    _bootstrap(workspace, argv=_PASSING_ARGV, uiux_bands=uiux_bands)

    readiness = compute_wave_close_readiness(
        _load(workspace),
        _close_mutation(),
        state_path=_state_path(workspace),
        repo_root=workspace,
    )

    assert readiness is not None
    assert readiness.ready
    assert [view.id for view in readiness.criteria] == ["CR-01"]


def test_compute_wave_close_readiness_pre_fix_narrowed_return_skips_failing_gate(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seeding the narrowed return back in makes the band-scoped pre-flight advisory."""
    _bootstrap(workspace, argv=_FAILING_ARGV, uiux_bands=["tui"])
    _seed_narrowed_loader(monkeypatch)

    readiness = compute_wave_close_readiness(
        _load(workspace),
        _close_mutation(),
        state_path=_state_path(workspace),
        repo_root=workspace,
    )

    assert readiness is None


def test_compute_wave_close_readiness_returns_none_without_enforcing_profile(
    workspace: Path,
) -> None:
    """Boundary: a profile that enforces nothing leaves the failing gate advisory."""
    _bootstrap(workspace, argv=_FAILING_ARGV, uiux_bands=[])
    (workspace / ".ea" / "profiles" / "enforcing.yaml").write_text(
        "name: enforcing\nverify:\n  enforce: false\n  argv_allowlist:\n    - git\n",
        encoding="utf-8",
    )

    readiness = compute_wave_close_readiness(
        _load(workspace),
        _close_mutation(),
        state_path=_state_path(workspace),
        repo_root=workspace,
    )

    assert readiness is None


def test_compute_wave_close_readiness_returns_none_for_unknown_wave(workspace: Path) -> None:
    """Error path: a mutation naming no known wave yields no view."""
    _bootstrap(workspace, argv=_FAILING_ARGV, uiux_bands=["tui"])

    readiness = compute_wave_close_readiness(
        _load(workspace),
        _close_mutation("P99-I99-W99"),
        state_path=_state_path(workspace),
        repo_root=workspace,
    )

    assert readiness is None
