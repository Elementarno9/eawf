"""Close-time spec-archive cascade helpers.

Two operator-facing entry points drive the daemon's force-archive path
(:func:`eawf.runtime.daemon.methods.spec.archive` with ``force=True``,
landed in P30-I14-W08) over a batch of spec scopes:

- :func:`archive_specs_for_scopes` — given an explicit list of scope ids
  (``P##`` / ``P##-I##`` / ``P##-I##-W##``), git-removes each spec, marks
  its cache row ``ARCHIVED``, and records the blob SHA so
  ``eawf spec show <urn> --from-git`` recovers the body. The ``iter close
  --archive-specs`` cascade calls this with the iter's wave scope ids.
- :func:`archive_phase_specs` — the phase-level escape hatch (back-fill):
  enumerates every non-``ARCHIVED`` row in the phase cache and archives
  each through :func:`archive_specs_for_scopes`, reusing the same force
  path.

Both reuse the single daemon force-archive coroutine rather than
re-implementing the ``git rm`` + cache-write atomicity (DRY): the
coroutine is driven in-process via :func:`asyncio.run` with a minimal
:class:`~eawf.runtime.daemon.methods.MethodContext`. The archive method
touches only the spec cache + git tree (never ``state.json``), so it runs
without a live daemon, mirroring ``tests/runtime/test_spec_archive_force.py``.

A scope that has no cache entry (never initialised) or whose row is
already ``ARCHIVED`` is skipped, so the cascade is idempotent: re-running
``iter close --archive-specs`` after a partial run archives only what
remains.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from eawf.kernel.spec import cache as spec_cache
from eawf.kernel.spec import writer as spec_writer
from eawf.kernel.state.urn import parse as parse_urn
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.spec import archive

logger = logging.getLogger(__name__)


def _build_archive_ctx() -> MethodContext:
    """Build a minimal context for an in-process force-archive call.

    The archive coroutine reads ``ctx.idempotency_cache`` (a plain dict)
    and ``ctx.bus`` (``None`` is tolerated by the publish guard) only — it
    never touches the state-mutation fields, so a bare shape suffices.

    Returns:
        A :class:`MethodContext` wired for an isolated, daemonless archive.
    """
    from eawf import __version__

    return MethodContext(
        started_at="",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        bus=None,
        state_path=None,
        idempotency_cache={},
    )


def _force_archive_one(
    *,
    scope_id: str,
    repo_code: str,
    repo_root: Path,
    cache_dir: Path | None,
) -> str:
    """Force-archive a single spec scope via the daemon coroutine.

    Drives :func:`eawf.runtime.daemon.methods.spec.archive` with
    ``force=True`` so a DRAFT / READY / IMPLEMENTED spec is git-removed,
    its cache row flipped to ``ARCHIVED``, and the blob SHA recorded for
    ``spec show --from-git`` recovery. Reuses the W08 force path verbatim
    (no re-implementation of the ``git rm`` + cache atomicity).

    Args:
        scope_id: ``P##`` / ``P##-I##`` / ``P##-I##-W##``.
        repo_code: Project code symbol used as the spec-URN owner.
        repo_root: Repo working-tree root the spec path resolves under.
        cache_dir: Optional spec-cache override (``None`` defers to the
            ``EAWF_SPEC_CACHE_DIR`` env seam).

    Returns:
        The recorded blob SHA of the archived body.

    Raises:
        ValueError: When *scope_id* was never initialised or its spec
            file is missing (surfaced from the daemon force path).
    """
    params: dict[str, Any] = {
        "scope_id": scope_id,
        "repo_code": repo_code,
        "repo_root": str(repo_root),
        "force": True,
    }
    if cache_dir is not None:
        params["cache_dir"] = str(cache_dir)
    ctx = _build_archive_ctx()

    async def _drive() -> dict[str, Any]:
        return await archive(ctx, params)

    result = asyncio.run(_drive())
    file_sha = str(result["file_sha"])
    logger.info(f"_force_archive_one scope_id={scope_id!r} sha={file_sha[:8]}")
    return file_sha


def archive_specs_for_scopes(
    scope_ids: list[str],
    *,
    repo_code: str,
    repo_root: Path,
    cache_dir: Path | None = None,
) -> list[str]:
    """Force-archive every spec scope in *scope_ids*, skipping absent rows.

    For each scope: look up its cache row; skip when the scope was never
    initialised (no row) or its row is already ``ARCHIVED`` (idempotent
    re-run); otherwise drive the W08 force-archive path. The order of
    *scope_ids* is preserved in the returned list.

    Args:
        scope_ids: Spec scope ids to archive (``P##`` / ``P##-I##`` /
            ``P##-I##-W##``).
        repo_code: Project code symbol used as the spec-URN owner.
        repo_root: Repo working-tree root the spec paths resolve under.
        cache_dir: Optional spec-cache override (``None`` defers to the
            ``EAWF_SPEC_CACHE_DIR`` env seam).

    Returns:
        The scope ids actually archived (those with a non-``ARCHIVED``
        cache row at call time), in input order.
    """
    archived: list[str] = []
    for scope_id in scope_ids:
        phase_id = spec_writer.phase_of(scope_id)
        spec_urn = spec_writer.build_spec_urn(scope_id, repo_code=repo_code)
        entry = spec_cache.find_cached_entry(
            spec_urn,
            phase_id=phase_id,
            cache_dir=cache_dir,
        )
        if entry is None or entry.status == "ARCHIVED":
            logger.debug(f"archive_specs_for_scopes skip scope_id={scope_id!r}")
            continue
        _force_archive_one(
            scope_id=scope_id,
            repo_code=repo_code,
            repo_root=repo_root,
            cache_dir=cache_dir,
        )
        archived.append(scope_id)
    logger.info(f"archive_specs_for_scopes count={len(archived)} of={len(scope_ids)}")
    return archived


def archive_phase_specs(
    phase_id: str,
    *,
    repo_code: str,
    repo_root: Path,
    cache_dir: Path | None = None,
) -> list[str]:
    """Archive every remaining (non-``ARCHIVED``) spec under *phase_id*.

    The phase-level escape hatch (back-fill): reads the per-phase spec
    cache, derives each non-``ARCHIVED`` row's scope id from its URN tail,
    and routes the batch through :func:`archive_specs_for_scopes` so the
    force path is reused unchanged. A phase with no cache file (no specs
    ever initialised) is a clean no-op returning an empty list.

    Args:
        phase_id: Phase symbol (e.g. ``P30``).
        repo_code: Project code symbol used as the spec-URN owner.
        repo_root: Repo working-tree root the spec paths resolve under.
        cache_dir: Optional spec-cache override (``None`` defers to the
            ``EAWF_SPEC_CACHE_DIR`` env seam).

    Returns:
        The scope ids archived in this call, in cache order.
    """
    cache = spec_cache.read_phase_cache(phase_id, cache_dir=cache_dir)
    scope_ids: list[str] = []
    for entry in cache.entries:
        if entry.status == "ARCHIVED":
            continue
        parsed = parse_urn(entry.spec_urn)
        if parsed.id is None:
            continue
        scope_ids.append(parsed.id.split("/")[-1])
    return archive_specs_for_scopes(
        scope_ids,
        repo_code=repo_code,
        repo_root=repo_root,
        cache_dir=cache_dir,
    )


__all__ = [
    "archive_phase_specs",
    "archive_specs_for_scopes",
]
