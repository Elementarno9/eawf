"""Progress manifests produced through the command-side channel, and liveness.

The executed command publishes its collection and each obligation's outcome
through :class:`~eawf.runtime.verification.progress.ProgressChannel`; the
gate child's :class:`~eawf.runtime.verification.progress.ProgressPublisher`
folds that channel into the durable manifest. Nothing is inferred from what
the command prints, and a command that publishes nothing still yields a
manifest carrying liveness.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest
from pydantic import ValidationError

from eawf.runtime.daemon import gate_execution
from eawf.runtime.lock import portalock
from eawf.runtime.verification import progress
from eawf.runtime.verification.progress import (
    OUTPUT_TAIL_LIMIT,
    PROGRESS_CHANNEL_FILENAME,
    PROGRESS_EVENTS_FILENAME,
    LegIdentity,
    LegOutcome,
    ObligationDisposition,
    ObligationRecord,
    ProgressChannel,
    ProgressEvent,
    ProgressManifest,
    ProgressMode,
    ProgressPublisher,
    ResidueSeed,
    collection_digest,
    progress_manifest_path,
    read_progress_manifest,
)
from eawf.workflow.audit_dsl.models import CheckSpec

pytestmark = pytest.mark.unit

_KEY = "a" * 64
_OTHER_KEY = "b" * 64
_PRODUCER = "sha256:" + "c" * 64
#: A publisher interval long enough that only explicit pumps write.
_QUIET_INTERVAL = 3_600.0


def _claim(state_path: Path, *, key: str = _KEY, attempt_id: str = "CA-01") -> LegIdentity:
    """Write a durable claim for *key* and return the identity it grants."""
    spec = CheckSpec(
        kind="command_exit_zero",
        name="G-01",
        args={"argv": ["pytest", "-q"], "scope": "all"},
    )
    claimed = gate_execution.claim_gate_execution(
        state_path,
        attempt_id=attempt_id,
        criterion_id="CR-01",
        gate_id="G-01",
        spec=spec,
        freshness_key=key,
    )
    assert claimed is None
    claim = gate_execution.load_gate_claim(state_path, key)
    assert claim is not None
    return LegIdentity(
        attempt_id=claim.attempt_id,
        criterion_id=claim.criterion_id,
        gate_id=claim.gate_id,
        freshness_key=claim.freshness_key,
        claimed_at=claim.claimed_at,
    )


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    """A live ledger path whose ``.ea/local`` holds claims and manifests."""
    path = tmp_path / "live" / ".ea" / "state.json"
    path.parent.mkdir(parents=True)
    return path


@pytest.fixture
def channel_dir(tmp_path: Path) -> Path:
    """The sandbox runtime directory a gate child offers its channel in."""
    path = tmp_path / "sandbox-runtime"
    path.mkdir()
    return path


@pytest.fixture
def make_publisher(
    state_path: Path, channel_dir: Path
) -> Iterator[Callable[..., ProgressPublisher]]:
    """Build publishers for a claimed leg; every one is closed at teardown."""
    opened: list[ProgressPublisher] = []

    def _make(**overrides: Any) -> ProgressPublisher:
        options: dict[str, Any] = {
            "state_path": state_path,
            "leg": overrides.pop("leg", None) or _claim(state_path),
            "producer_digest": _PRODUCER,
            "resolved_timeout_seconds": 60,
            "channel_dir": channel_dir,
            "interval_seconds": _QUIET_INTERVAL,
        }
        options.update(overrides)
        publisher = ProgressPublisher(**options)
        opened.append(publisher)
        return publisher

    yield _make
    for publisher in opened:
        publisher.close(outcome=LegOutcome.ERRORED)


def _channel(channel_dir: Path) -> ProgressChannel:
    channel = ProgressChannel.from_env({"EAWF_RUNTIME_DIR": str(channel_dir)})
    assert channel is not None, "the publisher did not offer a channel"
    return channel


def test_progress_channel_from_env_returns_none_without_runtime_dir() -> None:
    assert ProgressChannel.from_env({}) is None


def test_progress_channel_from_env_returns_none_without_offered_channel(channel_dir: Path) -> None:
    assert ProgressChannel.from_env({"EAWF_RUNTIME_DIR": str(channel_dir)}) is None


def test_progress_channel_from_env_returns_none_for_malformed_descriptor(
    channel_dir: Path,
) -> None:
    (channel_dir / PROGRESS_CHANNEL_FILENAME).write_text("{not json", encoding="utf-8")

    assert ProgressChannel.from_env({"EAWF_RUNTIME_DIR": str(channel_dir)}) is None


def test_progress_publisher_folds_command_channel_into_manifest(
    state_path: Path,
    channel_dir: Path,
    make_publisher: Callable[..., ProgressPublisher],
) -> None:
    """The manifest advances as the command reports, and a reader sees it mid-run."""
    publisher = make_publisher()
    first = publisher.open()
    assert first.progress_mode is ProgressMode.NONE
    channel = _channel(channel_dir)

    selected = channel.collect(["t::a", "t::b", "t::c"])
    channel.record("t::a", ObligationDisposition.PASS)
    mid = publisher.pump()

    assert selected == ("t::a", "t::b", "t::c")
    assert mid.outcome is LegOutcome.RUNNING
    assert mid.progress_mode is ProgressMode.ENUMERATED
    assert mid.collected == ("t::a", "t::b", "t::c")
    assert [(r.obligation_id, r.disposition) for r in mid.obligations] == [
        ("t::a", ObligationDisposition.PASS)
    ]
    assert mid.pending() == ("t::b", "t::c")
    assert mid.cursor == 2
    assert read_progress_manifest(state_path, _KEY) == mid

    channel.record("t::b", ObligationDisposition.FAIL)
    later = publisher.pump()

    assert later.cursor == 3
    assert later.pending() == ("t::b", "t::c")
    assert read_progress_manifest(state_path, _KEY) == later


def test_progress_publisher_never_infers_progress_from_command_output(
    channel_dir: Path,
    make_publisher: Callable[..., ProgressPublisher],
) -> None:
    """Output that reads like progress is a tail, never an obligation."""
    publisher = make_publisher()
    publisher.open()
    harness_output = "t::a PASSED\nt::b PASSED\ncollected 2 items\n"

    final = publisher.close(outcome=LegOutcome.PASSED, stdout_tail=harness_output)

    assert final.progress_mode is ProgressMode.NONE
    assert final.collected is None
    assert final.obligations == ()
    assert final.liveness.stdout_tail == harness_output
    assert not (channel_dir / PROGRESS_CHANNEL_FILENAME).exists(), "the channel outlived the leg"


def test_progress_publisher_publishes_liveness_when_progress_mode_none(
    state_path: Path,
    make_publisher: Callable[..., ProgressPublisher],
) -> None:
    """A leg offered no channel still publishes start, budget, elapsed and heartbeat."""
    publisher = make_publisher(channel_dir=None, resolved_timeout_seconds=900)

    first = publisher.open()
    time.sleep(0.02)
    second = publisher.pump()

    assert first.progress_mode is ProgressMode.NONE
    assert first.outcome is LegOutcome.RUNNING
    assert first.liveness.resolved_timeout_seconds == 900
    assert first.liveness.started_at <= first.liveness.heartbeat_at
    assert second.liveness.started_at == first.liveness.started_at
    assert second.liveness.heartbeat_at > first.liveness.heartbeat_at
    assert second.liveness.elapsed_ms >= first.liveness.elapsed_ms
    assert first.liveness.stdout_tail is None
    assert read_progress_manifest(state_path, _KEY) == second

    final = publisher.close(outcome=LegOutcome.TIMED_OUT, stderr_tail="still waiting")

    assert final.outcome is LegOutcome.TIMED_OUT
    assert final.liveness.stderr_tail == "still waiting"


def test_progress_publisher_heartbeat_republishes_without_a_pump(
    state_path: Path,
    make_publisher: Callable[..., ProgressPublisher],
) -> None:
    """The bounded write cadence keeps an opaque leg's heartbeat moving on its own."""
    publisher = make_publisher(channel_dir=None, interval_seconds=0.05)
    first = publisher.open()

    deadline = time.monotonic() + 5.0
    latest = first
    while time.monotonic() < deadline:
        seen = read_progress_manifest(state_path, _KEY)
        assert seen is not None
        latest = seen
        if latest.liveness.heartbeat_at > first.liveness.heartbeat_at:
            break
        time.sleep(0.02)

    assert latest.liveness.heartbeat_at > first.liveness.heartbeat_at
    publisher.close(outcome=LegOutcome.PASSED)


