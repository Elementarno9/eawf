"""The commit policy keeps Run exhaust local and the ground-truth set committed.

Run transcripts and the receipts of a Run's tool calls are exhaust: they are
appended to the selected generation's local ledger, which no clone carries.
What a clone needs to rebuild or verify the project -- the rule corpus, the
profile, the requirement catalog, Task and plan definitions, decisions, open
questions, the release notes, the package version, proof receipts and release
checkpoint receipts -- is committed. Moving exhaust out of version control
must never delete what a tree already tracks: lines committed before the
split stay in the committed ledger and are still read, and a state commit
stages only paths the policy commits.
"""

from __future__ import annotations

import subprocess  # noqa: EAWF024 -- the state commit is git's own; its pinned criterion gate lives here
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.runtime.content import CONTENT_PAYLOAD_KIND
from eawf.kernel.store.commit_policy import (
    LOCAL_LEDGER_ROWS,
    CommitPolicy,
    UndeclaredPathError,
    census_findings,
    classify_path,
    ledger_row_policy,
)
from eawf.kernel.store.ledger import (
    LedgerRecord,
    LedgerTornTailError,
    append_ledger_record,
    read_collection_records,
    read_ledger_records,
    render_ledger_line,
)
from eawf.kernel.store.paths import collection_ledger_paths, ledger_path, record_ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import epoch2_transaction
from eawf.runtime.daemon.epoch2_recovery import repair_native_ledger_tails, replay_native_wal
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.host_calls import HOST_RECEIPT_PAYLOAD_KIND
from eawf.runtime.daemon.semantic_gateway import RECEIPT_PAYLOAD_KIND
from eawf.workflow.integration.publish import Membership, publish
from eawf.workflow.integration.publish_binding import StagedBinding
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
    tree_root,
)

_GEN: Final = ".ea/generations/gen-0123456789abcdef"
_RUN_KEY: Final = "RUN-00000010"
_RUN_URN: Final = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{_RUN_KEY}"

#: One representative path per member of the ground-truth set.
COMMITTED_PATHS: Final[tuple[str, ...]] = (
    ".ea/rules.yaml",
    ".ea/profile.yaml",
    ".ea/requirements.json",
    f"{_GEN}/state.json",
    f"{_GEN}/ledger/task.jsonl",
    f"{_GEN}/ledger/plan_revision.jsonl",
    f"{_GEN}/ledger/decision.jsonl",
    f"{_GEN}/ledger/open_question.jsonl",
    ".ea/store/release_checkpoint_receipt.jsonl",
    "CHANGELOG.md",
    "src/eawf/_version.py",
)

#: One representative path per kind of Run exhaust a whole file carries.
LOCAL_PATHS: Final[tuple[str, ...]] = (
    f"{_GEN}/local/transcripts/run-00000001.jsonl",
    f"{_GEN}/local/ledger/run.jsonl",
    f"{_GEN}/local/status.json",
    ".ea/store/event.jsonl",
    ".ea/local/research/brief.md",
    ".ea/locks/state.lock",
)

#: Ledger lines that share a collection, each with the verdict its kind carries.
LEDGER_LINES: Final[tuple[tuple[Epoch2Collection, str, CommitPolicy], ...]] = (
    (Epoch2Collection.ARTIFACT, "content_artifact", CommitPolicy.NOT_COMMITTED),
    (Epoch2Collection.RECEIPT, "host_call_receipt", CommitPolicy.NOT_COMMITTED),
    (Epoch2Collection.RECEIPT, "semantic_receipt", CommitPolicy.NOT_COMMITTED),
    (Epoch2Collection.RECEIPT, "proof_receipt", CommitPolicy.COMMITTED),
    (Epoch2Collection.RECEIPT, "sandbox_decision", CommitPolicy.COMMITTED),
    (Epoch2Collection.ARTIFACT, "artifact_revision", CommitPolicy.COMMITTED),
    (Epoch2Collection.ARTIFACT, "spike_report", CommitPolicy.COMMITTED),
)


def _record(collection: Epoch2Collection, kind: str | None, key: str) -> LedgerRecord:
    payload: dict[str, Any] = {"note": key}
    if kind is not None:
        payload["payload_kind"] = kind
    return LedgerRecord(
        collection=collection, record_key=key, status="recorded", recorded_at=AT, payload=payload
    )


# ---- CR-724: the verdict of each representative path and line ---------------

