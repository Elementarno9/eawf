"""Unit tests for the ``--commit`` plumbing on ``eawf wave close``.

Covers the inline helper ``_resolve_commit_sha`` in
:mod:`eawf.surfaces.cli.commands.lifecycle`:

* Happy path: ``git rev-parse <ref>^{commit}`` returns a 40-char hex SHA.
* Error path: non-zero exit -> :class:`InvalidInput` with the canonical
  ``cannot resolve commit ref: <ref!r>`` phrasing.
* Error path: timeout -> :class:`InvalidInput` mentions ``timed out``.
* Error path: missing ``git`` binary -> :class:`InvalidInput`.
* Sanity: non-canonical stdout (e.g. blank, short) is rejected even on
  ``returncode==0``.

These tests monkeypatch :mod:`subprocess` inside the CLI module so the
helper exercises the real branching logic without depending on the
host's git state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import State
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands import lifecycle as lifecycle_cli
from eawf.surfaces.cli.commands import lifecycle_wave
from eawf.surfaces.cli.flags import GlobalFlags


def _patch_run(monkeypatch: pytest.MonkeyPatch, factory: Any) -> None:
    """Replace ``subprocess.run`` *as imported into* the lifecycle module."""
    monkeypatch.setattr("eawf.surfaces.cli.commands.lifecycle.subprocess.run", factory)


def test_resolve_commit_sha_returns_canonical_form(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean ``rev-parse`` invocation returns the stripped 40-char SHA."""
    sha = "0123456789abcdef0123456789abcdef01234567"  # pragma: allowlist secret

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert cmd[:2] == ["git", "rev-parse"]
        assert cmd[2].endswith("^{commit}")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=f"{sha}\n", stderr="")

    _patch_run(monkeypatch, fake_run)
    assert lifecycle_cli._resolve_commit_sha("HEAD") == sha


def test_resolve_commit_sha_passes_ref_with_commit_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The helper hard-codes the ``^{commit}`` suffix on the ref."""
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="a" * 40 + "\n",
            stderr="",
        )

    _patch_run(monkeypatch, fake_run)
    lifecycle_cli._resolve_commit_sha("feature/foo")
    assert captured["cmd"] == ["git", "rev-parse", "feature/foo^{commit}"]


def test_resolve_commit_sha_normalises_short_sha_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """A short-SHA ref is passed to rev-parse and normalised to 40-char hex (B045)."""
    full = "abc1234def5678abc1234def5678abc1234def56"  # pragma: allowlist secret
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=f"{full}\n", stderr="")

    _patch_run(monkeypatch, fake_run)
    resolved = lifecycle_cli._resolve_commit_sha("abc1234")
    assert captured["cmd"] == ["git", "rev-parse", "abc1234^{commit}"]
    assert resolved == full
    assert len(resolved) == 40


def test_resolve_commit_sha_rejects_non_zero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero ``rev-parse`` exit raises ``InvalidInput`` with the ref."""

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=cmd, returncode=128, stdout="", stderr="unknown revision\n"
        )

    _patch_run(monkeypatch, fake_run)
    with pytest.raises(cli_errors.UserError) as exc:
        lifecycle_cli._resolve_commit_sha("does-not-exist")
    assert "cannot resolve commit ref: 'does-not-exist'" in str(exc.value)


def test_resolve_commit_sha_rejects_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A subprocess timeout maps to ``InvalidInput`` with a ``timed out`` hint."""

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=0.1)

    _patch_run(monkeypatch, fake_run)
    with pytest.raises(cli_errors.UserError) as exc:
        lifecycle_cli._resolve_commit_sha("HEAD")
    message = str(exc.value)
    assert "cannot resolve commit ref: 'HEAD'" in message
    assert "timed out" in message


def test_resolve_commit_sha_rejects_missing_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing ``git`` binary surfaces as ``InvalidInput`` not a crash."""

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("git: not found")

    _patch_run(monkeypatch, fake_run)
    with pytest.raises(cli_errors.UserError) as exc:
        lifecycle_cli._resolve_commit_sha("HEAD")
    assert "cannot resolve commit ref: 'HEAD'" in str(exc.value)


