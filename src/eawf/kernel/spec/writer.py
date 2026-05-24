"""Daemon-internal spec writer helpers.

These functions are invoked from
:mod:`eawf.runtime.daemon.methods.spec` (the JSON-RPC handlers); they own:

* repo-relative spec file paths under ``.ea/specs/<phase>/[<iter>/]
  <wave|spec>.md``,
* the per-phase cache documents at the daemon-resident cache root
  (authority-map row 10),
* the four lifecycle transitions DRAFT / READY / IMPLEMENTED /
  ARCHIVED,
* the ``git rm`` step on ARCHIVED + the cache entry that records the
  archived blob SHA so :func:`eawf spec show <urn> --from-git` can
  recover the body by walking ``git log -- <path>``.

Per D-SUP-01 (authority-map row 10) the daemon is the sole writer for
both surfaces. Callers (CLI, audit DSL, future renderer hooks) reach
the writer only through the daemon RPC layer; importing these helpers
directly from agent / CLI code is a rule 4 violation.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from pathlib import Path
from typing import Literal

from eawf.kernel.spec.cache import (
    SpecCacheEntry,
    SpecCachePhase,
    SpecStatusStr,
    cache_path_for_phase,
    read_phase_cache,
    utcnow_iso,
)
from eawf.kernel.state.ids import RE_ITER, RE_PHASE, RE_WAVE
from eawf.kernel.state.urn import build as build_urn
from eawf.kernel.state.writer import atomic_write_json_locked

logger = logging.getLogger(__name__)


SpecScopeKind = Literal["phase", "iter", "wave"]


# ---- Scope / path resolution ----------------------------------------------


def classify_scope(scope_id: str) -> SpecScopeKind:
    """Return the scope kind implied by *scope_id*.

    Args:
        scope_id: ``P##`` / ``P##-I##`` / ``P##-I##-W##``.

    Returns:
        ``"phase"``, ``"iter"``, or ``"wave"``.

    Raises:
        ValueError: When *scope_id* does not match any of the three
            patterns.
    """
    if RE_WAVE.match(scope_id):
        return "wave"
    if RE_ITER.match(scope_id):
        return "iter"
    if RE_PHASE.match(scope_id):
        return "phase"
    raise ValueError(f"unknown spec scope id: {scope_id!r}")


def phase_of(scope_id: str) -> str:
    """Return the parent phase symbol of *scope_id*."""
    return scope_id.split("-", 1)[0]


def spec_file_path(scope_id: str, *, repo_root: Path) -> Path:
    """Return the repo-relative spec file path for *scope_id*.

    Layout per C03 §5.9::

        .ea/specs/<P##>/spec.md
        .ea/specs/<P##>/<I##>/spec.md
        .ea/specs/<P##>/<I##>/<W##>.md
    """
    kind = classify_scope(scope_id)
    phase = phase_of(scope_id)
    base = repo_root / ".ea" / "specs" / phase
    if kind == "phase":
        return base / "spec.md"
    parts = scope_id.split("-")
    iter_token = "-".join(parts[:2])
    if kind == "iter":
        return base / iter_token / "spec.md"
    wave_token = scope_id  # P##-I##-W##
    return base / iter_token / f"{wave_token}.md"


def build_spec_urn(scope_id: str, *, repo_code: str) -> str:
    """Build the spec URN for *scope_id* under *repo_code*.

    URN shape per C03 §5.8::

        urn:eawf:v1:spec:<repo>/<phase>[/<iter>[/<wave>]]

    Iter and wave tokens carry the full hyphenated form
    (``P25-I01``, ``P25-I01-W03``) so the URN remains parseable
    without external scope context.
    """
    kind = classify_scope(scope_id)
    phase = phase_of(scope_id)
    if kind == "phase":
        urn_id = phase
    elif kind == "iter":
        urn_id = f"{phase}/{scope_id}"
    else:
        parts = scope_id.split("-")
        iter_token = "-".join(parts[:2])
        urn_id = f"{phase}/{iter_token}/{scope_id}"
    return build_urn("spec", owner=repo_code, id=urn_id)


# ---- Scaffolding ----------------------------------------------------------


def scaffold_body(
    *,
    scope_id: str,
    title: str,
    spec_urn: str,
) -> str:
    """Return a minimal scaffolded spec markdown body.

    W03 ships the bare sentinel + frontmatter shape; the renderer hook
    (C03 §5.10) lands in a later wave. The body intentionally stays
    short so the scaffold is a starting point an agent fills in, not a
    final document.
    """
    kind = classify_scope(scope_id)
    template = f"spec-{kind}"
    return (
        f"<!-- eawf-template: {template} -->\n\n"
        f"# {title}\n\n"
        f"**Spec URN:** {spec_urn}\n"
        f"**Status:** DRAFT\n"
        f"**Scope:** {scope_id}\n"
    )


# ---- Disk + git helpers ----------------------------------------------------


def blob_sha_for(content: bytes) -> str:
    """Return the git blob SHA-1 for *content* (matches ``git hash-object``)."""
    header = f"blob {len(content)}\x00".encode()
    return hashlib.sha1(header + content).hexdigest()


def write_spec_file(path: Path, body: str) -> str:
    """Write *body* to *path* (creating parents); return its blob SHA.

    Plain text write — the daemon already serialises ``spec.*`` calls
    through ``in_flight_mutations`` so a sibling-lock per spec file is
    unnecessary for the W03 scope. The returned blob SHA is fed into
    the cache entry's ``file_sha`` field.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = body.encode("utf-8")
    path.write_bytes(content)
    return blob_sha_for(content)


