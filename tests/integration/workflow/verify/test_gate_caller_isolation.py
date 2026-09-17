"""The three remaining check callers run their checks in the sandboxed child.

The ``/audit`` skill, the ``/security-review`` skill and the ``eawf audit run
--checks`` verb each used to execute their checks in the calling process. A
check is routinely a test suite that drives eawf's own RPCs, so run there it
resolved the caller's live ledger and runtime directory and wrote them for
real. Each caller now hands its checks to
:func:`eawf.workflow.verify.sandboxed_checks.run_checks_out_of_process`,
whose child is bound to a throwaway copy of both.

Every test below drives one caller end to end with a check that rewrites
whatever ledger it resolves, then asserts two things at once: the live
``state.json`` is byte identical, and the check's own report proves the
mutation really happened somewhere else. A test that only asserted the first
half would pass for a check that never ran.

Every "live" tree here is a fixture built under ``tmp_path``; the repository's
own ``.ea`` is never read or written.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from typer.testing import CliRunner

from eawf.kernel.state.enums import ProjectStatus, ScopeKind
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.surfaces.cli.app import app
from eawf.workflow.skills.audit import AuditSkill
from eawf.workflow.skills.bodies.audit import AuditBody
from eawf.workflow.skills.engine import SkillContext, run_skill
from eawf.workflow.skills.security_review import SecurityReviewSkill

pytestmark = pytest.mark.integration

_PROBE_MODULE = "test_caller_probe.py"

#: ``pytest`` is the allowlisted vehicle for running observation code inside a
#: check, so the probe is a collected module whose import does the work.
_PROBE_ARGV = ["pytest", "-p", "no:cacheprovider", "-q", _PROBE_MODULE]

_RUNTIME_MARKER = "caller-probe.marker"

#: Resolves the ledger and runtime directory exactly as every eawf RPC does,
#: then writes both. The report goes to an absolute path handed in through an
#: ``LC_*`` variable, the one prefix family the check env-scrub floor keeps,
#: so the parent can read what the check reached after its sandbox is gone.
_PROBE_SOURCE = f"""
import json
import os
from pathlib import Path

from eawf.kernel.state.resolve import resolve_with_reason
from eawf.runtime.daemon.runtime_dir import runtime_dir

state_path, reason = resolve_with_reason(None)
resolved_runtime = runtime_dir()

mutated = False
if state_path.is_file():
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["dispatch_paused"] = True
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    mutated = True

resolved_runtime.mkdir(parents=True, exist_ok=True)
(resolved_runtime / {_RUNTIME_MARKER!r}).write_text("reached\\n", encoding="utf-8")

Path(os.environ["LC_CALLER_PROBE_REPORT"]).write_text(
    json.dumps(
        {{
            "cwd": os.getcwd(),
            "state_path": str(state_path),
            "reason": reason,
            "runtime_dir": str(resolved_runtime),
            "mutated": mutated,
        }}
    ),
    encoding="utf-8",
)


def test_probe_collected() -> None:
    pass
"""


@dataclass(frozen=True)
class LiveRepo:
    """A fixture repository standing in for an operator's live checkout.

    Attributes:
        root: Repository root; the checks' working directory.
        state_path: The live ``<root>/.ea/state.json``.
        runtime_dir: The live daemon runtime directory.
        report: Where the probe writes what it reached.
    """

    root: Path
    state_path: Path
    runtime_dir: Path
    report: Path

    def probe_report(self) -> dict[str, Any]:
        """Return the probe's report, failing when the check never ran."""
        assert self.report.is_file(), "the probe never ran; the test proved no isolation"
        return cast(dict[str, Any], json.loads(self.report.read_text(encoding="utf-8")))

    def assert_isolated(self, *, state_before: bytes) -> None:
        """Assert the check ran, mutated a copy, and left the live pair alone.

        Args:
            state_before: The live ledger bytes captured before the caller ran.
        """
        seen = self.probe_report()
        assert self.state_path.read_bytes() == state_before, "the check rewrote the live ledger"
        assert State.model_validate_json(self.state_path.read_bytes()).dispatch_paused is False
        assert not (self.runtime_dir / _RUNTIME_MARKER).exists(), (
            "the check wrote the live runtime directory"
        )
        assert seen["mutated"] is True, "the check mutated nothing; the test proved no isolation"
        assert seen["reason"] == "env"
        assert Path(seen["state_path"]) != self.state_path
        assert self.state_path.parent not in Path(seen["state_path"]).parents
        assert Path(seen["runtime_dir"]) != self.runtime_dir
        assert Path(seen["cwd"]).resolve() == self.root.resolve()


