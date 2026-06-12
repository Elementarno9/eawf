"""Tests: the I11 phase-track-tag binding stays wired in the idle gate.

P30-I11 wired the Track lifecycle (the durable strategy-track vehicle this repo
risks shipping built-but-idle, the B091 idle-verifier regression). P30-I11-W05
registers the binding in the idle-contract meta-gate
(``tools/idle_contract_gate.py``) so a future change that re-idles it fails the
gate:

- **W03 / TRACK-3** -- :func:`check_phase_track_tag_wired`: ``open_phase`` silently
  stamps every phase with ``track_id=state.current.track_id``, so a phase opened
  while a Track is in focus tags its owning Track.

The binding asserts two contracts, mirroring ``test_i09_jury_bindings_wired``:

- **pass on the real tree** -- the check passes against the live source.
- **red on a re-idled fixture** -- an injected ``module_text`` that drops the
  binding (a registration is removed, or the phase-tag stamp is gone) fails the
  check with the matching :class:`GateFailure`.

The gate module is loaded by path because ``tools/`` is excluded from the package
and so is not importable by name (mirrors ``tests/unit/test_idle_contract_gate.py``).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

#: Repo root: this file is ``tests/workflow/...`` so the root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_gate_module() -> Any:
    """Import ``tools/idle_contract_gate.py`` by path for the binding probes.

    The module is registered in ``sys.modules`` before ``exec_module`` so the
    gate's ``@dataclass`` definitions (which resolve ``cls.__module__`` under
    ``from __future__ import annotations``) find their own namespace.
    """
    name = "idle_contract_gate_i11_under_test"
    path = _REPO_ROOT / "tools" / "idle_contract_gate.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


@pytest.fixture()
def gate() -> Any:
    """Return the freshly loaded gate module."""
    return _load_gate_module()


# --------------------------------------------------------------------------- #
# W03 / TRACK-3: open_phase silently stamps the phase with the Track id.
# --------------------------------------------------------------------------- #


def test_phase_track_tag_passes_for_real_source(gate: Any) -> None:
    """check_phase_track_tag_wired passes against the real phase module source."""
    result = gate.check_phase_track_tag_wired()
    assert result.passed, result.message
    assert result.failure is None


def test_phase_track_tag_reds_when_stamp_removed(gate: Any) -> None:
    """Removing the ``track_id=state.current.track_id`` stamp reds the gate.

    A re-idled open_phase constructs each phase with no Track tag, so phases stop
    tagging their owning Track and the gate must fail PHASE_TRACK_TAG_IDLE.
    """
    idle_text = (
        "    phase = Phase(\n"
        "        id=phase_id,\n"
        "        scope_id=effective_scope,\n"
        "        title=title,  # the track_id stamp was dropped\n"
        "        status=PhaseStatus.ACTIVE,\n"
        "    )\n"
    )
    result = gate.check_phase_track_tag_wired(module_text=idle_text)
    assert not result.passed
    assert result.failure is gate.GateFailure.PHASE_TRACK_TAG_IDLE
    assert "phase track-tag stamp is idle" in result.message


def test_phase_track_tag_reds_on_unrelated_track_id_assignment(gate: Any) -> None:
    """A ``track_id`` bound to something else does not satisfy the stamp gate.

    Boundary: an assignment like ``track_id=None`` carries the keyword but not
    the ``state.current.track_id`` source, so the silent phase-tag binding is
    dead -- the gate must still red.
    """
    idle_text = (
        "    phase = Phase(\n"
        "        id=phase_id,\n"
        "        track_id=None,  # hardcoded, no longer reads the current Track\n"
        "    )\n"
    )
    result = gate.check_phase_track_tag_wired(module_text=idle_text)
    assert not result.passed
    assert result.failure is gate.GateFailure.PHASE_TRACK_TAG_IDLE


def test_phase_track_tag_passes_for_minimal_wired_text(gate: Any) -> None:
    """A boundary text carrying just the stamp satisfies the gate."""
    wired_text = "        track_id=state.current.track_id,\n"
    result = gate.check_phase_track_tag_wired(module_text=wired_text)
    assert result.passed
    assert result.failure is None


# --------------------------------------------------------------------------- #
# Whole-gate: main() exits 0 over the real post-I11 tree (all rows pass).
# --------------------------------------------------------------------------- #


def test_main_exits_zero_over_real_tree(gate: Any) -> None:
    """The whole idle-contract gate exits 0 over the post-I11 tree.

    Runs ``main`` against a no-op diff range so the meta-gate sees no staged
    change; the I11 phase-track-tag binding row (plus every prior row) passes
    against the real source, so the aggregate exit code is 0.
    """
    exit_code = gate.main(["idle_contract_gate.py", "HEAD..HEAD"])
    assert exit_code == 0
