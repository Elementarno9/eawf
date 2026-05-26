"""Tests for ``eawf evidence attest`` (P28-I01-W04)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.kernel.store.paths import store_path
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import evidence as evidence_cli

pytestmark = pytest.mark.unit

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Tmp workspace with ``EA_STATE`` pointing inside it."""
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    state_path = state_dir / "state.json"
    state_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("EA_STATE", str(state_path))
    # Default to daemonless for tests; per-case flips reactivate the
    # daemon path explicitly.
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    yield tmp_path


def _evidence_path(workspace: Path) -> Path:
    return store_path(workspace / ".ea" / "state.json", StoreKind.EVIDENCE)


def _read_envelopes(workspace: Path) -> list[Envelope]:
    path = _evidence_path(workspace)
    if not path.exists():
        return []
    return [
        Envelope.model_validate(orjson.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def test_attest_direct_write_appends_row(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With ``EAWF_EVIDENCE_DIRECT_WRITE=1`` the verb writes one JSONL row."""
    monkeypatch.setenv(evidence_cli.EVIDENCE_DIRECT_WRITE_ENV, "1")
    result = runner.invoke(
        app,
        [
            "evidence",
            "attest",
            "--scope-id",
            "P28-I01-W04",
            "--produced-by",
            "tool",
            "--evidence-kind",
            "deterministic",
            "--status",
            "pass",
            "--summary",
            "pytest green",
        ],
    )
    assert result.exit_code == 0, result.output
    envelopes = _read_envelopes(workspace)
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.kind is StoreKind.EVIDENCE
    record = EvidenceRecord.model_validate(env.payload)
    assert record.scope_id == "P28-I01-W04"
    assert record.evidence_kind == "deterministic"
    assert record.status == "pass"
    assert record.summary == "pytest green"
    assert record.id.startswith("EV-")


def test_attest_direct_write_repeated_refs_and_metrics(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--ref`` repeats and ``--metrics`` JSON round-trip cleanly."""
    monkeypatch.setenv(evidence_cli.EVIDENCE_DIRECT_WRITE_ENV, "1")
    result = runner.invoke(
        app,
        [
            "evidence",
            "attest",
            "--scope-id",
            "P28-I01-W04",
            "--produced-by",
            "human",
            "--evidence-kind",
            "attested",
            "--status",
            "waived",
            "--summary",
            "operator waiver",
            "--ref",
            "DEC-001",
            "--ref",
            "AUD-002",
            "--metrics",
            '{"votes": 3, "tag": "manual"}',
        ],
    )
    assert result.exit_code == 0, result.output
    envelopes = _read_envelopes(workspace)
    assert len(envelopes) == 1
    record = EvidenceRecord.model_validate(envelopes[0].payload)
    assert record.refs == ["DEC-001", "AUD-002"]
    assert record.metrics == {"votes": 3, "tag": "manual"}
    assert record.evidence_kind == "attested"


def test_attest_default_without_daemon_rejects_clearly(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the env var AND a reachable daemon, the verb rejects with a
    diagnostic that names ``EAWF_EVIDENCE_DIRECT_WRITE``.
    """
    monkeypatch.delenv(evidence_cli.EVIDENCE_DIRECT_WRITE_ENV, raising=False)
    # Steer the daemon client to an unreachable runtime dir so the
    # cold-spawn fails fast; the CLI should surface the env-var hint.
    bogus_runtime = workspace / "nope-runtime"
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(bogus_runtime))

    # Replace DaemonClient.__enter__ so the test never spawns a real
    # daemon. The fake raises OSError, mirroring an unreachable socket.
    from eawf.surfaces.cli import _daemon_client as dc

    def _fake_enter(self: Any) -> Any:
        raise OSError("simulated daemon unreachable")

    monkeypatch.setattr(dc.DaemonClient, "__enter__", _fake_enter)

    result = runner.invoke(
        app,
        [
            "evidence",
            "attest",
            "--scope-id",
            "P28-I01-W04",
            "--produced-by",
            "agent",
            "--evidence-kind",
            "jury",
            "--status",
            "pass",
            "--summary",
            "jury vote",
        ],
    )
    assert result.exit_code != 0
    assert "EAWF_EVIDENCE_DIRECT_WRITE" in result.output
    # No row should have been appended on the rejection path.
    assert _read_envelopes(workspace) == []


def test_attest_default_proxies_through_daemon_client(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the env var, the verb calls ``evidence.append`` over the daemon client."""
    monkeypatch.delenv(evidence_cli.EVIDENCE_DIRECT_WRITE_ENV, raising=False)
    captured: dict[str, Any] = {}

    from eawf.surfaces.cli import _daemon_client as dc

    class _FakeClient:
        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            *,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            captured["method"] = method
            captured["params"] = params
            # Mirror the daemon-side append envelope shape.
            record = EvidenceRecord.model_validate(params["record"])  # type: ignore[index]
            return {"id": record.id, "appended_at": "2026-05-26T12:00:00+00:00"}

    monkeypatch.setattr(dc, "DaemonClient", _FakeClient)

    result = runner.invoke(
        app,
        [
            "evidence",
            "attest",
            "--scope-id",
            "P28-I01-W04",
            "--produced-by",
            "tool",
            "--evidence-kind",
            "deterministic",
            "--status",
            "pass",
            "--summary",
            "via daemon",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["method"] == "evidence.append"
    assert "record" in captured["params"]
    record = captured["params"]["record"]
    assert record["evidence_kind"] == "deterministic"
    assert record["scope_id"] == "P28-I01-W04"
    # Direct-write path was NOT taken, so no on-disk row exists.
    assert _read_envelopes(workspace) == []


def test_attest_rejects_unknown_evidence_kind(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typo on ``--evidence-kind`` is rejected before any append."""
    monkeypatch.setenv(evidence_cli.EVIDENCE_DIRECT_WRITE_ENV, "1")
    result = runner.invoke(
        app,
        [
            "evidence",
            "attest",
            "--scope-id",
            "P28-I01-W04",
            "--produced-by",
            "tool",
            "--evidence-kind",
            "telepathy",
            "--status",
            "pass",
            "--summary",
            "should fail",
        ],
    )
    assert result.exit_code != 0
    assert _read_envelopes(workspace) == []


def test_attest_rejects_invalid_metrics_json(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed ``--metrics`` JSON is rejected before any append."""
    monkeypatch.setenv(evidence_cli.EVIDENCE_DIRECT_WRITE_ENV, "1")
    result = runner.invoke(
        app,
        [
            "evidence",
            "attest",
            "--scope-id",
            "P28-I01-W04",
            "--produced-by",
            "tool",
            "--evidence-kind",
            "deterministic",
            "--status",
            "pass",
            "--summary",
            "ok",
            "--metrics",
            "{not json",
        ],
    )
    assert result.exit_code != 0
    assert _read_envelopes(workspace) == []