def test_progress_publisher_close_bounds_output_tails(
    make_publisher: Callable[..., ProgressPublisher],
) -> None:
    publisher = make_publisher(channel_dir=None)
    publisher.open()
    long_output = "x" * (OUTPUT_TAIL_LIMIT + 10) + "END"

    final = publisher.close(outcome=LegOutcome.FAILED, stdout_tail=long_output)

    assert final.liveness.stdout_tail is not None
    assert len(final.liveness.stdout_tail) == OUTPUT_TAIL_LIMIT
    assert final.liveness.stdout_tail.endswith("END")


def test_progress_publisher_close_rejects_running_outcome(
    make_publisher: Callable[..., ProgressPublisher],
) -> None:
    publisher = make_publisher(channel_dir=None)
    publisher.open()

    with pytest.raises(ValueError, match="cannot still be running"):
        publisher.close(outcome=LegOutcome.RUNNING)


def test_progress_publisher_rejects_nonpositive_interval(
    make_publisher: Callable[..., ProgressPublisher],
) -> None:
    with pytest.raises(ValueError, match="interval_seconds must be positive"):
        make_publisher(interval_seconds=0)


def test_progress_publisher_leaves_a_torn_line_for_the_next_pump(
    channel_dir: Path,
    make_publisher: Callable[..., ProgressPublisher],
) -> None:
    """An event the command is still writing is never half-read."""
    publisher = make_publisher()
    publisher.open()
    event = ProgressEvent(freshness_key=_KEY, recorded_at=datetime.now(UTC), collected=("t::a",))
    line = orjson.dumps(event.model_dump(mode="json")) + b"\n"
    events = channel_dir / PROGRESS_EVENTS_FILENAME

    events.write_bytes(line[:10])
    torn = publisher.pump()
    events.write_bytes(line)
    whole = publisher.pump()

    assert torn.cursor == 0
    assert torn.collected is None
    assert whole.cursor == 1
    assert whole.collected == ("t::a",)


