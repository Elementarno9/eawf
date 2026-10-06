"""The statusline's scope and budget follow the Run the host session started.

Nothing here writes a Run row into the document by hand. The Run is admitted
by the daemon's create transaction and started by ``eawf run start`` from
inside a host session, so it reaches the status projection the way a live
Run does; its dispatch binding and usage reading are appended to the run
ledger the transaction keeps. The scope and budget segments then have to
find that Run from the session id Claude Code hands the statusline.
"""

from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from typer.testing import CliRunner

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.control import RunBinding
from eawf.kernel.runtime.events import RunEventKind, RunEventRecord
from eawf.kernel.runtime.usage import UsagePayload
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.runtimes.claude.statusline import render_pipeline
from eawf.runtime.runtimes.claude.statusline_modules import budget, scope
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain as domain_cmd
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    document_path,
    method_context,
    provision,
    root_context,
)
from tests.integration.runtime.daemon.test_delivery_landed_loop import InProcessClient
from tests.integration.runtime.daemon.test_native_create_transition import RUN_URN, admit_chain
from tests.unit.kernel.runtime.test_authority_capsule import review_fields

pytestmark = pytest.mark.integration

SESSION: Final = "5b0c1d2e-3f40-4152-8263-748596a7b8c9"
OTHER_SESSION: Final = "0f1e2d3c-4b5a-4697-8877-665544332211"
ANSI: Final = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(autouse=True)
def daemon_in_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Route the CLI into the registered verbs, with every root and home under tmp."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path / "statusline-cache"))
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.delenv("EAWF_STATUSLINE_THEME", raising=False)
    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", lambda *_a, **_k: 0)
    monkeypatch.setattr(domain_cmd, "DaemonClient", InProcessClient)
    InProcessClient.context = method_context(tmp_path / "runtime")
    methods.ensure_all_methods_registered()
    yield
    InProcessClient.context = None


def admitted(tmp_path: Path) -> CanaryProvision:
    """Provision a canary and admit a Task and its queued Run through the daemon."""
    canary = provision(tmp_path / "repo", code="SLR")
    admit_chain(root_context(canary, tmp_path / "runtime"))
    return canary


def start_from_session(canary: CanaryProvision, tmp_path: Path) -> None:
    """Run ``eawf run start`` the way a command inside the host session does."""
    spec = tmp_path / "start.json"
    spec.write_text(json.dumps({"updates": {"started_at": "2026-09-17T12:05:00Z"}}), "utf-8")
    result = CliRunner().invoke(
        app,
        [
            "--workspace",
            str(canary.root),
            "--json",
            "run",
            "start",
            RUN_URN,
            "--expected-run-revision",
            "1",
            "--idempotency-key",
            "start-1",
            "--actor",
            "OP-0001",
            "--from-spec",
            str(spec),
        ],
        env={"CLAUDE_CODE_SESSION_ID": SESSION},
    )
    assert result.exit_code == 0, result.output


def run_ledger_line(canary: CanaryProvision, record_key: str, payload: dict[str, object]) -> None:
    """Append one line to the run ledger the daemon's transaction keeps."""
    append_ledger_record(
        ledger_path(document_path(canary), Epoch2Collection.RUN),
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=record_key,
            status="recorded",
            recorded_at=AT,
            payload=payload,
        ),
    )


def bind_and_observe(canary: CanaryProvision, *, cap: int, spent: int) -> None:
    """File the Run's sealed token cap and one measured usage reading."""
    capsule = AuthorityCapsule.seal(
        review_fields(
            run_ref=RUN_URN, budget={"tokens": cap, "wall_seconds": 2400, "output_bytes": 1_048_576}
        )
    )
    binding = RunBinding(
        run_ref=parse_qualified_urn(RUN_URN),
        compiled_spec_digest=capsule.compiled_spec_digest,
        authority_capsule_digest=capsule.contract_digest,
        route_policy_revision=1,
        bound_at=AT,
        capsule=capsule,
    )
    run_ledger_line(canary, "BND-RUN-00000010", binding.model_dump(mode="json"))
    event = RunEventRecord(
        event_ref="EVT-00000001",
        run_ref=parse_qualified_urn(RUN_URN),
        run_sequence=1,
        event_kind=RunEventKind.USAGE_OBSERVED,
        provenance="provider_native",
        payload=UsagePayload(
            input_tokens=spent,
            usage_source="provider_receipt",
            is_cumulative=False,
            measurement_quality="measured",
        ),
        actor="OP-0001",
        recorded_at=AT,
    )
    run_ledger_line(canary, "EVT-00000001", event.model_dump(mode="json"))


def line(canary: CanaryProvision, session_id: str) -> str:
    """Return the rendered statusline, colour stripped, for *session_id*."""
    rendered = render_pipeline({"session_id": session_id}, workspace=canary.root, theme_name=None)
    return ANSI.sub("", rendered)


def test_live_run_started_in_the_session_shows_its_task_and_budget(tmp_path: Path) -> None:
    canary = admitted(tmp_path)
    start_from_session(canary, tmp_path)
    bind_and_observe(canary, cap=10_000, spent=1_500)

    # The Run reached the document through the daemon, not a hand-written row.
    row = read_document(document_path(canary))["run"]["RUN-00000010"]
    assert row["status"] == "RUNNING"
    assert row["vendor_session"]["session_digest"] != SESSION

    rendered = line(canary, SESSION)

    assert "scope:EAWF-0042" in rendered
    assert "budget:1.5k/10.0k" in rendered
    state_path = canary.root / ".ea" / "state.json"
    assert budget.build({"session_id": SESSION}, state_path).truth.provenance_refs == (RUN_URN,)


def test_live_run_without_a_sealed_cap_names_why_beside_its_scope(tmp_path: Path) -> None:
    canary = admitted(tmp_path)
    start_from_session(canary, tmp_path)

    rendered = line(canary, SESSION)

    assert "scope:EAWF-0042" in rendered
    assert "budget:n/a(no-token-cap)" in rendered


def test_another_sessions_statusline_finds_no_run(tmp_path: Path) -> None:
    canary = admitted(tmp_path)
    start_from_session(canary, tmp_path)
    bind_and_observe(canary, cap=10_000, spent=1_500)

    rendered = line(canary, OTHER_SESSION)

    assert "scope:n/a(no-run-for-session)" in rendered
    assert "budget:n/a(no-run-for-session)" in rendered


def test_queued_run_not_yet_started_binds_no_session(tmp_path: Path) -> None:
    canary = admitted(tmp_path)
    state_path = canary.root / ".ea" / "state.json"

    segment = scope.build({"session_id": SESSION}, state_path)

    assert segment.text == "scope:n/a(no-run-for-session)"
