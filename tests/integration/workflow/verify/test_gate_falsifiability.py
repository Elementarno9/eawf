"""Gates that cannot red, and the receipt every executed gate leaves.

Three required blocking gates shipped in this repository and passed on
every tree they ever ran against. Two are argv shapes -- a bare history
read and a release-tag preview whose sweep hangs off a flag the argv
omits -- and the tests here run both against a tree that a seeded defect
has already broken, to establish that they exit 0 anyway and that their
repaired forms exit non-zero on the same tree. The third, the
publication-credentials probe, is sunset: no target on this train holds
a named handle, so the probe could only ever return its own no-op pass.

Every fixture tree is built under ``tmp_path``; nothing here reads or
writes the repository's own ``.ea/``.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.kernel.spec.common import GateSpec
from eawf.kernel.spec.falsifiability import unfalsifiable_argv
from eawf.kernel.spec.promotion import SpecPromoteValidationError, validate_argv_gates
from eawf.kernel.spec.release_config import load_release_config
from eawf.surfaces.cli.app import app
from eawf.workflow.release.train import V07_TRAIN, checkpoint_config_yaml
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