def test_progress_publisher_drops_events_keyed_to_another_leg(
    channel_dir: Path,
    make_publisher: Callable[..., ProgressPublisher],
) -> None:
    publisher = make_publisher()
    publisher.open()
    foreign = ProgressEvent(
        freshness_key=_OTHER_KEY, recorded_at=datetime.now(UTC), collected=("t::x",)
    )
    (channel_dir / PROGRESS_EVENTS_FILENAME).write_bytes(
        orjson.dumps(foreign.model_dump(mode="json")) + b"\n"
    )

    manifest = publisher.pump()

    assert manifest.cursor == 1
    assert manifest.collected is None


def test_progress_publisher_drops_obligations_outside_the_collection(
    channel_dir: Path,
    make_publisher: Callable[..., ProgressPublisher],
) -> None:
    publisher = make_publisher()
    publisher.open()
    channel = _channel(channel_dir)
    channel.record("t::early", ObligationDisposition.PASS)
    channel.collect(["t::a"])
    channel.record("t::stray", ObligationDisposition.PASS)

    manifest = publisher.pump()

    assert manifest.cursor == 3
    assert manifest.obligations == ()


def test_progress_publisher_carries_seed_passes_for_the_planned_collection(
    channel_dir: Path,
    make_publisher: Callable[..., ProgressPublisher],
) -> None:
    collected = ("t::a", "t::b", "t::c")
    carried = ObligationRecord(
        obligation_id="t::a",
        disposition=ObligationDisposition.PASS,
        completed_at=datetime.now(UTC),
        carried=True,
    )
    seed = ResidueSeed(
        resumed_from=_OTHER_KEY,
        collection_digest=collection_digest(collected),
        residue=("t::b", "t::c"),
        carried=(carried,),
    )
    publisher = make_publisher(seed=seed)
    publisher.open()
    channel = _channel(channel_dir)

    selected = channel.collect(list(collected))
    channel.record("t::b", ObligationDisposition.PASS)
    channel.record("t::c", ObligationDisposition.PASS)
    final = publisher.close(outcome=LegOutcome.PASSED)

    assert selected == ("t::b", "t::c")
    assert final.seed == seed
    assert [r.obligation_id for r in final.obligations] == list(collected)
    assert final.obligations[0].carried is True
    assert final.pending() == ()


