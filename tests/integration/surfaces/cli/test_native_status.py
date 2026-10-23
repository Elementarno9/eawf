"""``eawf status`` reads only the native generation of the tree it resolves.

A tree born by ``eawf init`` holds no v1 ``state.json``, so status finds the
tree by its epoch marker and reports the generation's summary: the primary
Track, the active Milestone and the open Tasks. The v1 pointers (phase, iter
and active waves) are gone from both the JSON payload and the plain render.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.compaction import read_document, write_document
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from tests._epoch2_helpers import lay_epoch2_tree

pytestmark = pytest.mark.integration

runner = CliRunner()

TRACK_URN: Final = "eawf://WSP-MAIN/PRJ-DEMO/REP-DEMO/track/TRK-0001"
V1_POINTER_KEYS: Final = frozenset({"phase_id", "iter_id", "active_wave_ids"})


def _born_tree(repo: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run ``eawf init`` in ``repo`` and resolve status from there, return the tree."""
    result = runner.invoke(
        app, ["--json", "--no-input", "init", "--target", str(repo), "--project-code", "DEMO"]
    )
    assert result.exit_code == exit_codes.OK, result.output
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.chdir(repo)
    return repo / ".ea"


def _document_path(tree: Path) -> Path:
    authority = resolve_authority(tree)
    assert authority.target is not None and authority.generation_id is not None
    return authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT


def _status_json() -> dict[str, Any]:
    result = runner.invoke(app, ["--json", "status"])
    assert result.exit_code == exit_codes.OK, result.output
    payload: dict[str, Any] = json.loads(result.stdout)
    return payload


def test_status_on_a_fresh_native_tree_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = _born_tree(tmp_path, monkeypatch)
    assert not (tree / "state.json").exists()

    payload = _status_json()
    assert payload["authority"]["epoch"] == 2
    assert payload["native"] == {
        "track_key": None,
        "milestone_key": None,
        "open_task_count": 0,
        "open_tasks": [],
    }

    # A Track with one active Milestone under it names both; a planned one is not active.
    document_path = _document_path(tree)
    document = read_document(document_path)
    document["track"] = {"TRK-0001": {"urn": TRACK_URN, "revision": 1, "status": "ACTIVE"}}
    document["milestone"] = {
        key: {
            "urn": f"eawf://WSP-MAIN/PRJ-DEMO/REP-DEMO/milestone/{key}",
            "revision": 1,
            "status": status,
            "primary_track_ref": TRACK_URN,
        }
        for key, status in (("MLS-0001", "PLANNED"), ("MLS-0002", "ACTIVE"))
    }
    write_document(document_path, document)
    native = _status_json()["native"]
    assert (native["track_key"], native["milestone_key"]) == ("TRK-0001", "MLS-0002")

    # With no Milestone active the Track still leads and the Milestone is null.
    document["milestone"]["MLS-0002"]["status"] = "COMPLETED"
    write_document(document_path, document)
    native = _status_json()["native"]
    assert (native["track_key"], native["milestone_key"]) == ("TRK-0001", None)
    text = runner.invoke(app, ["status"])
    assert text.exit_code == exit_codes.OK, text.output
    assert "track: TRK-0001 milestone: <none>" in text.stdout


@pytest.mark.parametrize("tree_kind", ["born", "migrated"])
def test_status_carries_no_v1_pointers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tree_kind: str
) -> None:
    if tree_kind == "born":
        _born_tree(tmp_path, monkeypatch)
    else:
        # A tree migrated from v1 keeps a frozen document whose pointers name
        # a phase, an iter and a wave; none of them may leak into status.
        current = {
            "project_code": "QR",
            "track_id": None,
            "phase_id": "P01",
            "iter_id": "P01-I01",
            "active_wave_ids": ["P01-I01-W01"],
            "active_session_ids": [],
        }
        lay_epoch2_tree(tmp_path, state={"schema_version": "1.0", "current": current})
        monkeypatch.delenv("EA_STATE", raising=False)
        monkeypatch.chdir(tmp_path)

    payload = _status_json()
    assert "active_waves" not in payload
    assert not V1_POINTER_KEYS & set(payload.get("current") or {})
    text = runner.invoke(app, ["status"])
    assert text.exit_code == exit_codes.OK, text.output
    assert "phase=" not in text.stdout
    assert "iter=" not in text.stdout


def test_init_and_status_never_touch_v1_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v1_document = (tmp_path / ".ea" / "state.json").resolve()
    touched: list[str] = []

    def _recording(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        def _wrapper(self: Path, *args: Any, **kwargs: Any) -> Any:
            if self.resolve() == v1_document:
                touched.append(name)
            return original(self, *args, **kwargs)

        return _wrapper

    for name in ("open", "read_bytes", "read_text", "write_text"):
        monkeypatch.setattr(Path, name, _recording(name, getattr(Path, name)))

    _born_tree(tmp_path, monkeypatch)
    _status_json()
    text = runner.invoke(app, ["status"])
    assert text.exit_code == exit_codes.OK, text.output
    assert touched == []
