"""Milestone ship smoke: a scratch repository carried from init to an accepted Milestone.

A fresh Git repository -- a tiny uv project holding one pytest test per Task --
is initialised with ``eawf init --quick``, a Milestone of three Batches of two
Tasks each is planned through the create, activate and promote verbs, and every
Task is dispatched natively with ``eawf run dispatch``. Each worker writes one
note and files its report; the daemon commits the note with the Task's trailer,
the operator lands the commit on ``main`` by cherry-pick, and the Batch closes
itself on landing once the daemon has proved each Task's pytest criterion at the
landed head. The operator then opens the Milestone's review and approval, seals
the answer and accepts it.

The console ``eawf ui`` opens is attached for the whole run, driven headless
through Textual's pilot, and a frame timeline is recorded. Every Task, Run and
Batch status change the tree records, and each Batch's "proving" note, must be
on screen within :data:`LATENCY_BUDGET_S` of the change being committed.

The default provider is a deterministic stub: the real ``eawfd`` with only the
claude launcher replaced (``_smoke_stub_daemon.py``), so the supervisor, the Run
lifecycle, the lease, the commit of the worker's edits and the report binding
all run as shipped. ``EAWF_SMOKE_PROVIDER=claude`` runs the same flow on the
real claude-code runtime; ``EAWF_SMOKE_EVIDENCE_DIR`` then receives the log and
the frame timeline.

Nothing reaches the operator's environment: ``HOME``, the user config layer,
``EAWF_HOME``, the registry and the daemon runtime directory all live under the
test's own temporary directories.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import pytest

pytestmark = pytest.mark.e2e

#: Which runtime the Tasks are dispatched to: ``stub`` (default) or ``claude``.
PROVIDER: Final = os.environ.get("EAWF_SMOKE_PROVIDER", "stub")

#: Where the real-provider run copies its log and frame timeline, when set.
EVIDENCE_DIR_ENV: Final = "EAWF_SMOKE_EVIDENCE_DIR"

#: The stub daemon entry point, beside this file.
STUB_DAEMON: Final = Path(__file__).resolve().with_name("_smoke_stub_daemon.py")

#: Who every operator command is attributed to.
OPERATOR: Final = "OPERATOR"

#: Batches in the Milestone, and Tasks per Batch.
BATCHES: Final = 3
TASKS_PER_BATCH: Final = 2

#: How soon a committed status change must be on screen.
LATENCY_BUDGET_S: Final = 2.0

#: How often the console's frame is sampled.
FRAME_INTERVAL_S: Final = 0.05

#: The console size the frames are taken at.
CONSOLE_SIZE: Final = (200, 60)

#: How long one CLI command may take; a dispatch answers once its worker is announced.
CLI_TIMEOUT_S: Final = 120.0

#: The cheapest model the claude launcher lists, which the real run is narrowed to.
CHEAPEST_MODEL: Final = "claude-haiku-4-5"

#: The credentials claude reads from its environment. With one set, the claude run
#: isolates HOME like the stub; without one, it keeps the operator's HOME so claude
#: finds its own signed-in session, and isolates only what eawf owns.
CLAUDE_TOKEN_ENVS: Final = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")

#: How long a dispatched Run may take to reach its end.
RUN_TIMEOUT_S: Final = 60.0 if PROVIDER == "stub" else 600.0


def _git(repo: Path, *args: str, env: dict[str, str]) -> str:
    """Run one git command in *repo* and return its stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@dataclass
class Smoke:
    """The isolated scratch world one smoke run drives.

    Attributes:
        root: The temporary directory everything lives under.
        repo: The scratch repository.
        runtime_dir: The daemon's runtime directory (short, for AF_UNIX).
        env: The environment every subprocess runs with.
        log: Every command run and its outcome, in order.
        daemon: The daemon process, once started.
        sequence: The tree's canonical sequence, which a create names.
        operator_files: The operator's eawf files as they were before the run.
        dispatched: The Run each ``run dispatch`` started, in order.
    """

    root: Path
    repo: Path
    runtime_dir: Path
    env: dict[str, str]
    log: list[dict[str, Any]] = field(default_factory=list)
    daemon: subprocess.Popen[bytes] | None = None
    sequence: int = 0
    operator_files: dict[Path, bytes | None] = field(default_factory=dict)
    dispatched: list[str] = field(default_factory=list)

    def eawf(self, *args: str) -> dict[str, Any]:
        """Run one ``eawf --json`` command in the scratch repository; require exit 0."""
        argv = [sys.executable, "-m", "eawf", "--json", "--no-input", *args]
        started = time.time()
        done = subprocess.run(
            argv,
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_S,
            check=False,
        )
        entry = {
            "at": started,
            "argv": ["eawf", *args],
            "exit": done.returncode,
            "stdout": done.stdout[-4000:],
            "stderr": done.stderr[-4000:],
        }
        self.log.append(entry)
        assert done.returncode == 0, json.dumps(entry, indent=2)
        answer = json.loads(done.stdout)
        assert isinstance(answer, dict), answer
        return answer

    def git(self, *args: str) -> str:
        """Run one git command in the scratch repository, logged as the operator's."""
        out = _git(self.repo, *args, env=self.env)
        self.log.append({"at": time.time(), "argv": ["git", *args], "exit": 0, "stdout": out})
        return out


