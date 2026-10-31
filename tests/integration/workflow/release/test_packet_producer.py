"""The packet producer carries a scratch repository from init to a printed transcript.

The runtime is a deterministic stand-in: the shipped daemon with only the claude
launcher replaced, certified the way the certifier would record it. Every other
step -- ``eawf init --quick``, the planning verbs, ``eawf run dispatch`` with its
budget, the cherry-pick, ``eawf batch close-landed`` and ``eawf run transcript``
-- runs through the real CLI, and the receipt lands in the checkout's local store.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

from eawf._version import __version__
from eawf.workflow.release import packet_producer
from eawf.workflow.release.packet_producer import (
    PacketStepError,
    PacketWorld,
    run_packet,
)
from eawf.workflow.release.packet_run import (
    PacketOrigin,
    PacketRuntimeId,
    PacketStep,
    packet_run_dir,
    read_packet_receipts,
)

pytestmark = pytest.mark.integration

#: The stand-in daemon, beside this file.
STUB_DAEMON: Final = Path(__file__).resolve().with_name("_packet_stub_daemon.py")


class StubRuntime:
    """A claude-code runtime whose worker is the smoke's deterministic stub."""

    runtime_id: PacketRuntimeId = "claude-code"

    def version(self) -> str:
        from tests.integration.runtime.daemon.test_task_dispatch import CLAUDE_VERSION

        return CLAUDE_VERSION

    def daemon_argv(self) -> tuple[str, ...]:
        return (sys.executable, str(STUB_DAEMON))

    def certify(self, world: PacketWorld, task: str) -> None:
        """File the certification a passed canary records, semantic tools included."""
        from eawf.kernel.runtime.certification import CapabilityCertification
        from eawf.workflow.evidence.machine_certification import append_machine_certification
        from tests.integration.runtime.daemon.test_task_dispatch import (
            CLAUDE_VERSION,
            certification,
        )

        certified = certification("claude-code", CLAUDE_VERSION)
        (first, *_) = certified.capabilities
        semantic = CapabilityCertification.model_validate(
            {**first.model_dump(mode="json"), "capability_id": "semantic_tools"}
        )
        append_machine_certification(
            world.repo / ".ea" / "state.json",
            certified.model_copy(update={"capabilities": (*certified.capabilities, semantic)}),
        )


class RefusingRuntime(StubRuntime):
    """A runtime whose certification is refused, so the packet stops at certify."""

    def certify(self, world: PacketWorld, task: str) -> None:
        raise PacketStepError(world.step, "the canary escaped containment")


class CrashingRuntime(StubRuntime):
    """A runtime whose certification raises an exception no step anticipates."""

    def certify(self, world: PacketWorld, task: str) -> None:
        raise KeyError("run_key")


class NoDaemonRuntime(StubRuntime):
    """A runtime that names no daemon, so the packet stops at init before starting one."""

    def daemon_argv(self) -> tuple[str, ...]:
        raise PacketStepError(PacketStep.INIT, "no daemon")


class UnreadableDaemonRuntime(StubRuntime):
    """A runtime whose daemon command cannot be read from disk."""

    def daemon_argv(self) -> tuple[str, ...]:
        # Joined at run time so the source itself names no home directory.
        raise FileNotFoundError(2, "No such file or directory", "/".join(("", "home", "op", "d")))


