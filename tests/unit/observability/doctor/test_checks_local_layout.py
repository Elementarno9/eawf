"""The ``local-layout`` doctor check names each hand-made entry at the top of ``.ea/local``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner  # noqa: EAWF024 the contract is the doctor JSON envelope

from eawf.kernel.layout import REFLECT_DIRNAME
from eawf.kernel.store.paths import LEDGER_DIRNAME, STAGED_DIRNAME, STATUS_PROJECTION_FILENAME
from eawf.observability.doctor.checks_local_layout import CHECK_NAME, check_local_layout
from eawf.platform.install.canary import PROVISION_RECORD_LOCATOR, RUNTIME_DIR_PREFIX
from eawf.platform.install.instrument_probe import PROBE_VERSION, ProbeReport, ProbeResult
from eawf.platform.local_tidy import MACHINE_NAMES_07, MACHINE_PREFIXES_07, is_known_local_name
from eawf.platform.rules.staleness import REPORTED_SESSIONS_PATH
from eawf.runtime.daemon.runtime_certifier import DECOY_SCRATCH_PREFIX
from eawf.runtime.session.transcript_index import TRANSCRIPT_INDEX_FILENAME
from eawf.surfaces.cli.app import app

_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "local_layout"

#: The top-level names of the 2026-11-10 manifest that no eawf code writes.
_HAND_MADE_2026_11_10 = {
    "_archive",
    "2026-09-16-dev2-publication",
    "2026-10-11-backlog-review",
    "2026-10-30-mls0103-tasks.md",
    "2026-11-06-mls0107-execution-log.md",
    "dispatch",
    "issues",
    "patches",
    "plans",
    "reflect-review",
    "reviews",
}


def _finding(name: str) -> str:
    return f"local-layout: .ea/local/{name} is not a known folder; run eawf local tidy --dry-run"


def _details(workspace: Path) -> set[str]:
    return {
        result.detail or ""
        for result in check_local_layout(workspace=workspace)
        if result.status == "warn"
    }


def _lay(local: Path, rel: str) -> None:
    """Create *rel* below *local*: a folder for a name without a suffix, else a file."""
    path = local / rel
    if Path(rel).suffix:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
    else:
        path.mkdir(parents=True, exist_ok=True)


@pytest.fixture
def doctor_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep host daemons, probes, global config and temp dirs out of a doctor run."""
    from eawf.observability.doctor.repair import DoctorRepairPlan
    from eawf.runtime.daemon.service_install import SupervisedAgentReport

    report = SupervisedAgentReport(
        supervisor="none",
        label="",
        installed=False,
        loaded=False,
        program=None,
        drift=False,
        rival_pid=None,
    )
    monkeypatch.setattr(
        "eawf.runtime.daemon.service_install.detect_supervised_agent", lambda *_a, **_k: report
    )
    monkeypatch.setattr(
        "eawf.kernel.config.layered.global_config_path", lambda: tmp_path / "global.yaml"
    )
    monkeypatch.setattr(
        "eawf.observability.doctor.checks._probe_running_daemon_version", lambda: None
    )
    monkeypatch.setattr("eawf.observability.doctor.daemon_strays.find_stray_daemons", lambda: [])
    monkeypatch.setattr(
        "eawf.observability.doctor.checks.probe",
        lambda *_a, **_k: ProbeReport(
            probe_version=PROBE_VERSION,
            profile_ids=["core"],
            results=[ProbeResult(name="git", kind="hard", status="ok", path="/x/git")],
        ),
    )
    monkeypatch.setattr(
        "eawf.observability.doctor.repair.build_repair_plan",
        lambda workspace: DoctorRepairPlan(
            workspace=str(workspace),
            preview_digest=f"sha256:{'0' * 64}",
            actions=[],
            rerun_command="eawf doctor --fix --yes",
        ),
    )
    host_temp = tmp_path / "host-tmp"
    host_temp.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(host_temp))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "host-xdg"))
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(tmp_path / "eawfd"))
    workspace = tmp_path / "repo"
    (workspace / ".ea").mkdir(parents=True)
    (workspace / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(workspace)
    return workspace


def test_dispatch_finding(doctor_sandbox: Path) -> None:
    dispatch = doctor_sandbox / ".ea" / "local" / "dispatch"
    dispatch.mkdir(parents=True)
    (dispatch / "x.md").write_text("# notes\n", encoding="utf-8")

    result = CliRunner().invoke(app, ["--json", "-w", str(doctor_sandbox), "doctor"])

    assert result.exit_code == 0, result.output
    rows = [row for row in json.loads(result.output)["checks"] if row["name"] == CHECK_NAME]
    assert rows == [
        {"name": CHECK_NAME, "status": "warn", "detail": _finding("dispatch"), "provenance": None}
    ]


def test_fire_proof_fixture(tmp_path: Path) -> None:
    manifest = (_FIXTURES / "moved-2026-11-10.tsv").read_text(encoding="utf-8").splitlines()
    moved = tmp_path / "moved"
    for line in manifest:
        _lay(moved / ".ea" / "local", line.split("\t")[0])
    assert _details(moved) == {_finding(name) for name in _HAND_MADE_2026_11_10}

    machine = tmp_path / "machine"
    names = (_FIXTURES / "machine-names-2026-11-10.txt").read_text(encoding="utf-8").split()
    for name in names:
        _lay(machine / ".ea" / "local", name)
    results = check_local_layout(workspace=machine)
    assert [(result.status, result.detail) for result in results] == [
        ("ok", ".ea/local holds known folders")
    ]


def test_one_finding_per_entry_in_name_order(tmp_path: Path) -> None:
    local = tmp_path / ".ea" / "local"
    for rel in ("zeta", "alpha.md", "alpha.md.lock", "research"):
        _lay(local, rel)
    results = check_local_layout(workspace=tmp_path)
    assert [result.detail for result in results] == [
        _finding("alpha.md"),
        _finding("alpha.md.lock"),
        _finding("zeta"),
    ]
    assert {result.name for result in results} == {CHECK_NAME}


@pytest.mark.parametrize("make_local", [False, True], ids=["no-local", "empty-local"])
def test_no_entries_is_ok(tmp_path: Path, make_local: bool) -> None:
    (tmp_path / ".ea").mkdir()
    if make_local:
        (tmp_path / ".ea" / "local").mkdir()
    assert [r.status for r in check_local_layout(workspace=tmp_path)] == ["ok"]


def test_no_anchor_is_ok() -> None:
    results = check_local_layout(workspace=None)
    assert [(r.status, r.detail) for r in results] == [("ok", "no workspace anchor")]


def test_a_string_workspace_is_refused() -> None:
    with pytest.raises(TypeError):
        check_local_layout(workspace="repo")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("name", "known"),
    [
        ("", False),
        (".lock", False),
        ("research", True),
        ("research.lock", True),
        ("runtime", True),
        ("cache", True),
        ("operation.jsonl", True),
        ("operation.jsonl.lock", True),
        ("eawf-canary-", True),
        ("eawf-canary-x1", True),
        ("canary-credential-ab", True),
        ("eawf-canary", False),
        ("Research", False),
        ("research ", False),
        ("x" * 255, False),
    ],
)
def test_known_name_boundaries(name: str, known: bool) -> None:
    assert is_known_local_name(name) is known


