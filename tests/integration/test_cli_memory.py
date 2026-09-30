"""End-to-end CLI tests for ``eawf memory ...`` on an epoch-2 tree.

Drives the full path: add → list → render-context → view → stale → compact,
with the writers reaching the daemon's native ``memory.*`` verbs in process
and every note landing on the selected generation's memory ledger.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from tests.integration._memory_native import native_memory_tree, standing_notes

runner = CliRunner()


@pytest.fixture
def tmp_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    yield from native_memory_tree(tmp_path, monkeypatch)


def _add(*argv: str) -> dict[str, object]:
    result = runner.invoke(app, ["--json", "memory", "add", *argv])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["result"]


def test_memory_add_creates_entry(tmp_state: Path) -> None:
    frozen = tmp_state.read_bytes()
    payload = _add(
        "--scope",
        "QR",
        "--title",
        "Use uv run",
        "--body",
        "All Python invocations go through uv.",
        "--confidence",
        "h",
    )
    assert str(payload["id"]).startswith("MEM-")
    assert payload["confidence"] == "high"
    # The note is on the generation's memory ledger; the frozen document is untouched.
    note = standing_notes(tmp_state)[str(payload["id"])].note
    assert note.body == "All Python invocations go through uv."
    assert tmp_state.read_bytes() == frozen


def test_memory_list_returns_added_entry(tmp_state: Path) -> None:
    _add("--scope", "QR", "--title", "first", "--body", "body")
    result = runner.invoke(app, ["--json", "memory", "list"])
    assert result.exit_code == 0
    payload = json.loads(result.output)["result"]
    assert payload["count"] == 1


def test_memory_list_filter_by_scope(tmp_state: Path) -> None:
    _add("--scope", "QR", "--title", "qr-entry", "--body", "body")
    _add("--scope", "P01", "--title", "phase-entry", "--body", "body")
    result = runner.invoke(app, ["--json", "memory", "list", "--scope", "QR"])
    assert result.exit_code == 0
    payload = json.loads(result.output)["result"]
    assert payload["count"] == 1
    assert payload["entries"][0]["scope_id"] == "QR"


def test_memory_render_context_with_budget(tmp_state: Path) -> None:
    big_body = " ".join(["lorem"] * 100)
    for i in range(5):
        _add("--scope", "QR", "--title", f"entry {i}", "--body", big_body)
    result = runner.invoke(app, ["--json", "memory", "render-context", "--budget", "200"])
    assert result.exit_code == 0
    payload = json.loads(result.output)["result"]
    assert payload["budget"] == 200
    assert payload["tokens_used"] <= 200
    assert payload["skipped_count"] >= 1


def test_memory_view_shows_full_body(tmp_state: Path) -> None:
    mem_id = _add(
        "--scope", "QR", "--title", "title here", "--body", "complete body of memory entry"
    )["id"]
    view = runner.invoke(app, ["--json", "memory", "view", str(mem_id)])
    assert view.exit_code == 0
    body = json.loads(view.output)["result"]
    assert body["id"] == mem_id
    assert "complete body of memory entry" in body["body"]


def test_memory_view_unknown_returns_not_found(tmp_state: Path) -> None:
    result = runner.invoke(app, ["memory", "view", "MEM-NOPE"])
    assert result.exit_code == 1  # NOT_FOUND


def test_memory_stale_lists_low_confidence_aged(tmp_state: Path) -> None:
    _add("--scope", "QR", "--title", "stale candidate", "--body", "body", "--confidence", "l")
    # Use age=0 to ensure all low-confidence entries surface.
    result = runner.invoke(app, ["--json", "memory", "stale", "--age", "0"])
    assert result.exit_code == 0
    payload = json.loads(result.output)["result"]
    assert payload["count"] >= 1


def test_memory_compact_idempotent(tmp_state: Path) -> None:
    _add("--scope", "QR", "--title", "title", "--body", "body")
    a = runner.invoke(app, ["--json", "memory", "compact"])
    b = runner.invoke(app, ["--json", "memory", "compact"])
    assert a.exit_code == 0, a.output
    assert b.exit_code == 0, b.output
    pa = json.loads(a.output)["result"]
    pb = json.loads(b.output)["result"]
    # Second compaction should report 0 dedup (idempotent).
    assert pb["dedup_count"] == 0
    assert pa["records_in"] == pb["records_in"] or pa["records_out"] == pb["records_in"]


def test_memory_add_invalid_confidence_returns_invalid_input(tmp_state: Path) -> None:
    result = runner.invoke(
        app,
        ["memory", "add", "--scope", "QR", "--title", "t", "--body", "b", "--confidence", "x"],
    )
    assert result.exit_code == 1  # INVALID_INPUT
    assert standing_notes(tmp_state) == {}


def test_memory_full_pipeline_add_list_render_view_stale_compact(tmp_state: Path) -> None:
    """End-to-end: add → list → render-context → view → stale → compact."""
    mem_id = _add("--scope", "QR", "--title", "entry", "--body", "body")["id"]

    listed = runner.invoke(app, ["--json", "memory", "list"])
    assert listed.exit_code == 0
    assert json.loads(listed.output)["result"]["count"] == 1

    rendered = runner.invoke(app, ["--json", "memory", "render-context", "--budget", "100"])
    assert rendered.exit_code == 0
    assert json.loads(rendered.output)["result"]["budget"] == 100

    view = runner.invoke(app, ["--json", "memory", "view", str(mem_id)])
    assert view.exit_code == 0
    assert json.loads(view.output)["result"]["id"] == mem_id

    stale = runner.invoke(app, ["--json", "memory", "stale", "--age", "0"])
    assert stale.exit_code == 0

    compact = runner.invoke(app, ["--json", "memory", "compact"])
    assert compact.exit_code == 0


def test_memory_prune_gc_and_tier_revise_the_ledger(tmp_state: Path) -> None:
    """Each writer appends a revision that supersedes the note's previous line."""
    kept = str(_add("--scope", "QR", "--title", "kept", "--body", "b")["id"])
    pruned = str(_add("--scope", "P01", "--title", "retired", "--body", "b")["id"])

    tier = runner.invoke(app, ["--json", "memory", "tier", kept, "--tier", "archival"])
    assert tier.exit_code == 0, tier.output
    assert json.loads(tier.output)["result"] == {
        "id": kept,
        "tier": "archival",
        "prior_tier": "working",
    }

    dry = runner.invoke(
        app,
        [
            "--json",
            "--no-input",
            "memory",
            "prune",
            "--status",
            "active",
            "--older-than",
            "P0D",
            "--scope",
            "P01",
            "--dry-run",
        ],
    )
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)["result"]["pruned_ids"] == [pruned]
    assert standing_notes(tmp_state)[pruned].note.status.value == "active"

    prune = runner.invoke(
        app,
        [
            "--json",
            "--no-input",
            "memory",
            "prune",
            "--status",
            "active",
            "--older-than",
            "P0D",
            "--scope",
            "P01",
        ],
    )
    assert prune.exit_code == 0, prune.output
    assert json.loads(prune.output)["result"]["pruned_ids"] == [pruned]

    gc = runner.invoke(app, ["--json", "memory", "gc", "--threshold-days", "0"])
    assert gc.exit_code == 0, gc.output
    assert json.loads(gc.output)["result"]["archived_ids"] == []

    notes = standing_notes(tmp_state)
    assert notes[kept].note.tier.value == "archival"
    assert notes[pruned].note.status.value == "pruned"
    assert notes[pruned].note.expired_at is not None
    listed = runner.invoke(app, ["--json", "memory", "list", "--status", "pruned"])
    assert [e["id"] for e in json.loads(listed.output)["result"]["entries"]] == [pruned]