#: Every representative path and ledger line with the verdict it must carry: the
#: committed half and the local half are pinned by one gate, so removing either
#: side of the split fails it.
CLASSIFIED: Final = (
    *(
        pytest.param(path, CommitPolicy.COMMITTED, id=f"committed:{path}")
        for path in COMMITTED_PATHS
    ),
    *(pytest.param(path, CommitPolicy.NOT_COMMITTED, id=f"local:{path}") for path in LOCAL_PATHS),
    *(
        pytest.param((collection, kind), policy, id=f"line:{collection.value}:{kind}")
        for collection, kind, policy in LEDGER_LINES
    ),
)


@pytest.mark.parametrize(("subject", "policy"), CLASSIFIED)
def test_classification(subject: str | tuple[Epoch2Collection, str], policy: CommitPolicy) -> None:
    """Each path and ledger line carries its verdict; a line's file carries the same."""
    if isinstance(subject, str):
        assert classify_path(subject).policy is policy
        return
    collection, kind = subject
    state = Path(_GEN) / "state.json"
    line_path = record_ledger_path(state, collection, {"payload_kind": kind})

    assert ledger_row_policy(collection, {"payload_kind": kind}) is policy
    assert classify_path(line_path.as_posix()).policy is policy


def test_classification_names_the_kinds_the_writers_file() -> None:
    """The declared kinds are the ones the three exhaust writers stamp on their lines."""
    declared = dict(LOCAL_LEDGER_ROWS)
    assert declared == {
        Epoch2Collection.ARTIFACT: {CONTENT_PAYLOAD_KIND},
        Epoch2Collection.RECEIPT: {HOST_RECEIPT_PAYLOAD_KIND, RECEIPT_PAYLOAD_KIND},
    }


def test_ledger_row_policy_of_a_line_without_a_kind() -> None:
    """A gate receipt carries no payload kind and stays committed."""
    assert ledger_row_policy(Epoch2Collection.RECEIPT, {}) is CommitPolicy.COMMITTED


def test_ledger_row_policy_of_an_exhaust_kind_in_another_collection() -> None:
    """The split is per collection: an exhaust kind elsewhere is committed."""
    payload = {"payload_kind": CONTENT_PAYLOAD_KIND}
    assert ledger_row_policy(Epoch2Collection.TASK, payload) is CommitPolicy.COMMITTED


def test_ledger_row_policy_of_a_non_string_kind() -> None:
    """A kind of the wrong type matches no declared kind."""
    assert (
        ledger_row_policy(Epoch2Collection.RECEIPT, {"payload_kind": 7}) is CommitPolicy.COMMITTED
    )


def test_classification_of_a_single_segment_root_file() -> None:
    """A root file the table does not declare is refused, not defaulted."""
    with pytest.raises(UndeclaredPathError):
        classify_path("CHANGELOG.rst")


def test_classification_of_a_near_miss_version_path() -> None:
    """Only the version module itself is declared, not its siblings."""
    with pytest.raises(UndeclaredPathError):
        classify_path("src/eawf/_version.pyc")


def test_classification_folds_back_slashes() -> None:
    """A Windows-spelled ground-truth path classifies like its forward form."""
    assert classify_path("src\\eawf\\_version.py").policy is CommitPolicy.COMMITTED


def test_census_reports_no_finding_for_the_root_ground_truth_files() -> None:
    """Root ground-truth rows sit outside the census surface, so tracking them is clean."""
    assert census_findings(tracked=("CHANGELOG.md", "src/eawf/_version.py"), ignored_probes=()) == (
        census_findings(tracked=(), ignored_probes=())
    )


# ---- where a line lands, and how a reader finds it again --------------------


def test_collection_ledger_paths_of_an_unsplit_collection(tmp_path: Path) -> None:
    """A collection with no exhaust kinds is one committed file."""
    state = tmp_path / "state.json"
    assert collection_ledger_paths(state, Epoch2Collection.TASK) == (
        ledger_path(state, Epoch2Collection.TASK),
    )


def test_collection_ledger_paths_of_a_split_collection(tmp_path: Path) -> None:
    """A split collection is the committed file, then the local one."""
    state = tmp_path / "state.json"
    assert collection_ledger_paths(state, Epoch2Collection.RECEIPT) == (
        tmp_path / "ledger" / "receipt.jsonl",
        tmp_path / "local" / "ledger" / "receipt.jsonl",
    )


def test_ledger_paths_refuse_a_collection_with_no_ledger(tmp_path: Path) -> None:
    """A document-tier collection has no ledger file to route to."""
    state = tmp_path / "state.json"
    with pytest.raises(ValueError, match="not ledger"):
        collection_ledger_paths(state, Epoch2Collection.WORKSPACE)
    with pytest.raises(ValueError, match="not ledger"):
        record_ledger_path(state, Epoch2Collection.WORKSPACE, {})


