"""A gate that selected no test is refused, not filed against the wave.

Three required blocking gates shipped in this repository naming a pytest target
that does not exist. Two carried a ``-k`` expression matching no test in the file
they named; one named two files that are not in the tree at all. pytest answers
the first with exit 5 (nothing collected) and the second with exit 4 (usage
error), and both are non-zero -- so the close scorer read them as the wave's own
criterion failing. That is a false accusation: no test ran, nothing was asserted
about the tree, and the row that needs repairing is the gate's argv.

So a vacuous run is refused the way an argv that cannot red is refused. The
parallel is exact and deliberate: an argv fixed at exit 0 proves nothing and an
argv that asserted nothing proves nothing, and in both cases the honest verdict
is that the gate is unusable rather than that the work is wrong.

The first group of tests establishes the exit statuses against the real
interpreter, with no stubbing, because the whole argument rests on what pytest
actually does. The second drives the production close scorer over a gate
carrying a shipped offender's argv. The third runs the three repaired argv and
holds each to collecting at least one test, since a repair that selects nothing
would reproduce the defect it replaces.

Every fixture tree is built under ``tmp_path``; nothing here writes the
repository's own ``.ea/``.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from eawf.kernel.spec.common import CriterionSpec, GateSpec, OracleTier
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import Wave
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.kernel.store.paths import store_path
from eawf.workflow.audit_dsl.models import CheckResult
from eawf.workflow.verify import oracle
from eawf.workflow.verify.gate_receipts import RECEIPT_MARKER
from eawf.workflow.verify.oracle import OracleResult, run_oracle
from eawf.workflow.verify.vacuous_gate import (
    PYTEST_EXIT_NO_TESTS_COLLECTED,
    PYTEST_EXIT_USAGE_ERROR,
    vacuous_gate_run,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[4]

#: The argv of ``P32-I01-W41`` gate ``G-02``, verbatim. The file exists; the
#: ``-k`` expression matches none of its 35 tests.
SHIPPED_EMPTY_SELECTOR_ARGV = [
    "uv",
    "run",
    "pytest",
    "tests/unit/kernel/migration/test_status_map_and_defaults.py",
    "-k",
    "every_enum_member",
    "-q",
]

#: The argv of ``P32-I01-W42`` gate ``G-02``, verbatim. Same shape, different
#: file: the selector matches none of its tests either.
SHIPPED_EMPTY_SELECTOR_ARGV_2 = [
    "uv",
    "run",
    "pytest",
    "tests/integration/test_packaging.py",
    "-k",
    "reds_on_growth",
    "-q",
]

#: The argv of ``P32-I01-W26`` gate ``G-02``, verbatim. The file it names has
#: never existed in the tree.
SHIPPED_MISSING_FILE_ARGV = [
    "uv",
    "run",
    "pytest",
    "tests/integration/workflow/verify/test_close_receipts.py",
    "-q",
]

#: The repaired argv for each defective gate, keyed by the gate it replaces.
#: Each names test node ids rather than a ``-k`` expression: a node id that
#: stops resolving fails loudly with exit 4 instead of quietly selecting
#: nothing, which is the failure mode these three shipped with.
REPAIRED_ARGV: dict[str, list[str]] = {
    "W41-G-02": [
        "uv",
        "run",
        "pytest",
        "tests/unit/kernel/migration/test_status_map_and_defaults.py::test_wave_status_map_keys_match_the_wave_status_enum_exactly",
        "tests/unit/kernel/migration/test_status_map_and_defaults.py::test_map_source_status_classifies_every_wave_status_member",
        "-q",
    ],
    "W42-G-02": [
        "uv",
        "run",
        "pytest",
        "tests/integration/test_packaging.py::test_wheel_size_ceiling_reds_on_an_oversize_measurement",
        "-q",
    ],
    "W26-G-01": [
        "uv",
        "run",
        "pytest",
        "tests/integration/workflow/verify/test_gate_falsifiability.py",
        "-q",
    ],
    "W26-G-02": [
        "uv",
        "run",
        "pytest",
        "tests/integration/workflow/verify/test_gate_falsifiability.py::test_credentials_signal_has_no_producer_and_is_never_required",
        "-q",
    ],
    "W26-G-03": [
        "uv",
        "run",
        "pytest",
        "tests/integration/workflow/verify/test_gate_falsifiability.py::test_a_close_receipts_every_gate_and_executes_none_that_cannot_red",
        "-q",
    ],
}


def _collect(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run *argv* in collect-only mode against the repository checkout."""
    return subprocess.run(
        [*argv, "--collect-only"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


# --- what pytest actually does ---------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [SHIPPED_EMPTY_SELECTOR_ARGV, SHIPPED_EMPTY_SELECTOR_ARGV_2],
    ids=["w41-g02", "w42-g02"],
)
def test_a_shipped_empty_selector_exits_with_the_nothing_collected_status(
    argv: list[str],
) -> None:
    """The selector deselects everything, so pytest exits 5 having asserted nothing."""
    completed = _collect(argv)

    assert completed.returncode == PYTEST_EXIT_NO_TESTS_COLLECTED
    assert "no tests collected" in completed.stdout


def test_a_shipped_missing_file_exits_with_the_usage_error_status() -> None:
    """The named file is absent, so pytest exits 4 without building a session."""
    completed = _collect(SHIPPED_MISSING_FILE_ARGV)

    assert completed.returncode == PYTEST_EXIT_USAGE_ERROR


# --- the classification ----------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "exit_status", "fragment"),
    [
        (SHIPPED_EMPTY_SELECTOR_ARGV, PYTEST_EXIT_NO_TESTS_COLLECTED, "selector matched no test"),
        (
            SHIPPED_EMPTY_SELECTOR_ARGV_2,
            PYTEST_EXIT_NO_TESTS_COLLECTED,
            "selector matched no test",
        ),
        (SHIPPED_MISSING_FILE_ARGV, PYTEST_EXIT_USAGE_ERROR, "did not resolve"),
    ],
    ids=["w41-g02", "w42-g02", "w26-g02"],
)
def test_vacuous_gate_run_classifies_each_shipped_offender(
    argv: list[str], exit_status: int, fragment: str
) -> None:
    """Each shipped offender is classified, with the reason an author can act on."""
    verdict = vacuous_gate_run(argv, exit_status=exit_status)

    assert verdict is not None
    assert fragment in verdict.reason
    assert verdict.repair
    assert fragment in verdict.message()


