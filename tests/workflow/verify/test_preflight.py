"""Tests for :mod:`eawf.workflow.verify.preflight` (shared close pre-flight).

Pins the P30-I23-W06 extraction contract: the bundle runs the three
pre-apply close checks in their canonical order (gate-ref validation ->
enforcing close gate -> floor-pack readiness), a raising seam
short-circuits the rest, and the daemon close pipeline actually calls
``run_close_preflight`` (so the extraction cannot silently regress into
a re-plaited inline sequence).
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any

import pytest

from eawf.workflow.verify.preflight import ClosePreflight, run_close_preflight

_STATE = object()
_MUTATION = object()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_run_close_preflight_runs_seams_in_canonical_order(tmp_path: Path) -> None:
    calls: list[str] = []
    readiness = object()

    def _validate(state: Any, mutation: Any) -> None:
        assert state is _STATE and mutation is _MUTATION
        calls.append("validate")

    async def _enforce(
        state: Any, mutation: Any, *, state_path: Path, repo_root: Path
    ) -> list[Any]:
        calls.append("enforce")
        return ["evidence-row"]

    def _readiness(state: Any, mutation: Any, *, state_path: Path, repo_root: Path) -> Any:
        calls.append("readiness")
        return readiness

    result = _run(
        run_close_preflight(
            _STATE,  # type: ignore[arg-type]
            _MUTATION,  # type: ignore[arg-type]
            state_path=tmp_path / "state.json",
            repo_root=tmp_path,
            validate_gate_refs=_validate,
            enforce_close_gate=_enforce,
            compute_readiness=_readiness,
        )
    )

    assert calls == ["validate", "enforce", "readiness"]
    assert isinstance(result, ClosePreflight)
    assert result.evidence == ["evidence-row"]
    assert result.readiness is readiness


def test_run_close_preflight_validate_failure_short_circuits(tmp_path: Path) -> None:
    calls: list[str] = []

    def _validate(state: Any, mutation: Any) -> None:
        raise ValueError("orphan gate ref")

    async def _enforce(
        state: Any, mutation: Any, *, state_path: Path, repo_root: Path
    ) -> list[Any]:
        calls.append("enforce")
        return []

    def _readiness(state: Any, mutation: Any, *, state_path: Path, repo_root: Path) -> Any:
        calls.append("readiness")
        return None

    with pytest.raises(ValueError, match="orphan gate ref"):
        _run(
            run_close_preflight(
                _STATE,  # type: ignore[arg-type]
                _MUTATION,  # type: ignore[arg-type]
                state_path=tmp_path / "state.json",
                repo_root=tmp_path,
                validate_gate_refs=_validate,
                enforce_close_gate=_enforce,
                compute_readiness=_readiness,
            )
        )
    assert calls == []


def test_run_close_preflight_enforce_refusal_skips_readiness(tmp_path: Path) -> None:
    calls: list[str] = []

    def _validate(state: Any, mutation: Any) -> None:
        calls.append("validate")

    async def _enforce(
        state: Any, mutation: Any, *, state_path: Path, repo_root: Path
    ) -> list[Any]:
        calls.append("enforce")
        raise RuntimeError("oracle refused close")

    def _readiness(state: Any, mutation: Any, *, state_path: Path, repo_root: Path) -> Any:
        calls.append("readiness")
        return None

    with pytest.raises(RuntimeError, match="oracle refused close"):
        _run(
            run_close_preflight(
                _STATE,  # type: ignore[arg-type]
                _MUTATION,  # type: ignore[arg-type]
                state_path=tmp_path / "state.json",
                repo_root=tmp_path,
                validate_gate_refs=_validate,
                enforce_close_gate=_enforce,
                compute_readiness=_readiness,
            )
        )
    assert calls == ["validate", "enforce"]


def test_daemon_close_pipeline_calls_run_close_preflight() -> None:
    """The daemon mutate pipeline is wired through the shared bundle."""
    from eawf.runtime.daemon.methods import state as daemon_state
    from eawf.workflow.verify import preflight

    # The daemon imports the bundle (identity, not a re-implementation) ...
    assert daemon_state.run_close_preflight is preflight.run_close_preflight
    # ... and the WAVE_CLOSE branch of mutate awaits it with the three
    # daemon seams injected.
    source = inspect.getsource(daemon_state.mutate)
    assert "await run_close_preflight(" in source
    assert "validate_gate_refs=_validate_wave_close_gate_refs" in source
    assert "enforce_close_gate=_enforce_wave_close_gate" in source
    assert "compute_readiness=_compute_wave_close_readiness" in source