def test_memory_tier_unknown_returns_not_found(tmp_state: Path) -> None:
    result = runner.invoke(app, ["memory", "tier", "MEM-NOPE", "--tier", "archival"])
    assert result.exit_code == 1  # NOT_FOUND


def test_memory_add_no_state_returns_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "absent.json"))
    result = runner.invoke(app, ["memory", "add", "--scope", "QR", "--title", "t", "--body", "b"])
    assert result.exit_code != 0
    assert not (tmp_path / ".ea").exists()


def test_memory_add_writes_only_the_generation_ledger(tmp_state: Path) -> None:
    """``memory add -w`` lands on the memory ledger, with one firehose row and no epoch-1 store."""
    workspace = tmp_state.parent.parent
    result = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "memory",
            "add",
            "--scope",
            "QR",
            "--title",
            "Use uv run",
            "--body",
            "All Python invocations go through uv.",
        ],
    )
    assert result.exit_code == 0, result.output

    assert len(standing_notes(tmp_state)) == 1
    store_dir = tmp_state.parent / "store"
    assert not (store_dir / "memory.jsonl").exists()
    rows = (store_dir / "event.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(row)["payload"]["name"] for row in rows] == ["ledger.memory.appended"]


def test_memory_promote_copies_a_frozen_store_record(tmp_state: Path) -> None:
    store = tmp_state.parent / "store" / "research.jsonl"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps(
            {
                "id": "RES-01",
                "kind": "research",
                "scope_id": "P01",
                "created_at": "2026-05-01T00:00:00Z",
                "summary": "uv is the runner",
                "payload": {"findings": "Every command goes through uv run."},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "--json",
            "memory",
            "promote",
            "--session",
            "SES-1",
            "--source",
            "RES-01",
            "--source-kind",
            "research",
        ],
    )
    assert result.exit_code == 0, result.output
    answer = json.loads(result.output)["result"]
    assert answer["scope_id"] == "P01"
    note = standing_notes(tmp_state)[answer["id"]].note
    assert note.body == "Every command goes through uv run."
    assert note.source_ref == "research/RES-01"

    missing = runner.invoke(
        app,
        [
            "memory",
            "promote",
            "--session",
            "SES-1",
            "--source",
            "RES-99",
            "--source-kind",
            "research",
        ],
    )
    assert missing.exit_code == 1  # NOT_FOUND


def test_memory_promote_to_artifact_needs_a_standing_decision(tmp_state: Path) -> None:
    mem_id = str(_add("--scope", "QR", "--title", "rule", "--body", "b")["id"])
    base = [
        "memory",
        "promote",
        "--session",
        "SES-1",
        "--source",
        mem_id,
        "--source-kind",
        "memory",
        "--to",
        "artifact",
    ]

    unnamed = runner.invoke(app, base)
    assert unnamed.exit_code == 1, unnamed.output
    assert "--artifact-id" in unnamed.output

    unknown = runner.invoke(app, [*base, "--artifact-id", "DEC-NOPE"])
    assert unknown.exit_code == 1  # NOT_FOUND
    assert standing_notes(tmp_state)[mem_id].note.status.value == "active"
