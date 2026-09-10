"""Gates that cannot red, and the receipt every executed gate leaves.

Three required blocking gates shipped in this repository and passed on
every tree they ever ran against. Two are argv shapes -- a bare history
read and a release-tag preview whose sweep hangs off a flag the argv
omits -- and the tests here run both against a tree that a seeded defect
has already broken, to establish that they exit 0 anyway and that their
repaired forms exit non-zero on the same tree. The third, the
publication-credentials probe, is sunset: no target on this train holds
a named handle, so the probe could only ever return its own no-op pass.

The last test drives one close end to end and holds both halves at once:
every gate the close executed left a durable receipt naming its argv,
exit status and timestamp, and none of the executed required gates was
one whose exit status could not depend on the tree.

Every fixture tree is built under ``tmp_path``; nothing here reads or
writes the repository's own ``.ea/``.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.kernel.spec.falsifiability import unfalsifiable_argv
from eawf.kernel.spec.promotion import SpecPromoteValidationError, validate_argv_gates
from eawf.kernel.spec.release_config import load_release_config
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import Wave
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.kernel.store.paths import store_path
from eawf.surfaces.cli.app import app
from eawf.workflow.release.train import V07_TRAIN, checkpoint_config_yaml
from eawf.workflow.verify.gate_receipts import RECEIPT_MARKER
from eawf.workflow.verify.oracle import OracleResult, run_oracle
from eawf.workflow.verify.release_probes import TagPreflightInputs, build_tag_probes
from eawf.workflow.verify.release_readiness import (
    ReleaseSignalName,
    ReleaseSignalStatus,
    compute_readiness,
    derive_required_signals,
)

pytestmark = pytest.mark.integration

runner = CliRunner()

DEV1_VERSION = "0.7.0.dev1"

#: The argv of ``P31-I01-W25`` gate ``G-04``, verbatim. It was required
#: and blocking, and it exits 0 on every tree that is a git repository.
SHIPPED_HISTORY_READ_ARGV = ["git", "log", "--stat"]

#: The argv of ``P31-I01-W26`` gate ``G-03``, verbatim. Also required
#: and blocking; the readiness sweep it was meant to rehearse runs only
#: under ``--push``.
SHIPPED_TAG_PREVIEW_ARGV = ["uv", "run", "eawf", "release", "tag", DEV1_VERSION, "--dry-run"]

#: The revision the history-read gate existed to assert. The seeded
#: defect is that it is absent from the fixture's history.
MISSING_REVISION = "v9.9.9"


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run *args* in *cwd*, returning the completed process unchecked."""
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def _seeded_repo(tmp_path: Path) -> Path:
    """Return a repository carrying the seeded defect.

    The defect: the revision the history-read gate exists to assert
    (:data:`MISSING_REVISION`) was never created, so any check that
    genuinely reads for it has something to fail on.

    Args:
        tmp_path: Per-test scratch root.

    Returns:
        The repository root.
    """
    work = tmp_path / "seeded"
    work.mkdir()
    _run(["git", "init", "-b", "main"], work)
    _run(["git", "config", "user.email", "test@example.invalid"], work)
    _run(["git", "config", "user.name", "Test"], work)
    (work / "f.txt").write_text("x\n", encoding="utf-8")
    _run(["git", "add", "f.txt"], work)
    _run(["git", "commit", "-m", "init"], work)
    return work


# --- the two argv shapes, run against a seeded defect ------------------------


def test_shipped_history_read_gate_exits_zero_over_a_seeded_defect(tmp_path: Path) -> None:
    """``git log --stat`` reports history and exits 0 though the revision is gone."""
    work = _seeded_repo(tmp_path)
    assert _run(["git", "rev-parse", "--verify", MISSING_REVISION], work).returncode != 0
    assert _run(SHIPPED_HISTORY_READ_ARGV, work).returncode == 0


def test_repaired_history_read_gate_exits_non_zero_over_the_same_defect(tmp_path: Path) -> None:
    """The repaired argv names a revision that must resolve, so the defect reds it."""
    work = _seeded_repo(tmp_path)
    repaired = ["git", "log", "--max-count=1", MISSING_REVISION]
    assert _run(repaired, work).returncode != 0
    tagged = _run(["git", "tag", MISSING_REVISION], work)
    assert tagged.returncode == 0
    assert _run(repaired, work).returncode == 0


def test_shipped_tag_preview_gate_exits_zero_over_a_seeded_defect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preview skips the readiness sweep, so an unready checkpoint still exits 0."""
    work = _seeded_repo(tmp_path)
    monkeypatch.chdir(work)
    result = runner.invoke(app, ["release", "tag", DEV1_VERSION, "--dry-run"])
    assert result.exit_code == 0


def test_repaired_tag_preview_gate_exits_non_zero_over_the_same_defect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Arming ``--push`` runs the sweep, which the unready checkpoint fails."""
    work = _seeded_repo(tmp_path)
    monkeypatch.chdir(work)
    result = runner.invoke(app, ["release", "tag", DEV1_VERSION, "--push", "--dry-run"])
    assert result.exit_code != 0
    assert "release preflight refuses" in (result.stdout + str(result.exception or ""))
    assert _run(["git", "tag", "--list"], work).stdout.strip() == ""