def test_read_collection_records_of_an_empty_collection(tmp_path: Path) -> None:
    """Neither file exists, so the collection reads as empty."""
    assert read_collection_records(tmp_path / "state.json", Epoch2Collection.ARTIFACT) == ()


def test_read_collection_records_reads_committed_then_local(tmp_path: Path) -> None:
    """Lines committed before the split are still read, ahead of the local ones."""
    state = tmp_path / "state.json"
    committed, local = collection_ledger_paths(state, Epoch2Collection.ARTIFACT)
    old = _record(Epoch2Collection.ARTIFACT, CONTENT_PAYLOAD_KIND, "old-content")
    audit = _record(Epoch2Collection.ARTIFACT, None, "A-0001")
    new = _record(Epoch2Collection.ARTIFACT, CONTENT_PAYLOAD_KIND, "new-content")
    append_ledger_record(committed, old)
    append_ledger_record(committed, audit)
    append_ledger_record(local, new)

    assert read_collection_records(state, Epoch2Collection.ARTIFACT) == (old, audit, new)


def test_read_collection_records_of_a_local_only_collection(tmp_path: Path) -> None:
    """A clone-free tree with only local lines reads them."""
    state = tmp_path / "state.json"
    _, local = collection_ledger_paths(state, Epoch2Collection.RECEIPT)
    line = _record(Epoch2Collection.RECEIPT, HOST_RECEIPT_PAYLOAD_KIND, "call-1")
    append_ledger_record(local, line)

    assert read_collection_records(state, Epoch2Collection.RECEIPT) == (line,)


def test_read_collection_records_reads_a_line_held_twice_once(tmp_path: Path) -> None:
    """A local line identical to a committed one is read once."""
    state = tmp_path / "state.json"
    committed, local = collection_ledger_paths(state, Epoch2Collection.RECEIPT)
    line = _record(Epoch2Collection.RECEIPT, RECEIPT_PAYLOAD_KIND, "call-2")
    append_ledger_record(committed, line)
    append_ledger_record(local, line)

    assert read_collection_records(state, Epoch2Collection.RECEIPT) == (line,)


def test_read_collection_records_refuses_a_torn_local_ledger(tmp_path: Path) -> None:
    """A local file ending mid-line is refused rather than half read."""
    state = tmp_path / "state.json"
    _, local = collection_ledger_paths(state, Epoch2Collection.RECEIPT)
    local.parent.mkdir(parents=True)
    local.write_text('{"schema_version"', encoding="utf-8")

    with pytest.raises(LedgerTornTailError):
        read_collection_records(state, Epoch2Collection.RECEIPT)


