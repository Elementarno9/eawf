"""A dispatched Run's whole message text is kept machine-local, redacted, and pruned.

The stream keeps each message's 500-character summary; the dispatch sink keeps the
whole text in one file per Run under the selected generation's ``local/`` directory,
each line the state-leak scan flags withheld, and names it on the stream line through
``content_artifact_ref``. ``runtime.run.content.read`` resolves that reference from the
file, and refuses it as ``content_pruned`` once the daemon's start-up prune removed
the file for being older than ``runtime.transcript.retention_days``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError

from eawf.kernel.config.layered import repo_config_path
from eawf.kernel.config.registry import LEAF_KEY_REGISTRY
from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.content import CONTENT_LINE_CHARS, WITHHELD_LINE
from eawf.kernel.runtime.events import MessageSummaryPayload
from eawf.kernel.runtime.provider import ArtifactUrn
from eawf.kernel.store.commit_policy import CommitPolicy
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.dispatch_stream import message_sink
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.main import _prune_transcripts
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.run import RUN_EVENTS_READ_METHOD
from eawf.runtime.daemon.methods.run_content import RUN_CONTENT_READ_METHOD
from eawf.runtime.daemon.run_events import RunScopedParams
from eawf.runtime.daemon.transcript_store import (
    DEFAULT_RETENTION_DAYS,
    TRANSCRIPT_DIRNAME,
    TranscriptEntry,
    keep_message,
    prune_transcripts,
    read_message,
    redacted_lines,
    resolve_retention_days,
    transcript_dir,
    transcript_ref,
)
from eawf.runtime.runtimes.host_transcript import SUMMARY_LIMIT, WITHHELD_TEXT, SpokenMessage
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
    tree_root,
)

pytestmark = pytest.mark.integration

ACTOR: Final = "OP-0001"
RUN_KEY: Final = "RUN-00000010"
RUN_URN: Final = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{RUN_KEY}"
OTHER_RUN_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011"
REPOSITORY_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
#: A fixed clock the prune is asked against, so no case depends on the wall clock.
NOW: Final = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
#: A host path shape the state-leak scan flags; built so no real path is committed.
LEAKED_PATH: Final = "/" + "/".join(("Users", "someone", "src", "app.py"))

_ARTIFACT_URN: Final = TypeAdapter(ArtifactUrn)


@pytest.fixture(autouse=True)
def hermetic_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the global config layer, canary runtime dirs and transcripts under tmp."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary admitting one repository and holding one running Run."""
    provisioned = provision(tmp_path / "repo", code="NOTE")
    seed(
        provisioned,
        {
            Epoch2Collection.REPOSITORY.value: {
                "REP-EAWF": {
                    "key": "REP-EAWF",
                    "urn": REPOSITORY_URN,
                    "revision": 1,
                    "head_sha": "a" * 40,
                    "created_at": AT.isoformat(),
                    "updated_at": AT.isoformat(),
                }
            },
            Epoch2Collection.RUN.value: {RUN_KEY: seed_row("run", "RUNNING")},
        },
    )
    return provisioned


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    return method_context(tmp_path / "runtime")


@pytest.fixture
def context(ctx: MethodContext, canary: CanaryProvision) -> Epoch2RootContext:
    return ctx.native_root_context(tree_root(canary))


def run_urn(urn: str = RUN_URN) -> QualifiedUrn:
    return RunScopedParams.model_validate({"urn": urn}).urn


def call(method: str, ctx: MethodContext, canary: CanaryProvision, **params: Any) -> dict[str, Any]:
    """Dispatch one daemon verb against the canary, the way the server does."""
    answer: dict[str, Any] = asyncio.run(
        methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params})
    )
    return answer


def say(context: Epoch2RootContext, *messages: SpokenMessage) -> None:
    """Hand *messages* to the dispatch sink of the canary's Run, in order."""
    sink = message_sink(context, run_urn(), actor=ACTOR, attempt_ref="ATT-store")

    async def run() -> None:
        for message in messages:
            await sink(message)

    asyncio.run(run())


