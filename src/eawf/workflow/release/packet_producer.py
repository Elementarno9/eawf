"""Run one perfect-realization packet and file its receipt.

A packet run is what a newcomer does with eawf on a real runtime, driven
through the shipped CLI and daemon in a scratch repository outside the
checkout: initialise the tree, plan one Milestone with one Task, dispatch the
Task as a native Run, land the Run's commit so the Batch closes on the
Task's proven criterion, and print the Run's transcript. The repository is
born native (eawf first, project after) or adopted (a project with history,
then eawf). The receipt lands in the checkout's local store, where
:mod:`eawf.workflow.release.packet_run` reads it for the release.

The runtime is a :class:`PacketRuntime`: it names the daemon to start and
certifies itself in the scratch tree, so a test can stand in a deterministic
runtime without touching any other step.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol

from eawf._version import __version__
from eawf.platform.scrub.scan import redact_text
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.workflow.evidence.provider_certification import OsClass
from eawf.workflow.release.packet_run import (
    PacketOrigin,
    PacketRunReceipt,
    PacketRuntimeId,
    PacketStep,
    append_packet_receipt,
    write_packet_evidence,
)

logger = logging.getLogger(__name__)

#: The token ceiling a packet's dispatched Run is capped at unless the
#: operator names another: room for a one-file Task, far below a runaway.
DEFAULT_PACKET_BUDGET: Final[int] = 300_000

#: Who every command of the packet is attributed to.
PACKET_ACTOR: Final[str] = "OPERATOR"

#: The scratch repository's directory name; ``init --quick`` infers the
#: project code ``PACKET`` from it.
PACKET_REPO_DIRNAME: Final[str] = "packet"

#: How long one CLI command may take; a dispatch answers once its worker is
#: announced, and a certification walk awaits its canary's turn.
CLI_TIMEOUT_SECONDS: Final[float] = 1200.0

#: How long the dispatched Run may take to end.
RUN_TIMEOUT_SECONDS: Final[float] = 1200.0

#: How long the Batch may take to close once the commit lands; closing
#: runs the Task's criterion at the landed head.
CLOSE_TIMEOUT_SECONDS: Final[float] = 300.0

#: How long the daemon may take to open its socket.
DAEMON_START_SECONDS: Final[float] = 30.0

#: The eawf package this process imports. The packet's ``sys.executable -m
#: eawf`` commands import the same tree: they keep this interpreter and its
#: PYTHONPATH, and the scratch repository they run in carries no eawf package.
_PACKAGE_DIR: Final[Path] = Path(__file__).resolve().parents[2]

#: The operating-system class of this machine, by ``sys.platform``.
_OS_CLASSES: Final[dict[str, OsClass]] = {"linux": "linux", "darwin": "macos", "win32": "windows"}

_CONTAINER: Final = "eawf://PACKET/PACKET/PACKET"
_REPOSITORY_URN: Final = f"{_CONTAINER}/repository/PACKET"
_TRACK_URN: Final = f"{_CONTAINER}/track/TRK-PACKET"
_MILESTONE_URN: Final = f"{_CONTAINER}/milestone/MLS-0001"
_TASK_KEY: Final = "PACKET-0001"
_TASK_URN: Final = f"{_CONTAINER}/task/{_TASK_KEY}"
_BATCH_KEY: Final = "BAT-0001"
_BATCH_URN: Final = f"{_CONTAINER}/batch/{_BATCH_KEY}"

#: The file the Task asks for, and the test that proves it.
_NOTE: Final = "notes/1.txt"
_NOTE_TEST: Final = "tests/test_note_1.py"

_TERMINAL_RUN: Final = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


class PacketStepError(Exception):
    """One step of the packet did not do what it must.

    Attributes:
        step: The step that failed.
        detail: What it reported.
    """

    def __init__(self, step: PacketStep, detail: str) -> None:
        super().__init__(f"{step.value}: {detail}")
        self.step = step
        self.detail = detail


@dataclass
class PacketWorld:
    """The scratch repository one packet run drives, and its log.

    Attributes:
        repo: The scratch repository.
        env: The environment every command runs with.
        step: The step being run, which a failing command is charged to.
        log: Every command run and its outcome, in order.
        sequence: The tree's canonical sequence, which a create names.
    """

    repo: Path
    env: dict[str, str]
    step: PacketStep = PacketStep.SCAFFOLD
    log: list[dict[str, Any]] = field(default_factory=list)
    sequence: int = 1

    def eawf(self, *args: str, timeout: float = CLI_TIMEOUT_SECONDS) -> dict[str, Any]:
        """Run one ``eawf --json`` command in the scratch repository.

        Returns:
            The command's JSON answer.

        Raises:
            PacketStepError: The command timed out, exited non-zero or
                printed no JSON object.
        """
        argv = [sys.executable, "-m", "eawf", "--json", "--no-input", *args]
        started = time.time()
        try:
            done = subprocess.run(
                argv,
                cwd=self.repo,
                env=self.env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                **no_window_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            self.log.append({"at": started, "argv": ["eawf", *args], "timeout": timeout})
            raise PacketStepError(self.step, f"eawf {' '.join(args)} timed out") from exc
        self.log.append(
            {
                "at": started,
                "argv": ["eawf", *args],
                "exit": done.returncode,
                "stdout": done.stdout[-4000:],
                "stderr": done.stderr[-4000:],
            }
        )
        if done.returncode != 0:
            tail = (done.stdout.strip() or done.stderr.strip())[-600:]
            raise PacketStepError(
                self.step, f"eawf {' '.join(args)} exited {done.returncode}: {tail}"
            )
        try:
            answer = json.loads(done.stdout)
        except ValueError as exc:
            raise PacketStepError(self.step, f"eawf {' '.join(args)} printed no JSON") from exc
        if not isinstance(answer, dict):
            raise PacketStepError(self.step, f"eawf {' '.join(args)} printed no JSON object")
        return answer

    def git(self, *args: str) -> str:
        """Run one git command in the scratch repository and return its stdout.

        Raises:
            PacketStepError: The command exited non-zero.
        """
        done = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            **no_window_kwargs(),
        )
        self.log.append({"at": time.time(), "argv": ["git", *args], "exit": done.returncode})
        if done.returncode != 0:
            raise PacketStepError(
                self.step, f"git {' '.join(args)} exited {done.returncode}: {done.stderr[-600:]}"
            )
        return done.stdout.strip()


class PacketRuntime(Protocol):
    """The runtime a packet dispatches its Task to."""

    @property
    def runtime_id(self) -> PacketRuntimeId:
        """Return the runtime, as ``eawf run dispatch --provider`` names it."""
        ...

    def version(self) -> str:
        """Return the runtime's installed version."""
        ...

    def daemon_argv(self) -> tuple[str, ...]:
        """Return the command that starts the daemon the packet drives."""
        ...

    def certify(self, world: PacketWorld, task: str) -> None:
        """Certify the runtime for *world*'s tree, walking any canary under *task*."""
        ...