def _state_payload() -> dict[str, Any]:
    """Return a minimal valid ledger with dispatch running."""
    return {
        "schema_version": "1.0",
        "scope_kind": ScopeKind.REPO.value,
        "urn": "urn:eawf:v1:state:ISO",
        "updated_at": "2026-09-17T00:00:00+00:00",
        "project": Project(
            code="ISO",
            slug="iso",
            title="ISO",
            description=None,
            domains=["x"],
            default_branch="main",
            status=ProjectStatus.ACTIVE,
            repo_urn="urn:eawf:v1:repo:ISO",
        ).model_dump(mode="json"),
        "current": CurrentPointers(project_code="ISO").model_dump(mode="json"),
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
        "dispatch_paused": False,
    }


def _git(root: Path, *args: str) -> None:
    """Run a quiet ``git -C <root>`` command, raising on non-zero exit."""
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def live_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LiveRepo:
    """Build the live repo, point every resolver at it, and plant the probe."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    (root / _PROBE_MODULE).write_text(_PROBE_SOURCE, encoding="utf-8")

    state_path = root / ".ea" / "state.json"
    state_path.parent.mkdir()
    state_path.write_text(
        State.model_validate(_state_payload()).model_dump_json(),
        encoding="utf-8",
    )
    runtime = tmp_path / "live-runtime"
    runtime.mkdir()
    (runtime / "eawfd.pid").write_text("4242\n", encoding="utf-8")
    report = tmp_path / "probe-report.json"

    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_path.parent / "instrument-probe.json"))
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("LC_CALLER_PROBE_REPORT", str(report))
    monkeypatch.chdir(tmp_path)
    return LiveRepo(root=root, state_path=state_path, runtime_dir=runtime, report=report)


def _write_check_file(path: Path) -> Path:
    """Write a check-DSL document holding the probe as one command check."""
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "checks": [
                    {
                        "kind": "command_exit_zero",
                        "name": "probe",
                        "args": {"argv": _PROBE_ARGV, "scope": "all"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _skill_ctx(args: dict[str, Any]) -> SkillContext:
    """Return a skill context over the fixture project."""
    ctx = SkillContext(
        scope="urn:eawf:v1:state:ISO/P00",
        session="urn:eawf:v1:store:ISO/sessions/SES-1",
    )
    ctx.args = args
    return ctx


# ---- /audit --------------------------------------------------------------------


def test_audit_skill_state_mutating_check_leaves_live_state_byte_identical(
    live_repo: LiveRepo,
) -> None:
    """The ``/audit`` criterion check runs sandboxed at the repo root."""
    state_before = live_repo.state_path.read_bytes()
    ctx = _skill_ctx(
        {"criterion_checks": [{"criterion": "the probe observes", "argv": _PROBE_ARGV}]}
    )

    envelope = run_skill(AuditSkill(), ctx)

    body = AuditBody.model_validate(cast(dict[str, Any], envelope.body))
    assert [run.status for run in body.checks_run] == ["pass"], body.checks_run
    assert envelope.header.status == "ok"
    live_repo.assert_isolated(state_before=state_before)


# ---- /security-review ----------------------------------------------------------


def test_security_review_state_mutating_check_leaves_live_state_byte_identical(
    live_repo: LiveRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no ``cwd`` arg the checks run sandboxed in the process working directory."""
    spec = _write_check_file(live_repo.root.parent / "security.yaml")
    monkeypatch.chdir(live_repo.root)
    state_before = live_repo.state_path.read_bytes()

    envelope = run_skill(SecurityReviewSkill(), _skill_ctx({"spec_path": str(spec)}))

    body = cast(dict[str, Any], envelope.body)
    assert envelope.header.status == "ok", body
    assert [(finding["name"], finding["passed"]) for finding in body["findings"]] == [
        ("probe", True)
    ]
    live_repo.assert_isolated(state_before=state_before)


