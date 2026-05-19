"""Integration tests for the ``spec.*`` JSON-RPC handlers (P25-W03 / C03).

Covers all four daemon mutator RPCs plus the ``eawf spec show
<urn> --from-git`` recovery path:

* :func:`spec.init` writes the scaffold + cache entry.
* :func:`spec.validate` re-hashes the on-disk body.
* :func:`spec.promote` graduates DRAFT → READY → IMPLEMENTED with
  predecessor-only forward steps.
* :func:`spec.archive` ``git rm``'s the spec file AND writes the
  cache entry with the blob SHA pre-populated.
* CLI ``eawf spec show --from-git`` recovers the body via
  ``git log -- <path>`` after ARCHIVED.
* SPEC_UPDATED envelope publishes on the bus.

Handlers are driven directly through the module-level coroutines so
the tests do not need a live UDS / named-pipe transport.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf import __version__
from eawf.cli.app import app
from eawf.daemon import PROTOCOL_VERSION
from eawf.daemon.bus import EventBus
from eawf.daemon.methods import MethodContext
from eawf.daemon.methods.spec import archive, init, promote, validate
from eawf.state.enums import StoreKind

pytestmark = pytest.mark.integration


def _run(body: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(body())


def _build_ctx(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[MethodContext, Path, Path]:
    """Wire a :class:`MethodContext` with a per-test cache dir + repo root.

    Returns the context, the repo-root path (a fresh git repo), and the
    cache-dir path. ``EAWF_SPEC_CACHE_DIR`` points at the cache root so
    the daemon resolver lands on a ``tmp_path``-rooted file.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "--quiet", "-b", "main"], cwd=repo_root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "ci@example.invalid"],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "ci"],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=repo_root,
        check=True,
    )

    cache_dir = tmp_path / "spec-cache"
    monkeypatch.setenv("EAWF_SPEC_CACHE_DIR", str(cache_dir))

    bus = EventBus()
    ctx = MethodContext(
        started_at="2026-05-19T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=bus,
        state_path=None,
        idempotency_cache={},
    )
    return ctx, repo_root, cache_dir


# ---- spec.init -----------------------------------------------------------


def test_init_writes_scaffold_and_cache_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, repo_root, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        result: dict[str, Any] = await init(
            ctx,
            {
                "scope_id": "P25-I01-W03",
                "title": "Daemon spec writer + cache",
                "repo_code": "EAWF",
                "repo_root": str(repo_root),
            },
        )
        assert result["operation"] == "init"
        assert result["status"] == "DRAFT"
        assert result["scope_id"] == "P25-I01-W03"
        assert result["spec_urn"] == "urn:eawf:v1:spec:EAWF/P25/P25-I01/P25-I01-W03"
        # Spec file on disk.
        spec_path = repo_root / ".ea" / "specs" / "P25" / "P25-I01" / "P25-I01-W03.md"
        assert spec_path.is_file()
        body_str = spec_path.read_text(encoding="utf-8")
        assert "Daemon spec writer + cache" in body_str
        assert "eawf-template: spec-wave" in body_str

    _run(body)