# --- the rule that refuses them ---------------------------------------------


@pytest.mark.parametrize(
    ("argv", "fragment"),
    [
        (SHIPPED_HISTORY_READ_ARGV, "exits 0 on every tree"),
        (SHIPPED_TAG_PREVIEW_ARGV, "readiness sweep only under --push"),
    ],
)
def test_unfalsifiable_argv_refuses_each_shipped_offender(argv: list[str], fragment: str) -> None:
    """Both shipped argvs are named, with the reason the exit status is fixed."""
    verdict = unfalsifiable_argv(argv)
    assert verdict is not None
    assert fragment in verdict.reason
    assert verdict.repair


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "log", "--max-count=1", MISSING_REVISION],
        ["git", "log", "--exit-code", "--stat"],
        ["uv", "run", "eawf", "release", "tag", DEV1_VERSION, "--push", "--dry-run"],
        ["uv", "run", "pytest", "tests/integration/workflow/verify", "-q"],
        ["git", "status", "--porcelain"],
    ],
)
def test_unfalsifiable_argv_admits_a_falsifiable_argv(argv: list[str]) -> None:
    """A command whose exit status can depend on the tree is not refused."""
    assert unfalsifiable_argv(argv) is None


def test_unfalsifiable_argv_ignores_a_pathspec_operand() -> None:
    """Tokens after ``--`` are pathspecs; git accepts them matching nothing."""
    verdict = unfalsifiable_argv(["git", "log", "--max-count=1", "--", "absent.txt"])
    assert verdict is not None


def test_unfalsifiable_argv_on_an_empty_argv_returns_none() -> None:
    """An empty argv has no command to judge; the L0 policy refuses it first."""
    assert unfalsifiable_argv([]) is None


def test_promote_refuses_a_gate_whose_argv_cannot_fail() -> None:
    """The promote-time seam names the gate, the reason and the repair."""
    gate = GateSpec(
        id="G-04",
        criterion_id="CR-04",
        kind="command_exit_zero",
        args={"argv": SHIPPED_HISTORY_READ_ARGV},
        policy="block",
        cadence="every-wave",
        required=True,
    )
    with pytest.raises(SpecPromoteValidationError) as excinfo:
        validate_argv_gates([gate])
    message = str(excinfo.value)
    assert "G-04" in message
    assert "cannot fail" in message
    assert "write 'git log --max-count=1" in message


# --- the sunset credentials probe -------------------------------------------


def test_credentials_signal_has_no_producer_and_is_never_required(tmp_path: Path) -> None:
    """The sunset probe is gone: the row is unavailable, not a free green."""
    work = _seeded_repo(tmp_path)
    config = load_release_config(checkpoint_config_yaml(DEV1_VERSION), train=V07_TRAIN)
    assert ReleaseSignalName.CREDENTIALS not in derive_required_signals(config)
    inputs = TagPreflightInputs(
        repo_root=work,
        version=DEV1_VERSION,
        tag=f"v{DEV1_VERSION}",
        package_version=DEV1_VERSION,
        remote="origin",
    )
    assert ReleaseSignalName.CREDENTIALS not in build_tag_probes(inputs)
    readiness = compute_readiness(
        config,
        probes=build_tag_probes(inputs),
        observed_revision="deadbeef",
        computed_at=datetime.now(UTC),
    )
    statuses = {row.signal: row.status for row in readiness.signals}
    assert statuses[ReleaseSignalName.CREDENTIALS] is ReleaseSignalStatus.UNAVAILABLE


# --- one close: a receipt per gate, and no gate that cannot red --------------


def _criterion(gate_ids: list[str]) -> CriterionSpec:
    """Build the deterministic criterion the close scorer escalates."""
    return CriterionSpec(
        id="CR-01",
        text="every gate a close executes leaves a receipt and can red",
        kind="contract",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=gate_ids,
        quality_dimension="functional_suitability",
        measurable_signal="a measurable signal of at least twenty characters",
    )


def _wave() -> Wave:
    """Build the minimal valid wave the close scores."""
    return Wave(
        id="P32-I01-W26",
        iter_id="P32-I01",
        title="replace the gates that cannot fail and persist a receipt per close",
        status="claimed",
        opened_at="2026-09-09T00:00:00Z",
    )


