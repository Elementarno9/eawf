"""Tests for the ``spec.archive`` force flag (P30-I14-W08).

The ``force`` param relaxes the IMPLEMENTED-status gate on
:func:`eawf.runtime.daemon.methods.spec.archive`:

* Archiving a DRAFT spec WITHOUT force raises (the status gate holds).
* Archiving a DRAFT spec WITH ``force=True`` succeeds, still records the
  blob SHA, and the body is recoverable via
  ``eawf spec show <urn> --from-git``.
* A missing cache entry still raises even with ``force=True`` — force only
  bypasses the status gate, not the initialise-first guard.

Handlers are driven directly through the module-level coroutines so the
tests do not need a live UDS / named-pipe transport.
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
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.spec import archive, init
from eawf.surfaces.cli.app import app

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
    cache-dir path. ``EAWF_SPEC_CACHE_DIR`` points at the cache root so the
    daemon resolver lands on a ``tmp_path``-rooted file.
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
        started_at="2026-06-10T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=bus,
        state_path=None,
        idempotency_cache={},
    )
    return ctx, repo_root, cache_dir


async def _init_draft_and_commit(ctx: MethodContext, repo_root: Path, *, body_text: str) -> None:
    """Init a P25 phase spec DRAFT, hand-fill the body, and commit it.

    Leaves the spec in DRAFT status (no promote) so the force path is
    exercised from the earliest lifecycle status; commits the file so the
    archive ``git rm`` has a tracked file to remove.
    """
    await init(
        ctx,
        {
            "scope_id": "P25",
            "title": "Phase",
            "repo_code": "EAWF",
            "repo_root": str(repo_root),
        },
    )
    spec_path = repo_root / ".ea" / "specs" / "P25" / "spec.md"
    spec_path.write_text(body_text, encoding="utf-8")
    subprocess.run(["git", "add", ".ea/specs/P25/spec.md"], cwd=repo_root, check=True)
    subprocess.run(
        ["git", "commit", "--quiet", "-m", "seed"],
        cwd=repo_root,
        check=True,
    )


def test_archive_from_draft_without_force_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Archiving a DRAFT spec without ``force`` trips the status gate."""
    ctx, repo_root, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        await _init_draft_and_commit(ctx, repo_root, body_text="# Draft body\n")
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


def test_archive_from_draft_with_force_records_blob_sha_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forced DRAFT archive succeeds, records the SHA, and round-trips via git."""
    ctx, repo_root, cache_dir = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def setup() -> None:
        await _init_draft_and_commit(ctx, repo_root, body_text="# Forced recovered body\n")
        result: dict[str, Any] = await archive(
            ctx,
            {
                "scope_id": "P25",
                "repo_code": "EAWF",
                "repo_root": str(repo_root),
                "force": True,
            },
        )
        # Status flipped to ARCHIVED despite starting from DRAFT.
        assert result["status"] == "ARCHIVED"
        # Blob SHA recorded so the body is recoverable from git history.
        assert result["file_sha"]
        # File removed from disk and the archived cache row written.
        spec_path = repo_root / ".ea" / "specs" / "P25" / "spec.md"
        assert not spec_path.exists()
        assert (cache_dir / "P25.json").is_file()
        # Commit the staged removal so the file is gone from HEAD.
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "archive"],
            cwd=repo_root,
            check=True,
        )

    _run(setup)

    # `eawf spec show --from-git` recovers the archived body via the
    # recorded blob SHA history. Force daemonless so the CLI uses the
    # in-process cache reader.
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
    assert "Forced recovered body" in result.output


def test_archive_force_still_raises_on_missing_cache_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``force=True`` does not bypass the initialise-first cache-entry guard."""
    ctx, repo_root, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    async def body() -> None:
        # No init: the scope has no cache entry, so even a forced archive
        # raises the not-initialised guard.
        with pytest.raises(ValueError, match="not initialised"):
            await archive(
                ctx,
                {
                    "scope_id": "P25",
                    "repo_code": "EAWF",
                    "repo_root": str(repo_root),
                    "force": True,
                },
            )

    _run(body)