def test_read_collection_records_refuses_a_collection_with_no_ledger(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not ledger"):
        read_collection_records(tmp_path / "state.json", Epoch2Collection.WORKSPACE)


@pytest.fixture
def canary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CanaryProvision:
    """A provisioned canary holding one running Run."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    provisioned = provision(tmp_path / "repo")
    seed(provisioned, {"run": {_RUN_KEY: seed_row("run", "RUNNING")}})
    return provisioned


def test_exhaust_appends_land_in_the_local_ledger(tmp_path: Path, canary: CanaryProvision) -> None:
    """The one ledger append files exhaust locally and leaves the committed document as it was."""
    context = method_context(tmp_path / "runtime").native_root_context(tree_root(canary))
    state = document_path(canary)
    committed, local = collection_ledger_paths(state, Epoch2Collection.RECEIPT)
    exhaust = _record(Epoch2Collection.RECEIPT, HOST_RECEIPT_PAYLOAD_KIND, "call-3")
    proof = _record(Epoch2Collection.RECEIPT, "proof_receipt", "PRF-0001")
    before = state.read_bytes()

    with context.session([_RUN_URN]) as session:
        commit_ledger_append(session, exhaust)
    after_exhaust = state.read_bytes()
    with context.session([_RUN_URN]) as session:
        commit_ledger_append(session, proof)

    assert read_ledger_records(local) == (exhaust,)
    assert read_ledger_records(committed) == (proof,)
    assert after_exhaust == before
    assert state.read_bytes() != before
    assert read_collection_records(state, Epoch2Collection.RECEIPT) == (proof, exhaust)


def test_replay_files_a_journalled_exhaust_line_locally(
    tmp_path: Path, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A line the WAL journalled before a crash is replayed into the file it was meant for."""
    context = method_context(tmp_path / "runtime").native_root_context(tree_root(canary))
    state = document_path(canary)
    committed, local = collection_ledger_paths(state, Epoch2Collection.ARTIFACT)
    line = _record(Epoch2Collection.ARTIFACT, CONTENT_PAYLOAD_KIND, "artifact://content/x")

    class _Kill(BaseException):
        pass

    def killed(_path: Path, _record: LedgerRecord, **_kwargs: Any) -> int:
        raise _Kill

    monkeypatch.setattr(epoch2_transaction, "append_ledger_record", killed)
    with pytest.raises(_Kill), context.session([_RUN_URN]) as session:
        commit_ledger_append(session, line)
    monkeypatch.undo()

    report = replay_native_wal(context.wal_dir.parent.parent)

    assert report.replayed_ledger_count == 1
    assert read_ledger_records(local) == (line,)
    assert not committed.exists()


def test_boot_repair_cuts_a_torn_local_ledger(tmp_path: Path, canary: CanaryProvision) -> None:
    """The boot pass repairs the local exhaust ledger as it does the committed one."""
    state = document_path(canary)
    _, local = collection_ledger_paths(state, Epoch2Collection.RECEIPT)
    whole = render_ledger_line(_record(Epoch2Collection.RECEIPT, RECEIPT_PAYLOAD_KIND, "call-4"))
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(f'{whole}\n{{"torn', encoding="utf-8")

    report = repair_native_ledger_tails(
        tmp_path / "runtime" / "wal", tree_roots=[tree_root(canary)]
    )

    assert report.truncated_ledgers == 1
    assert local.read_text(encoding="utf-8") == f"{whole}\n"


# ---- CR-725: a tree that tracks exhaust loses none of it --------------------


def _git(repo: Path, *args: str) -> str:
    """Run one git command in *repo* and return its stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_no_tracked_deletion(tmp_path: Path) -> None:
    """A policy-driven state commit stages no deletion of tracked exhaust.

    The fixture tracks a committed artifact ledger holding transcript lines,
    a transcript and a Run status file, as a tree written before the split
    does. The bind step removes the two local files from disk, files a new
    transcript line locally and a proof line in the committed receipt
    ledger; the state commit carries only the proof line and every tracked
    path stays tracked.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "ci@example.com")
    _git(repo, "config", "user.name", "ci")
    _git(repo, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    exhaust = (f"{_GEN}/local/transcripts/run-00000001.jsonl", f"{_GEN}/local/status.json")
    committed_artifacts = f"{_GEN}/ledger/artifact.jsonl"
    for path in exhaust:
        _write(repo, path, "{}\n")
    _write(repo, committed_artifacts, '{"payload_kind": "content_artifact"}\n')
    _write(repo, f"{_GEN}/ledger/receipt.jsonl", '{"proof": 1}\n')
    _write(repo, "src/module.py", "x = 1\n")
    _git(
        repo,
        "add",
        "-f",
        "--",
        *exhaust,
        committed_artifacts,
        f"{_GEN}/ledger/receipt.jsonl",
        "src/module.py",
    )
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", base)
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _git(repo, "checkout", "-q", "-b", "feature/work")
    _write(repo, "src/module.py", "x = 2\n")
    _git(repo, "commit", "-q", "-am", "feat: change the module\n\nTask: EAWF-0042")
    work = _git(repo, "rev-parse", "HEAD")
    staged = StagedBinding.model_validate(
        {
            "batch_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0007",
            "task_refs": ["eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"],
            "tree_sha": _git(repo, "rev-parse", f"{work}^{{tree}}"),
            "head_sha": work,
            "report_verdict": "pass",
            "evidence_refs": ["A-0001"],
            "staged_by": "OP-0001",
            "staged_at": "2026-11-01T00:00:00Z",
        }
    )
    state = repo / _GEN / "state.json"
    local_line = _record(Epoch2Collection.ARTIFACT, CONTENT_PAYLOAD_KIND, "new-content")

    def bind(_branch: str, _targets: object) -> int:
        for path in exhaust:
            (repo / path).unlink()
        append_ledger_record(
            record_ledger_path(state, Epoch2Collection.ARTIFACT, local_line.payload), local_line
        )
        with (repo / _GEN / "ledger" / "receipt.jsonl").open("a", encoding="utf-8") as ledger:
            ledger.write('{"proof": 2}\n')
        return 1

    result = publish(
        repo,
        membership=Membership(task_batches={"EAWF-0042": "BAT-0007"}, staged=(staged,)),
        bind=bind,
    )

    assert result.state_commit is not None
    changed = _git(repo, "show", "--name-status", "--format=", result.state_commit).splitlines()
    assert changed == [f"M\t{_GEN}/ledger/receipt.jsonl"]
    tracked = set(_git(repo, "ls-files").splitlines())
    assert {*exhaust, committed_artifacts} <= tracked
    assert _git(repo, "diff", "--cached", "--name-status") == ""
