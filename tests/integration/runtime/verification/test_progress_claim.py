"""The progress manifest is written only by a sandboxed gate child under its claim.

A real gate child is spawned for each positive case: the executed command
records the channel it was offered and the runtime directory it ran in, and
the manifest records the process that wrote it. The negative cases prove the
refusal from every direction an unclaimed writer could come from: a direct
write, a publisher, a forged file, the readiness child that never claims, a
child that lost the claim race, and a nested sandbox copying the channel.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.daemon import gate_execution
from eawf.runtime.verification.progress import (
    PROGRESS_CHANNEL_FILENAME,
    PROGRESS_EVENTS_FILENAME,
    LegIdentity,
    LegLiveness,
    LegOutcome,
    ObligationDisposition,
    ProgressManifest,
    ProgressMode,
    ProgressPublisher,
    UnclaimedProgressError,
    list_progress_manifests,
    progress_manifest_id,
    progress_manifest_path,
    read_progress_manifest,
    write_progress_manifest,
)
from eawf.workflow.audit_dsl.models import CheckSpec
from eawf.workflow.verify.sandboxed_checks import run_checks_out_of_process

_KEY = "d" * 64
_OBSERVER_MODULE = "test_channel_observer.py"
_FORGER_MODULE = "test_channel_forger.py"
_PLUGIN_ARGV = [
    "pytest",
    "-p",
    "no:cacheprovider",
    "-p",
    "eawf.runtime.verification.progress",
    "-q",
]

_OBSERVER_BODY = """
import json
import os

from eawf.runtime.verification.progress import ProgressChannel


def test_observe():
    channel = ProgressChannel.from_env()
    leg = None if channel is None else channel.descriptor.leg
    record = {
        "runtime_dir": os.environ.get("EAWF_RUNTIME_DIR", ""),
        "offered": channel is not None,
        "attempt_id": None if leg is None else leg.attempt_id,
        "freshness_key": None if leg is None else leg.freshness_key,
    }
    with open(OBSERVED, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\\n")
"""

_FORGER_BODY = """
import json
import os
import pathlib

from eawf.runtime.verification.progress import ObligationDisposition, ProgressChannel


def test_forge():
    runtime = pathlib.Path(os.environ["EAWF_RUNTIME_DIR"])
    descriptor = {
        "leg": {
            "attempt_id": "CA-FORGED",
            "criterion_id": "CR-01",
            "gate_id": "G-FORGED",
            "freshness_key": FORGED_KEY,
            "claimed_at": "2026-01-01T00:00:00Z",
        },
        "events_path": str(runtime / "progress-events.jsonl"),
    }
    (runtime / "progress-channel.json").write_text(json.dumps(descriptor), encoding="utf-8")
    channel = ProgressChannel.from_env()
    assert channel is not None
    channel.collect(["forged::x"])
    channel.record("forged::x", ObligationDisposition.PASS)
    with open(OBSERVED, "a", encoding="utf-8") as handle:
        handle.write("forged\\n")
"""


def _state_path(root: Path) -> Path:
    path = root / "live" / ".ea" / "state.json"
    path.parent.mkdir(parents=True)
    return path


def _plant(cwd: Path, *, module: str, body: str, observed: Path) -> None:
    cwd.mkdir(parents=True, exist_ok=True)
    header = f"OBSERVED = {str(observed)!r}\nFORGED_KEY = {_KEY!r}\n"
    (cwd / module).write_text(header + body, encoding="utf-8")


def _observations(observed: Path) -> list[dict[str, Any]]:
    if not observed.is_file():
        return []
    return [json.loads(line) for line in observed.read_text(encoding="utf-8").splitlines()]


def _spec(module: str) -> CheckSpec:
    return CheckSpec(
        kind="command_exit_zero",
        name="G-CLAIM",
        args={"argv": [*_PLUGIN_ARGV, module], "scope": "all"},
    )


def _run_gate(state_path: Path, cwd: Path, spec: CheckSpec, *, attempt_id: str) -> Any:
    return gate_execution.run_gate_out_of_process(
        spec,
        cwd=cwd,
        context=gate_execution.GateExecutionContext(state_path=state_path, attempt_id=attempt_id),
        criterion_id="CR-01",
        gate_id="G-CLAIM",
    )


def _claim(state_path: Path, *, key: str = _KEY, attempt_id: str = "CA-01") -> LegIdentity:
    claimed = gate_execution.claim_gate_execution(
        state_path,
        attempt_id=attempt_id,
        criterion_id="CR-01",
        gate_id="G-01",
        spec=_spec(_OBSERVER_MODULE),
        freshness_key=key,
    )
    assert claimed is None
    claim = gate_execution.load_gate_claim(state_path, key)
    assert claim is not None
    return LegIdentity(
        attempt_id=claim.attempt_id,
        criterion_id=claim.criterion_id,
        gate_id=claim.gate_id,
        freshness_key=claim.freshness_key,
        claimed_at=claim.claimed_at,
    )


def _manifest(leg: LegIdentity) -> ProgressManifest:
    now = datetime.now(UTC)
    return ProgressManifest(
        id=progress_manifest_id(leg.freshness_key),
        leg=leg,
        producer_digest="sha256:" + "0" * 64,
        writer_pid=os.getpid(),
        progress_mode=ProgressMode.NONE,
        outcome=LegOutcome.RUNNING,
        cursor=0,
        liveness=LegLiveness(started_at=now, heartbeat_at=now, elapsed_ms=0),
        created_at=now,
        updated_at=now,
    )


def test_run_gate_out_of_process_writes_manifest_from_claimed_sandboxed_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writer is the gate child, bound to its claim, offering the channel in its sandbox."""
    live_runtime = tmp_path / "live-runtime"
    live_runtime.mkdir()
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(live_runtime))
    state_path = _state_path(tmp_path)
    observed = tmp_path / "observed.jsonl"
    cwd = tmp_path / "repo"
    _plant(cwd, module=_OBSERVER_MODULE, body=_OBSERVER_BODY, observed=observed)

    result = _run_gate(state_path, cwd, _spec(_OBSERVER_MODULE), attempt_id="CA-CLAIM")

    assert result.passed is True, result.details
    assert result.freshness_key is not None
    claim = gate_execution.load_gate_claim(state_path, result.freshness_key)
    assert claim is not None
    manifest = read_progress_manifest(state_path, result.freshness_key)
    assert manifest is not None
    assert manifest.writer_pid != os.getpid(), "the parent, not the gate child, wrote the manifest"
    assert (manifest.leg.attempt_id, manifest.leg.criterion_id, manifest.leg.gate_id) == (
        claim.attempt_id,
        claim.criterion_id,
        claim.gate_id,
    )
    assert manifest.leg.claimed_at == claim.claimed_at
    assert manifest.outcome is LegOutcome.PASSED
    assert [(r.obligation_id, r.disposition) for r in manifest.obligations] == [
        (f"{_OBSERVER_MODULE}::test_observe", ObligationDisposition.PASS)
    ]
    (seen,) = _observations(observed)
    assert seen["offered"] is True
    assert seen["attempt_id"] == "CA-CLAIM"
    assert seen["freshness_key"] == result.freshness_key
    command_runtime = Path(seen["runtime_dir"])
    assert command_runtime != live_runtime, "the channel was offered in the live runtime dir"
    assert live_runtime not in command_runtime.parents
    assert not command_runtime.exists(), "the sandbox holding the channel outlived the leg"
    assert not (live_runtime / PROGRESS_CHANNEL_FILENAME).exists()


