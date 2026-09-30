"""REL-021: on an epoch-2 tree every epoch-1 mutation refuses with typed migration guidance.

The repository is laid down as an epoch-1 tree from a committed state, then
marked the way a cutover leaves it. Each refused verb must move nothing, exit in the
validation bucket with the stable ``legacy_operation_removed`` code and the
``LegacyOperationRemoved`` kind, and name what to run instead: the epoch-2 verb
that replaces it, or the fact that it retired. Epoch-1 reads keep working.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Final

import click
import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.canary import GENERATIONS_DIRNAME, MARKER_FILENAME
from eawf.kernel.state.io import LEGACY_OPERATION_REMOVED
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli import exit_codes, flag_day_gate
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

runner = CliRunner()


def _invoke(*argv: str) -> tuple[int, str]:
    result = runner.invoke(app, list(argv))
    return result.exit_code, result.output


#: A committed epoch-1 state holding one phase, one iter and one wave.
EPOCH1_STATE: Final = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "states"
    / "valid"
    / "03-phase-iter-wave-active.json"
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An epoch-1 repository holding one wave, laid down from a committed state.

    The flag day refuses every epoch-1 verb on a plain epoch-1 tree, so the
    tree is copied into place rather than built through the CLI.
    """
    root = tmp_path / "repo"
    state_path = root / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    shutil.copyfile(EPOCH1_STATE, state_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(tmp_path / "registry.json"))
    return root


def _mark(repo: Path) -> None:
    """Write the epoch marker, as the cutover's last step does."""
    marker = repo / ".ea" / GENERATIONS_DIRNAME / MARKER_FILENAME
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"{}")


def _state_digest(repo: Path) -> str:
    return hashlib.sha256((repo / ".ea" / "state.json").read_bytes()).hexdigest()


def test_rel_021_a_refused_wave_plan_names_task_create(repo: Path) -> None:
    _mark(repo)
    before = _state_digest(repo)
    code, output = _invoke(
        "wave",
        "plan",
        "P01-I01",
        "--id",
        "P01-I01-W02",
        "--title",
        "two",
        "--files",
        "src/",
        "--effort-bucket",
        "M",
    )
    assert code == exit_codes.VALIDATION_ERROR, output
    assert LEGACY_OPERATION_REMOVED in output
    assert "run `eawf task create` instead" in output
    assert f"kind: {cli_errors.LEGACY_OPERATION_REMOVED_KIND}" in output
    assert _state_digest(repo) == before


def test_rel_021_a_refused_phase_open_names_milestone_create(repo: Path) -> None:
    _mark(repo)
    before = _state_digest(repo)
    code, output = _invoke("phase", "open", "--auto", "--title", "y")
    assert code == exit_codes.VALIDATION_ERROR, output
    assert "run `eawf milestone create` instead" in output
    assert _state_digest(repo) == before


def test_rel_021_the_json_envelope_carries_the_typed_refusal(repo: Path) -> None:
    _mark(repo)
    code, output = _invoke(
        "--json",
        "wave",
        "plan",
        "P01-I01",
        "--id",
        "P01-I01-W02",
        "--title",
        "two",
        "--files",
        "src/",
        "--effort-bucket",
        "M",
    )
    assert code == exit_codes.VALIDATION_ERROR, output
    envelope = json.loads(output)
    assert envelope["error"] == "ValidationError"
    assert envelope["exit_code"] == exit_codes.VALIDATION_ERROR
    assert envelope["data"]["kind"] == cli_errors.LEGACY_OPERATION_REMOVED_KIND
    assert envelope["message"].startswith(LEGACY_OPERATION_REMOVED)
    assert envelope["message"].endswith(
        "run `eawf task create` instead, the epoch-2 verb that replaces `eawf wave plan`"
    )
    assert (
        envelope["suggested_next_step"]
        == cli_errors._KIND_HINTS[cli_errors.LEGACY_OPERATION_REMOVED_KIND]
    )


@pytest.mark.parametrize("argv", [("wave", "graph"), ("roadmap", "show"), ("state", "show")])
def test_rel_021_epoch1_reads_keep_working_on_an_epoch2_tree(
    repo: Path, argv: tuple[str, ...]
) -> None:
    _mark(repo)
    code, output = _invoke(*argv)
    assert code == exit_codes.OK, output
    assert LEGACY_OPERATION_REMOVED not in output


def test_rel_021_an_unmarked_tree_refuses_the_epoch1_write_too(repo: Path) -> None:
    """After the flag day a plain epoch-1 tree is read-only until it migrates."""
    before = _state_digest(repo)
    code, output = _invoke(
        "wave",
        "plan",
        "P01-I01",
        "--id",
        "P01-I01-W02",
        "--title",
        "two",
        "--files",
        "src/",
        "--effort-bucket",
        "M",
    )
    assert code == exit_codes.ATTACH_FAILURE, output
    assert f"kind: {flag_day_gate.MIGRATION_REQUIRED_KIND}" in output
    assert "eawf migrate epoch2" in output
    assert _state_digest(repo) == before


def _rendered(message: str, command_path: str | None) -> cli_errors.ErrorEnvelope:
    """Build the envelope of a daemon-mapped validation error under ``command_path``."""
    err = cli_errors.cli_error_for_rpc(cli_errors.RPC_VALIDATION_FAILED, message)
    if command_path is None:
        return cli_errors.build_envelope(err)
    with click.Context(click.Command("leaf"), info_name=command_path):
        return cli_errors.build_envelope(err)


def test_rel_021_a_daemon_refusal_gets_the_replacement_of_the_running_verb() -> None:
    message = f"validation_failed: {LEGACY_OPERATION_REMOVED}: .ea is frozen"
    envelope = _rendered(message, "eawf wave claim")
    assert envelope.message == (
        f"{message}; run `eawf task claim` instead, the epoch-2 verb that replaces "
        "`eawf wave claim`"
    )
    assert envelope.data["kind"] == cli_errors.LEGACY_OPERATION_REMOVED_KIND
    assert envelope.exit_code == exit_codes.VALIDATION_ERROR


def test_rel_021_a_retired_verb_refusal_says_nothing_replaces_it() -> None:
    envelope = _rendered(f"{LEGACY_OPERATION_REMOVED}: frozen", "eawf wave release")
    assert envelope.message.endswith(
        "`eawf wave release` retired at the flag day and no epoch-2 verb replaces it"
    )


def test_rel_021_a_refusal_outside_any_command_points_at_the_native_nouns() -> None:
    envelope = _rendered(f"{LEGACY_OPERATION_REMOVED}: frozen", None)
    assert envelope.message.endswith("(eawf milestone|batch|task|run --help)")


def test_rel_021_an_ordinary_validation_error_is_left_alone() -> None:
    envelope = _rendered("validation_failed: wave not found", "eawf wave claim")
    assert envelope.message == "validation_failed: wave not found"
    assert "kind" not in envelope.data