def _head(path: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _checkout(path: Path) -> Path:
    """Return a git checkout with one commit, standing in for the eawf source."""
    path.mkdir()
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    for argv in (
        ("init", "-q", "-b", "main"),
        (
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "start",
        ),
    ):
        subprocess.run(["git", *argv], cwd=path, check=True, capture_output=True, env=env)
    return path


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the operator's HOME, and with it their eawf user config, out of the packet."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return tmp_path


@pytest.mark.parametrize("origin", list(PacketOrigin), ids=lambda origin: origin.value)
def test_run_packet_passes_a_packet_end_to_end(isolated: Path, origin: PacketOrigin) -> None:
    checkout = _checkout(isolated / "checkout")

    receipt = run_packet(
        checkout,
        isolated / "scratch",
        runtime=StubRuntime(),
        origin=origin,
        budget=600_000,
    )

    assert receipt.outcome == "pass", receipt.failure
    assert receipt.origin is origin
    assert receipt.eawf_version == __version__
    assert receipt.eawf_commit == _head(packet_producer._PACKAGE_DIR)
    assert receipt.budget_tokens == 600_000
    assert read_packet_receipts(checkout) == (receipt,)
    evidence = packet_run_dir(checkout) / f"{receipt.evidence_digest.removeprefix('sha256:')}.json"
    log = json.loads(evidence.read_text(encoding="utf-8"))
    commands = [entry["argv"][:3] for entry in log if "argv" in entry]
    assert ["eawf", "run", "dispatch"] in commands
    assert ["eawf", "batch", "close-landed"] in commands
    assert commands[-1] == ["eawf", "run", "transcript"]
    notes = isolated / "scratch" / "packet" / "notes" / "1.txt"
    assert notes.read_text(encoding="utf-8") == "note 1\n"


def test_run_packet_records_the_step_a_refused_certification_stops_at(isolated: Path) -> None:
    checkout = _checkout(isolated / "checkout")

    receipt = run_packet(
        checkout, isolated / "scratch", runtime=RefusingRuntime(), origin=PacketOrigin.ADOPTED
    )

    assert receipt.outcome == "fail"
    assert receipt.failed_step is PacketStep.CERTIFY
    assert receipt.failure == "the canary escaped containment"
    history = subprocess.run(
        ["git", "log", "--format=%s"],
        cwd=isolated / "scratch" / "packet",
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\n")
    assert history[:3] == [
        "test: add the note test",
        "feat: add the package",
        "chore: start the project",
    ]


@pytest.mark.parametrize(
    ("budget", "scratch", "message"),
    [
        (0, "scratch", "packet_budget_invalid"),
        (1, "checkout/inside", "packet_scratch_inside_checkout"),
        (1, "occupied", "packet_scratch_not_empty"),
    ],
    ids=["zero-budget", "scratch-inside-checkout", "scratch-not-empty"],
)
def test_run_packet_refuses_before_running(
    isolated: Path, budget: int, scratch: str, message: str
) -> None:
    checkout = _checkout(isolated / "checkout")
    (isolated / "occupied").mkdir()
    (isolated / "occupied" / "file").touch()

    with pytest.raises(ValueError, match=message):
        run_packet(
            checkout,
            isolated / scratch,
            runtime=StubRuntime(),
            origin=PacketOrigin.BORN_NATIVE,
            budget=budget,
        )
    assert read_packet_receipts(checkout) == ()


def test_run_packet_refuses_a_package_outside_a_checkout(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = _checkout(isolated / "checkout")
    (isolated / "site-packages" / "eawf").mkdir(parents=True)
    monkeypatch.setattr(packet_producer, "_PACKAGE_DIR", isolated / "site-packages" / "eawf")

    with pytest.raises(ValueError, match="packet_source_unknown"):
        run_packet(
            checkout, isolated / "scratch", runtime=StubRuntime(), origin=PacketOrigin.ADOPTED
        )
    assert read_packet_receipts(checkout) == ()


@pytest.mark.parametrize(
    ("edit", "dirty"),
    [(None, False), ("modified", True), ("untracked", True), ("outside", False)],
    ids=["clean", "modified-module", "untracked-module", "edit-outside-the-package"],
)
def test_run_packet_records_the_commit_and_dirt_of_the_package_it_runs(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, edit: str | None, dirty: bool
) -> None:
    checkout = _checkout(isolated / "checkout")
    source = _checkout(isolated / "source")
    package = source / "src" / "eawf"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    for argv in (
        ("add", "-A"),
        ("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "package"),
    ):
        subprocess.run(["git", *argv], cwd=source, check=True, capture_output=True, env=env)
    if edit == "modified":
        (package / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    elif edit == "untracked":
        (package / "new.py").write_text("", encoding="utf-8")
    elif edit == "outside":
        (source / "notes.txt").write_text("scratch\n", encoding="utf-8")
    monkeypatch.setattr(packet_producer, "_PACKAGE_DIR", package)

    receipt = run_packet(
        checkout, isolated / "scratch", runtime=NoDaemonRuntime(), origin=PacketOrigin.BORN_NATIVE
    )

    assert receipt.eawf_commit == _head(source)
    assert receipt.source_dirty is dirty
    assert receipt.failed_step is PacketStep.INIT


@pytest.mark.parametrize(
    ("runtime", "error", "step", "failure"),
    [
        (CrashingRuntime(), KeyError, PacketStep.CERTIFY, "KeyError: 'run_key'"),
        (
            UnreadableDaemonRuntime(),
            FileNotFoundError,
            PacketStep.INIT,
            "FileNotFoundError: [Errno 2] No such file or directory: '<local-path>",
        ),
    ],
    ids=["key-error-mid-daemon", "os-error-before-daemon"],
)
def test_run_packet_files_a_failed_receipt_for_an_unexpected_exception(
    isolated: Path,
    runtime: StubRuntime,
    error: type[Exception],
    step: PacketStep,
    failure: str,
) -> None:
    checkout = _checkout(isolated / "checkout")

    with pytest.raises(error):
        run_packet(checkout, isolated / "scratch", runtime=runtime, origin=PacketOrigin.ADOPTED)

    (receipt,) = read_packet_receipts(checkout)
    assert receipt.outcome == "fail"
    assert receipt.failed_step is step
    assert receipt.failure == failure
    evidence = packet_run_dir(checkout) / f"{receipt.evidence_digest.removeprefix('sha256:')}.json"
    log = json.loads(evidence.read_text(encoding="utf-8"))
    assert log[-1]["crash"] == failure


def test_release_packet_run_refuses_a_scratch_inside_the_checkout(isolated: Path) -> None:
    checkout = _checkout(isolated / "checkout")

    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "eawf",
            "--json",
            "--no-input",
            "release",
            "packet-run",
            "--runtime",
            "codex",
            "--origin",
            "adopted",
            "--scratch",
            "inside",
        ],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode != 0
    assert "packet_scratch_inside_checkout" in done.stdout + done.stderr
    assert read_packet_receipts(checkout) == ()
