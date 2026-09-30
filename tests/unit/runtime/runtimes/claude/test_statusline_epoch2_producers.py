"""The budget, MCP, memory and plugins segments read an epoch-2 producer.

On a tree cut over to epoch 2 the epoch-1 document is frozen, so each of
these segments reads the fact that is current there instead: the budget
notice of the Run bound to the host's session, the MCP servers the Claude
runtime config registers, the memory ledger of the selected generation, and
Claude Code's own record of installed plugins. Every canary is provisioned
through the production provisioning path under the test's own tmp directory.
"""

from __future__ import annotations

import json
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from eawf.kernel.projection.truth import Precision, TruthKind, TruthState
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
from eawf.runtime.budget.notices import (
    BudgetCrossing,
    BudgetNoticeLedger,
    notices_path,
    upsert_notice,
)
from eawf.runtime.runtimes.claude import statusline as orchestrator
from eawf.runtime.runtimes.claude.statusline_modules import (
    budget,
    hooks_plugins,
    mcp_health,
    memory,
)
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
DIGEST_A: Final = f"sha256:{'a' * 64}"
DIGEST_B: Final = f"sha256:{'b' * 64}"


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


def crossing(
    *,
    scope_id: str = RUN_KEY,
    observed: int = 12_000,
    budget_value: int | None = 10_000,
    axis: str = "tokens",
    digest: str | None = DIGEST_A,
    at: datetime = PROVISIONED_AT,
) -> BudgetCrossing:
    """Return a limit-reached crossing of a hard Run cap."""
    return BudgetCrossing(
        scope_id=scope_id,
        axis=axis,
        basis="hard_limit",
        band="limit_reached",
        observed_value=observed,
        budget_value=budget_value,
        observed_at=at,
        contract_digest=digest,
        audience=("OPERATOR",),
    )


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


# ---- budget: the session Run's budget notice -------------------------------


def test_budget_epoch2_renders_the_session_runs_open_notice(tmp_path: Path) -> None:
    state_path = session_canary(tmp_path)
    upsert = upsert_notice(notices_path(state_path), crossing())

    segment = budget.build(PAYLOAD, state_path)

    assert segment.text == "budget:12.0k/10.0k !limit"
    assert segment.status == "warn"
    assert segment.truth.state is TruthState.KNOWN
    assert segment.truth.producer == budget.NOTICE_PRODUCER
    assert segment.truth.provenance_refs == (
        f".ea/local/budget_notices.json#{upsert.notice.notice_key}",
    )
    assert segment.truth.producer_revision == upsert.notice.revision
    assert segment.truth.truth_kind is TruthKind.STORED
    assert segment.truth.precision is Precision.APPROXIMATE


def test_budget_epoch2_a_resolved_notice_shows_the_reading_without_the_mark(
    tmp_path: Path,
) -> None:
    state_path = session_canary(tmp_path)
    path = notices_path(state_path)
    notice = upsert_notice(path, crossing()).notice
    resolved = notice.model_copy(
        update={"status": "RESOLVED", "resolved_at": PROVISIONED_AT + timedelta(minutes=1)}
    )
    path.write_text(
        BudgetNoticeLedger(notices={notice.notice_key: resolved}).model_dump_json(),
        encoding="utf-8",
    )

    segment = budget.build(PAYLOAD, state_path)

    assert segment.text == "budget:12.0k/10.0k"
    assert segment.status == "ok"


def test_budget_epoch2_takes_the_latest_notice_of_the_run(tmp_path: Path) -> None:
    state_path = session_canary(tmp_path)
    path = notices_path(state_path)
    upsert_notice(path, crossing(observed=5_000, budget_value=4_000, digest=DIGEST_A))
    later = PROVISIONED_AT + timedelta(hours=1)
    upsert_notice(path, crossing(observed=9_000, budget_value=8_000, digest=DIGEST_B, at=later))

    assert budget.build(PAYLOAD, state_path).text == "budget:9.0k/8.0k !limit"


