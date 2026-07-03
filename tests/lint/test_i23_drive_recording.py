"""The committed I23 live-drive recording stays valid (P30-I23-W33).

The recording is the phase's machine-checkable proof that the hardened
autopilot ran LIVE — priced, EU-captured, gate-executing, jailed, and
campaign-converged — under explicit caps. This suite pins the committed
directory through the seven-assertion validator plus the criterion-level
facts the validator abstracts over.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.validate_drive_recording import RecordingInvalidError, validate_recording

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RECORDING = _REPO_ROOT / ".ea" / "artifacts" / "evidence" / "2026-07-03-i23-live-drive"


def test_recording_passes_all_seven_assertions() -> None:
    """CR-01: the committed recording validates end to end."""
    confirmations = validate_recording(_RECORDING)
    assert len(confirmations) == 8


def test_gate_executing_clean_close_fired_at_least_twice() -> None:
    """CR-02: >= 2 waves closed via the W19 gate-executing clean path."""
    summary = json.loads((_RECORDING / "summary.json").read_text(encoding="utf-8"))
    assert len(summary.get("gate_executing_closes") or []) >= 2
    gates_log = (_RECORDING / "close_gates.log").read_text(encoding="utf-8")
    assert gates_log.count("run_close_gates") >= 2
    assert gates_log.count("passed=True") >= 2


def test_campaign_converged_and_caps_were_armed() -> None:
    """CR-02/CR-03: the campaign reached CONVERGED and every cap was set."""
    summary = json.loads((_RECORDING / "summary.json").read_text(encoding="utf-8"))
    assert summary["campaign_terminal_status"] == "converged"
    caps = summary.get("caps") or {}
    assert caps.get("eu_cap") and caps.get("usd_cap")
    assert float(summary["total_cost_usd"]) < float(caps["usd_cap"])


def test_attestation_present_with_zero_intervention_statement() -> None:
    """CR-03: the operator attestation carries the required statements."""
    attestation = (_RECORDING / "2026-07-03-attestation.md").read_text(encoding="utf-8")
    assert "Zero manual interventions" in attestation
    assert "uptime" in attestation
    assert "eu_cap" in attestation and "usd_cap" in attestation


def test_recording_carries_no_machine_paths() -> None:
    """Hygiene: the promoted excerpts are scrubbed."""
    home_needle = "/" + "Users" + "/"
    fixture_needle = "/private/tmp/" + "eawf-smoke"
    for path in _RECORDING.iterdir():
        body = path.read_text(encoding="utf-8")
        assert home_needle not in body, f"{path.name} leaks a home path"
        assert fixture_needle not in body, f"{path.name} leaks the fixture path"


def test_validator_rejects_a_zero_cost_recording(tmp_path: Path) -> None:
    """Falsifier: the validator can fail — a stubbed run is refused."""
    for name in ("dispatch_cost.jsonl", "watch_tail.txt", "jail_smoke.log", "close_gates.log"):
        (tmp_path / name).write_text((_RECORDING / name).read_text(encoding="utf-8"))
    summary = json.loads((_RECORDING / "summary.json").read_text(encoding="utf-8"))
    summary["total_cost_usd"] = 0.0
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    with pytest.raises(RecordingInvalidError, match="assertion 1"):
        validate_recording(tmp_path)