def _live_tree(work: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return the fixture's ``state.json``, with the runtime dir redirected."""
    state_dir = work / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scope_kind": "repo",
                "urn": "urn:eawf:v1:state:ABC",
                "updated_at": "2026-09-09T00:00:00Z",
                "project": {
                    "code": "ABC",
                    "slug": "abc",
                    "title": "Abc",
                    "domains": ["infra"],
                    "default_branch": "main",
                    "status": "active",
                    "repo_urn": "urn:eawf:v1:repo:ABC",
                },
                "current": {
                    "project_code": "ABC",
                    "track_id": None,
                    "phase_id": None,
                    "iter_id": None,
                    "active_wave_ids": [],
                    "active_session_ids": [],
                },
                "workspace": None,
                "phases": {},
                "iters": {},
                "waves": {},
                "artifacts": {},
                "agent_sessions": {},
                "plugins": {},
                "indexes": {},
            }
        ),
        encoding="utf-8",
    )
    runtime = work / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(runtime))
    return state_path


def _read_receipts(state_path: Path, *, scope_id: str) -> list[EvidenceRecord]:
    """Reconstruct the wave's gate-execution receipts from the store alone.

    The reader deliberately walks the raw JSONL rather than calling a
    library helper: the contract under test is that the persisted rows
    themselves answer "which gates ran", with no in-process state.

    Args:
        state_path: Path to the fixture's ``state.json``.
        scope_id: Wave URN to filter on.

    Returns:
        The receipts in append order.
    """
    path = store_path(state_path, StoreKind.EVIDENCE)
    if not path.is_file():
        return []
    receipts: list[EvidenceRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        envelope = Envelope.model_validate_json(line)
        if envelope.kind is not StoreKind.EVIDENCE or envelope.scope_id != scope_id:
            continue
        record = EvidenceRecord.model_validate(envelope.payload)
        if (record.metrics or {}).get("receipt") == RECEIPT_MARKER:
            receipts.append(record)
    return receipts


def _score(gates: list[GateSpec], *, state_path: Path, repo_root: Path) -> OracleResult:
    """Drive one criterion's gates through the production close scorer."""
    return asyncio.run(
        run_oracle(
            _criterion([gate.id for gate in gates]),
            gates,
            wave=_wave(),
            state=object(),  # type: ignore[arg-type]
            state_path=state_path,
            events_path=state_path.parent / "event.jsonl",
            repo_root=repo_root,
            spawn_factory=lambda _runtime: pytest.fail("the jury tier must not be reached"),
            require_all_deterministic=True,
        )
    )


def test_a_close_receipts_every_gate_and_executes_none_that_cannot_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One close holds both properties at once.

    The wave carries three required blocking gates: two whose exit status
    can depend on the tree, and one carrying the shipped history-read
    argv that cannot. Driving the close end to end must leave one durable
    receipt per gate that actually ran -- each naming the gate, the argv,
    the exit status and the timestamp -- while the gate that cannot red
    refuses the close instead of scoring a pass, so it never joins the
    executed set and never mints a receipt claiming it proved anything.
    """
    work = _seeded_repo(tmp_path)
    (work / "payload.txt").write_text("gate target\n", encoding="utf-8")
    state_path = _live_tree(work, monkeypatch)
    wave_id = _wave().id

    falsifiable = [
        GateSpec(
            id="G-01",
            criterion_id="CR-01",
            kind="command_exit_zero",
            args={"argv": ["git", "log", "--max-count=1", "HEAD"], "scope": "all"},
            policy="block",
            cadence="every-wave",
            required=True,
        ),
        GateSpec(
            id="G-02",
            criterion_id="CR-01",
            kind="file_exists",
            args={"path": "payload.txt"},
            policy="block",
            cadence="every-wave",
            required=True,
        ),
    ]
    unfalsifiable = GateSpec(
        id="G-03",
        criterion_id="CR-01",
        kind="command_exit_zero",
        args={"argv": SHIPPED_HISTORY_READ_ARGV, "scope": "all"},
        policy="block",
        cadence="every-wave",
        required=True,
    )

    result = _score([*falsifiable, unfalsifiable], state_path=state_path, repo_root=work)

    assert result.status == "blocked"
    assert result.gate_id == "G-03"
    assert "cannot fail" in result.detail

    receipts = _read_receipts(state_path, scope_id=wave_id)
    by_gate = {str((row.metrics or {})["gate_id"]): row for row in receipts}
    assert set(by_gate) == {gate.id for gate in falsifiable}

    for gate in falsifiable:
        metrics = by_gate[gate.id].metrics or {}
        assert metrics["gate_id"] == gate.id
        assert metrics["criterion_id"] == "CR-01"
        assert metrics["exit_status"] != ""
        assert str(metrics["executed_at"]).startswith("20")
        recorded = shlex.split(str(metrics["argv"]))
        if gate.kind == "command_exit_zero":
            assert recorded == list(gate.args["argv"])
        assert unfalsifiable_argv(recorded) is None
    assert by_gate["G-01"].status == "pass"
    assert str((by_gate["G-01"].metrics or {})["exit_status"]) == "0"