@dataclass(frozen=True, slots=True)
class LiveRuntime:
    """An installed runtime, certified and dispatched through the shipped daemon.

    Attributes:
        runtime_id: The runtime to run.
    """

    runtime_id: PacketRuntimeId

    def version(self) -> str:
        """Return the installed version of the runtime.

        Raises:
            ValueError: The runtime has no binary on PATH that reports one.
        """
        from eawf.runtime.daemon.runtime_certifier import installed_version
        from eawf.runtime.runtimes.probes.sdk_baseline import probe_runtime

        observed = probe_runtime(self.runtime_id)
        version = installed_version(observed.version) if observed.installed else None
        if version is None:
            raise ValueError(
                f"packet_runtime_not_installed: {self.runtime_id} has no binary on PATH "
                "that reports a version"
            )
        return version

    def daemon_argv(self) -> tuple[str, ...]:
        """Return the shipped daemon's entry point."""
        return (sys.executable, "-m", "eawf.runtime.daemon.main")

    def certify(self, world: PacketWorld, task: str) -> None:
        """Probe the runtime, then walk its containment canary under *task*."""
        world.eawf("runtime", "certify", self.runtime_id)
        world.eawf("runtime", "certify", self.runtime_id, "--task", task, "--actor", PACKET_ACTOR)