def test_init_is_idempotent_for_existing_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-init the same scope returns the cached entry untouched."""
    ctx, repo_root, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        first = await init(
            ctx,
            {
                "scope_id": "P25-I01-W03",
                "title": "Original",
                "repo_code": "EAWF",
                "repo_root": str(repo_root),
            },
        )
        spec_path = repo_root / ".ea" / "specs" / "P25" / "P25-I01" / "P25-I01-W03.md"
        original_sha = spec_path.read_bytes()
        second = await init(
            ctx,
            {
                "scope_id": "P25-I01-W03",
                "title": "Different title",
                "repo_code": "EAWF",
                "repo_root": str(repo_root),
            },
        )
        # File on disk is unchanged (re-init does not overwrite).
        assert spec_path.read_bytes() == original_sha
        assert first["file_sha"] == second["file_sha"]

    _run(body)


def test_init_publishes_spec_updated_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, repo_root, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)
    published: list[Any] = []
    ctx.bus.publish = lambda env: published.append(env)  # type: ignore[method-assign]

    async def body() -> None:
        await init(
            ctx,
            {
                "scope_id": "P25",
                "title": "Phase spec",
                "repo_code": "EAWF",
                "repo_root": str(repo_root),
            },
        )

    _run(body)
    assert len(published) == 1
    assert published[0].kind is StoreKind.SPEC_UPDATED
    assert published[0].payload["operation"] == "init"
    assert published[0].payload["status"] == "DRAFT"


def test_init_rejects_unknown_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, repo_root, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        with pytest.raises(ValueError, match="validation_failed"):
            await init(
                ctx,
                {
                    "scope_id": "X99",
                    "title": "Bad",
                    "repo_code": "EAWF",
                    "repo_root": str(repo_root),
                },
            )

    _run(body)


# ---- spec.validate ------------------------------------------------------


def test_validate_recomputes_blob_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Validate refreshes the cache entry after an out-of-band edit."""
    ctx, repo_root, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        initial = await init(
            ctx,
            {
                "scope_id": "P25",
                "title": "Phase",
                "repo_code": "EAWF",
                "repo_root": str(repo_root),
            },
        )
        # Out-of-band edit (e.g. operator hand-fills the body).
        spec_path = repo_root / ".ea" / "specs" / "P25" / "spec.md"
        spec_path.write_text("# Phase\n\nBody filled in.\n", encoding="utf-8")

        result = await validate(
            ctx,
            {
                "scope_id": "P25",
                "repo_code": "EAWF",
                "repo_root": str(repo_root),
            },
        )
        assert result["operation"] == "validate"
        assert result["file_sha"] != initial["file_sha"]
        assert result["status"] == "DRAFT"

    _run(body)


def test_validate_rejects_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, repo_root, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        with pytest.raises(ValueError, match="spec file missing"):
            await validate(
                ctx,
                {
                    "scope_id": "P25",
                    "repo_code": "EAWF",
                    "repo_root": str(repo_root),
                },
            )

    _run(body)


# ---- spec.promote -------------------------------------------------------


def test_promote_draft_to_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, repo_root, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        await init(
            ctx,
            {
                "scope_id": "P25",
                "title": "Phase",
                "repo_code": "EAWF",
                "repo_root": str(repo_root),
            },
        )
        result = await promote(
            ctx,
            {
                "scope_id": "P25",
                "repo_code": "EAWF",
                "target_status": "READY",
                "repo_root": str(repo_root),
            },
        )
        assert result["status"] == "READY"

    _run(body)


def test_promote_ready_to_implemented(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, repo_root, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        await init(
            ctx,
            {
                "scope_id": "P25",
                "title": "Phase",
                "repo_code": "EAWF",
                "repo_root": str(repo_root),
            },
        )
        await promote(
            ctx,
            {
                "scope_id": "P25",
                "repo_code": "EAWF",
                "target_status": "READY",
                "repo_root": str(repo_root),
            },
        )
        result = await promote(
            ctx,
            {
                "scope_id": "P25",
                "repo_code": "EAWF",
                "target_status": "IMPLEMENTED",
                "repo_root": str(repo_root),
            },
        )
        assert result["status"] == "IMPLEMENTED"

    _run(body)


def test_promote_rejects_skip_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Skipping DRAFT → IMPLEMENTED fails because the predecessor step missed."""
    ctx, repo_root, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        await init(
            ctx,
            {
                "scope_id": "P25",
                "title": "Phase",
                "repo_code": "EAWF",
                "repo_root": str(repo_root),
            },
        )
        with pytest.raises(ValueError, match="invalid graduation"):
            await promote(
                ctx,
                {
                    "scope_id": "P25",
                    "repo_code": "EAWF",
                    "target_status": "IMPLEMENTED",
                    "repo_root": str(repo_root),
                },
            )

    _run(body)


# ---- spec.archive (criterion 2) ------------------------------------------


def test_archive_git_rms_and_writes_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ARCHIVED atomically ``git rm``'s + writes the daemon-resident cache."""
    ctx, repo_root, cache_dir = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        await init(
            ctx,
            {
                "scope_id": "P25",
                "title": "Phase",
                "repo_code": "EAWF",
                "repo_root": str(repo_root),
            },
        )
        await promote(
            ctx,
            {
                "scope_id": "P25",
                "repo_code": "EAWF",
                "target_status": "READY",
                "repo_root": str(repo_root),
            },
        )
        await promote(
            ctx,
            {
                "scope_id": "P25",
                "repo_code": "EAWF",
                "target_status": "IMPLEMENTED",
                "repo_root": str(repo_root),
            },
        )
        # Stage + commit so ``git rm`` has something to remove from the index.
        spec_rel = ".ea/specs/P25/spec.md"
        subprocess.run(["git", "add", spec_rel], cwd=repo_root, check=True)
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "seed"],
            cwd=repo_root,
            check=True,
        )

        result = await archive(
            ctx,
            {
                "scope_id": "P25",
                "repo_code": "EAWF",
                "repo_root": str(repo_root),
            },
        )
        # File no longer on disk.
        spec_path = repo_root / spec_rel
        assert not spec_path.exists()
        # Cache file under the daemon-resident cache dir has the
        # archived entry with the blob SHA pre-populated.
        cache_file = cache_dir / "P25.json"
        assert cache_file.is_file()
        assert result["status"] == "ARCHIVED"
        assert result["file_sha"]  # non-empty — recoverable via git log.

    _run(body)