def git_rm_spec(
    *,
    repo_root: Path,
    repo_relative_path: Path,
) -> None:
    """Run ``git rm -- <path>`` under *repo_root*.

    Raises:
        ValueError: When the git command fails (mapped onto
            ``-32602 validation_failed`` by the RPC handler).
    """
    cmd = ["git", "rm", "--quiet", "--", str(repo_relative_path)]
    try:
        completed = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ValueError(f"git rm failed: {exc}") from exc
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise ValueError(f"git rm exited {completed.returncode}: {stderr}")


# ---- Cache writers --------------------------------------------------------


def _upsert_entry(
    cache: SpecCachePhase,
    *,
    entry: SpecCacheEntry,
) -> SpecCachePhase:
    """Return a copy of *cache* with *entry* upserted by ``spec_urn``."""
    new_entries: list[SpecCacheEntry] = []
    replaced = False
    for existing in cache.entries:
        if existing.spec_urn == entry.spec_urn:
            new_entries.append(entry)
            replaced = True
        else:
            new_entries.append(existing)
    if not replaced:
        new_entries.append(entry)
    return SpecCachePhase(
        schema_version=cache.schema_version,
        phase_id=cache.phase_id,
        entries=new_entries,
    )


def write_cache_entry(
    *,
    phase_id: str,
    entry: SpecCacheEntry,
    cache_dir: Path | None = None,
) -> Path:
    """Upsert *entry* into the per-phase cache document.

    Args:
        phase_id: Phase symbol the entry belongs to.
        entry: Cache row to upsert.
        cache_dir: Optional override directory.

    Returns:
        Path of the cache file that was written.
    """
    cache = read_phase_cache(phase_id, cache_dir=cache_dir)
    updated = _upsert_entry(cache, entry=entry)
    cache_path = cache_path_for_phase(phase_id, cache_dir=cache_dir)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json_locked(cache_path, updated.model_dump(mode="json"))
    logger.info(f"write_cache_entry phase={phase_id} urn={entry.spec_urn!r} status={entry.status}")
    return cache_path


def build_entry(
    *,
    spec_urn: str,
    file_sha: str,
    file_path: Path,
    repo_root: Path,
    status: SpecStatusStr,
    archived_commit: str | None = None,
) -> SpecCacheEntry:
    """Build a typed :class:`SpecCacheEntry` with a repo-relative path."""
    try:
        rel = file_path.relative_to(repo_root)
    except ValueError:
        rel = file_path
    return SpecCacheEntry(
        spec_urn=spec_urn,
        file_sha=file_sha,
        file_path=str(rel),
        status=status,
        last_modified=utcnow_iso(),
        archived_commit=archived_commit,
    )


__all__ = [
    "SpecScopeKind",
    "blob_sha_for",
    "build_entry",
    "build_spec_urn",
    "classify_scope",
    "git_rm_spec",
    "phase_of",
    "scaffold_body",
    "spec_file_path",
    "write_cache_entry",
    "write_spec_file",
]
