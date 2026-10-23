"""Integration tests for ``eawf memory digest`` driven through the CLI.

The digest reads the selected generation's decisions. It is a pure
projection: every file of the generation is byte-equal before and after the
command. An epoch-1 tree selects no generation, so it is refused as needing
migration. Also pins the ``--md`` and ``--json`` surfaces.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.epoch2.decision import Decision, ratify
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from tests._epoch2_helpers import lay_epoch2_tree

runner = CliRunner()

DECISION_TITLE = "Pick portalocker for cross-platform file locks"


def _generation(state_path: Path) -> Path:
    """Return the directory of the generation the tree holding *state_path* selects."""
    authority = resolve_authority(state_path.parent)
    assert authority.target is not None and authority.generation_id is not None
    return authority.target.generation_path(authority.generation_id)


def _file_decision(state_path: Path) -> None:
    """Append one ratified native decision to the generation's decision ledger."""
    proposed = Decision.model_validate(
        {
            "key": "D01",
            "scope_ref": "eawf://WSP-MAIN/PRJ-QR/REP-QR/milestone/MLS-0001",
            "title": DECISION_TITLE,
            "decision": "Use portalocker.",
            "rationale": "portalocker is the only maintained cross-platform lock.",
            "alternatives": [{"key": "use", "label": "Use it"}, {"key": "skip", "label": "Skip"}],
            "chosen_option_key": "use",
            "consequences": ["Locks behave alike on every platform"],
            "evidence_refs": ["eawf://WSP-MAIN/PRJ-QR/REP-QR/evidence/EVD-0001"],
            "effective_policy_revision": 1,
            "created_at": "2026-05-08T00:00:00Z",
        }
    )
    decision = ratify(proposed, by="OP-0001", at=proposed.created_at)
    append_ledger_record(
        ledger_path(_generation(state_path) / GENERATION_DOCUMENT, Epoch2Collection.DECISION),
        LedgerRecord(
            collection=Epoch2Collection.DECISION,
            record_key=decision.key,
            status=decision.status.value,
            recorded_at=decision.created_at,
            supersedes=None,
            payload={"payload": decision.model_dump(mode="json")},
        ),
    )


@pytest.fixture
def tmp_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_path = lay_epoch2_tree(tmp_path)
    _file_decision(state_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.delenv("EA_LOCK_TIMEOUT", raising=False)
    return state_path


def test_memory_digest_md_renders_standup(tmp_state: Path) -> None:
    """``eawf memory digest --md`` emits the markdown standup with glossed ids."""
    result = runner.invoke(app, ["memory", "digest", "--md"])
    assert result.exit_code == 0, result.output
    assert "# Standup digest" in result.stdout
    assert "No phase or iter is active right now." in result.stdout
    assert f"D01 ({DECISION_TITLE})" in result.stdout


def test_memory_digest_default_is_md(tmp_state: Path) -> None:
    """No format flag defaults to the markdown standup."""
    result = runner.invoke(app, ["memory", "digest"])
    assert result.exit_code == 0, result.output
    assert "# Standup digest" in result.stdout


def test_memory_digest_json_surface(tmp_state: Path) -> None:
    """``eawf memory digest --json`` emits the structured projection."""
    result = runner.invoke(app, ["memory", "digest", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["result"]
    assert payload["format"] == "json"
    assert payload["phase"] is None
    assert payload["iter"] is None
    assert payload["recently_closed"] == []
    assert [e["ref_id"] for e in payload["recent_decisions"]] == ["D01"]


def test_memory_digest_root_json_flag_emits_json(tmp_state: Path) -> None:
    """The root ``--json`` flag selects the structured surface too."""
    result = runner.invoke(app, ["--json", "memory", "digest"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)["result"]
    assert payload["format"] == "json"


def test_memory_digest_is_byte_read_only(tmp_state: Path) -> None:
    """The digest mutates nothing: every file of the generation is unchanged."""
    generation = _generation(tmp_state)

    def _digests() -> dict[str, str]:
        return {
            path.relative_to(generation).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(generation.rglob("*"))
            if path.is_file()
        }

    before = _digests()
    result = runner.invoke(app, ["memory", "digest", "--json"])
    assert result.exit_code == 0, result.output
    assert _digests() == before


def test_memory_digest_refuses_an_epoch1_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tree with no epoch marker selects no generation, so it must migrate first."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(orjson.dumps({}))
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(app, ["--json", "memory", "digest"])
    assert result.exit_code == exit_codes.ATTACH_FAILURE, result.output
    assert "migrate" in result.stdout
