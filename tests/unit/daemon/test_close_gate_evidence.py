"""Tests: the daemon close gate mints a deterministic-pass evidence row (P29-I12-W06).

Exercises the W06 wiring of
:func:`eawf.runtime.daemon.methods.state._enforce_wave_close_gate` ->
:func:`eawf.kernel.store.kinds.evidence.deterministic_pass_record` ->
``evidence.jsonl`` and the downstream
:func:`eawf.workflow.estimation.trust_scorecard.compute_trust_scorecard`
``verified`` label:

* positive -- an enforcing close whose required criterion carries a
  PASSING deterministic gate (``file_exists`` over a file that exists in
  the repo root) lands exactly one
  :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord` with
  ``evidence_kind="deterministic"`` + ``status="pass"`` in
  ``evidence.jsonl``, AND the trust scorecard labels the wave
  ``verified`` off that row;
* negative -- an enforcing close whose required deterministic gate FAILS
  (``file_exists`` over a missing file) is blocked at the jury fallthrough
  (stubbed to FAIL so no real spawn runs) and mints NO deterministic-pass
  row, and the wave stays CLAIMED.

Both tests run the REAL ordered oracle + REAL compile-gate + REAL gate
runner against the checkout -- only the cross-vendor jury (the last-resort
tier for the failing deterministic gate) is stubbed, so the
deterministic-evidence pipeline is proven end-to-end.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf import __version__
from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.kernel.store.paths import store_path
from eawf.observability.eval.cross_vendor_jury import CrossVendorJuryResult
from eawf.observability.eval.jury import JuryAggregateOutcome
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.state import mutate
from eawf.workflow.estimation.trust_scorecard import compute_trust_scorecard

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 6, 7, 12, 0, 0, tzinfo=UTC)

_WAVE = "P29-I12-W06"
_CRITERION = "CR-01"
_GATE = "GATE-01"
#: File the passing ``file_exists`` gate points at; created in the repo root.
_TARGET_FILE = "deliverable.txt"


def _now() -> datetime:
    return _T0


def _deterministic_criterion() -> dict[str, Any]:
    """A required deterministic criterion gated by a single ``file_exists`` gate."""
    spec = CriterionSpec(
        id=_CRITERION,
        text="the deterministic deliverable file exists in the repo root at close",
        kind="contract",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=[_GATE],
        required=True,
        quality_dimension="functional_suitability",  # type: ignore[arg-type]
        measurable_signal="a measurable signal of at least twenty characters long",
    )
    return spec.model_dump(mode="json")


def _file_exists_gate(*, path: str) -> dict[str, Any]:
    """A ``file_exists`` deterministic gate over *path* (relative to repo root)."""
    spec = GateSpec(
        id=_GATE,
        criterion_id=_CRITERION,
        kind="file_exists",
        args={"path": path},
        policy="block",
        cadence="every-wave",
        required=True,
    )
    return spec.model_dump(mode="json")


def _state_payload(*, gate_path: str) -> dict[str, Any]:
    """A minimal valid State with one CLAIMED L-effort wave + a deterministic gate."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": _now().isoformat(),
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "ABC",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ABC",
        },
        "current": {"project_code": "ABC"},
        "workspace": None,
        "phases": {
            "P29": {
                "id": "P29",
                "scope_id": "ABC",
                "subproject_id": None,
                "title": "P29",
                "status": "active",
                "iter_ids": ["P29-I12"],
                "outcome_ids": [],
                "opened_at": _now().isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P29-I12": {
                "id": "P29-I12",
                "phase_id": "P29",
                "title": "I12",
                "status": "active",
                "wave_ids": [_WAVE],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _now().isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE: {
                "id": _WAVE,
                "iter_id": "P29-I12",
                "title": "mint deterministic evidence at the close gate",
                "status": "claimed",
                "claim_session_id": "session-abc",
                "success_criteria": [_deterministic_criterion()],
                "gates": [_file_exists_gate(path=gate_path)],
                "effort_bucket": "L",
                "agent_role": "executor",
                "opened_at": _now().isoformat(),
                "sessions": {},
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _init_git_repo(root: Path) -> None:
    """Init a git repo with one empty commit so a read-only floor check passes."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t.t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t.t",
    }
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=root,
        check=True,
        env=env,
    )


def _write_enforcing_profile(root: Path) -> None:
    """Enable an enforcing profile with the jury opted in + a passing floor check.

    ``cross_vendor_jury: true`` is set so the high-risk (L-effort,
    ``"always"``) close routes through the ``run_oracle`` loop rather than
    the dedicated single-auditor producer. With a passing floor check
    (``git status`` inside the repo) the always-on readiness projection
    clears, so the only gate that can refuse close is the ordered oracle.
    """
    profile_dir = root / ".ea" / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (root / ".ea" / "config.yaml").write_text(
        "profiles:\n  enabled:\n    - enforcing\n",
        encoding="utf-8",
    )
    profile_dir.joinpath("enforcing.yaml").write_text(
        "\n".join(
            [
                "name: enforcing",
                "verify:",
                "  enforce: true",
                "  cross_vendor_jury: true",
                "  argv_allowlist:",
                "    - git",
                "  floor_checks:",
                "    - name: pass-floor",
                '      cmd: ["git", "status"]',
                "      scope: all",
                "      cadence: every-wave",
                "      policy: warn",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_state(state_path: Path, payload: dict[str, Any]) -> State:
    state = State.model_validate(payload)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state


def _build_ctx(tmp_path: Path, state_path: Path) -> MethodContext:
    event_path = store_path(state_path, StoreKind.EVENT)
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    return MethodContext(
        started_at="2026-06-07T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        event_path=event_path,
        state_path=state_path,
        wal_dir=wal_dir,
        idempotency_cache={},
    )


def _close_mutation() -> Mutation:
    return Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id=_WAVE,
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": _WAVE, "outcome": "ok"},
    )


def _run(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(body())


def _read_evidence_rows(state_path: Path) -> list[EvidenceRecord]:
    """Decode every payload in ``evidence.jsonl`` (empty list when absent)."""
    path = store_path(state_path, StoreKind.EVIDENCE)
    if not path.exists():
        return []
    rows: list[EvidenceRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        envelope = orjson.loads(line)
        rows.append(EvidenceRecord.model_validate(envelope["payload"]))
    return rows


# --------------------------------------------------------------------------- #
# Positive: a passing deterministic gate mints one deterministic/pass row and
# the trust scorecard labels the wave ``verified``.
# --------------------------------------------------------------------------- #


def test_close_passing_deterministic_gate_mints_evidence_and_verifies(
    tmp_path: Path,
) -> None:
    """A passing ``file_exists`` gate at the close gate mints a verified row.

    End-to-end: the real ordered oracle compiles + runs the deterministic
    gate, it passes, and the close gate mints exactly one
    ``deterministic`` / ``pass`` :class:`EvidenceRecord` scoped to the
    wave. The trust scorecard then labels the wave ``verified`` off that
    row -- the criterion of W06.
    """
    _write_enforcing_profile(tmp_path)
    _init_git_repo(tmp_path)
    # The gate passes: the target file exists in the repo root the gate
    # runs against.
    (tmp_path / _TARGET_FILE).write_text("delivered\n", encoding="utf-8")
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path, _state_payload(gate_path=_TARGET_FILE))
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        await mutate(ctx, {"mutation": _close_mutation().model_dump(mode="json")})

        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_WAVE]["status"] == "closed"

        rows = _read_evidence_rows(state_path)
        deterministic_pass = [
            r for r in rows if r.evidence_kind == "deterministic" and r.status == "pass"
        ]
        assert len(deterministic_pass) == 1
        row = deterministic_pass[0]
        assert row.scope_id == _WAVE
        assert row.produced_by == "tool"
        assert _GATE in row.refs
        assert _CRITERION in row.refs

        state = State.model_validate(payload)
        scorecard = compute_trust_scorecard(state, state_path=state_path)
        label = next(lbl for lbl in scorecard.output_labels if lbl.scope_id == _WAVE)
        assert label.tier == "verified"
        assert row.id in label.evidence_refs

    _run(body)


# --------------------------------------------------------------------------- #
# Negative: a failing deterministic gate blocks close and mints NO pass row.
# --------------------------------------------------------------------------- #


def test_close_failing_deterministic_gate_blocks_and_mints_no_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing ``file_exists`` gate blocks close and leaves no deterministic row.

    The deterministic gate points at a missing file, so the real oracle
    exhausts the deterministic tier without a pass and falls through to the
    jury tier (stubbed to FAIL so no live spawn runs). The close is refused
    (``DaemonValidationError``), the wave stays CLAIMED, and no
    ``deterministic`` / ``pass`` evidence row is minted.
    """
    _write_enforcing_profile(tmp_path)
    _init_git_repo(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    # The gate FAILS: the target file is never created in the repo root.
    _write_state(state_path, _state_payload(gate_path="missing-deliverable.txt"))
    ctx = _build_ctx(tmp_path, state_path)

    async def _jury_fail(*_a: Any, wave: Any = None, **_k: Any) -> CrossVendorJuryResult:
        return CrossVendorJuryResult(
            wave_id=_WAVE,
            outcome=JuryAggregateOutcome.FAIL,
            aggregate=None,
            jurors=(),
            voted_count=0,
            abstained_count=0,
            reasons=("stubbed fail",),
        )

    # ``run_oracle`` imports the convener by name at module load, so the
    # patch must target the binding inside the oracle module, not the
    # original definition site.
    monkeypatch.setattr(
        "eawf.workflow.verify.oracle.convene_cross_vendor_jury",
        _jury_fail,
    )

    async def body() -> None:
        with pytest.raises(DaemonValidationError) as excinfo:
            await mutate(ctx, {"mutation": _close_mutation().model_dump(mode="json")})
        assert "oracle blocked close" in str(excinfo.value)

        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_WAVE]["status"] == "claimed"

        rows = _read_evidence_rows(state_path)
        assert not [r for r in rows if r.evidence_kind == "deterministic" and r.status == "pass"]

    _run(body)