def test_progress_channel_collect_runs_everything_when_the_collection_changed(
    channel_dir: Path,
    make_publisher: Callable[..., ProgressPublisher],
) -> None:
    seed = ResidueSeed(
        resumed_from=_OTHER_KEY,
        collection_digest=collection_digest(["t::a", "t::b"]),
        residue=("t::b",),
    )
    publisher = make_publisher(seed=seed)
    publisher.open()

    selected = _channel(channel_dir).collect(["t::a", "t::b", "t::new"])
    manifest = publisher.pump()

    assert selected == ("t::a", "t::b", "t::new")
    assert manifest.obligations == ()


def test_progress_channel_collect_rejects_duplicate_obligation_ids(
    channel_dir: Path,
    make_publisher: Callable[..., ProgressPublisher],
) -> None:
    make_publisher().open()

    with pytest.raises(ValueError, match="cannot repeat an obligation id"):
        _channel(channel_dir).collect(["t::a", "t::a"])


def test_progress_channel_record_rejects_empty_obligation_id(
    channel_dir: Path,
    make_publisher: Callable[..., ProgressPublisher],
) -> None:
    make_publisher().open()

    with pytest.raises(ValidationError):
        _channel(channel_dir).record("", ObligationDisposition.PASS)


def _manifest_payload(**overrides: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "id": progress.progress_manifest_id(_KEY),
        "leg": {
            "attempt_id": "CA-01",
            "criterion_id": "CR-01",
            "gate_id": "G-01",
            "freshness_key": _KEY,
            "claimed_at": now,
        },
        "producer_digest": _PRODUCER,
        "writer_pid": 1,
        "progress_mode": "enumerated",
        "outcome": "running",
        "collected": ["t::a"],
        "collection_digest": collection_digest(["t::a"]),
        "obligations": [],
        "cursor": 1,
        "liveness": {"started_at": now, "heartbeat_at": now, "elapsed_ms": 0},
        "created_at": now,
        "updated_at": now,
    }
    payload.update(overrides)
    return payload


def test_progress_manifest_accepts_a_consistent_record() -> None:
    manifest = ProgressManifest.model_validate(_manifest_payload())

    assert manifest.pending() == ("t::a",)


def test_progress_manifest_rejects_obligation_outside_collection() -> None:
    stray = {"obligation_id": "t::z", "disposition": "pass", "completed_at": datetime.now(UTC)}

    with pytest.raises(ValidationError, match="outside the collection"):
        ProgressManifest.model_validate(_manifest_payload(obligations=[stray]))


def test_progress_manifest_rejects_collection_digest_mismatch() -> None:
    with pytest.raises(ValidationError, match="collection_digest does not match"):
        ProgressManifest.model_validate(
            _manifest_payload(collection_digest=collection_digest(["t::b"]))
        )


def test_progress_manifest_rejects_enumerated_mode_without_collection() -> None:
    with pytest.raises(ValidationError, match="requires a collection"):
        ProgressManifest.model_validate(_manifest_payload(collected=None, collection_digest=None))


def test_progress_manifest_rejects_id_for_another_key() -> None:
    with pytest.raises(ValidationError, match="does not name its freshness key"):
        ProgressManifest.model_validate(
            _manifest_payload(id=progress.progress_manifest_id(_OTHER_KEY))
        )


