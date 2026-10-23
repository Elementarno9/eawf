"""The frozen epoch-1 snapshot loads through the v1 ``State``, its argv gates given a family.

A gate row was stored without its command family and is given one as it
loads, so the closed wave's ``release tag --dry-run`` gate in the snapshot
still loads without a hand edit.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from eawf.kernel.migration.v1_state import State

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[4]
_STATE_PATH: Final[Path] = _REPO_ROOT / "tests" / "fixtures" / "epoch1-snapshot" / "state.json"
_DRY_RUN_ARGV: Final[list[str]] = [
    "uv",
    "run",
    "eawf",
    "release",
    "tag",
    "0.7.0.dev1",
    "--dry-run",
]


def test_auth_039_the_committed_closed_wave_dry_run_gate_still_loads() -> None:
    raw = json.loads(_STATE_PATH.read_bytes())
    stored = next(g for g in raw["waves"]["P31-I01-W26"]["gates"] if g["id"] == "G-03")
    assert stored["args"]["argv"] == _DRY_RUN_ARGV

    state = State.model_validate(raw)

    wave = state.waves["P31-I01-W26"]
    gate = next(g for g in wave.gates if g.id == "G-03")
    assert gate.args["argv"] == _DRY_RUN_ARGV
    assert gate.command_family_ref == "eawf"
    argv_gates = [g for w in state.waves.values() for g in w.gates if g.kind == "command_exit_zero"]
    assert argv_gates
    assert all(g.command_family_ref is not None for g in argv_gates)