@pytest.mark.parametrize("exit_status", [0, 1, 2, 3, 6, 127])
def test_vacuous_gate_run_admits_every_status_that_reflects_an_observation(
    exit_status: int,
) -> None:
    """A status that does reflect a session is not reclassified.

    The rule must stay conservative: reclassifying a genuine red as an unusable
    gate would hide a real failure behind a harness complaint.
    """
    assert vacuous_gate_run(SHIPPED_EMPTY_SELECTOR_ARGV, exit_status=exit_status) is None


def test_vacuous_gate_run_on_a_missing_exit_status_is_none() -> None:
    """A runner that never reached a process has no status to classify."""
    assert vacuous_gate_run(SHIPPED_EMPTY_SELECTOR_ARGV, exit_status=None) is None


def test_vacuous_gate_run_on_an_empty_argv_is_none() -> None:
    """The empty boundary."""
    assert vacuous_gate_run([], exit_status=PYTEST_EXIT_NO_TESTS_COLLECTED) is None


def test_vacuous_gate_run_on_a_single_token_argv_is_none() -> None:
    """The single boundary: a bare wrapper head names no command to classify."""
    assert vacuous_gate_run(["uv"], exit_status=PYTEST_EXIT_NO_TESTS_COLLECTED) is None


def test_vacuous_gate_run_ignores_a_non_pytest_command() -> None:
    """Exit 5 means something else entirely outside pytest, so it is left alone."""
    assert (
        vacuous_gate_run(
            ["git", "log", "--max-count=1", "HEAD"],
            exit_status=PYTEST_EXIT_NO_TESTS_COLLECTED,
        )
        is None
    )


def test_vacuous_gate_run_sees_through_the_module_invocation_form() -> None:
    """``python -m pytest`` is the same command and gets the same classification."""
    verdict = vacuous_gate_run(
        ["python", "-m", "pytest", "tests", "-k", "nothing_matches_this"],
        exit_status=PYTEST_EXIT_NO_TESTS_COLLECTED,
    )

    assert verdict is not None


# --- the refusal, through the production close scorer ----------------------


