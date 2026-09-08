"""Tests: the in-process iter-close fallback arms the same ODR gate the daemon does.

``eawf iter close`` routes through the daemon when it is reachable and through
the in-process portalocker fallback otherwise (CI, one-shot, recovery shell).
The daemon path threads the resolved verify block's ``odr_floor`` /
``odr_blocking`` into
:func:`~eawf.workflow.lifecycle.transitions.close_iter`; these tests pin that
the fallback threads the same two dials from the same block, so a sub-floor
close is refused on both paths instead of enforcing purely by whether the
daemon happened to be reachable.

The handler is driven in process (a constructed :class:`typer.Context` plus
:class:`~eawf.surfaces.cli.flags.GlobalFlags`) rather than through a CLI
runner, so the suite stays inside the unit tier while still exercising the
real fallback body.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import orjson
import pytest
import typer

from eawf.kernel.spec.common import CriterionSpec, OracleTier, QualityDimension
from eawf.kernel.state.enums import ProjectStatus, ScopeKind, WaveStatus
from eawf.kernel.state.models import CurrentPointers, Project, State, Wave
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.observability.metrics.odr import DEFAULT_ODR_FLOOR
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.state import _apply_iter_close
from eawf.runtime.daemon.methods.state_apply import apply_mutation_under_lock

# The parent ``lifecycle`` module re-exports sibling helpers at the bottom of
# its module body, so it has to be imported before its sibling: importing
# ``lifecycle_iter`` first re-enters a partially initialized module.
from eawf.surfaces.cli.commands import lifecycle as _lifecycle_parent  # noqa: F401
from eawf.surfaces.cli.commands.lifecycle_iter import iter_close_cmd
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.workflow.lifecycle.transitions import open_iter, open_phase

pytestmark = pytest.mark.unit

_ITER_ID = "P01-I01"
_WAVE_ID = "P01-I01-W01"
_AUDIT_ID = "AUD-ODR"


def _empty_state() -> State:
    """Build a minimal valid state carrying one project and no scopes."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
                description=None,
                domains=["workflow"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _write_state(workspace: Path, state: State) -> None:
    """Persist *state* to ``<workspace>/.ea/state.json``."""
    state_path = workspace / ".ea" / "state.json"
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json"), option=orjson.OPT_INDENT_2))


def _read_state(workspace: Path) -> dict[str, Any]:
    """Decode the persisted state payload of *workspace*."""
    payload: dict[str, Any] = orjson.loads(workspace.joinpath(".ea", "state.json").read_bytes())
    return payload


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Create one ACTIVE empty iter routed through the daemonless fallback."""
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    tmp_path.joinpath(".ea").mkdir()
    state = _empty_state()
    open_phase(state, phase_id="P01", title="Phase")
    open_iter(state, iter_id=_ITER_ID, phase_id="P01", title="Iter")
    _write_state(tmp_path, state)
    yield tmp_path


def _close_iter(workspace: Path, *, iter_id: str = _ITER_ID) -> int:
    """Run the close handler in process and return its CLI exit code."""
    ctx = typer.Context(click.Command("close"))
    ctx.obj = GlobalFlags(json_output=True, workspace=workspace)
    try:
        iter_close_cmd(
            ctx,
            iter_id=iter_id,
            audit=_AUDIT_ID,
            checkpoint=None,
            archive_specs=False,
        )
    except click.exceptions.Exit as exit_signal:
        return int(exit_signal.exit_code)
    return 0


def _write_odr_profile(workspace: Path, *, odr_floor: float, odr_blocking: bool) -> None:
    """Enable one local profile carrying the two ODR dials under test."""
    profile_dir = workspace / ".ea" / "profiles"
    profile_dir.mkdir(exist_ok=True)
    profile_dir.joinpath("odr.yaml").write_text(
        "name: odr\n"
        "verify:\n"
        f"  odr_floor: {odr_floor}\n"
        f"  odr_blocking: {'true' if odr_blocking else 'false'}\n",
        encoding="utf-8",
    )
    workspace.joinpath(".ea", "config.yaml").write_text(
        "profiles:\n  enabled:\n    - odr\n",
        encoding="utf-8",
    )


def _odr_criterion(criterion_id: str, *, tier: OracleTier) -> CriterionSpec:
    """Build a minimal required criterion carrying *tier* as its oracle tier."""
    return CriterionSpec(
        id=criterion_id,
        text=f"criterion {criterion_id} succeeds and is observable",
        kind="deterministic",
        acceptance_style="binary",
        evidence_kind="deterministic",
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="a deterministic check produces a bit verdict",
        required=True,
        oracle_tier=tier,
    )


def _install_sub_floor_wave(workspace: Path) -> None:
    """Add one CLOSED wave whose criteria score an ODR of 1/3 to the iter."""
    now = datetime.now(UTC)
    wave = Wave(
        id=_WAVE_ID,
        iter_id=_ITER_ID,
        title="sub-floor wave",
        status=WaveStatus.CLOSED,
        file_scopes=["src/"],
        success_criteria=[
            _odr_criterion("CR-01", tier=OracleTier.T1_STATIC),
            _odr_criterion("CR-02", tier=OracleTier.T7_JURY),
            _odr_criterion("CR-03", tier=OracleTier.T7_JURY),
        ],
        opened_at=now,
        closed_at=now,
    )
    payload = _read_state(workspace)
    payload["waves"] = {_WAVE_ID: wave.model_dump(mode="json")}
    payload["iters"][_ITER_ID]["wave_ids"] = [_WAVE_ID]
    workspace.joinpath(".ea", "state.json").write_bytes(
        orjson.dumps(payload, option=orjson.OPT_INDENT_2)
    )


def _spy_on_close_iter(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record the kwargs of every ``close_iter`` call, delegating to the real one."""
    from eawf.workflow.lifecycle import transitions

    calls: list[dict[str, Any]] = []
    real_close_iter = transitions.close_iter

    def _recording_close_iter(state: State, **kwargs: Any) -> Any:
        calls.append(dict(kwargs))
        return real_close_iter(state, **kwargs)

    monkeypatch.setattr(transitions, "close_iter", _recording_close_iter)
    return calls


def _daemon_close_rejection(workspace: Path) -> DaemonValidationError:
    """Run the same close through the daemon applier and return its rejection."""
    state_path = workspace / ".ea" / "state.json"
    state = State.model_validate(_read_state(workspace))
    mutation = Mutation(
        kind=MutationKind.ITER_CLOSE,
        scope_id=_ITER_ID,
        mutation_id="m" * 32,
        params={"iter_id": _ITER_ID, "audit_id": _AUDIT_ID},
    )
    with pytest.raises(DaemonValidationError) as excinfo:
        apply_mutation_under_lock(
            state,
            mutation,
            apply_func=_apply_iter_close,
            state_path=state_path,
            repo_root_override=str(workspace),
        )
    return excinfo.value


def test_fallback_close_passes_odr_dials(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fallback hands ``close_iter`` the loaded block's dials, not the defaults."""
    _write_odr_profile(workspace, odr_floor=0.95, odr_blocking=True)
    calls = _spy_on_close_iter(monkeypatch)

    exit_code = _close_iter(workspace)

    assert exit_code == 0, capsys.readouterr().out
    assert len(calls) == 1
    assert calls[0]["odr_floor"] == pytest.approx(0.95)
    assert calls[0]["odr_blocking"] is True
    # The configured dials must differ from close_iter's defaults, or the
    # assertions above could not tell threading apart from doing nothing.
    assert calls[0]["odr_floor"] != pytest.approx(DEFAULT_ODR_FLOOR)


def test_fallback_close_without_verify_block_keeps_advisory_defaults(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Boundary: no verify block in hand leaves the ODR gate advisory-only.

    A repo with no profile and no ``verify:`` leaf resolves no block, so the
    fallback must fall back to the same defaults ``close_iter`` documents --
    threading the dials must not turn every repo into a blocking one.
    """
    _install_sub_floor_wave(workspace)
    calls = _spy_on_close_iter(monkeypatch)

    exit_code = _close_iter(workspace)

    assert exit_code == 0, capsys.readouterr().out
    assert calls[0]["odr_floor"] == pytest.approx(DEFAULT_ODR_FLOOR)
    assert calls[0]["odr_blocking"] is False
    assert _read_state(workspace)["iters"][_ITER_ID]["status"] == "closed"


def test_sub_floor_close_raises_through_fallback(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sub-floor close is refused on the fallback path, exactly as daemon-side."""
    _write_odr_profile(workspace, odr_floor=0.80, odr_blocking=True)
    _install_sub_floor_wave(workspace)
    before = workspace.joinpath(".ea", "state.json").read_bytes()

    exit_code = _close_iter(workspace)

    out = capsys.readouterr().out
    assert exit_code == 2, out
    assert "odr_blocking is set" in out
    # The gate fires before any state mutation: the iter stays ACTIVE on disk.
    assert workspace.joinpath(".ea", "state.json").read_bytes() == before
    assert orjson.loads(before)["iters"][_ITER_ID]["status"] == "active"

    # Parity: the daemon applier refuses the same input for the same reason.
    daemon_message = str(_daemon_close_rejection(workspace))
    assert "odr_blocking is set" in daemon_message
    assert daemon_message.removeprefix("validation_failed: ") in out


def test_sub_floor_close_advisory_when_blocking_unset(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Off-by-one against the gate: the same ODR closes when blocking is unset."""
    _write_odr_profile(workspace, odr_floor=0.80, odr_blocking=False)
    _install_sub_floor_wave(workspace)

    exit_code = _close_iter(workspace)

    assert exit_code == 0, capsys.readouterr().out
    assert _read_state(workspace)["iters"][_ITER_ID]["status"] == "closed"


def test_iter_close_rejects_malformed_iter_id(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Error path: a malformed iter id is refused before any mutation runs."""
    _write_odr_profile(workspace, odr_floor=0.80, odr_blocking=True)
    before = workspace.joinpath(".ea", "state.json").read_bytes()

    exit_code = _close_iter(workspace, iter_id="P01_I01")

    out = capsys.readouterr().out
    assert exit_code == 1, out
    assert "invalid iter id" in out
    assert workspace.joinpath(".ea", "state.json").read_bytes() == before