def test_progress_event_rejects_mixed_kinds() -> None:
    with pytest.raises(ValidationError, match="either a collection or an obligation"):
        ProgressEvent(
            freshness_key=_KEY,
            recorded_at=datetime.now(UTC),
            collected=("t::a",),
            obligation_id="t::a",
            disposition=ObligationDisposition.PASS,
        )


def test_progress_event_rejects_half_obligation() -> None:
    with pytest.raises(ValidationError, match="needs both"):
        ProgressEvent(freshness_key=_KEY, recorded_at=datetime.now(UTC), obligation_id="t::a")


def test_read_progress_manifest_never_waits_on_the_writer_lock(
    state_path: Path,
    make_publisher: Callable[..., ProgressPublisher],
) -> None:
    """A reader takes no lock, so a writer holding one cannot stall it."""
    published = make_publisher(channel_dir=None).open()
    target = progress_manifest_path(state_path, _KEY)

    with portalock.acquire(target, timeout=1.0):
        started = time.monotonic()
        seen = read_progress_manifest(state_path, _KEY)
        waited = time.monotonic() - started

    assert seen == published
    assert waited < 1.0


def test_read_progress_manifest_returns_none_for_unreadable_file(state_path: Path) -> None:
    _claim(state_path)
    target = progress_manifest_path(state_path, _KEY)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{", encoding="utf-8")

    assert read_progress_manifest(state_path, _KEY) is None


def test_read_progress_manifest_returns_none_when_absent(state_path: Path) -> None:
    assert read_progress_manifest(state_path, _KEY) is None


class _Report:
    def __init__(self, nodeid: str, when: str, *, failed: bool = False) -> None:
        self.nodeid = nodeid
        self.when = when
        self.failed = failed


class _Item:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid


class _Hook:
    def __init__(self) -> None:
        self.deselected: list[str] = []

    def pytest_deselected(self, *, items: list[_Item]) -> None:
        self.deselected.extend(item.nodeid for item in items)


class _PluginManager:
    def __init__(self) -> None:
        self.registered: list[tuple[object, str]] = []

    def register(self, plugin: object, name: str) -> None:
        self.registered.append((plugin, name))


class _Config:
    def __init__(self) -> None:
        self.hook = _Hook()
        self.pluginmanager = _PluginManager()


def test_pytest_configure_registers_nothing_without_a_channel(
    monkeypatch: pytest.MonkeyPatch, channel_dir: Path
) -> None:
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(channel_dir))
    config = _Config()

    progress.pytest_configure(config)

    assert config.pluginmanager.registered == []


def test_pytest_plugin_publishes_items_and_deselects_outside_residue(
    monkeypatch: pytest.MonkeyPatch,
    channel_dir: Path,
    make_publisher: Callable[..., ProgressPublisher],
) -> None:
    """The pytest end of the channel: residue selection plus one record per test."""
    nodeids = ["t::a", "t::b", "t::c"]
    seed = ResidueSeed(
        resumed_from=_OTHER_KEY,
        collection_digest=collection_digest(nodeids),
        residue=("t::b", "t::c"),
    )
    publisher = make_publisher(seed=seed)
    publisher.open()
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(channel_dir))
    config = _Config()
    progress.pytest_configure(config)
    ((plugin, _name),) = config.pluginmanager.registered
    items = [_Item(nodeid) for nodeid in nodeids]

    plugin.pytest_collection_modifyitems(config, items)  # type: ignore[attr-defined]
    for report in (
        _Report("t::b", "setup"),
        _Report("t::b", "call"),
        _Report("t::b", "teardown"),
        _Report("t::c", "setup"),
        _Report("t::c", "call", failed=True),
        _Report("t::c", "teardown"),
    ):
        plugin.pytest_runtest_logreport(report)  # type: ignore[attr-defined]
    manifest = publisher.pump()

    assert [item.nodeid for item in items] == ["t::b", "t::c"]
    assert config.hook.deselected == ["t::a"]
    assert manifest.collected == tuple(nodeids)
    assert [(r.obligation_id, r.disposition) for r in manifest.obligations] == [
        ("t::b", ObligationDisposition.PASS),
        ("t::c", ObligationDisposition.FAIL),
    ]