def test_archive_refuses_when_not_implemented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Archive must follow IMPLEMENTED; calling on DRAFT raises."""
    ctx, repo_root, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        await init(
            ctx,
            {
                "scope_id": "P25",
                "title": "Phase",
                "repo_code": "EAWF",
                "repo_root": str(repo_root),
            },
        )
        with pytest.raises(ValueError, match="cannot archive from status"):
            await archive(
                ctx,
                {
                    "scope_id": "P25",
                    "repo_code": "EAWF",
                    "repo_root": str(repo_root),
                },
            )

    _run(body)


# ---- spec show --from-git (criterion 3) ----------------------------------


def test_cli_spec_show_from_git_recovers_archived_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`eawf spec show <urn> --from-git` recovers a body archived via the daemon."""
    ctx, repo_root, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def setup() -> None:
        await init(
            ctx,
            {
                "scope_id": "P25",
                "title": "Phase",
                "repo_code": "EAWF",
                "repo_root": str(repo_root),
            },
        )
        # Hand-fill the body so we have a distinctive payload to recover.
        spec_path = repo_root / ".ea" / "specs" / "P25" / "spec.md"
        spec_path.write_text("# Recovered body\n", encoding="utf-8")
        # Validate to refresh the cache file_sha.
        await validate(
            ctx,
            {
                "scope_id": "P25",
                "repo_code": "EAWF",
                "repo_root": str(repo_root),
            },
        )
        # Commit so ``git rm`` has a tracked file to remove.
        subprocess.run(
            ["git", "add", ".ea/specs/P25/spec.md"],
            cwd=repo_root,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "seed"],
            cwd=repo_root,
            check=True,
        )
        # Promote then archive.
        await promote(
            ctx,
            {
                "scope_id": "P25",
                "repo_code": "EAWF",
                "target_status": "READY",
                "repo_root": str(repo_root),
            },
        )
        await promote(
            ctx,
            {
                "scope_id": "P25",
                "repo_code": "EAWF",
                "target_status": "IMPLEMENTED",
                "repo_root": str(repo_root),
            },
        )
        await archive(
            ctx,
            {
                "scope_id": "P25",
                "repo_code": "EAWF",
                "repo_root": str(repo_root),
            },
        )
        # Stage + commit the archive so the file is gone from HEAD.
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "archive"],
            cwd=repo_root,
            check=True,
        )

    _run(setup)

    # ``eawf spec show`` reads via the CLI surface; force daemonless mode so
    # the CLI uses the in-process cache reader.
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(repo_root),
            "spec",
            "show",
            "urn:eawf:v1:spec:EAWF/P25",
            "--from-git",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Recovered body" in result.output
