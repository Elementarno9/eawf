"""Integration test mirroring the manual coauthor verification procedure.

The companion doc :doc:`docs/architecture/coauthor.md` describes how an
operator confirms that a real ``git commit`` carries the
``Co-Authored-By`` trailer. This test exercises the deterministic core
of that procedure: run ``eawf coauthor resolve`` in a fresh tmp
workspace and assert the returned trailer line is the canonical
runtime identity. A failure here means the resolver itself is broken,
so any missing-trailer symptom in a real commit is a hook-wiring issue
rather than a policy-layer one.

KISS-001 contract (P25-W12)
---------------------------

Pre-W12 the runtime resolver sniffed environment-variable name prefixes
(``CLAUDE*`` / ``CODEX*``) — implicit detection that operators could not
opt out of. Per KISS-001 the resolver now accepts exactly two explicit
inputs: :data:`~eawf.runtime.runtimes.coauthor.COAUTHOR_RUNTIME_ENV_VAR` (or
the legacy alias :data:`~eawf.runtime.runtimes.coauthor.COAUTHOR_RUNTIME_LEGACY_ENV_VAR`)
and the ``detected_runtime`` field on dispatch payloads. The
``test_kiss_001_*`` tests below assert the explicit-opt-in path AND
the rejection path (no opt-in → no inferred runtime, default applies).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.kernel.config import layered
from eawf.runtime.runtimes.coauthor import (
    COAUTHOR_RUNTIME_ENV_VAR,
    COAUTHOR_RUNTIME_LEGACY_ENV_VAR,
    ImplicitDetectionRejected,
    resolve_runtime_explicit,
)

runner = CliRunner()

_CLAUDE_TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"
_CODEX_TRAILER = "Co-Authored-By: Codex <noreply@openai.com>"


@pytest.fixture
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Yield a tmp repo with no overlays so defaults drive resolution."""
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    fake_global = tmp_path / "global.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    monkeypatch.chdir(repo)
    yield repo


def test_coauthor_resolve_default_runtime_returns_claude_trailer(
    repo_root: Path,
) -> None:
    """Default config + no overrides resolves to the canonical Claude trailer."""
    result = runner.invoke(app, ["--json", "coauthor", "resolve"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["mode"] == "runtime"
    assert body["runtime"] == "claude"
    assert body["trailer"] == _CLAUDE_TRAILER
    assert body["required"] is True


def test_coauthor_resolve_text_mode_emits_trailer_only(repo_root: Path) -> None:
    """Without ``--json`` the resolver prints the bare trailer line.

    This is the exact byte sequence the runtime hook appends to a commit
    message, so the manual verification step in
    ``docs/architecture/coauthor.md`` can compare ``git log -1`` against
    this output verbatim.
    """
    result = runner.invoke(app, ["coauthor", "resolve"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == _CLAUDE_TRAILER


def test_coauthor_resolve_runtime_override_returns_codex_trailer(
    repo_root: Path,
) -> None:
    """``--runtime codex`` overrides ``default_runtime`` cleanly."""
    result = runner.invoke(
        app,
        ["--json", "coauthor", "resolve", "--runtime", "codex"],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["runtime"] == "codex"
    assert body["trailer"] == _CODEX_TRAILER


# ---------------------------------------------------------------------------
# KISS-001: explicit opt-in only
# ---------------------------------------------------------------------------


def test_kiss_001_env_var_opt_in_resolves_codex() -> None:
    """``EAWF_COAUTHOR_RUNTIME=codex`` resolves explicitly to codex."""
    runtime = resolve_runtime_explicit(env={COAUTHOR_RUNTIME_ENV_VAR: "codex"})
    assert runtime == "codex"


def test_kiss_001_legacy_env_var_still_resolves() -> None:
    """The legacy ``EAWF_COAUTHOR_HARNESS`` alias is preserved for the pre-W12 hook."""
    runtime = resolve_runtime_explicit(
        env={COAUTHOR_RUNTIME_LEGACY_ENV_VAR: "claude"},
    )
    assert runtime == "claude"


def test_kiss_001_payload_field_overrides_env() -> None:
    """The explicit payload ``detected_runtime`` field takes precedence over env."""
    runtime = resolve_runtime_explicit(
        env={COAUTHOR_RUNTIME_ENV_VAR: "claude"},
        detected_runtime="codex",
    )
    assert runtime == "codex"


def test_kiss_001_implicit_env_keys_do_not_trigger_detection() -> None:
    """Stray ``CLAUDE*`` / ``CODEX*`` env vars do NOT infer a runtime.

    Pre-W12 the resolver would sniff any key starting with ``CLAUDE`` /
    ``CODEX`` and infer a runtime from it. KISS-001 forbids this — the
    resolver returns ``None`` so the caller falls through to the
    configured default.
    """
    runtime = resolve_runtime_explicit(
        env={
            "CLAUDE_HOME": "/tmp/claude",
            "CODEX_SHELL": "1",
            "CLAUDECODE": "1",
        },
    )
    assert runtime is None


def test_kiss_001_strict_mode_rejects_missing_opt_in() -> None:
    """``strict=True`` raises a clear error pointing at the canonical env var."""
    with pytest.raises(ImplicitDetectionRejected, match=COAUTHOR_RUNTIME_ENV_VAR):
        resolve_runtime_explicit(env={"CLAUDE_HOME": "/tmp"}, strict=True)


def test_kiss_001_strict_mode_passes_with_explicit_env() -> None:
    """``strict=True`` succeeds when the canonical env var is set."""
    runtime = resolve_runtime_explicit(
        env={COAUTHOR_RUNTIME_ENV_VAR: "codex"},
        strict=True,
    )
    assert runtime == "codex"


def test_kiss_001_cli_resolves_runtime_via_explicit_env_var(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: ``EAWF_COAUTHOR_RUNTIME=codex`` flows through to the CLI."""
    monkeypatch.setenv(COAUTHOR_RUNTIME_ENV_VAR, "codex")
    result = runner.invoke(app, ["--json", "coauthor", "resolve"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["trailer"] == _CODEX_TRAILER


def test_kiss_001_cli_ignores_implicit_claude_env_keys(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stray ``CLAUDE_HOME`` does NOT force claude when default is configured.

    Without the implicit detection, the resolver falls through to the
    configured ``default_runtime`` (claude in the fixture defaults) but
    NOT because the env var is sniffed — because the config default
    is claude. Setting the config default to codex and leaving only
    a stray ``CLAUDECODE=1`` env var must still yield codex.
    """
    (repo_root / ".ea" / "config.yaml").write_text(
        "vcs:\n  coauthor:\n    default_runtime: codex\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.delenv(COAUTHOR_RUNTIME_ENV_VAR, raising=False)
    monkeypatch.delenv(COAUTHOR_RUNTIME_LEGACY_ENV_VAR, raising=False)
    result = runner.invoke(app, ["--json", "coauthor", "resolve"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    # default_runtime=codex; no explicit opt-in; stray CLAUDECODE is ignored.
    assert body["trailer"] == _CODEX_TRAILER
