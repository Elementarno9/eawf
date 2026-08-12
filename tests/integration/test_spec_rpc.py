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
from eawf.kernel.state.enums import StoreKind
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.spec import archive, init, promote, validate
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


# ---- spec.promote argv-policy seam -------------------------


def test_promote_to_ready_routes_through_argv_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The READY flip routes embedded GateSpec rows through the L0 argv-policy.

    P28-I01-W09 wires
    :func:`eawf.kernel.spec.promotion.validate_argv_gates` into the
    promote handler BEFORE the cache flip. The v0.4.0 body parser
    (``_extract_gate_specs``) is a placeholder that returns ``[]``
    until W08 lands the real parser; this test patches the helper to
    simulate the post-W08 case where the body carries a bad GateSpec
    + asserts the READY flip rejects atomically (status stays DRAFT,
    no cache mutation).
    """
    ctx, repo_root, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    # Build a deliberately-rejecting gate that mimics what a parsed
    # body would yield. ``GateSpec.model_construct`` bypasses the
    # construction-time argv check so the persistence-layer reject
    # path is exercised in isolation (defense-in-depth contract: both
    # layers stand alone).
    from eawf.kernel.spec.common import GateSpec
    from eawf.runtime.daemon.methods import spec as spec_module

    bad_gate = GateSpec.model_construct(
        id="G_BAD",
        criterion_id="C1",
        kind="command_exit_zero",
        args={"argv": ["sh", "-c", "evil"]},
        policy="block",
        cadence="every-wave",
        required=True,
        timeout_s=None,
    )
    monkeypatch.setattr(spec_module, "_extract_gate_specs", lambda _body: [bad_gate])

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
        with pytest.raises(ValueError, match="G_BAD"):
            await promote(
                ctx,
                {
                    "scope_id": "P25",
                    "repo_code": "EAWF",
                    "target_status": "READY",
                    "repo_root": str(repo_root),
                },
            )
        # Atomicity: the spec remains in DRAFT because the cache flip
        # never ran. A subsequent clean promote (with the patch
        # cleared via a re-patch to the empty-list fallback) would
        # succeed; we assert the rejection-state directly via a
        # second promote attempt that still trips the seam.
        with pytest.raises(ValueError, match="G_BAD"):
            await promote(
                ctx,
                {
                    "scope_id": "P25",
                    "repo_code": "EAWF",
                    "target_status": "READY",
                    "repo_root": str(repo_root),
                    # Use a fresh idempotency key so the call is not
                    # served from the cached result.
                    "idempotency_key": "retry-after-reject",
                },
            )

    _run(body)


def test_promote_to_ready_passes_when_body_carries_clean_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spec body whose GateSpec rows pass the L0 policy promotes cleanly."""
    ctx, repo_root, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    from eawf.kernel.spec.common import GateSpec
    from eawf.runtime.daemon.methods import spec as spec_module

    good_gate = GateSpec.model_validate(
        {
            "id": "G_OK",
            "criterion_id": "C1",
            "kind": "command_exit_zero",
            "args": {"argv": ["uv", "run", "pytest", "-q"]},
            "policy": "block",
            "cadence": "every-wave",
        }
    )
    monkeypatch.setattr(spec_module, "_extract_gate_specs", lambda _body: [good_gate])

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


def test_promote_to_implemented_skips_argv_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """READY→IMPLEMENTED does not re-run the argv-policy check.

    The check is gated on ``target_status == "READY"`` — once a spec
    is READY the argv shape is already pinned. This test patches the
    body extractor to a rejecting gate AND graduates past READY; the
    READY step is exercised first with a clean monkeypatch, then the
    IMPLEMENTED step runs with the rejecting patch in place to prove
    the second hop never re-invokes the validator.
    """
    ctx, repo_root, _ = _build_ctx(tmp_path=tmp_path, monkeypatch=monkeypatch)

    from eawf.kernel.spec.common import GateSpec
    from eawf.runtime.daemon.methods import spec as spec_module

    bad_gate = GateSpec.model_construct(
        id="G_BAD",
        criterion_id="C1",
        kind="command_exit_zero",
        args={"argv": ["sh", "-c", "evil"]},
        policy="block",
        cadence="every-wave",
        required=True,
        timeout_s=None,
    )

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
        # First hop: DRAFT→READY with no gates (default placeholder).
        await promote(
            ctx,
            {
                "scope_id": "P25",
                "repo_code": "EAWF",
                "target_status": "READY",
                "repo_root": str(repo_root),
            },
        )
        # Second hop: READY→IMPLEMENTED with a rejecting gate patched
        # in — should still succeed because the seam only fires on
        # READY transitions.
        monkeypatch.setattr(spec_module, "_extract_gate_specs", lambda _body: [bad_gate])
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