def test_resolve_commit_sha_rejects_blank_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty stdout despite ``returncode==0`` fails the canonical-form check."""

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="\n", stderr="")

    _patch_run(monkeypatch, fake_run)
    with pytest.raises(cli_errors.UserError) as exc:
        lifecycle_cli._resolve_commit_sha("HEAD")
    assert "non-canonical sha" in str(exc.value)


def test_resolve_commit_sha_rejects_short_sha_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """``rev-parse`` should always emit 40 chars; anything else is rejected."""

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="abc1234\n", stderr="")

    _patch_run(monkeypatch, fake_run)
    with pytest.raises(cli_errors.UserError) as exc:
        lifecycle_cli._resolve_commit_sha("abc1234")
    assert "non-canonical sha" in str(exc.value)


def test_resolve_commit_sha_rejects_non_hex_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    """40-char output with non-hex characters is rejected."""

    def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        # Right length, wrong alphabet.
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout=("Z" * 40) + "\n", stderr=""
        )

    _patch_run(monkeypatch, fake_run)
    with pytest.raises(cli_errors.UserError) as exc:
        lifecycle_cli._resolve_commit_sha("weird")
    assert "non-canonical sha" in str(exc.value)


def test_wave_close_daemon_proxy_forwards_tokens_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bespoke wave-close daemon proxy includes the final token tally."""
    captured: dict[str, Any] = {}

    class FakeClient:
        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def state_mutate(self, mutation: Any, *, repo_root: str | None = None) -> dict[str, Any]:
            captured["params"] = dict(mutation.params)
            captured["repo_root"] = repo_root
            return {
                "event": {"id": "EV-1"},
                "before_version": "before",
                "after_version": "after",
            }

    monkeypatch.setattr("eawf.surfaces.cli._mutation._daemon_reachable", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", FakeClient)

    handled = lifecycle_cli._wave_close_via_daemon(
        flags=GlobalFlags(json_output=True),
        wave_id="P05-I01-W01",
        outcome="ok",
        resolved_sha=None,
        tokens_consumed=1234,
    )

    assert handled is True
    assert captured["params"]["tokens_consumed"] == 1234
    assert captured["params"]["wave_id"] == "P05-I01-W01"
    assert captured["params"]["outcome"] == "ok"


def test_wave_close_daemon_proxy_forwards_no_runtime_waiver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bespoke wave-close daemon proxy carries the close-scoped runtime waiver."""
    captured: dict[str, Any] = {}

    class FakeClient:
        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def state_mutate(self, mutation: Any, *, repo_root: str | None = None) -> dict[str, Any]:
            captured["params"] = dict(mutation.params)
            captured["repo_root"] = repo_root
            return {
                "event": {"id": "EV-1"},
                "before_version": "before",
                "after_version": "after",
            }

    monkeypatch.setattr("eawf.surfaces.cli._mutation._daemon_reachable", lambda: True)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", FakeClient)

    handled = lifecycle_cli._wave_close_via_daemon(
        flags=GlobalFlags(json_output=True),
        wave_id="P05-I01-W01",
        outcome="ok",
        resolved_sha=None,
        tokens_consumed=None,
        no_runtime_waiver=True,
    )

    assert handled is True
    assert captured["params"]["no_runtime_waiver"] is True
    assert captured["params"]["wave_id"] == "P05-I01-W01"
    assert captured["params"]["outcome"] == "ok"


def test_no_runtime_waiver_records_human_producer(tmp_path: Path) -> None:
    """The no-runtime waiver evidence row is human-produced and waived."""
    state = State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": "repo",
            "urn": "urn:eawf:v1:state:ABC",
            "updated_at": "2026-05-28T00:00:00Z",
            "project": {
                "code": "ABC",
                "slug": "abc",
                "title": "ABC",
                "description": None,
                "domains": ["x"],
                "default_branch": "main",
                "status": "active",
                "repo_urn": "urn:eawf:v1:repo:ABC",
            },
            "current": {
                "project_code": "ABC",
                "active_session_ids": ["SES-OP"],
            },
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {
                "SES-OP": {
                    "id": "SES-OP",
                    "role": "operator",
                    "runtime": "codex",
                    "scope_id": "P05-I01-W01",
                    "status": "active",
                    "claimed_wave_ids": [],
                    "worktree_ids": [],
                    "artifact_ids": [],
                    "started_at": "2026-05-28T00:00:00Z",
                    "ended_at": None,
                    "summary": None,
                    "agent_principal_id": None,
                }
            },
            "plugins": {},
            "indexes": {},
        }
    )

    record = lifecycle_wave._build_no_runtime_waiver_record(
        state,
        wave_id="P05-I01-W01",
        operator_identity="SES-OP",
        state_path=tmp_path / ".ea" / "state.json",
        repo_root=tmp_path,
    )

    assert record.produced_by == "human"
    assert record.status == "waived"
    assert record.evidence_kind == "attested"
    assert record.refs == [lifecycle_wave.NO_RUNTIME_WAIVER_REF]
    assert record.metrics is not None
    assert record.metrics["operator_session"] == "SES-OP"