def spoken(text: str) -> SpokenMessage:
    """Return *text* as the launchers hand a message: summary cut, whole text carried."""
    stripped = text.strip()
    return SpokenMessage(
        message_role="assistant",
        summary=stripped[:SUMMARY_LIMIT].strip(),
        truncated=len(stripped) > SUMMARY_LIMIT,
        text=stripped,
    )


def message_lines(ctx: MethodContext, canary: CanaryProvision) -> list[dict[str, Any]]:
    answer = call(RUN_EVENTS_READ_METHOD, ctx, canary, urn=RUN_URN)
    return [
        line["payload"]
        for line in answer["events"]
        if line["payload"]["payload_kind"] == "message_summary"
    ]


def run_file(context: Epoch2RootContext) -> Path:
    return transcript_dir(context) / f"{RUN_KEY.lower()}.jsonl"


def stored(context: Epoch2RootContext) -> list[TranscriptEntry]:
    return [
        TranscriptEntry.model_validate_json(line)
        for line in run_file(context).read_text(encoding="utf-8").splitlines()
    ]


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


# ---- CR-1: the store is machine-local, redacted, and linked --------------------------


def test_the_whole_text_is_kept_under_the_generation_local_dir_and_linked(
    ctx: MethodContext, canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    say(context, spoken("Reading the cache module.\nThe expiry check runs first."))

    (line,) = message_lines(ctx, canary)
    ref = line["content_artifact_ref"]
    assert _ARTIFACT_URN.validate_python(ref) == ref
    path = run_file(context)
    assert path.parent == document_path(canary).parent / "local" / TRANSCRIPT_DIRNAME
    assert context.classify(path).policy is CommitPolicy.NOT_COMMITTED
    (entry,) = stored(context)
    assert entry.ref == ref
    assert entry.lines == ("Reading the cache module.", "The expiry check runs first.")
    assert entry.withheld_lines == 0


def test_the_store_never_touches_the_committed_artifact_ledger(
    canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    artifact = ledger_path(document_path(canary), Epoch2Collection.ARTIFACT)
    before = list(read_ledger_records(artifact)) if artifact.exists() else []

    keep_message(context, run_urn(), event_ref="EVT-a", text="kept words", now=NOW)

    after = list(read_ledger_records(artifact)) if artifact.exists() else []
    assert after == before


def test_keeping_a_message_changes_no_tracked_path(
    canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    git(canary.root, "init", "-q")
    git(canary.root, "add", "-A")
    before = set(git(canary.root, "status", "--porcelain").splitlines())

    keep_message(context, run_urn(), event_ref="EVT-a", text="kept words", now=NOW)

    new = set(git(canary.root, "status", "--porcelain").splitlines()) - before
    assert new, "the kept file is visible to git as untracked"
    assert all(entry.startswith("?? ") for entry in new), new


def test_each_line_the_scan_flags_is_withheld_whole(context: Epoch2RootContext) -> None:
    lines, withheld = redacted_lines(f"first line\nopened {LEAKED_PATH}\nlast line\n")

    assert lines == ("first line", WITHHELD_LINE, "last line")
    assert withheld == 1


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_a_message_with_no_words_keeps_nothing(context: Epoch2RootContext, text: str) -> None:
    assert redacted_lines(text) == ((), 0)
    assert keep_message(context, run_urn(), event_ref="EVT-a", text=text, now=NOW) is None
    assert not run_file(context).exists()


def test_a_replayed_message_names_the_line_already_kept(context: Epoch2RootContext) -> None:
    first = keep_message(context, run_urn(), event_ref="EVT-a", text="once", now=NOW)
    again = keep_message(context, run_urn(), event_ref="EVT-a", text="once", now=NOW)
    other = keep_message(context, run_urn(), event_ref="EVT-b", text="twice", now=NOW)

    assert first == again == transcript_ref(run_urn(), "EVT-a")
    assert other != first
    assert [entry.ref for entry in stored(context)] == [first, other]


def test_a_plain_summary_carries_no_whole_text_and_keeps_nothing(
    ctx: MethodContext, canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    sink = message_sink(context, run_urn(), actor=ACTOR, attempt_ref="ATT-plain")

    async def run() -> None:
        await sink(MessageSummaryPayload(message_role="assistant", summary="short"))

    asyncio.run(run())

    (line,) = message_lines(ctx, canary)
    assert line["content_artifact_ref"] is None
    assert not run_file(context).exists()


def test_the_carrier_never_serialises_its_whole_text() -> None:
    message = spoken("x" * (SUMMARY_LIMIT + 1))

    assert "text" not in message.model_dump()
    assert "x" * (SUMMARY_LIMIT + 1) not in message.model_dump_json()


def test_a_stored_entry_refuses_an_unknown_key() -> None:
    with pytest.raises(ValidationError):
        TranscriptEntry.model_validate(
            {
                "ref": "artifact://transcript/run-1/evt-a",
                "lines": ["a"],
                "withheld_lines": 0,
                "recorded_at": NOW.isoformat(),
                "extra": 1,
            }
        )


def test_a_stored_entry_refuses_a_ref_outside_the_artifact_grammar() -> None:
    with pytest.raises(ValidationError):
        TranscriptEntry(ref=LEAKED_PATH, lines=("a",), withheld_lines=0, recorded_at=NOW)


# ---- CR-4: long and leaking messages -------------------------------------------------


def test_a_long_message_stays_truncated_on_the_stream_and_whole_in_the_store(
    ctx: MethodContext, canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    whole = " ".join(f"word{index}" for index in range(200))
    assert len(whole) > SUMMARY_LIMIT

    say(context, spoken(whole))

    (line,) = message_lines(ctx, canary)
    assert line["truncated"] is True
    assert len(line["summary"]) <= SUMMARY_LIMIT
    (entry,) = stored(context)
    assert entry.lines == (whole,)
    read = call(
        RUN_CONTENT_READ_METHOD, ctx, canary, urn=RUN_URN, refs=[line["content_artifact_ref"]]
    )
    (content,) = read["contents"]
    assert all(len(piece) <= CONTENT_LINE_CHARS for piece in content["lines"])
    assert "".join(content["lines"]) == whole
    assert content["total_lines"] == len(content["lines"])


def test_a_message_with_a_path_is_withheld_on_the_stream_and_by_line_in_the_store(
    ctx: MethodContext, canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    text = f"I read the module.\nIt lives at {LEAKED_PATH}\nThe check runs first."
    message = SpokenMessage(
        message_role="assistant", summary=WITHHELD_TEXT, truncated=True, text=text
    )

    say(context, message)

    (line,) = message_lines(ctx, canary)
    assert line["summary"] == WITHHELD_TEXT
    (entry,) = stored(context)
    assert entry.lines == ("I read the module.", WITHHELD_LINE, "The check runs first.")
    assert entry.withheld_lines == 1
    assert LEAKED_PATH not in run_file(context).read_text(encoding="utf-8")


# ---- CR-2: the read resolves from the store, or refuses a pruned file -----------------


def test_the_read_resolves_a_present_ref_and_refuses_a_pruned_one(
    ctx: MethodContext, canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    say(context, spoken("Reading the cache module."))
    (line,) = message_lines(ctx, canary)
    ref = line["content_artifact_ref"]
    unknown = transcript_ref(run_urn(), "EVT-unknown")

    present = call(RUN_CONTENT_READ_METHOD, ctx, canary, urn=RUN_URN, refs=[ref, unknown])
    assert [content["lines"] for content in present["contents"]] == [["Reading the cache module."]]
    assert present["missing"] == [unknown]
    assert present["refused"] == []

    run_file(context).unlink()
    pruned = call(RUN_CONTENT_READ_METHOD, ctx, canary, urn=RUN_URN, refs=[ref])
    assert pruned["contents"] == []
    assert pruned["missing"] == []
    assert pruned["refused"] == [{"ref": ref, "code": "content_pruned"}]


def test_a_ref_of_another_run_is_missing_not_pruned(context: Epoch2RootContext) -> None:
    keep_message(context, run_urn(), event_ref="EVT-a", text="kept", now=NOW)
    foreign = transcript_ref(run_urn(OTHER_RUN_URN), "EVT-a")

    read = read_message(context, run_urn(), foreign)

    assert read.content is None
    assert read.pruned is False


def test_a_read_over_more_lines_than_a_content_holds_states_what_it_cut(
    context: Epoch2RootContext,
) -> None:
    text = "\n".join(f"line {index}" for index in range(250))
    ref = keep_message(context, run_urn(), event_ref="EVT-a", text=text, now=NOW)
    assert ref is not None

    read = read_message(context, run_urn(), ref)

    assert read.content is not None
    assert len(read.content.lines) == 200
    assert read.content.total_lines == 250
    assert read.content.unkept == 50


# ---- CR-3: retention -------------------------------------------------------------------


def _aged(directory: Path, name: str, days: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("{}\n", encoding="utf-8")
    stamp = (NOW - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def test_the_daemon_start_prune_removes_only_files_older_than_the_retention(
    canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    directory = transcript_dir(context)
    young = _aged(directory, "run-00000029.jsonl", 29)
    edge = _aged(directory, "run-00000030.jsonl", 30)
    old = _aged(directory, "run-00000031.jsonl", 31)
    stranger = _aged(directory, "notes.txt", 90)
    sibling = _aged(directory.parent, "run-00000099.jsonl", 90)

    removed = _prune_transcripts((tree_root(canary),), now=NOW)

    assert removed == 1
    assert (young.exists(), edge.exists(), old.exists()) == (True, True, False)
    assert stranger.exists() and sibling.exists()


def test_the_prune_reads_the_configured_retention(
    canary: CanaryProvision, context: Epoch2RootContext
) -> None:
    config = repo_config_path(canary.root)
    body = yaml.safe_load(config.read_text(encoding="utf-8")) if config.exists() else None
    layer = body if isinstance(body, dict) else {}
    layer.setdefault("runtime", {})["transcript"] = {"retention_days": 29}
    config.write_text(yaml.safe_dump(layer), encoding="utf-8")
    directory = transcript_dir(context)
    files = [_aged(directory, f"run-0000002{days - 20}.jsonl", days) for days in (28, 29, 30)]

    removed = _prune_transcripts((tree_root(canary),), now=NOW)

    assert removed == 1
    assert [path.exists() for path in files] == [True, True, False]


def test_a_tree_with_no_transcripts_or_no_native_state_prunes_nothing(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    assert _prune_transcripts((tree_root(canary), tmp_path / "absent" / ".ea"), now=NOW) == 0
    assert prune_transcripts(tmp_path / "absent", retention_days=1, now=NOW) == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_RETENTION_DAYS),
        (1, 1),
        (365, 365),
        (0, DEFAULT_RETENTION_DAYS),
        (366, DEFAULT_RETENTION_DAYS),
        ("30", DEFAULT_RETENTION_DAYS),
        (True, DEFAULT_RETENTION_DAYS),
        (7.5, DEFAULT_RETENTION_DAYS),
    ],
)
def test_the_retention_leaf_is_read_within_its_range(
    tmp_path: Path, raw: object, expected: int
) -> None:
    repo = tmp_path / "plain"
    config = repo_config_path(repo)
    config.parent.mkdir(parents=True)
    if raw is not None:
        config.write_text(
            yaml.safe_dump({"runtime": {"transcript": {"retention_days": raw}}}), encoding="utf-8"
        )

    assert resolve_retention_days(repo) == expected


def test_the_retention_leaf_is_catalogued() -> None:
    leaf = LEAF_KEY_REGISTRY["runtime.transcript.retention_days"]

    assert (leaf.type, leaf.default, leaf.value_range) == ("int", 30, (1, 365))
    assert leaf.consumer == "eawf.runtime.daemon.transcript_store.resolve_retention_days"