#: What every dispatch names beyond the Task: the runtime and a token ceiling.
PROVIDER_ARGS: Final = ("--provider", "claude-code", "--budget", "200000")

#: The prefix every record of the scratch tree is spelled under (``init --quick``
#: infers the project code ``SMOKE`` from the directory name).
CONTAINER: Final = "eawf://SMOKE/SMOKE/SMOKE"
REPOSITORY_URN: Final = f"{CONTAINER}/repository/SMOKE"
TRACK_URN: Final = f"{CONTAINER}/track/TRK-SMOKE"
MILESTONE_URN: Final = f"{CONTAINER}/milestone/MLS-0001"


@dataclass(frozen=True)
class Plan:
    """The records the smoke planned, by URN.

    Attributes:
        batches: Every Batch, in delivery order.
        tasks: Every Task, Batch by Batch.
        batch_of: The Batch each Task belongs to.
        note_of: The note each Task's worker writes.
    """

    batches: tuple[str, ...]
    tasks: tuple[str, ...]
    batch_of: dict[str, str]
    note_of: dict[str, str]


def _urn(kind: str, key: str) -> str:
    return f"{CONTAINER}/{kind}/{key}"


def _spec(world: Smoke, name: str, payload: Any) -> str:
    """Save one request document under ``.ea/local/`` and return its path."""
    path = world.repo / ".ea" / "local" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _create(world: Smoke, kind: str, key: str, spec: dict[str, Any]) -> str:
    """Admit one record at the tree's current sequence, keep the next, return its URN."""
    urn = _urn(kind, key)
    path = _spec(world, f"{kind}-{key}.json", {"key": key, **spec})
    answer = world.eawf(
        kind,
        "create",
        urn,
        "--expected-revision",
        str(world.sequence),
        "--idempotency-key",
        f"create-{key}",
        "--actor",
        OPERATOR,
        "--from-spec",
        path,
    )
    world.sequence = int(answer["result"]["canonical_sequence"])
    return urn


def _test_path(note: str) -> str:
    """Return the scratch test that proves *note* (``notes/<n>.txt``)."""
    return f"tests/test_note_{Path(note).stem}.py"


def _criterion(note: str) -> dict[str, Any]:
    """Return a criterion the Batch close proves by running its pytest test."""
    return {
        "id": "CR-01",
        "text": f"{note} holds the one line `note {Path(note).stem}`",
        "kind": "behavioral",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "gate_ids": ["G-01"],
        "quality_dimension": "functional_suitability",
        "measurable_signal": f"`uv run pytest -q {_test_path(note)}` exits 0.",
        "response": {
            "observe": "exits",
            "object": "zero from the criterion's pytest",
            "locus": "pytest",
            "gate_ref": "command_exit_zero",
            "quantifier": "single",
        },
    }


