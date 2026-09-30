"""The memory and plugins segments read an epoch-2 producer.

On a tree cut over to epoch 2 the epoch-1 document is frozen, so each of
these segments reads the fact that is current there instead: the memory
ledger of the selected generation and Claude Code's own record of installed
plugins. The budget and MCP segments are covered beside the truth contract in
``test_statusline_truth``. Every canary is provisioned through the production
provisioning path under the test's own tmp directory.
"""

from __future__ import annotations

import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from eawf.kernel.projection.truth import TruthKind
from eawf.kernel.state.enums import Confidence
from eawf.kernel.state.epoch2.authority import require_native_authority
from eawf.kernel.store.compaction import read_document, write_document
from eawf.kernel.store.kinds.memory import MemoryNote
from eawf.kernel.store.ledger import (
    LedgerRecord,
    append_correction,
    append_ledger_record,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import canary_ref, provision_canary
from eawf.platform.memory.book import note_line
from eawf.runtime.runtimes.claude import statusline as orchestrator
from eawf.runtime.runtimes.claude.statusline_modules import hooks_plugins, memory
from eawf.runtime.runtimes.claude.statusline_modules._spine import selected_generation
from eawf.runtime.session.vendor_id import hash_vendor_session_id

SEED_RECORDS: Final = (
    Path(__file__).resolve().parents[4]
    / "fixtures"
    / "epoch2"
    / "transitions"
    / "seed_records.yaml"
)
PROVISIONED_AT: Final = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
SESSION_ID: Final = "sess-producers-0001"
RUN_KEY: Final = "RUN-00000020"
PAYLOAD: Final[dict[str, Any]] = {"session_id": SESSION_ID}


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Allocate canary scratch and ``$HOME`` under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def run_row(key: str, *, session_id: str) -> dict[str, Any]:
    """Return a RUNNING seed Run keyed ``key`` bound to ``session_id``."""
    document = yaml.safe_load(SEED_RECORDS.read_text(encoding="utf-8"))
    row: dict[str, Any] = dict(document["records"]["run"]["RUNNING"])
    row["key"] = key
    row["urn"] = f"{str(row['urn']).rsplit('/', 1)[0]}/{key}"
    row["vendor_session"] = {
        "harness": "claude-code",
        "session_digest": hash_vendor_session_id(session_id),
    }
    return row


def canary(tmp_path: Path, runs: dict[str, dict[str, Any]] | None = None) -> Path:
    """Provision a canary whose selected generation holds ``runs``; return its state path."""
    repo = tmp_path / "canary"
    provision_canary(repo_root=repo, ref=canary_ref("SLP"), provisioned_at=PROVISIONED_AT)
    tree = (repo / ".ea").resolve()
    if runs:
        authority = require_native_authority(tree)
        assert authority.target is not None and authority.generation_id is not None
        path = authority.target.generation_path(authority.generation_id) / "state.json"
        document = read_document(path)
        document.setdefault("run", {}).update(runs)
        write_document(path, document)
    return tree / "state.json"


def session_canary(tmp_path: Path) -> Path:
    """Provision a canary whose generation binds ``RUN_KEY`` to the host session."""
    return canary(tmp_path, {RUN_KEY: run_row(RUN_KEY, session_id=SESSION_ID)})


def memory_ledger(state_path: Path) -> Path:
    """Return the selected generation's memory ledger of the canary at ``state_path``."""
    document = selected_generation(state_path)
    assert isinstance(document, Path)
    return ledger_path(document, Epoch2Collection.MEMORY)


def memory_record(
    mem_id: str, *, summary: str = "note", replaces: LedgerRecord | None = None
) -> LedgerRecord:
    """Return the ledger line filing one native memory note, superseding *replaces*."""
    note = MemoryNote(
        id=mem_id,
        scope_id="QR",
        title=summary,
        summary=summary,
        confidence=Confidence.MEDIUM,
        created_at=PROVISIONED_AT,
    )
    return note_line(note, at=PROVISIONED_AT, replaces=replaces)


def installed_plugins(home: Path, plugins: object) -> Path:
    """Write Claude Code's installed-plugins record under ``home``."""
    path = home / ".claude" / "plugins" / "installed_plugins.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 2, "plugins": plugins}), encoding="utf-8")
    return path


# ---- memory: the selected generation's memory ledger -----------------------


def test_memory_epoch2_counts_the_memory_ledger_records(tmp_path: Path) -> None:
    state_path = canary(tmp_path)
    ledger = memory_ledger(state_path)
    first = memory_record("MEM-1", summary="one")
    append_ledger_record(ledger, first)
    append_ledger_record(ledger, memory_record("MEM-2", summary="two"))
    append_correction(ledger, memory_record("MEM-1", summary="one, corrected", replaces=first))

    segment = memory.build({}, state_path)

    size = ledger.stat().st_size
    assert segment.text == (f"mem:2@{size}B" if size < 1024 else f"mem:2@{size // 1024}KiB")
    assert segment.truth.producer == memory.LEDGER_PRODUCER
    assert segment.truth.provenance_refs == ("generation#ledger/memory.jsonl",)
    assert segment.truth.truth_kind is TruthKind.STORED


def test_memory_epoch2_with_one_record_renders_it(tmp_path: Path) -> None:
    state_path = canary(tmp_path)
    ledger = memory_ledger(state_path)
    append_ledger_record(ledger, memory_record("MEM-1"))

    assert memory.build({}, state_path).text == f"mem:1@{ledger.stat().st_size}B"


def test_memory_epoch2_with_an_empty_ledger_names_why(tmp_path: Path) -> None:
    state_path = canary(tmp_path)

    segment = memory.build({}, state_path)

    assert segment.text == "mem:n/a(no-memory-records)"
    assert segment.status == "missing"


def test_memory_epoch2_with_a_torn_ledger_names_why(tmp_path: Path) -> None:
    state_path = canary(tmp_path)
    ledger = memory_ledger(state_path)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text('{"half": ', encoding="utf-8")

    assert memory.build({}, state_path).text == "mem:n/a(memory-ledger-unreadable)"


# ---- plugins: Claude Code's record of installed plugins --------------------


def test_plugins_epoch2_count_user_and_this_projects_installs(
    tmp_path: Path, isolated_runtime: Path
) -> None:
    state_path = canary(tmp_path)
    repo = state_path.parent.parent
    installed_plugins(
        isolated_runtime,
        {
            "a@m": [{"scope": "user"}],
            "b@m": [{"scope": "project", "projectPath": str(repo)}],
            "c@m": [{"scope": "local", "projectPath": str(tmp_path / "elsewhere")}],
            "d@m": [{"scope": "user"}, {"scope": "project", "projectPath": str(repo)}],
        },
    )

    segment = hooks_plugins.build({}, state_path)

    assert segment.text == "hooks:0 plugins:3"
    assert segment.status == "degraded"


@pytest.mark.parametrize(
    ("plugins", "rendered"),
    [
        pytest.param({}, "0", id="none-installed"),
        pytest.param({"a@m": [{"scope": "user"}]}, "1", id="one"),
        pytest.param([], "n/a(plugin-record-unreadable)", id="not-a-map"),
    ],
)
def test_plugins_epoch2_render_the_installed_count_or_why(
    tmp_path: Path, isolated_runtime: Path, plugins: object, rendered: str
) -> None:
    state_path = canary(tmp_path)
    installed_plugins(isolated_runtime, plugins)

    assert hooks_plugins.build({}, state_path).text == f"hooks:0 plugins:{rendered}"


def test_plugins_epoch2_without_a_plugin_record_names_why(tmp_path: Path) -> None:
    state_path = canary(tmp_path)

    assert hooks_plugins.build({}, state_path).text == "hooks:0 plugins:n/a(no-plugin-record)"


def test_plugins_epoch2_with_a_malformed_record_names_why(
    tmp_path: Path, isolated_runtime: Path
) -> None:
    state_path = canary(tmp_path)
    path = installed_plugins(isolated_runtime, {})
    path.write_text("{", encoding="utf-8")

    assert (
        hooks_plugins.build({}, state_path).text == "hooks:0 plugins:n/a(plugin-record-unreadable)"
    )


# ---- the whole line stays fast on an epoch-2 tree --------------------------


def test_epoch2_producers_render_the_line_inside_the_statusline_budget(tmp_path: Path) -> None:
    state_path = session_canary(tmp_path)
    append_ledger_record(memory_ledger(state_path), memory_record("MEM-1"))
    payload = {**PAYLOAD, "cwd": str(tmp_path)}

    started = time.perf_counter()
    segments = orchestrator._build_segments(payload, state_path)
    elapsed = time.perf_counter() - started

    texts = {segment.module: segment.text for segment in segments}
    assert texts["budget"] == "budget:n/a(no-token-cap)"
    assert texts["memory"].startswith("mem:1@")
    # A generous ceiling: the line has a 100 ms cold target and CI runners are slow.
    assert elapsed < 1.0
