"""Integration tests for forwarded agent output events.

The agent watch surface reads persisted ``agent.output.chunk`` rows, whose only
other producer is the daemon piping a runtime it spawned itself. A session an
external orchestrator spawned therefore renders a correct roster row with an
empty pane for the whole run. ``eawf hook agent-output`` closes that half of
the seam: the orchestrator forwards each output batch and it lands as the same
chunk row the spawn path writes.

Pins:

- A well-formed forwarded envelope is persisted as one ``agent.output.chunk``
  row, scoped and attributed from the named native Run.
- A malformed envelope (unknown key, negative ``seq``, missing ``text``, wrong
  type) is rejected with exit 1 and persists nothing.
- An unknown Run is rejected with exit 1 (``NotFound``).
- Forwarded text with no renderable line is a no-op, not an empty row.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.events import AgentOutputChunkPayload
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.dispatch_runner import (
    persist_forwarded_output_chunk,
)
from eawf.surfaces.cli.app import app
from eawf.surfaces.render.envelope import OutputEnvelope
from tests.integration.surfaces.cli._native_run import (
    RUN_KEY,
    RUN_URN,
    TASK_KEY,
    native_repo,
    run_urn,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _drop_inherited_state_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the ``-w`` workspace, not an inherited ``EA_STATE``, pick the ledger.

    A close or re-receipt gate runs this suite in a sandbox that exports
    ``EA_STATE`` at a copy of the live ledger, and ``EA_STATE`` outranks
    ``-w``, so every forward would otherwise land in that copy.
    """
    monkeypatch.delenv("EA_STATE", raising=False)


def _workspace_with_external_session(tmp_path: Path) -> Path:
    """Build a native tree whose only Run was dispatched outside the daemon's pipe."""
    return native_repo(tmp_path / "ws")


def _event_path(workspace: Path) -> Path:
    """Return the workspace's ``event.jsonl`` path."""
    return store_path(workspace / ".ea" / "state.json", StoreKind.EVENT)


def _forward(workspace: Path, payload: object) -> Result:
    """Forward *payload* through ``eawf hook agent-output`` in *workspace*."""
    return runner.invoke(
        app,
        ["-w", str(workspace), "hook", "agent-output"],
        input=json.dumps(payload),
    )


def _chunk_payloads(workspace: Path) -> list[AgentOutputChunkPayload]:
    """Return every persisted output-chunk payload, in store order."""
    path = _event_path(workspace)
    if not path.is_file():
        return []
    rows = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    payloads = [Envelope.model_validate_json(ln).payload for ln in rows]
    return [
        AgentOutputChunkPayload.model_validate(payload)
        for payload in payloads
        if payload.get("event_type") == "agent.output.chunk"
    ]


def test_hook_ingests_agent_output_event(tmp_path: Path) -> None:
    """A well-formed forwarded envelope persists one attributed chunk row."""
    workspace = _workspace_with_external_session(tmp_path)
    result = _forward(workspace, {"run_ref": RUN_URN, "seq": 0, "text": "external chunk one"})

    assert result.exit_code == 0, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert isinstance(env.body, dict)
    assert env.body["persisted"] is True
    assert env.body["scope_id"] == TASK_KEY
    assert env.footer.persisted_store_records == [env.body["persisted_store_record"]]

    chunks = _chunk_payloads(workspace)
    assert len(chunks) == 1, chunks
    assert chunks[0].wave_id == TASK_KEY
    assert chunks[0].session_id == RUN_KEY
    assert chunks[0].seq == 0
    assert chunks[0].lines == "external chunk one"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"run_ref": RUN_URN, "seq": 0, "text": "x", "runtime": "claude"}, "unknown key"),
        ({"run_ref": RUN_URN, "seq": -1, "text": "x"}, "negative seq"),
        ({"run_ref": RUN_URN, "seq": 0}, "missing text"),
        ({"run_ref": RUN_URN, "seq": "first", "text": "x"}, "seq wrong type"),
        ({"seq": 0, "text": "x"}, "missing run"),
    ],
)
def test_hook_rejects_malformed_output_event(
    tmp_path: Path, payload: dict[str, object], reason: str
) -> None:
    """A malformed forwarded envelope is rejected and persists no partial row."""
    workspace = _workspace_with_external_session(tmp_path)
    result = _forward(workspace, payload)

    assert result.exit_code == 1, f"{reason}: {result.stdout}"
    assert not _event_path(workspace).exists(), reason


def test_hook_rejects_non_object_output_event(tmp_path: Path) -> None:
    """A JSON array (not an object) is rejected before any append."""
    workspace = _workspace_with_external_session(tmp_path)
    result = _forward(workspace, [{"run_ref": RUN_URN, "seq": 0, "text": "x"}])

    assert result.exit_code == 1, result.stdout
    assert not _event_path(workspace).exists()


def test_hook_agent_output_unknown_run_rejected(tmp_path: Path) -> None:
    """A chunk forwarded for a Run the generation does not hold is rejected."""
    workspace = _workspace_with_external_session(tmp_path)
    result = _forward(workspace, {"run_ref": run_urn("RUN-00000099"), "seq": 0, "text": "x"})

    assert result.exit_code == 1, result.stdout
    assert not _event_path(workspace).exists()


def test_hook_agent_output_blank_text_persists_no_row(tmp_path: Path) -> None:
    """Forwarded text with no renderable line is a no-op, not an empty row."""
    workspace = _workspace_with_external_session(tmp_path)
    result = _forward(workspace, {"run_ref": RUN_URN, "seq": 0, "text": "   \n\n"})

    assert result.exit_code == 0, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert isinstance(env.body, dict)
    assert env.body["persisted"] is False
    assert env.body["event_id"] is None
    assert env.footer.persisted_store_records == []
    assert _chunk_payloads(workspace) == []


def test_persist_forwarded_output_chunk_unknown_run_raises(tmp_path: Path) -> None:
    """The library ingest raises ``KeyError`` for a Run the generation does not hold."""
    workspace = _workspace_with_external_session(tmp_path)

    with pytest.raises(KeyError, match="unknown run"):
        persist_forwarded_output_chunk(
            _event_path(workspace),
            tree_root=workspace / ".ea",
            run_ref=parse_qualified_urn(run_urn("RUN-00000099")),
            seq=0,
            text="x",
        )
    assert not _event_path(workspace).exists()