def _scratch_project(world: Smoke) -> None:
    """Make the scratch repository a uv project with one pytest test per Task's note."""
    (world.repo / "pyproject.toml").write_text(
        '[project]\nname = "smoke"\nversion = "0.0.0"\nrequires-python = ">=3.12"\n\n'
        '[dependency-groups]\ndev = ["pytest"]\n',
        encoding="utf-8",
    )
    (world.repo / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    tests = world.repo / "tests"
    tests.mkdir()
    for number in range(1, BATCHES * TASKS_PER_BATCH + 1):
        (tests / f"test_note_{number}.py").write_text(
            "from pathlib import Path\n\n\ndef test_note() -> None:\n"
            f'    note = Path(__file__).resolve().parents[1] / "notes/{number}.txt"\n'
            f'    assert note.read_text().strip() == "note {number}"\n',
            encoding="utf-8",
        )
    subprocess.run(
        ["uv", "lock", "--no-config", "--quiet"], cwd=world.repo, env=world.env, check=True
    )


def _plan(world: Smoke) -> Plan:
    """Plan the Track, the Milestone, its Batches and Tasks, and open them for work."""
    world.sequence = 1
    _create(
        world,
        "track",
        "TRK-SMOKE",
        {
            "title": "Ship the smoke",
            "charter": "Carry one Milestone of three Batches to acceptance.",
            "scope": {"scope_kind": "repository", "repository_ref": REPOSITORY_URN},
            "owner": {"principal_kind": "operator", "principal_id": OPERATOR},
            "policy": {
                "revision": 1,
                "wip": {"active_milestones": 1, "active_batches_per_repo": 1},
                "ownership_principal": {"principal_kind": "operator", "principal_id": OPERATOR},
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
            "primary_track_ref": TRACK_URN,
            "title": "Write six notes",
            "outcome": "The repository carries six notes on main.",
            "appetite": "S",
            "exclusions": ["any note beyond the six"],
            "acceptance_journey": [
                {
                    "step_id": "AS-01",
                    "actor": "operator",
                    "action": "list the notes on main",
                    "expected_observation": "six notes are listed",
                    "evidence_kinds": ["artifact"],
                }
            ],
        },
    )
    tasks: list[str] = []
    batches: list[str] = []
    batch_of: dict[str, str] = {}
    note_of: dict[str, str] = {}
    number = 0
    for b in range(1, BATCHES + 1):
        batch_key = f"BAT-{b:04d}"
        members = []
        for _ in range(TASKS_PER_BATCH):
            number += 1
            key = f"SMOKE-{number:04d}"
            note = f"notes/{number}.txt"
            intent = f"Write {note} whose whole content is the one line `note {number}`"
            _create(world, "task", key, {"priority": "P1", "intent": intent})
            members.append(_urn("task", key))
            note_of[_urn("task", key)] = note
        batch = _create(
            world,
            "batch",
            batch_key,
            {
                "milestone_ref": MILESTONE_URN,
                "repository_ref": REPOSITORY_URN,
                "task_refs": members,
            },
        )
        batches.append(batch)
        for task in members:
            batch_of[task] = batch
            tasks.append(task)
    world.eawf(
        "milestone",
        "activate",
        MILESTONE_URN,
        "--expected-milestone-revision",
        "1",
        "--idempotency-key",
        "activate-milestone",
        "--actor",
        OPERATOR,
    )
    for task in tasks:
        promote = _spec(
            world,
            f"promote-{task.rsplit('/', 1)[1]}.json",
            {
                "updates": {
                    "batch_ref": batch_of[task],
                    "due_scope": MILESTONE_URN,
                    "write_claims": [note_of[task]],
                    "criteria": [_criterion(note_of[task])],
                }
            },
        )
        world.eawf(
            "task",
            "promote",
            task,
            "--expected-task-revision",
            "1",
            "--idempotency-key",
            f"promote-{task}",
            "--actor",
            OPERATOR,
            "--from-spec",
            promote,
        )
    return Plan(tuple(batches), tuple(tasks), batch_of, note_of)


def _certify(world: Smoke, plan: Plan) -> None:
    """Certify the claude-code harness for the scratch tree.

    The stub stands in for a harness no probe can reach, so its certification is
    the row the certifier writes, filed in the tree's machine-local store with the
    semantic tools a dispatched canary observes. The real runtime is narrowed to
    its cheapest listed model, probed, and walked through the containment canary
    under the first Task, which is what observes the semantic tools a worker
    reports through.
    """
    if PROVIDER != "stub":
        import yaml

        config = world.repo / ".ea" / "config.yaml"
        document = yaml.safe_load(config.read_text(encoding="utf-8")) if config.is_file() else {}
        document = document or {}
        document.setdefault("runtime", {})["profiles"] = [
            {
                "profile_id": "claude-default",
                "source_ref": "config://repository/runtime",
                "model_policy": {"allowed": [CHEAPEST_MODEL], "default": CHEAPEST_MODEL},
            }
        ]
        config.write_text(yaml.safe_dump(document), encoding="utf-8")
        world.eawf("runtime", "certify", "claude-code")
        world.eawf(
            "runtime", "certify", "claude-code", "--task", plan.tasks[0], "--actor", OPERATOR
        )
        return
    from eawf.kernel.runtime.certification import CapabilityCertification
    from eawf.workflow.evidence.machine_certification import append_machine_certification
    from tests.integration.runtime.daemon.test_task_dispatch import CLAUDE_VERSION, certification

    certified = certification("claude-code", CLAUDE_VERSION)
    (first, *_) = certified.capabilities
    semantic = CapabilityCertification.model_validate(
        {**first.model_dump(mode="json"), "capability_id": "semantic_tools"}
    )
    append_machine_certification(
        world.repo / ".ea" / "state.json",
        certified.model_copy(update={"capabilities": (*certified.capabilities, semantic)}),
    )


def _short_runtime_dir() -> Path:
    """Return a unique runtime directory short enough for the AF_UNIX bind address."""
    return Path(tempfile.gettempdir()) / f"eawf-smk-{uuid.uuid4().hex[:8]}"


def _operator_files() -> dict[Path, bytes | None]:
    """Read the operator's eawf registry and user config, which no smoke may change."""
    from eawf.kernel.config.layered import global_config_path

    paths = (Path.home() / ".eawf" / "registry.json", global_config_path())
    return {path: path.read_bytes() if path.is_file() else None for path in paths}


@pytest.fixture
def smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Smoke]:
    """Yield an isolated scratch world with a daemon running; reap it on exit."""
    operator_files = _operator_files()
    repo = tmp_path / "smoke"
    repo.mkdir()
    runtime_dir = _short_runtime_dir()
    runtime_dir.mkdir()
    gitconfig = tmp_path / "gitconfig"
    gitconfig.touch()
    isolated = {
        "EAWF_HOME": str(tmp_path / "eawf-home"),
        "EAWF_REGISTRY_PATH": str(tmp_path / "registry.json"),
        "EAWF_RUNTIME_DIR": str(runtime_dir),
        "EAWF_DAEMON_IDLE_TIMEOUT": "900",
        "EAWF_DAEMON_SESSION_TTL": "900",
        "GIT_CONFIG_NOSYSTEM": "1",
        # the operator's global git config (and its hooks) stays out of the scratch commits
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_AUTHOR_NAME": "smoke",
        "GIT_AUTHOR_EMAIL": "smoke@example.com",
        "GIT_COMMITTER_NAME": "smoke",
        "GIT_COMMITTER_EMAIL": "smoke@example.com",
    }
    if PROVIDER == "stub" or any(map(os.environ.get, CLAUDE_TOKEN_ENVS)):
        # eawf reads its user config layer from ``Path.home()``, so HOME is what isolates it
        home = tmp_path / "home"
        home.mkdir()
        isolated["HOME"] = str(home)
    for key, value in isolated.items():
        monkeypatch.setenv(key, value)
    # VIRTUAL_ENV names eawf's own venv; the proof checkout's ``uv run`` must use its own
    for key in ("EA_STATE", "EAWF_DAEMONLESS", "EAWF_VERBOSE", "VIRTUAL_ENV"):
        monkeypatch.delenv(key, raising=False)
    env = dict(os.environ)
    world = Smoke(
        root=tmp_path, repo=repo, runtime_dir=runtime_dir, env=env, operator_files=operator_files
    )
    try:
        yield world
    finally:
        if world.daemon is not None and world.daemon.poll() is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                world.daemon.send_signal(signal.SIGTERM)
            try:
                world.daemon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                world.daemon.kill()
                world.daemon.wait(timeout=10)
        log = runtime_dir / "eawfd.log"
        if log.is_file():
            shutil.copy(log, tmp_path / "eawfd.log")
        shutil.rmtree(runtime_dir, ignore_errors=True)


def _start_daemon(world: Smoke) -> None:
    """Start the daemon the smoke runs against and wait for its socket."""
    argv = (
        [sys.executable, str(STUB_DAEMON)]
        if PROVIDER == "stub"
        else [sys.executable, "-m", "eawf.runtime.daemon.main"]
    )
    world.daemon = subprocess.Popen(
        argv,
        cwd=world.repo,
        env=world.env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    sock = world.runtime_dir / "eawfd.sock"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if sock.exists():
            with contextlib.suppress(OSError), socket.socket(socket.AF_UNIX) as probe:
                probe.connect(str(sock))
                return
        time.sleep(0.05)
    raise AssertionError("the smoke daemon never opened its socket")


# ---- reading the tree back ------------------------------------------------------


def _generation_state(repo: Path) -> Path:
    """Return the selected generation's ``state.json`` of the scratch tree."""
    from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
    from eawf.kernel.state.epoch2.authority import require_native_authority

    authority = require_native_authority(repo / ".ea")
    assert authority.target is not None and authority.generation_id is not None
    return authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT


def _stored(repo: Path, kind: str, key: str) -> dict[str, Any] | None:
    """Return one record from whichever tier holds it now, or ``None``."""
    from eawf.kernel.store.compaction import read_document
    from eawf.kernel.store.ledger import effective_records, read_ledger_records
    from eawf.kernel.store.paths import ledger_path
    from eawf.kernel.store.tiers import Epoch2Collection

    path = _generation_state(repo)
    collection = Epoch2Collection(kind)
    row = read_document(path).get(collection.value, {}).get(key)
    if isinstance(row, dict):
        return row
    lines = effective_records(read_ledger_records(ledger_path(path, collection)))
    payloads = [
        item.payload
        for item in lines
        if item.record_key == key and "payload_kind" not in item.payload
    ]
    return dict(payloads[-1]) if payloads else None


def _status(repo: Path, kind: str, key: str) -> str | None:
    """Return the stored status of one record, read again on a torn read."""
    for _ in range(5):
        try:
            row = _stored(repo, kind, key)
        except ValueError, OSError:
            time.sleep(0.1)
            continue
        return None if row is None else str(row.get("status"))
    return None


def _wait_status(repo: Path, kind: str, key: str, wanted: set[str], timeout: float) -> str:
    """Poll one record until its status is in *wanted*; fail naming where it stopped."""
    deadline = time.monotonic() + timeout
    status = _status(repo, kind, key)
    while status not in wanted and time.monotonic() < deadline:
        time.sleep(0.2)
        status = _status(repo, kind, key)
    assert status in wanted, f"{kind} {key} stayed {status}, wanted one of {sorted(wanted)}"
    return status


@dataclass(frozen=True)
class StatusChange:
    """One status the tree committed for one record.

    Attributes:
        kind: The record's collection.
        key: The record's key.
        status: The status it moved to.
        at: When the commit landed, as epoch seconds.
        event: The event the commit emitted.
        sequence: The commit's canonical sequence, which orders changes stamped alike.
    """

    kind: str
    key: str
    status: str
    at: float
    event: str
    sequence: int


def _commit_times(log: Path) -> dict[int, float]:
    """Return when the daemon committed each canonical sequence, read off its log.

    A change record is stamped when its request started, which for a dispatched
    Run is before the provider process was spawned; a console can show a change
    only once it is committed, so latency is measured from the commit.
    """
    from datetime import datetime

    committed = re.compile(r"^(\S+ \S+) INFO .* committed root=.* sequence=(\d+)")
    times: dict[int, float] = {}
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if match := committed.match(line):
            at = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S,%f").timestamp()
            times.setdefault(int(match[2]), at)
    return times


def _status_changes(repo: Path, log: Path) -> list[StatusChange]:
    """Return every Task, Run, Batch and Milestone status change the tree recorded."""
    from eawf.kernel.store.changes import ChangeRecord, read_change_page

    commits = _commit_times(log)

    records: list[ChangeRecord] = []
    cursor: int | None = None
    while True:
        page = read_change_page(_generation_state(repo), record_key=None, cursor=cursor, limit=200)
        records.extend(page.changes)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    changes = []
    for record in records:
        if record.collection.value not in {"task", "run", "batch", "milestone"}:
            continue
        for change in record.changes:
            if change.field == "status" and change.after is not None:
                changes.append(
                    StatusChange(
                        kind=record.collection.value,
                        key=record.record_key,
                        status=str(change.after.value),
                        at=max(
                            record.recorded_at.timestamp(),
                            commits.get(record.canonical_sequence, 0.0),
                        ),
                        event=record.event_name,
                        sequence=record.canonical_sequence,
                    )
                )
    return sorted(changes, key=lambda change: (change.sequence, change.at))


# ---- the console ----------------------------------------------------------------


@dataclass(frozen=True)
class Frame:
    """One captured console frame.

    Attributes:
        at: When it was captured, as epoch seconds.
        route: The route on screen.
        subject: The record the route is about, if any.
        text: The screen, as the terminal shows it.
    """

    at: float
    route: str
    subject: str | None
    text: str


def _rotation() -> list[tuple[str, str | None]]:
    """Return the views the console cycles through: Runs, each Batch's Tasks, the Milestone."""
    views: list[tuple[str, str | None]] = [("activity", None), ("backlog", None)]
    views += [("batch.detail", f"BAT-{b:04d}") for b in range(1, BATCHES + 1)]
    views += [("scope.home", None), ("milestone", "MLS-0001")]
    return views


def _shown(frame: Frame, change: StatusChange) -> bool:
    """Return whether *frame* shows the record of *change* in the status it moved to.

    A Task is read off its Batch's Task list, or off the backlog while it is a
    draft; a Batch off its own header; a Run off the Activity list; the Milestone
    off the home list or its own header.
    """
    key, status = re.escape(change.key), re.escape(change.status)

    def row_shows() -> bool:
        return any(
            change.key in line and re.search(rf"\b{status}\b", line, re.IGNORECASE)
            for line in frame.text.splitlines()
        )

    if change.kind == "task":
        if frame.route == "batch.detail":
            return re.search(rf"\b{key} · {status}\b", frame.text) is not None
        return frame.route == "backlog" and change.status == "DRAFT" and _drafted(frame, change.key)
    if change.kind == "batch":
        return (
            frame.route == "batch.detail"
            and frame.subject == change.key
            and re.search(rf"Batch {key} · {status}\b", frame.text) is not None
        )
    if change.kind == "run":
        return frame.route == "activity" and row_shows()
    if frame.route == "milestone":
        return re.search(rf"Milestone {key} · {status}\b", frame.text) is not None
    return frame.route == "scope.home" and row_shows()


def _drafted(frame: Frame, key: str) -> bool:
    """Return whether the backlog frame lists *key* in its DRAFTS group, which states DRAFT."""
    group = ""
    for line in frame.text.splitlines():
        words = line.split()
        if words and words[0] in {"DRAFTS", "DEFERRED"}:
            group = words[0]
        elif group == "DRAFTS" and key in line.split("│", 1)[0]:
            return True
    return False


@dataclass
class Latency:
    """How soon each status change was on screen.

    Attributes:
        seen: ``(change, seconds)`` for every change a frame showed.
        transient: Changes superseded before any frame could show them.
        missed: Changes no frame showed within the budget.
    """

    seen: list[tuple[StatusChange, float]] = field(default_factory=list)
    transient: list[StatusChange] = field(default_factory=list)
    missed: list[StatusChange] = field(default_factory=list)


def _latency(changes: list[StatusChange], frames: list[Frame]) -> Latency:
    """Match every status change to the first frame showing it.

    A change the same record left again within the budget, before a frame showed
    it, is counted as transient rather than missed: a CLAIMED Task a dispatch starts
    in the same request is never on screen long enough to paint.
    """
    result = Latency()
    for index, change in enumerate(changes):
        later = [c.at for c in changes[index + 1 :] if (c.kind, c.key) == (change.kind, change.key)]
        superseded = later[0] if later else None
        hit = next(
            (frame for frame in frames if frame.at >= change.at and _shown(frame, change)), None
        )
        if hit is not None and (superseded is None or hit.at <= superseded + LATENCY_BUDGET_S):
            result.seen.append((change, hit.at - change.at))
        elif superseded is not None and superseded - change.at < LATENCY_BUDGET_S:
            result.transient.append(change)
        else:
            result.missed.append(change)
    return result


class Console:
    """The console ``eawf ui`` opens on the scratch tree, sampled headless."""

    def __init__(self, world: Smoke) -> None:
        """Build the console exactly as ``eawf ui`` builds it for an epoch-2 tree."""
        from eawf.kernel.state.epoch2.authority import resolve_authority
        from eawf.runtime.daemon.epoch2_root import RootIdentity
        from eawf.surfaces.tui.console.app import OUTER_GUTTER, ConsoleApp
        from eawf.surfaces.tui.console.chrome import load_chrome
        from eawf.surfaces.tui.console.clock import Clock
        from eawf.surfaces.tui.console.seam import ProjectionSeam
        from eawf.surfaces.tui.launch import project_name

        state_path = world.repo / ".ea" / "state.json"
        authority = resolve_authority(state_path.parent)
        self.seam = ProjectionSeam(
            route="scope.home",
            scope_id=RootIdentity.of(authority.root).root_id,
            state_path=state_path,
            repo_root=world.repo,
            scope_name=project_name(state_path, world.repo),
        )
        self.app = ConsoleApp(
            chrome=load_chrome(), seam=self.seam, clock=Clock(), gutter=OUTER_GUTTER
        )
        self.frames: list[Frame] = []
        self.views = _rotation()

    async def sample(self, pilot: Any, stop: asyncio.Event) -> None:
        """Capture a frame of each view in turn until *stop* is set."""
        from eawf.surfaces.tui.console.harness import capture, settle
        from eawf.surfaces.tui.console.session import SessionSetup

        index = 0
        while not stop.is_set():
            route, subject = self.views[index % len(self.views)]
            index += 1
            self.app.reset(SessionSetup(route=route, subjId=subject, size=2))
            self.app.render_frame()
            await settle(pilot)
            self.frames.append(Frame(time.time(), route, subject, capture(self.app)))
            await asyncio.sleep(FRAME_INTERVAL_S)


# ---- the operator's flow ----------------------------------------------------------


def _deliver_batch(world: Smoke, plan: Plan, batch: str, *, close_verb: bool) -> None:
    """Dispatch a Batch's Tasks, land their commits on main, and see the Batch close."""
    members = [task for task in plan.tasks if plan.batch_of[task] == batch]
    runs: dict[str, tuple[str, str]] = {}
    for task in members:
        answer = world.eawf("run", "dispatch", task, "--actor", OPERATOR, *PROVIDER_ARGS)
        result = answer["result"]
        runs[task] = (result["run_key"], result["dispatch"]["lease"]["branch"])
        world.dispatched.append(result["run_key"])
    for task, (run_key, _branch) in runs.items():
        status = _wait_status(
            world.repo, "run", run_key, {"COMPLETED", "FAILED", "CANCELLED"}, RUN_TIMEOUT_S
        )
        assert status == "COMPLETED", f"{run_key} for {task} ended {status}"
        _wait_status(world.repo, "task", task.rsplit("/", 1)[1], {"READY_TO_INTEGRATE"}, 10)
    for _task, (_run_key, branch) in runs.items():
        world.git("cherry-pick", branch)
    key = batch.rsplit("/", 1)[1]
    if close_verb:
        world.eawf("batch", "close-landed", key, "--actor", OPERATOR)
    _wait_status(world.repo, "batch", key, {"COMPLETED"}, 45 if not close_verb else 10)


def _accept(world: Smoke, plan: Plan) -> None:
    """Open the Milestone's review and approval, seal the answer and accept it."""
    last = _stored(world.repo, "batch", plan.batches[-1].rsplit("/", 1)[1])
    assert last is not None
    binding = last["current_head_binding"]

    def revision() -> str:
        row = _stored(world.repo, "milestone", "MLS-0001")
        assert row is not None
        return str(row["revision"])

    review = _spec(world, "review.json", {"updates": {"acceptance_bundle_revision": 1}})
    world.eawf(
        "milestone",
        "open-review",
        MILESTONE_URN,
        "--expected-milestone-revision",
        revision(),
        "--idempotency-key",
        "review-1",
        "--actor",
        OPERATOR,
        "--from-spec",
        review,
    )
    notes = world.git("ls-tree", "--name-only", "HEAD", "notes/")
    recorded = world.eawf(
        "record",
        "evidence",
        MILESTONE_URN,
        "--kind",
        "artifact",
        "--summary",
        f"main lists {len(notes.splitlines())} notes",
        "--expected-revision",
        revision(),
        "--idempotency-key",
        "evd-1",
        "--actor",
        OPERATOR,
    )
    evidence = recorded["result"]["evidence_ref"]
    approval = _spec(
        world,
        "approval.json",
        {
            "requested_by": {"principal_kind": "human", "principal_id": OPERATOR},
            "steps": [
                {
                    "step_id": "AS-01",
                    "passed": True,
                    "observation": "six notes are listed",
                    "evidence_kinds": ["artifact"],
                    "evidence_refs": [evidence],
                }
            ],
            "accepted_binding": binding,
        },
    )
    bundle = world.repo / ".ea" / "local" / "bundle.json"
    opened = world.eawf(
        "milestone",
        "open-approval",
        MILESTONE_URN,
        "--actor",
        OPERATOR,
        "--expected-milestone-revision",
        revision(),
        "--from-spec",
        approval,
        "--bundle-out",
        str(bundle),
    )["result"]
    sealed = world.eawf(
        "milestone",
        "seal-approval",
        opened["action_ref"],
        "--expected-approval-revision",
        str(opened["revision"]),
        "--idempotency-key",
        "seal-1",
        "--actor",
        OPERATOR,
        "--option-id",
        "approve",
        "--receipt-ref",
        evidence,
    )["result"]
    world.eawf(
        "milestone",
        "accept",
        MILESTONE_URN,
        "--expected-milestone-revision",
        revision(),
        "--idempotency-key",
        "accept-1",
        "--actor",
        OPERATOR,
        "--approval-receipt-ref",
        sealed["action_ref"],
        "--acceptance-bundle",
        str(bundle),
    )


def _flow(world: Smoke) -> Plan:
    """Everything the operator types after ``init``, with the console watching."""
    plan = _plan(world)
    _certify(world, plan)
    for index, batch in enumerate(plan.batches):
        # The last Batch is left to the daemon's own sweep: no operator verb closes it.
        _deliver_batch(world, plan, batch, close_verb=index < len(plan.batches) - 1)
    _accept(world, plan)
    return plan


async def _drive(world: Smoke, frames: list[Frame]) -> Plan:
    """Run the flow in a worker thread with the console attached for its whole length."""
    console = Console(world)
    console.frames = frames
    await console.seam.connect()
    try:
        async with console.app.run_test(size=CONSOLE_SIZE) as pilot:
            stop = asyncio.Event()
            sampler = asyncio.create_task(console.sample(pilot, stop))
            try:
                plan = await asyncio.to_thread(_flow, world)
                await asyncio.sleep(LATENCY_BUDGET_S + 1)
            finally:
                stop.set()
                await sampler
    finally:
        await console.seam.disconnect()
    return plan


def _content_failures(frames: list[Frame]) -> list[str]:
    """Return what the console states wrongly: on every frame of the run, and at its end.

    Every Activity Run row names its Task and an AS OF time, and no Backlog draft is
    titled unknown, on any frame. The last Milestone frame names its Track and counts
    every Batch and Task done, and the last Home frame states the same counts.
    """
    failures: list[str] = []
    for frame in frames:
        rows = [line.split("│", 1)[0] for line in frame.text.splitlines()]
        if frame.route == "activity":
            for row in rows:
                run = re.search(r"\bRUN-\d{8}\b", row)
                if run and not re.search(r"\bRUN-\d{8}\s+SMOKE-\d{4}\b.*\b\d\d:\d\d\s*$", row):
                    failures.append(f"activity at {frame.at:.3f}: {' '.join(row.split())}")
        if frame.route == "backlog":
            failures += [
                f"backlog at {frame.at:.3f}: {' '.join(row.split())}"
                for row in rows
                if re.search(r"\bSMOKE-\d{4}\b", row) and "? unknown" in row
            ]
    last = {frame.route: frame.text for frame in frames}
    milestone, home = last.get("milestone", ""), last.get("scope.home", "")
    tasks, batches = BATCHES * TASKS_PER_BATCH, BATCHES
    wanted = {
        "milestone": (
            milestone,
            [
                r"TRACK\s+TRK-SMOKE\b",
                rf"\b{tasks} completed\b",
                rf"\bof {tasks} tasks\b",
                rf"\b{batches} of {batches} batches done\b",
            ],
        ),
        "scope.home": (
            home,
            [
                r"\bTRK-SMOKE\b",
                rf"\b{tasks} of {tasks} tasks\b",
                rf"\b{batches} of {batches} batches done\b",
                r"MLS-0001\b.*\bCOMPLETED\b",
            ],
        ),
    }
    for route, (text, patterns) in wanted.items():
        failures += [f"{route} at the end lacks /{p}/" for p in patterns if not re.search(p, text)]
    return failures


def _log_times(log: Path, pattern: str) -> list[tuple[float, re.Match[str]]]:
    """Return each daemon log line matching *pattern*, with the line's own stamp."""
    from datetime import datetime

    lines = re.compile(rf"^(\S+ \S+) INFO .*{pattern}")
    found = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if match := lines.match(line):
            found.append((datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S,%f").timestamp(), match))
    return found


def _proof_receipts(repo: Path) -> dict[str, list[str]]:
    """Return the result of every proof receipt the tree filed, by Task key."""
    from eawf.kernel.store.ledger import read_ledger_records
    from eawf.kernel.store.paths import ledger_path
    from eawf.kernel.store.tiers import Epoch2Collection

    receipts: dict[str, list[str]] = {}
    path = ledger_path(_generation_state(repo), Epoch2Collection.RECEIPT)
    for record in read_ledger_records(path):
        if record.payload.get("payload_kind") == "proof_receipt":
            task = str(record.payload["task_ref"]).rsplit("/", 1)[1]
            receipts.setdefault(task, []).append(record.status)
    return receipts


def _proof_failures(
    world: Smoke, plan: Plan, changes: list[StatusChange], frames: list[Frame]
) -> list[str]:
    """Return how the Batches' proofs fell short of prove-before-close and its console note.

    Each Batch is proved once at its landed head before it closes: a proof starts and
    settles, every Task files a passing proof receipt, the Batch's COMPLETED commit comes
    after the proof settled, and the Batch's own view shows "proving N criteria at <head>"
    within the budget of the proof starting.
    """
    log = world.runtime_dir / "eawfd.log"
    started = _log_times(log, r"batch_proof started batch=(\S+) head=(\w+) criteria=(\d+)")
    settled = _log_times(log, r"batch_proof settled batch=(\S+) head=(\w+)")
    receipts = _proof_receipts(world.repo)
    failures: list[str] = []
    for batch in plan.batches:
        key = batch.rsplit("/", 1)[1]
        starts = [(at, m) for at, m in started if m[2] == batch]
        ends = [at for at, m in settled if m[2] == batch]
        done = [c.at for c in changes if (c.kind, c.key, c.status) == ("batch", key, "COMPLETED")]
        if not starts or not ends or not done:
            failures.append(
                f"{key}: started={len(starts)} settled={len(ends)} completed={len(done)}"
            )
            continue
        if done[0] < ends[0]:
            failures.append(f"{key} completed {ends[0] - done[0]:.3f}s before its proof settled")
        at, match = starts[0]
        note = f"proving {match[4]} criteria at {match[3][:12]}"
        seen = [
            frame.at - at
            for frame in frames
            if frame.route == "batch.detail" and frame.subject == key and note in frame.text
        ]
        if not seen or seen[0] > LATENCY_BUDGET_S:
            failures.append(f"{key}: no frame showed {note!r} within {LATENCY_BUDGET_S}s")
        for task in plan.tasks:
            if plan.batch_of[task] == batch:
                filed = receipts.get(task.rsplit("/", 1)[1], [])
                if filed != ["pass"]:
                    failures.append(f"{task.rsplit('/', 1)[1]} proof receipts {filed}")
    return failures


def _save_evidence(world: Smoke, frames: list[Frame], latency: Latency) -> Path:
    """Write the frame timeline and the command log under the test's tmp dir."""
    out = world.root / "evidence"
    out.mkdir(exist_ok=True)
    timeline = [
        {"at": frame.at, "route": frame.route, "subject": frame.subject, "text": frame.text}
        for frame in frames
    ]
    (out / "frame-timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
    (out / "latency.json").write_text(
        json.dumps(
            {
                "seen": [
                    {"kind": c.kind, "key": c.key, "status": c.status, "seconds": s}
                    for c, s in latency.seen
                ],
                "transient": [vars(c) for c in latency.transient],
                "missed": [vars(c) for c in latency.missed],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out / "commands.json").write_text(json.dumps(world.log, indent=2), encoding="utf-8")
    log = world.runtime_dir / "eawfd.log"
    if log.is_file():
        shutil.copy(log, out / "eawfd.log")
    target = os.environ.get(EVIDENCE_DIR_ENV)
    if target:
        shutil.copytree(out, Path(target), dirs_exist_ok=True)
    return out


def test_smoke_milestone(smoke: Smoke) -> None:
    """Carry a three-Batch Milestone from init to acceptance with the console attached."""
    world = smoke
    world.git("init", "-q", "-b", "main")
    (world.repo / "README.md").write_text("# smoke\n", encoding="utf-8")
    _scratch_project(world)
    world.git("add", "-A")
    world.git("commit", "-q", "-m", "chore: start")
    _start_daemon(world)
    world.eawf("init", "--quick")

    frames: list[Frame] = []
    try:
        plan = asyncio.run(_drive(world, frames))
    finally:
        changes = _status_changes(world.repo, world.runtime_dir / "eawfd.log")
        latency = _latency(changes, frames)
        out = _save_evidence(world, frames, latency)
        changed = [
            str(path)
            for path, before in world.operator_files.items()
            if (path.read_bytes() if path.is_file() else None) != before
        ]
        assert not changed, f"the smoke changed the operator's eawf files: {changed}"
    seconds = [s for _c, s in latency.seen]
    print(
        f"\nsmoke: {len(frames)} frames, {len(changes)} status changes, "
        f"{len(latency.seen)} seen, {len(latency.transient)} transient, "
        f"{len(latency.missed)} missed; latency max {max(seconds):.3f}s "
        f"median {statistics.median(seconds):.3f}s; evidence {out}"
    )

    for task in plan.tasks:
        assert _status(world.repo, "task", task.rsplit("/", 1)[1]) == "COMPLETED", task
    for batch in plan.batches:
        assert _status(world.repo, "batch", batch.rsplit("/", 1)[1]) == "COMPLETED", batch
    # the real runtime's certification canary adds Runs of its own beside the dispatched ones
    assert len(world.dispatched) == len(plan.tasks)
    for run_key in world.dispatched:
        assert _status(world.repo, "run", run_key) == "COMPLETED", run_key
    for run_key in {c.key for c in changes if c.kind == "run"}:
        assert _status(world.repo, "run", run_key) in {"COMPLETED", "FAILED", "CANCELLED"}
    milestone = _stored(world.repo, "milestone", "MLS-0001")
    assert milestone is not None and milestone["status"] == "COMPLETED"
    assert milestone.get("accepted_binding"), "the Milestone was not accepted on a binding"
    trailers = world.git("log", "--format=%(trailers:key=Task,valueonly)", "main")
    assert sorted(filter(None, trailers.split())) == sorted(t.rsplit("/", 1)[1] for t in plan.tasks)

    missed = [f"{c.kind} {c.key} -> {c.status} at {c.at:.3f} ({c.event})" for c in latency.missed]
    slow = [
        f"{c.kind} {c.key} -> {c.status} after {s:.3f}s"
        for c, s in latency.seen
        if s > LATENCY_BUDGET_S
    ]
    assert not missed and not slow, "\n".join(["console lagged:", *missed, *slow])
    proofs = _proof_failures(world, plan, changes, frames)
    assert not proofs, "\n".join(["prove-before-close fell short:", *proofs])
    wrong = _content_failures(frames)
    assert not wrong, "\n".join(["console misstated:", *wrong[:20]])