def _write(repo: Path, relative: str, text: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_project(world: PacketWorld) -> tuple[str, ...]:
    """Make the scratch repository a uv project whose one test proves the Task's note.

    Returns:
        The paths written, for the caller to commit.
    """
    _write(
        world.repo,
        "pyproject.toml",
        '[project]\nname = "packet"\nversion = "0.0.0"\nrequires-python = ">=3.12"\n\n'
        '[dependency-groups]\ndev = ["pytest"]\n',
    )
    _write(
        world.repo,
        _NOTE_TEST,
        "from pathlib import Path\n\n\ndef test_note() -> None:\n"
        f'    note = Path(__file__).resolve().parents[1] / "{_NOTE}"\n'
        '    assert note.read_text().strip() == "note 1"\n',
    )
    lock = subprocess.run(
        ["uv", "lock", "--no-config", "--quiet"],
        cwd=world.repo,
        env=world.env,
        capture_output=True,
        text=True,
        check=False,
        **no_window_kwargs(),
    )
    world.log.append({"at": time.time(), "argv": ["uv", "lock"], "exit": lock.returncode})
    if lock.returncode != 0:
        raise PacketStepError(world.step, f"uv lock exited {lock.returncode}: {lock.stderr}")
    return ("pyproject.toml", "uv.lock", _NOTE_TEST)


def _scaffold(world: PacketWorld, origin: PacketOrigin) -> None:
    """Create the scratch repository; an adopted one gets files and history first."""
    world.git("init", "-q", "-b", "main")
    if origin is PacketOrigin.BORN_NATIVE:
        world.git("commit", "-q", "--allow-empty", "-m", "chore: start")
        return
    _write(world.repo, ".gitignore", ".venv/\n")
    _write(world.repo, "README.md", "# packet\n\nA small project that adopts eawf.\n")
    world.git("add", "-A")
    world.git("commit", "-q", "-m", "chore: start the project")
    _write(world.repo, "src/packet/__init__.py", '"""The packet project."""\n')
    world.git("add", "-A")
    world.git("commit", "-q", "-m", "feat: add the package")
    world.git("add", *_write_project(world))
    world.git("commit", "-q", "-m", "test: add the note test")


def _start_daemon(
    world: PacketWorld, argv: tuple[str, ...], socket_path: Path
) -> subprocess.Popen[bytes]:
    """Start the daemon and wait for its socket.

    Raises:
        PacketStepError: The socket never opened.
    """
    daemon = subprocess.Popen(
        argv,
        cwd=world.repo,
        env=world.env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **no_window_kwargs(),
    )
    deadline = time.monotonic() + DAEMON_START_SECONDS
    while time.monotonic() < deadline:
        if socket_path.exists():
            with contextlib.suppress(OSError), socket.socket(socket.AF_UNIX) as probe:
                probe.connect(str(socket_path))
                return daemon
        time.sleep(0.05)
    _stop_daemon(daemon)
    raise PacketStepError(world.step, "the daemon never opened its socket")


def _stop_daemon(daemon: subprocess.Popen[bytes]) -> None:
    if daemon.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        daemon.send_signal(signal.SIGTERM)
    try:
        daemon.wait(timeout=10)
    except subprocess.TimeoutExpired:
        daemon.kill()
        daemon.wait(timeout=10)


def _create(world: PacketWorld, kind: str, key: str, spec: dict[str, Any]) -> None:
    """Admit one record at the tree's current sequence and keep the next one."""
    path = world.repo / ".ea" / "local" / f"{kind}-{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"key": key, **spec}), encoding="utf-8")
    answer = world.eawf(
        kind,
        "create",
        f"{_CONTAINER}/{kind}/{key}",
        "--expected-revision",
        str(world.sequence),
        "--idempotency-key",
        f"create-{key}",
        "--actor",
        PACKET_ACTOR,
        "--from-spec",
        str(path),
    )
    world.sequence = int(answer["result"]["canonical_sequence"])


def _criterion() -> dict[str, Any]:
    """Return the Task's criterion, which the Batch close proves by running its test."""
    return {
        "id": "CR-01",
        "text": f"{_NOTE} holds the one line `note 1`",
        "kind": "behavioral",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "gate_ids": ["G-01"],
        "quality_dimension": "functional_suitability",
        "measurable_signal": f"`uv run pytest -q {_NOTE_TEST}` exits 0.",
        "response": {
            "observe": "exits",
            "object": "zero from the criterion's pytest",
            "locus": "pytest",
            "gate_ref": "command_exit_zero",
            "quantifier": "single",
        },
    }


def _plan(world: PacketWorld) -> None:
    """Plan one Milestone of one Batch of one Task and open the Task for work."""
    operator = {"principal_kind": "operator", "principal_id": PACKET_ACTOR}
    _create(
        world,
        "track",
        "TRK-PACKET",
        {
            "title": "Ship the packet",
            "charter": "Carry one Milestone of one Task to a closed Batch.",
            "scope": {"scope_kind": "repository", "repository_ref": _REPOSITORY_URN},
            "owner": operator,
            "policy": {
                "revision": 1,
                "wip": {"active_milestones": 1, "active_batches_per_repo": 1},
                "ownership_principal": operator,
                "permitted_milestone_kinds": ["product"],
                "campaign_templates": [],
                "outcome_metrics": [],
                "promotion_rules": [],
                "integration_priority": 50,
                "presentation": {"default_view": "roadmap", "color_token": "accent_blue"},
            },
        },
    )
    _create(
        world,
        "milestone",
        "MLS-0001",
        {
            "primary_track_ref": _TRACK_URN,
            "title": "Write one note",
            "outcome": "The repository carries the note on main.",
            "appetite": "S",
            "exclusions": ["any other note"],
            "acceptance_journey": [
                {
                    "step_id": "AS-01",
                    "actor": "operator",
                    "action": "read the note on main",
                    "expected_observation": "the note holds one line",
                    "evidence_kinds": ["artifact"],
                }
            ],
        },
    )
    intent = f"Write {_NOTE} whose whole content is the one line `note 1`"
    _create(world, "task", _TASK_KEY, {"priority": "P1", "intent": intent})
    _create(
        world,
        "batch",
        _BATCH_KEY,
        {
            "milestone_ref": _MILESTONE_URN,
            "repository_ref": _REPOSITORY_URN,
            "task_refs": [_TASK_URN],
        },
    )
    world.eawf(
        "milestone",
        "activate",
        _MILESTONE_URN,
        "--expected-milestone-revision",
        "1",
        "--idempotency-key",
        "activate-milestone",
        "--actor",
        PACKET_ACTOR,
    )
    promote = world.repo / ".ea" / "local" / f"promote-{_TASK_KEY}.json"
    promote.write_text(
        json.dumps(
            {
                "updates": {
                    "batch_ref": _BATCH_URN,
                    "due_scope": _MILESTONE_URN,
                    "write_claims": [_NOTE],
                    "criteria": [_criterion()],
                }
            }
        ),
        encoding="utf-8",
    )
    world.eawf(
        "task",
        "promote",
        _TASK_URN,
        "--expected-task-revision",
        "1",
        "--idempotency-key",
        f"promote-{_TASK_KEY}",
        "--actor",
        PACKET_ACTOR,
        "--from-spec",
        str(promote),
    )


def _status(repo: Path, kind: str, key: str) -> str | None:
    """Return the stored status of one record, or ``None`` before it exists."""
    from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
    from eawf.kernel.state.epoch2.authority import require_native_authority
    from eawf.kernel.store.compaction import read_document
    from eawf.kernel.store.ledger import effective_records, read_ledger_records
    from eawf.kernel.store.paths import ledger_path
    from eawf.kernel.store.tiers import Epoch2Collection

    for _ in range(5):
        try:
            authority = require_native_authority(repo / ".ea")
            assert authority.target is not None and authority.generation_id is not None
            path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
            collection = Epoch2Collection(kind)
            row = read_document(path).get(collection.value, {}).get(key)
            if isinstance(row, dict):
                return str(row.get("status"))
            payloads = [
                item.payload
                for item in effective_records(read_ledger_records(ledger_path(path, collection)))
                if item.record_key == key and "payload_kind" not in item.payload
            ]
        except ValueError, OSError:
            # The daemon may be mid-write; read again.
            time.sleep(0.1)
            continue
        return str(payloads[-1].get("status")) if payloads else None
    return None


def _await_status(
    world: PacketWorld, kind: str, key: str, wanted: frozenset[str], timeout: float
) -> str:
    """Poll one record until its status is in *wanted*.

    Raises:
        PacketStepError: The status never reached *wanted*.
    """
    deadline = time.monotonic() + timeout
    status = _status(world.repo, kind, key)
    while status not in wanted and time.monotonic() < deadline:
        time.sleep(0.5)
        status = _status(world.repo, kind, key)
    world.log.append({"at": time.time(), "await": f"{kind} {key}", "status": status})
    if status not in wanted:
        raise PacketStepError(world.step, f"{kind} {key} stayed {status}, wanted {sorted(wanted)}")
    return status


def _dispatch(world: PacketWorld, runtime_id: PacketRuntimeId, budget: int) -> tuple[str, str]:
    """Dispatch the Task and await its Run's end.

    Returns:
        The Run key and the branch its commit sits on.

    Raises:
        PacketStepError: The Run did not complete, or the Task did not
            become ready to integrate.
    """
    answer = world.eawf(
        "run",
        "dispatch",
        _TASK_URN,
        "--actor",
        PACKET_ACTOR,
        "--provider",
        runtime_id,
        "--budget",
        str(budget),
    )
    result = answer["result"]
    run_key, branch = str(result["run_key"]), str(result["dispatch"]["lease"]["branch"])
    ended = _await_status(world, "run", run_key, _TERMINAL_RUN, RUN_TIMEOUT_SECONDS)
    if ended != "COMPLETED":
        raise PacketStepError(world.step, f"run {run_key} ended {ended}")
    _await_status(world, "task", _TASK_KEY, frozenset({"READY_TO_INTEGRATE"}), 30.0)
    return run_key, branch


def _land(world: PacketWorld, branch: str) -> None:
    """Cherry-pick the Run's commit and see the Batch close on the proven criterion."""
    world.git("cherry-pick", branch)
    world.eawf("batch", "close-landed", _BATCH_KEY, "--actor", PACKET_ACTOR)
    _await_status(world, "batch", _BATCH_KEY, frozenset({"COMPLETED"}), CLOSE_TIMEOUT_SECONDS)
    _await_status(world, "task", _TASK_KEY, frozenset({"COMPLETED"}), 30.0)


def _transcript(world: PacketWorld, run_key: str) -> None:
    """Print the Run's transcript and require at least one entry it said or did.

    Raises:
        PacketStepError: The transcript holds nothing but reader notices.
    """
    answer = world.eawf("run", "transcript", run_key)
    entries = answer["result"]["entries"]
    if not any(entry.get("kind") != "notice" for entry in entries):
        raise PacketStepError(world.step, f"the transcript of {run_key} printed no entry")


def _packet_env(scratch: Path, runtime_dir: Path) -> dict[str, str]:
    """Return the environment that keeps the packet off the operator's eawf state.

    HOME stays, because a live runtime finds its signed-in session there.
    """
    gitconfig = scratch / "gitconfig"
    gitconfig.touch()
    env = {
        key: value
        for key, value in os.environ.items()
        # CLAUDECODE makes a nested claude refuse to start; VIRTUAL_ENV would
        # point the scratch project's ``uv run`` at eawf's own environment.
        if key not in {"EA_STATE", "EAWF_DAEMONLESS", "EAWF_VERBOSE", "VIRTUAL_ENV", "CLAUDECODE"}
    }
    env.update(
        {
            "EAWF_HOME": str(scratch / "eawf-home"),
            "EAWF_REGISTRY_PATH": str(scratch / "registry.json"),
            "EAWF_RUNTIME_DIR": str(runtime_dir),
            "EAWF_DAEMON_IDLE_TIMEOUT": "1800",
            "EAWF_DAEMON_SESSION_TTL": "1800",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_AUTHOR_NAME": "packet",
            "GIT_AUTHOR_EMAIL": "packet@example.com",
            "GIT_COMMITTER_NAME": "packet",
            "GIT_COMMITTER_EMAIL": "packet@example.com",
        }
    )
    return env


def _drive(
    world: PacketWorld, runtime: PacketRuntime, origin: PacketOrigin, budget: int, sock: Path
) -> None:
    """Run every step of the packet in order, raising on the first that fails."""
    _scaffold(world, origin)
    world.step = PacketStep.INIT
    daemon = _start_daemon(world, runtime.daemon_argv(), sock)
    try:
        world.eawf("init", "--quick")
        if origin is PacketOrigin.BORN_NATIVE:
            world.git("add", *_write_project(world))
            world.git("commit", "-q", "-m", "test: add the note test")
        world.step = PacketStep.PLAN
        _plan(world)
        world.step = PacketStep.CERTIFY
        runtime.certify(world, _TASK_URN)
        world.step = PacketStep.DISPATCH
        run_key, branch = _dispatch(world, runtime.runtime_id, budget)
        world.step = PacketStep.LAND
        _land(world, branch)
        world.step = PacketStep.TRANSCRIPT
        _transcript(world, run_key)
    finally:
        _stop_daemon(daemon)


def _source_tree(package_dir: Path) -> tuple[str, bool]:
    """Return the commit the checkout holding *package_dir* is at, and whether the package is dirty.

    Only the package directory is judged dirty, because it is the code the
    packet runs; an edit elsewhere in the checkout runs nothing.

    Raises:
        ValueError: *package_dir* is not inside a git checkout.
    """
    head = subprocess.run(
        ["git", "-C", str(package_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        **no_window_kwargs(),
    )
    if head.returncode != 0:
        raise ValueError(
            f"packet_source_unknown: the eawf package at {package_dir.name} is not in a git "
            "checkout, so no commit names the code the packet would run"
        )
    status = subprocess.run(
        [
            "git",
            "-C",
            str(package_dir),
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--",
            ".",
        ],
        capture_output=True,
        text=True,
        check=True,
        **no_window_kwargs(),
    )
    return head.stdout.strip(), bool(status.stdout.strip())


def run_packet(
    repo_root: Path,
    scratch: Path,
    *,
    runtime: PacketRuntime,
    origin: PacketOrigin,
    budget: int = DEFAULT_PACKET_BUDGET,
) -> PacketRunReceipt:
    """Run one packet in *scratch* and append its receipt to *repo_root*'s local store.

    A step that fails is recorded on the receipt rather than raised, so a
    failed run is as visible to the release as a passed one. Any other
    exception is recorded as a failure of the step it escaped, and then
    raised.

    Args:
        repo_root: The checkout whose local store receives the receipt.
        scratch: An empty or absent directory outside *repo_root* that the
            scratch repository is created in.
        runtime: The runtime the Task is dispatched to.
        origin: How the scratch repository comes to carry eawf.
        budget: The token ceiling of the dispatched Run.

    Returns:
        The appended receipt.

    Raises:
        ValueError: *scratch* is inside *repo_root* or not empty, *budget*
            is below one, the eawf package is not in a git checkout, or the
            runtime is not installed.
        Exception: Whatever a step raised other than :class:`PacketStepError`,
            once its failed receipt is appended.
    """
    if budget < 1:
        raise ValueError(f"packet_budget_invalid: the budget must be positive, got {budget}")
    scratch = scratch.resolve()
    if scratch.is_relative_to(repo_root.resolve()):
        raise ValueError("packet_scratch_inside_checkout: the scratch directory must be outside it")
    if scratch.exists() and any(scratch.iterdir()):
        raise ValueError(f"packet_scratch_not_empty: {scratch.name} already holds files")
    commit, dirty = _source_tree(_PACKAGE_DIR)
    runtime_version = runtime.version()
    os_class = _OS_CLASSES.get(sys.platform)
    if os_class is None:
        raise ValueError(f"packet_platform_unknown: {sys.platform} names no os_class")
    scratch.mkdir(parents=True, exist_ok=True)
    repo = scratch / PACKET_REPO_DIRNAME
    repo.mkdir()
    # AF_UNIX socket paths are short, so the runtime directory sits in the
    # system temp directory rather than under the scratch path.
    runtime_dir = Path(tempfile.gettempdir()) / f"eawf-pkt-{uuid.uuid4().hex[:8]}"
    runtime_dir.mkdir()
    world = PacketWorld(repo=repo, env=_packet_env(scratch, runtime_dir))
    started = datetime.now(UTC)
    failed: PacketStepError | None = None
    crashed: Exception | None = None
    try:
        _drive(world, runtime, origin, budget, runtime_dir / "eawfd.sock")
    except PacketStepError as exc:
        failed = exc
    except Exception as exc:
        crashed = exc
        failed = PacketStepError(world.step, redact_text(f"{type(exc).__name__}: {exc}"))
        world.log.append({"at": time.time(), "crash": failed.detail})
    finally:
        log = runtime_dir / "eawfd.log"
        if log.is_file():
            shutil.copy(log, scratch / "eawfd.log")
        shutil.rmtree(runtime_dir, ignore_errors=True)
    ended = datetime.now(UTC)
    digest = write_packet_evidence(
        repo_root, json.dumps(world.log, indent=2, default=str).encode("utf-8")
    )
    receipt = PacketRunReceipt(
        runtime=runtime.runtime_id,
        origin=origin,
        os_class=os_class,
        eawf_version=__version__,
        eawf_commit=commit,
        source_dirty=dirty,
        runtime_version=runtime_version,
        budget_tokens=budget,
        outcome="fail" if failed else "pass",
        failed_step=None if failed is None else failed.step,
        failure=None if failed is None else failed.detail[:2000],
        started_at=started,
        ended_at=ended,
        evidence_digest=digest,
    )
    append_packet_receipt(repo_root, receipt)
    if crashed is not None:
        raise crashed
    return receipt


__all__ = [
    "DEFAULT_PACKET_BUDGET",
    "PACKET_ACTOR",
    "LiveRuntime",
    "PacketRuntime",
    "PacketStepError",
    "PacketWorld",
    "run_packet",
]