def _criterion() -> CriterionSpec:
    """Build the deterministic criterion the scorer escalates."""
    return CriterionSpec(
        id="CR-01",
        text="a gate whose selector matches no test is refused, not scored",
        kind="contract",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=["G-02"],
        quality_dimension="functional_suitability",  # type: ignore[arg-type]
        measurable_signal="a measurable signal of at least twenty characters",
    )


def _gate(argv: list[str]) -> GateSpec:
    """Build the required blocking gate carrying *argv*."""
    return GateSpec(
        id="G-02",
        criterion_id="CR-01",
        kind="command_exit_zero",
        args={"argv": argv, "scope": "all"},
        policy="block",
        cadence="every-wave",
        required=True,
    )


def _wave() -> Wave:
    """Build the minimal valid wave the scorer reads for its jury fallthrough."""
    return Wave(
        id="P32-I01-W43",
        iter_id="P32-I01",
        title="refuse a gate argv that selected no test",
        status="claimed",  # type: ignore[arg-type]
        opened_at="2026-09-10T00:00:00Z",  # type: ignore[arg-type]
    )


def _live_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a throwaway ``state.json`` with the runtime dir redirected."""
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scope_kind": "repo",
                "urn": "urn:eawf:v1:state:ABC",
                "updated_at": "2026-09-10T00:00:00Z",
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
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(runtime))
    return state_path


def _score(gate: GateSpec, *, state_path: Path) -> OracleResult:
    """Drive one gate through the production close scorer's advisory lane."""
    return asyncio.run(
        run_oracle(
            _criterion(),
            [gate],
            wave=_wave(),
            state=object(),  # type: ignore[arg-type]
            state_path=state_path,
            events_path=state_path.parent / "event.jsonl",
            repo_root=REPO_ROOT,
            spawn_factory=lambda _runtime: pytest.fail("the jury tier must not be reached"),
            require_all_deterministic=True,
        )
    )