def test_security_review_explicit_cwd_wins_over_the_process_directory(
    live_repo: LiveRepo,
) -> None:
    """Boundary: a passed ``cwd`` is the one the sandboxed checks run in."""
    spec = _write_check_file(live_repo.root.parent / "security.yaml")
    state_before = live_repo.state_path.read_bytes()

    envelope = run_skill(
        SecurityReviewSkill(),
        _skill_ctx({"spec_path": str(spec), "cwd": str(live_repo.root)}),
    )

    assert envelope.header.status == "ok", envelope.body
    live_repo.assert_isolated(state_before=state_before)


def test_security_review_missing_cwd_fails_before_any_check_runs(
    live_repo: LiveRepo,
) -> None:
    """Error path: a ``cwd`` that is not a directory fails the skill and runs nothing."""
    spec = _write_check_file(live_repo.root.parent / "security.yaml")
    state_before = live_repo.state_path.read_bytes()

    envelope = run_skill(
        SecurityReviewSkill(),
        _skill_ctx({"spec_path": str(spec), "cwd": str(live_repo.root / "absent")}),
    )

    assert envelope.header.status == "failed"
    assert not live_repo.report.exists(), "a check ran against a cwd that does not exist"
    assert live_repo.state_path.read_bytes() == state_before


# ---- eawf audit run --checks ---------------------------------------------------


def test_audit_run_checks_leave_live_state_byte_identical(
    live_repo: LiveRepo,
) -> None:
    """The verb's checks run sandboxed before its own transaction.

    The verb records an audit row on success, which is a legitimate write, so
    the byte-identity assertion needs a run whose own write is refused: the
    audit id already exists. The checks still run first, which is the window
    in which an in-process check used to rewrite the live ledger.
    """
    runner = CliRunner()
    seeded = runner.invoke(app, ["audit", "run", "AUD-ISO", "--scope-id", "ISO"])
    assert seeded.exit_code == 0, seeded.output
    spec = _write_check_file(live_repo.root.parent / "audit.yaml")
    state_before = live_repo.state_path.read_bytes()

    result = runner.invoke(
        app,
        ["audit", "run", "AUD-ISO", "--scope-id", "ISO", "--checks", str(spec)],
    )

    assert result.exit_code != 0
    assert "already exists" in result.output
    live_repo.assert_isolated(state_before=state_before)


def test_audit_run_checks_record_the_sandboxed_result(live_repo: LiveRepo) -> None:
    """The child's result is what the verb records, and only the audit row lands."""
    spec = _write_check_file(live_repo.root.parent / "audit.yaml")
    before = json.loads(live_repo.state_path.read_bytes())

    result = CliRunner().invoke(
        app,
        ["audit", "run", "AUD-NEW", "--scope-id", "ISO", "--checks", str(spec)],
    )

    assert result.exit_code == 0, result.output
    after = json.loads(live_repo.state_path.read_bytes())
    recorded = after["audits"]["AUD-NEW"]["check_results"]
    assert [(row["name"], row["passed"]) for row in recorded] == [("probe", True)]
    assert after["dispatch_paused"] is False
    assert before.get("audits") in (None, {})
    assert live_repo.probe_report()["mutated"] is True
    assert not (live_repo.runtime_dir / _RUNTIME_MARKER).exists()