def test_write_progress_manifest_refuses_unclaimed_leg(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    now = datetime.now(UTC)
    leg = LegIdentity(
        attempt_id="CA-01", criterion_id="CR-01", gate_id="G-01", freshness_key=_KEY, claimed_at=now
    )

    with pytest.raises(UnclaimedProgressError, match="no durable claim"):
        write_progress_manifest(state_path, _manifest(leg))

    assert not progress_manifest_path(state_path, _KEY).exists()


def test_write_progress_manifest_refuses_leg_claimed_by_another_attempt(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    leg = _claim(state_path, attempt_id="CA-OWNER")
    impostor = leg.model_copy(update={"attempt_id": "CA-IMPOSTOR"})

    with pytest.raises(UnclaimedProgressError, match="differs on attempt_id"):
        write_progress_manifest(state_path, _manifest(impostor))

    assert not progress_manifest_path(state_path, _KEY).exists()


def test_write_progress_manifest_refuses_leg_from_an_earlier_claim(tmp_path: Path) -> None:
    """Boundary: the same ids under another claim instant are a different claim."""
    state_path = _state_path(tmp_path)
    leg = _claim(state_path)
    stale = leg.model_copy(update={"claimed_at": datetime(2020, 1, 1, tzinfo=UTC)})

    with pytest.raises(UnclaimedProgressError, match="differs on claimed_at"):
        write_progress_manifest(state_path, _manifest(stale))


def test_write_progress_manifest_accepts_the_claim_holder(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    manifest = _manifest(_claim(state_path))

    write_progress_manifest(state_path, manifest)

    assert read_progress_manifest(state_path, _KEY) == manifest


def test_progress_publisher_open_refuses_unclaimed_leg_before_offering_channel(
    tmp_path: Path,
) -> None:
    state_path = _state_path(tmp_path)
    channel_dir = tmp_path / "sandbox-runtime"
    channel_dir.mkdir()
    now = datetime.now(UTC)
    publisher = ProgressPublisher(
        state_path=state_path,
        leg=LegIdentity(
            attempt_id="CA-01",
            criterion_id="CR-01",
            gate_id="G-01",
            freshness_key=_KEY,
            claimed_at=now,
        ),
        producer_digest="sha256:" + "0" * 64,
        resolved_timeout_seconds=None,
        channel_dir=channel_dir,
    )

    with pytest.raises(UnclaimedProgressError):
        publisher.open()

    assert list(channel_dir.iterdir()) == [], "an unclaimed publisher offered a channel"


def test_read_progress_manifest_refuses_forged_manifest_without_claim(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    now = datetime.now(UTC)
    forged = _manifest(
        LegIdentity(
            attempt_id="CA-01",
            criterion_id="CR-01",
            gate_id="G-01",
            freshness_key=_KEY,
            claimed_at=now,
        )
    )
    target = progress_manifest_path(state_path, _KEY)
    target.parent.mkdir(parents=True)
    target.write_text(forged.model_dump_json(), encoding="utf-8")

    assert read_progress_manifest(state_path, _KEY) is None
    assert list_progress_manifests(state_path) == []


def test_read_progress_manifest_refuses_manifest_filed_under_another_key(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    manifest = _manifest(_claim(state_path))
    other_key = "9" * 64
    _claim(state_path, key=other_key)
    target = progress_manifest_path(state_path, other_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(manifest.model_dump_json(), encoding="utf-8")

    assert read_progress_manifest(state_path, other_key) is None


def test_run_checks_out_of_process_unclaimed_child_publishes_no_manifest(tmp_path: Path) -> None:
    """The readiness child never claims, so neither an offered nor a forged channel publishes."""
    state_path = _state_path(tmp_path)
    observed = tmp_path / "observed.jsonl"
    cwd = tmp_path / "repo"
    _plant(cwd, module=_OBSERVER_MODULE, body=_OBSERVER_BODY, observed=observed)
    _plant(cwd, module=_FORGER_MODULE, body=_FORGER_BODY, observed=tmp_path / "forged.log")

    results = run_checks_out_of_process(
        [_spec(_OBSERVER_MODULE), _spec(_FORGER_MODULE).model_copy(update={"name": "G-FORGE"})],
        cwd=cwd,
        live_state_path=state_path,
    )

    assert [result.passed for result in results] == [True, True], [r.details for r in results]
    (seen,) = _observations(observed)
    assert seen["offered"] is False, "the unclaimed readiness child offered a channel"
    assert (tmp_path / "forged.log").read_text(encoding="utf-8") == "forged\n"
    assert not progress_manifest_path(state_path, _KEY).parent.exists()
    assert list_progress_manifests(state_path) == []


def test_run_gate_out_of_process_child_that_loses_the_claim_publishes_nothing(
    tmp_path: Path,
) -> None:
    state_path = _state_path(tmp_path)
    observed = tmp_path / "observed.jsonl"
    cwd = tmp_path / "repo"
    _plant(cwd, module=_OBSERVER_MODULE, body=_OBSERVER_BODY, observed=observed)
    spec = _spec(_OBSERVER_MODULE)
    first = _run_gate(state_path, cwd, spec, attempt_id="CA-FIRST")
    assert first.freshness_key is not None
    manifest_path = progress_manifest_path(state_path, first.freshness_key)
    published = manifest_path.read_bytes()

    second = _run_gate(state_path, cwd, spec, attempt_id="CA-SECOND")

    assert first.passed is True, first.details
    assert second.status == "blocked"
    assert second.details is not None
    assert "indeterminate gate execution" in second.details
    assert len(_observations(observed)) == 1, "the losing child ran the command"
    assert manifest_path.read_bytes() == published, "the losing child rewrote the manifest"


def test_seed_gate_sandbox_never_copies_a_progress_channel(tmp_path: Path) -> None:
    """A nested sandbox cannot inherit, and so cannot publish into, an enclosing leg."""
    outer_runtime = tmp_path / "outer-runtime"
    outer_runtime.mkdir()
    (outer_runtime / PROGRESS_CHANNEL_FILENAME).write_text("{}", encoding="utf-8")
    (outer_runtime / PROGRESS_EVENTS_FILENAME).write_text("", encoding="utf-8")
    (outer_runtime / "kept.json").write_text("{}", encoding="utf-8")

    sandbox = gate_execution.seed_gate_sandbox(
        root=tmp_path / "nested",
        live_state_path=tmp_path / "absent" / ".ea" / "state.json",
        live_runtime_dir=outer_runtime,
    )

    assert sorted(entry.name for entry in sandbox.runtime_dir.iterdir()) == ["kept.json"]
