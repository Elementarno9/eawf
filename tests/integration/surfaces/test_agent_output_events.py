"""Integration tests for forwarded agent output events.

The agent watch surface reads persisted ``agent.output.chunk`` rows, whose only
other producer is the daemon piping a runtime it spawned itself. A session an
external orchestrator spawned therefore renders a correct roster row with an
empty pane for the whole run. ``eawf hook agent-output`` closes that half of
the seam: the orchestrator forwards each output batch and it lands as the same
chunk row the spawn path writes.

Pins:

- A well-formed forwarded envelope is persisted as one ``agent.output.chunk``
  row, scoped and attributed from the named session row.
- A malformed envelope (unknown key, negative ``seq``, missing ``text``, wrong
  type) is rejected with exit 1 and persists nothing.
- An unknown session id is rejected with exit 1 (``NotFound``).
- Forwarded text with no renderable line is a no-op, not an empty row.
- The watch reader returns the forwarded chunk for the externally created
  session where it previously returned empty.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.events import AgentOutputChunkPayload
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.dispatch_runner import (
    persist_agent_output_chunk,
    persist_forwarded_output_chunk,
)
from eawf.surfaces.cli.app import app
from eawf.surfaces.render.envelope import OutputEnvelope
from eawf.surfaces.tui.modes.agent_watch import load_output_chunk_lines

runner = CliRunner()

_SESSION_ID = "SES-EXT-01"
_RUNTIME_SESSION_ID = "runtime-sess-ext-01"
_SCOPE_ID = "P32-I01-W29"
_OTHER_SCOPE_ID = "P32-I01-W28"


def _workspace_with_external_session(tmp_path: Path) -> Path:
    """Build a workspace whose only session was created by an external orchestrator."""
    workspace = tmp_path / "ws"
    state_dir = workspace / ".ea"
    state_dir.mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scope_kind": "repo",
                "urn": "urn:eawf:v1:state:QR",
                "updated_at": "2026-09-08T00:00:00Z",
                "project": {
                    "code": "QR",
                    "slug": "qr",
                    "title": "QR",
                    "domains": [],
                    "default_branch": "main",
                    "status": "active",
                    "repo_urn": "urn:eawf:v1:repo:QR",
                },
                "current": {
                    "project_code": "QR",
                    "track_id": None,
                    "phase_id": None,
                    "iter_id": None,
                    "active_wave_ids": [],
                    "active_session_ids": [_SESSION_ID],
                },
                "workspace": None,
                "phases": {},
                "iters": {},
                "waves": {},
                "artifacts": {},
                "agent_sessions": {
                    _SESSION_ID: {
                        "id": _SESSION_ID,
                        "role": "executor",
                        "runtime": "generic",
                        "runtime_session_id": _RUNTIME_SESSION_ID,
                        "scope_id": _SCOPE_ID,
                        "status": "active",
                        "claimed_wave_ids": [],
                        "worktree_ids": [],
                        "artifact_ids": [],
                        "started_at": "2026-09-08T00:00:00Z",
                        "ended_at": None,
                        "summary": None,
                    }
                },
                "plugins": {},
                "indexes": {},
            }
        ),
        encoding="utf-8",
    )
    return workspace


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
    result = _forward(
        workspace, {"session_id": _SESSION_ID, "seq": 0, "text": "external chunk one"}
    )

    assert result.exit_code == 0, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert isinstance(env.body, dict)
    assert env.body["persisted"] is True
    assert env.body["scope_id"] == _SCOPE_ID
    assert env.footer.persisted_store_records == [env.body["persisted_store_record"]]

    chunks = _chunk_payloads(workspace)
    assert len(chunks) == 1, chunks
    assert chunks[0].wave_id == _SCOPE_ID
    assert chunks[0].session_id == _RUNTIME_SESSION_ID
    assert chunks[0].seq == 0
    assert chunks[0].lines == "external chunk one"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"session_id": _SESSION_ID, "seq": 0, "text": "x", "runtime": "claude"}, "unknown key"),
        ({"session_id": _SESSION_ID, "seq": -1, "text": "x"}, "negative seq"),
        ({"session_id": _SESSION_ID, "seq": 0}, "missing text"),
        ({"session_id": _SESSION_ID, "seq": "first", "text": "x"}, "seq wrong type"),
        ({"seq": 0, "text": "x"}, "missing session"),
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
    result = _forward(workspace, [{"session_id": _SESSION_ID, "seq": 0, "text": "x"}])

    assert result.exit_code == 1, result.stdout
    assert not _event_path(workspace).exists()


def test_hook_agent_output_unknown_session_rejected(tmp_path: Path) -> None:
    """A chunk forwarded for a session state does not know is rejected."""
    workspace = _workspace_with_external_session(tmp_path)
    result = _forward(workspace, {"session_id": "SES-missing", "seq": 0, "text": "x"})

    assert result.exit_code == 1, result.stdout
    assert not _event_path(workspace).exists()


def test_hook_agent_output_blank_text_persists_no_row(tmp_path: Path) -> None:
    """Forwarded text with no renderable line is a no-op, not an empty row."""
    workspace = _workspace_with_external_session(tmp_path)
    result = _forward(workspace, {"session_id": _SESSION_ID, "seq": 0, "text": "   \n\n"})

    assert result.exit_code == 0, result.stdout
    env = OutputEnvelope.model_validate_json(result.stdout)
    assert isinstance(env.body, dict)
    assert env.body["persisted"] is False
    assert env.body["event_id"] is None
    assert env.footer.persisted_store_records == []
    assert _chunk_payloads(workspace) == []


def test_forwarded_chunk_reaches_watch_reader(tmp_path: Path) -> None:
    """The watch reader returns a forwarded chunk where it previously read empty."""
    workspace = _workspace_with_external_session(tmp_path)
    event_path = _event_path(workspace)

    # A populated store that holds only another lane's chunk: the externally
    # created session's pane is honest empty because nothing produced its stream.
    persist_agent_output_chunk(
        event_path,
        scope_id=_OTHER_SCOPE_ID,
        session_id="runtime-sess-other",
        seq=0,
        text="another lane's output",
    )
    before = load_output_chunk_lines(event_path, _SCOPE_ID, runtime_session_id=_RUNTIME_SESSION_ID)
    assert before == []

    assert (
        _forward(
            workspace, {"session_id": _SESSION_ID, "seq": 0, "text": "forwarded line one"}
        ).exit_code
        == 0
    )
    assert (
        _forward(
            workspace, {"session_id": _SESSION_ID, "seq": 1, "text": "forwarded line two"}
        ).exit_code
        == 0
    )

    after = load_output_chunk_lines(event_path, _SCOPE_ID, runtime_session_id=_RUNTIME_SESSION_ID)
    assert after == ["forwarded line one", "forwarded line two"]
    # The other lane's chunk stays out of this session's stream.
    assert load_output_chunk_lines(
        event_path, _OTHER_SCOPE_ID, runtime_session_id="runtime-sess-other"
    ) == ["another lane's output"]


def test_persist_forwarded_output_chunk_unknown_session_raises(tmp_path: Path) -> None:
    """The library ingest raises ``KeyError`` for a session state does not carry."""
    from eawf.kernel.validate.strict import validate_state

    workspace = _workspace_with_external_session(tmp_path)
    state_path = workspace / ".ea" / "state.json"
    report = validate_state(
        json.loads(state_path.read_text(encoding="utf-8")), strict_optional=False
    )
    assert report.state is not None

    with pytest.raises(KeyError, match="unknown agent session"):
        persist_forwarded_output_chunk(
            store_path(state_path, StoreKind.EVENT),
            state=report.state,
            session_id="SES-missing",
            seq=0,
            text="x",
        )
