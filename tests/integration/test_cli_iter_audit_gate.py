"""Daemonless CLI parity tests for strict iter-audit acceptance."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

_ITER_ID = "P01-I01"
_AUDIT_ID = "AUD-ITER"

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Create one active empty iter routed through the daemonless fallback."""
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    assert (
        runner.invoke(
            app,
            ["project", "init", "QR", "--title", "QR", "--domains", "workflow"],
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["phase", "open", "--auto", "--title", "Phase"]).exit_code == 0
    assert (
        runner.invoke(
            app,
            ["iter", "open", "--phase", "P01", "--title", "Iter"],
        ).exit_code
        == 0
    )
    yield tmp_path


def _write_strict_config(workspace: Path, *, value: bool = True) -> None:
    workspace.joinpath(".ea", "config.yaml").write_text(
        f"verify:\n  require_iter_audit_accepted: {'true' if value else 'false'}\n",
        encoding="utf-8",
    )


def _valid_audit(*, verdict: str = "pass") -> dict[str, object]:
    return {
        "id": _AUDIT_ID,
        "scope_id": _ITER_ID,
        "kind": "evaluation",
        "status": "complete",
        "created_at": "2026-01-01T00:00:00Z",
        "verdict": verdict,
        "check_results": [
            {
                "name": "acceptance",
                "passed": True,
                "details": "targeted verification passed",
            }
        ],
    }


def _install_audit(workspace: Path, audit: dict[str, object] | None) -> None:
    state_path = workspace / ".ea" / "state.json"
    state = orjson.loads(state_path.read_bytes())
    state["audits"] = {} if audit is None else {_AUDIT_ID: audit}
    state_path.write_bytes(orjson.dumps(state, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))


def _file_digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _durable_digests(workspace: Path) -> dict[str, str | None]:
    ea_dir = workspace / ".ea"
    paths = {
        "state": ea_dir / "state.json",
        "event": ea_dir / "store" / "event.jsonl",
        "audit": ea_dir / "store" / "audit.jsonl",
        "memory": ea_dir / "store" / "memory.jsonl",
    }
    return {name: _file_digest(path) for name, path in paths.items()}


def _unknown(_audit: dict[str, object]) -> None:
    return None


def _wrong_scope(audit: dict[str, object]) -> None:
    audit["scope_id"] = "P01"


def _wrong_kind(audit: dict[str, object]) -> None:
    audit["kind"] = "review"


def _pending(audit: dict[str, object]) -> None:
    audit["status"] = "pending"


def _failed(audit: dict[str, object]) -> None:
    audit["status"] = "failed"


def _major(audit: dict[str, object]) -> None:
    audit["verdict"] = "major"


def _future(audit: dict[str, object]) -> None:
    audit["created_at"] = "2099-01-01T00:00:00Z"


def _stub_only(audit: dict[str, object]) -> None:
    audit["check_results"] = [{"name": "stub", "passed": True, "details": "Phase 2 stub"}]


def _empty(audit: dict[str, object]) -> None:
    audit["check_results"] = []


@pytest.mark.parametrize(
    ("mutate_audit", "guard_code"),
    [
        (_unknown, "audit_not_found"),
        (_wrong_scope, "audit_scope_mismatch"),
        (_wrong_kind, "audit_kind_invalid"),
        (_pending, "audit_not_complete"),
        (_failed, "audit_not_complete"),
        (_major, "audit_verdict_rejected"),
        (_future, "audit_not_complete"),
        (_stub_only, "audit_evidence_missing"),
        (_empty, "audit_evidence_missing"),
    ],
    ids=[
        "unknown",
        "wrong-scope",
        "wrong-kind",
        "pending",
        "failed",
        "major",
        "future",
        "stub-only",
        "empty",
    ],
)
def test_iter_close_daemonless_strict_invalid_matrix_preserves_durable_digests(
    workspace: Path,
    mutate_audit: Callable[[dict[str, object]], None],
    guard_code: str,
) -> None:
    _write_strict_config(workspace)
    audit = _valid_audit()
    mutate_audit(audit)
    _install_audit(workspace, None if mutate_audit is _unknown else audit)
    store_dir = workspace / ".ea" / "store"
    store_dir.mkdir(exist_ok=True)
    store_dir.joinpath("audit.jsonl").write_text("audit sentinel\n", encoding="utf-8")
    store_dir.joinpath("memory.jsonl").write_text("memory sentinel\n", encoding="utf-8")
    before = _durable_digests(workspace)

    result = runner.invoke(
        app,
        ["--json", "iter", "close", _ITER_ID, "--audit", _AUDIT_ID],
    )

    assert result.exit_code == 2, result.stdout
    assert guard_code in result.stdout
    assert _durable_digests(workspace) == before


def test_iter_close_daemonless_strict_false_keeps_legacy_compatibility(
    workspace: Path,
) -> None:
    _write_strict_config(workspace, value=False)
    _install_audit(workspace, None)

    result = runner.invoke(
        app,
        ["--json", "iter", "close", _ITER_ID, "--audit", "AUD-PHANTOM"],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["audit"] == "AUD-PHANTOM"
    assert payload["warnings"] == []


@pytest.mark.parametrize("verdict", ["pass", "minor"])
def test_iter_close_daemonless_strict_accepts_real_audit_and_returns_warning_parity(
    workspace: Path,
    verdict: str,
) -> None:
    _write_strict_config(workspace)
    _install_audit(workspace, _valid_audit(verdict=verdict))

    result = runner.invoke(
        app,
        ["--json", "iter", "close", _ITER_ID, "--audit", _AUDIT_ID],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    expected = ["audit_minor_backlog_triage"] if verdict == "minor" else []
    assert payload["warnings"] == expected
    state = orjson.loads(workspace.joinpath(".ea", "state.json").read_bytes())
    assert state["iters"][_ITER_ID]["status"] == "closed"
    assert state["iters"][_ITER_ID]["audit_id"] == _AUDIT_ID


def test_iter_close_daemonless_profile_strictness_cannot_be_loosened_by_repo(
    workspace: Path,
) -> None:
    profile_dir = workspace / ".ea" / "profiles"
    profile_dir.mkdir()
    profile_dir.joinpath("strict.yaml").write_text(
        "name: strict\nverify:\n  require_iter_audit_accepted: true\n",
        encoding="utf-8",
    )
    workspace.joinpath(".ea", "config.yaml").write_text(
        "profiles:\n  enabled:\n    - strict\nverify:\n  require_iter_audit_accepted: false\n",
        encoding="utf-8",
    )
    _install_audit(workspace, None)

    result = runner.invoke(
        app,
        ["--json", "iter", "close", _ITER_ID, "--audit", "AUD-PHANTOM"],
    )

    assert result.exit_code == 2, result.stdout
    assert "audit_not_found" in result.stdout