@pytest.mark.parametrize(
    "other",
    [
        pytest.param({"scope_id": "RUN-00000099"}, id="another-run"),
        pytest.param({"axis": "wall_seconds"}, id="wall-axis"),
        pytest.param({"budget_value": None}, id="no-budget-value"),
    ],
)
def test_budget_epoch2_without_a_token_reading_of_the_run_names_why(
    tmp_path: Path, other: dict[str, Any]
) -> None:
    state_path = session_canary(tmp_path)
    upsert_notice(notices_path(state_path), crossing(**other))

    segment = budget.build(PAYLOAD, state_path)

    assert segment.text == "budget:n/a(no-run-budget-reading)"
    assert segment.truth.state is TruthState.UNAVAILABLE


def test_budget_epoch2_with_no_notice_ledger_names_why(tmp_path: Path) -> None:
    state_path = session_canary(tmp_path)

    assert budget.build(PAYLOAD, state_path).text == "budget:n/a(no-run-budget-reading)"


def test_budget_epoch2_with_a_corrupt_notice_ledger_names_why(tmp_path: Path) -> None:
    state_path = session_canary(tmp_path)
    path = notices_path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"notices": []}', encoding="utf-8")

    assert budget.build(PAYLOAD, state_path).text == "budget:n/a(notices-unreadable)"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [({}, "no-session"), ({"session_id": "someone-else"}, "no-run-for-session")],
)
def test_budget_epoch2_without_the_session_run_names_the_spine_gap(
    tmp_path: Path, payload: dict[str, Any], reason: str
) -> None:
    state_path = session_canary(tmp_path)

    assert budget.build(payload, state_path).text == f"budget:n/a({reason})"


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


# ---- mcp: the servers the Claude runtime config registers ------------------


def test_mcp_epoch2_counts_the_registered_servers_without_a_health_claim(
    tmp_path: Path,
) -> None:
    state_path = canary(tmp_path)
    (state_path.parent.parent / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "x"}, "b": {"command": "y"}}}),
        encoding="utf-8",
    )

    segment = mcp_health.build({}, state_path)

    assert segment.text == "mcp:2 registered"
    assert segment.status == "degraded"
    assert segment.truth.producer == mcp_health.RUNTIME_CONFIG_PRODUCER
    assert segment.truth.provenance_refs == (".mcp.json#mcpServers",)


def test_mcp_epoch2_with_one_server_renders_it(tmp_path: Path) -> None:
    state_path = canary(tmp_path)
    (state_path.parent.parent / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "x"}}}), encoding="utf-8"
    )

    assert mcp_health.build({}, state_path).text == "mcp:1 registered"


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        pytest.param(None, "no-mcp-servers", id="absent"),
        pytest.param('{"mcpServers": {}}', "no-mcp-servers", id="empty"),
        pytest.param("[]", "mcp-config-unreadable", id="not-an-object"),
        pytest.param('{"mcpServers": []}', "mcp-config-unreadable", id="servers-not-a-map"),
        pytest.param("{", "mcp-config-unreadable", id="not-json"),
    ],
)
def test_mcp_epoch2_without_registered_servers_names_why(
    tmp_path: Path, content: str | None, reason: str
) -> None:
    state_path = canary(tmp_path)
    if content is not None:
        (state_path.parent.parent / ".mcp.json").write_text(content, encoding="utf-8")

    assert mcp_health.build({}, state_path).text == f"mcp:n/a({reason})"


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
    upsert_notice(notices_path(state_path), crossing())
    append_ledger_record(memory_ledger(state_path), memory_record("MEM-1"))
    payload = {**PAYLOAD, "cwd": str(tmp_path)}

    started = time.perf_counter()
    segments = orchestrator._build_segments(payload, state_path)
    elapsed = time.perf_counter() - started

    texts = {segment.module: segment.text for segment in segments}
    assert texts["budget"] == "budget:12.0k/10.0k !limit"
    assert texts["memory"].startswith("mem:1@")
    # A generous ceiling: the line has a 100 ms cold target and CI runners are slow.
    assert elapsed < 1.0