def test_a_legacy_reflect_folder_is_flagged_with_its_move(tmp_path: Path) -> None:
    _lay(tmp_path / ".ea" / "local", "reflect/history-index.sqlite")
    assert REFLECT_DIRNAME not in MACHINE_NAMES_07
    assert _details(tmp_path) == {
        "local-layout: .ea/local/reflect holds reflect data of an earlier version that nothing"
        " reads; run eawf local tidy to move it to .ea/local/reports/reflect"
    }


def test_a_reflect_file_is_flagged_as_unknown(tmp_path: Path) -> None:
    _lay(tmp_path / ".ea" / "local", "research")
    (tmp_path / ".ea" / "local" / "reflect").write_text("x\n", encoding="utf-8")
    assert _details(tmp_path) == {_finding("reflect")}


def test_machine_names_match_their_writers() -> None:
    writers = {
        LEDGER_DIRNAME,
        STAGED_DIRNAME,
        STATUS_PROJECTION_FILENAME,
        TRANSCRIPT_INDEX_FILENAME,
        Path(REPORTED_SESSIONS_PATH).name,
        Path(PROVISION_RECORD_LOCATOR).name,
    }
    assert writers <= MACHINE_NAMES_07
    assert set(MACHINE_PREFIXES_07) == {RUNTIME_DIR_PREFIX, DECOY_SCRATCH_PREFIX}