def _receipts(state_path: Path) -> list[EvidenceRecord]:
    """Return the gate-execution receipts the scorer persisted."""
    path = store_path(state_path, StoreKind.EVIDENCE)
    if not path.is_file():
        return []
    rows: list[EvidenceRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = EvidenceRecord.model_validate(Envelope.model_validate_json(line).payload)
        if (record.metrics or {}).get("receipt") == RECEIPT_MARKER:
            rows.append(record)
    return rows


def _stub_runner(monkeypatch: pytest.MonkeyPatch, *, argv: list[str], exit_status: int) -> None:
    """Hand the scorer one completed run with *exit_status*, sparing the spawn.

    The property under test is how the scorer CLASSIFIES a completed run, and
    the statuses the stub reports are the ones the tests above measured from the
    real interpreter. Spawning a second pytest session per case would re-measure
    what is already pinned and pay a sandbox snapshot for it.
    """
    monkeypatch.setattr(
        oracle,
        "run_checks_out_of_process",
        lambda specs, **_kwargs: [
            CheckResult(
                name=specs[0].name,
                kind=specs[0].kind,
                passed=False,
                status="fail",
                details="1 error",
                argv=argv,
                exit_status=exit_status,
                started_at="2026-09-10T00:00:00Z",  # type: ignore[arg-type]
                ended_at="2026-09-10T00:00:01Z",  # type: ignore[arg-type]
            )
        ],
    )


@pytest.mark.parametrize(
    ("argv", "exit_status"),
    [
        (SHIPPED_EMPTY_SELECTOR_ARGV, PYTEST_EXIT_NO_TESTS_COLLECTED),
        (SHIPPED_MISSING_FILE_ARGV, PYTEST_EXIT_USAGE_ERROR),
    ],
    ids=["empty-selector", "missing-file"],
)
def test_the_scorer_refuses_a_vacuous_run_instead_of_failing_the_criterion(
    argv: list[str],
    exit_status: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal is ``blocked``, naming the argv -- never ``fail``.

    ``fail`` is the word for the wave's work being rejected, and a gate that
    asserted nothing has not rejected anything. The status word is the whole
    point of the classification: it decides whether the operator is sent to
    repair the code or the gate row.
    """
    state_path = _live_tree(tmp_path, monkeypatch)
    _stub_runner(monkeypatch, argv=argv, exit_status=exit_status)

    result = _score(_gate(argv), state_path=state_path)

    assert result.status == "blocked"
    assert result.gate_id == "G-02"
    assert "observed nothing" in result.detail


def test_a_vacuous_run_is_receipted_as_blocked_not_as_a_tree_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receipt records the run, and records that it proved nothing."""
    state_path = _live_tree(tmp_path, monkeypatch)
    _stub_runner(
        monkeypatch,
        argv=SHIPPED_EMPTY_SELECTOR_ARGV,
        exit_status=PYTEST_EXIT_NO_TESTS_COLLECTED,
    )

    _score(_gate(SHIPPED_EMPTY_SELECTOR_ARGV), state_path=state_path)

    receipts = _receipts(state_path)
    assert len(receipts) == 1
    assert receipts[0].status == "blocked"
    assert str((receipts[0].metrics or {})["exit_status"]) == str(PYTEST_EXIT_NO_TESTS_COLLECTED)


def test_a_non_blocking_vacuous_gate_is_dropped_rather_than_credited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verdict the close may ignore is dropped, exactly as an unfalsifiable one is.

    Dropped means the gate credits the criterion with nothing: the result carries
    no ``gate_id``, so the criterion falls through to the verdict tier as though
    the gate had never been attached. What it must NOT do is contribute a
    deterministic pass off a run that asserted nothing.
    """
    state_path = _live_tree(tmp_path, monkeypatch)
    _stub_runner(
        monkeypatch,
        argv=SHIPPED_EMPTY_SELECTOR_ARGV,
        exit_status=PYTEST_EXIT_NO_TESTS_COLLECTED,
    )
    gate = GateSpec(
        id="G-02",
        criterion_id="CR-01",
        kind="command_exit_zero",
        args={"argv": SHIPPED_EMPTY_SELECTOR_ARGV, "scope": "all"},
        policy="warn",
        cadence="every-wave",
        required=True,
    )

    result = _score(gate, state_path=state_path)

    assert result.gate_id is None
    assert int(result.tier) == int(OracleTier.T7_JURY)
    assert _receipts(state_path)[0].status == "blocked"


def test_a_genuine_red_still_fails_the_criterion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gate that ran tests and saw one fail still refuses as ``fail``.

    The guard on the other side of the classification: turning every non-zero
    exit into a harness complaint would make the close gate unable to reject
    work at all.
    """
    state_path = _live_tree(tmp_path, monkeypatch)
    _stub_runner(monkeypatch, argv=SHIPPED_EMPTY_SELECTOR_ARGV, exit_status=1)

    result = _score(_gate(SHIPPED_EMPTY_SELECTOR_ARGV), state_path=state_path)

    assert result.status == "fail"
    assert result.gate_id == "G-02"


# --- the repaired argv -----------------------------------------------------


@pytest.mark.parametrize("gate_key", sorted(REPAIRED_ARGV))
def test_each_repaired_argv_selects_at_least_one_test(gate_key: str) -> None:
    """A repair that collected nothing would reproduce the defect it replaces."""
    completed = _collect(REPAIRED_ARGV[gate_key])

    assert completed.returncode == 0, completed.stdout[-2000:]
    assert " tests collected" in completed.stdout or " test collected" in completed.stdout


@pytest.mark.parametrize("gate_key", sorted(REPAIRED_ARGV))
def test_no_repaired_argv_classifies_as_vacuous_on_a_clean_collect(gate_key: str) -> None:
    """Exit 0 from a repaired argv is an observation, so it is never reclassified."""
    assert vacuous_gate_run(REPAIRED_ARGV[gate_key], exit_status=0) is None


def test_every_defective_gate_has_a_repaired_argv() -> None:
    """The repair set covers each gate the three waves shipped."""
    assert set(REPAIRED_ARGV) == {
        "W41-G-02",
        "W42-G-02",
        "W26-G-01",
        "W26-G-02",
        "W26-G-03",
    }


@pytest.mark.parametrize("gate_key", sorted(REPAIRED_ARGV))
def test_each_repaired_argv_is_accepted_as_a_gate_row(gate_key: str) -> None:
    """Each repair passes the L0 argv policy, so the operator can apply it as-is."""
    gate = GateSpec(
        id="G-02",
        criterion_id="CR-01",
        kind="command_exit_zero",
        args={"argv": REPAIRED_ARGV[gate_key], "scope": "touched"},
        policy="block",
        cadence="every-wave",
        required=True,
    )

    assert gate.args["argv"] == REPAIRED_ARGV[gate_key]
