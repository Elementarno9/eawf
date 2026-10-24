"""Inspect the Claude Code plugin tree for drift / missing files.

``eawf plugin doctor claude`` does a hash-equality sweep over every
file the installer claims ownership of:

- The file is missing → :attr:`DoctorReport.missing` grows.
- The file's bytes hash differs from the expected (registry-rendered)
  bytes → :attr:`DoctorReport.drifted` grows.
- The file matches expectation → :attr:`DoctorReport.ok` grows.

``settings.json`` is also read for wiring an older install left behind
(its recorded hash still matches, so the byte check alone passes it), and,
when the marketplace plugin is installed too, for project hooks that make
Claude Code run every eawf handler twice.

Exit code mapping (the CLI surface in
:mod:`eawf.surfaces.cli.commands.plugin`):

- Clean (no drift, no missing) → exit 0.
- Any drift or missing entry → exit 8 (``INTEGRITY_VIOLATION``).

``--strict`` mode (:func:`doctor_plugin_strict`) runs the same
hash-equality sweep but under the shared sync/doctor advisory lock so
the checksum it reads is the post-``plugin sync`` checksum from the
same lock-scope — closing the sync-then-doctor race where a concurrent
edit to a managed file (or to ``AGENTS.md``) lands between a successful
``plugin sync`` and the drift gate. ``plugin sync`` acquires the same
lock target (:func:`plugin_sync_lock_path`) before regenerating the
tree, so the two verbs serialise.

Public API::

    DoctorReport                          # dataclass with summary lists
    DoctorEntry                           # one line of the sweep
    doctor_plugin(target_dir) -> DoctorReport
    doctor_plugin_strict(target_dir) -> DoctorReport   # under shared lock
    plugin_sync_lock_path(repo_root) -> Path           # shared lock target
    plugin_sync_lock(repo_root) -> ContextManager      # shared lock CM
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eawf.kernel.config.layered import resolve_agent_extra_tools
from eawf.runtime.harness.fan_out import unwritten_plan_keys
from eawf.runtime.lock import portalock
from eawf.runtime.runtimes.claude.hook_map import build_plugin_hooks_json
from eawf.runtime.runtimes.claude.plugin_install import (
    _HOOK_COMMAND_MARKER,
    _MANAGED_KEY,
    _eawf_settings_hooks,
    _event_type_for,
    _expected_bytes_for,
    expected_paths,
)
from eawf.surfaces.render.manifest import Manifest, ManifestEntry

logger = logging.getLogger(__name__)


def plugin_sync_lock_path(repo_root: Path) -> Path:
    """Return the shared sync/doctor advisory-lock target under ``.ea/locks/``.

    Both ``eawf plugin sync`` and ``eawf plugin doctor --strict`` acquire
    this lock so the strict drift gate reads the post-sync checksum from
    the same lock-scope (closing the race in which a concurrent edit
    lands between a successful sync and the doctor sweep). The parent
    directory is created on demand; the flock file is materialised by
    :func:`portalock.acquire` and removed on release.

    Args:
        repo_root: Repository root (the directory containing ``.ea/``).

    Returns:
        ``<repo_root>/.ea/locks/plugin-sync.lock`` — gitignored per the
        ``.ea/`` commit policy (only ``state.json`` / config / store are
        committed; ``.ea/locks/`` is not).
    """
    return Path(repo_root) / ".ea" / "locks" / "plugin-sync.lock"


@contextmanager
def plugin_sync_lock(repo_root: Path, *, timeout: float = 5.0) -> Iterator[None]:
    """Acquire the shared ``plugin sync`` / ``plugin doctor --strict`` lock.

    The lock is a :func:`eawf.runtime.lock.portalock.acquire` on
    :func:`plugin_sync_lock_path`. ``flock`` is non-recursive, so callers
    must hold the lock at exactly one nesting level per invocation.

    Args:
        repo_root: Repository root (the directory containing ``.ea/``).
        timeout: Seconds to wait before raising :class:`LockTimeout`.

    Raises:
        LockTimeout: When the lock cannot be acquired within *timeout*.
    """
    lock_target = plugin_sync_lock_path(repo_root)
    lock_target.parent.mkdir(parents=True, exist_ok=True)
    logger.debug(f"plugin_sync_lock acquiring target={lock_target}")
    with portalock.acquire(lock_target, timeout=timeout):
        yield
    logger.debug(f"plugin_sync_lock released target={lock_target}")


@dataclass(frozen=True)
class DoctorEntry:
    """One row of the doctor sweep.

    Attributes:
        region_id: The plugin region identifier (e.g.
            ``"plugin.claude.skill.research"``).
        path: Disk path that was inspected.
        kind: One of ``"ok"``, ``"missing"``, ``"drifted"``.
        on_disk_hash: Recomputed hash of the on-disk bytes; ``None``
            when the file is missing.
        expected_hash: Hash of the bytes the installer would emit for
            this region.
        fix: The command or step that clears this entry, when it is not
            the generic reinstall.
    """

    region_id: str
    path: Path
    kind: str  # Literal["ok", "missing", "drifted"]
    on_disk_hash: str | None
    expected_hash: str
    fix: str | None = None


@dataclass(frozen=True)
class DoctorReport:
    """Aggregate result of one :func:`doctor_plugin` call.

    Attributes:
        target_dir: Workspace root that was inspected.
        ok: Entries whose on-disk bytes match the expected bytes.
        drifted: Entries whose on-disk bytes have been hand-edited.
        missing: Entries whose file is gone.
        clean: Property derived from the lists.
    """

    target_dir: Path
    ok: list[DoctorEntry] = field(default_factory=list)
    drifted: list[DoctorEntry] = field(default_factory=list)
    missing: list[DoctorEntry] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """``True`` when no entry is drifted or missing."""
        return not self.drifted and not self.missing


def _hash_bytes(payload: bytes) -> str:
    """Return the canonical 16-hex blake2b digest used across the renderer."""
    return hashlib.blake2b(payload, digest_size=8).hexdigest()


def _load_manifest(target_dir: Path) -> Manifest:
    """Load ``.ea/indexes/generated.json`` under *target_dir* (empty when absent)."""
    manifest_path = target_dir / ".ea" / "indexes" / "generated.json"
    if not manifest_path.exists():
        return Manifest()
    raw = manifest_path.read_text(encoding="utf-8")
    if not raw.strip():
        return Manifest()
    body = json.loads(raw)
    return Manifest.model_validate(body)


def _settings_managed_hash_from_manifest(manifest: Manifest, settings_path: Path) -> str | None:
    """Look up the recorded hash for ``settings.json`` from *manifest*.

    Returns the recorded hash for the ``plugin.claude.settings`` entry
    keyed at *settings_path*, or ``None`` when the manifest has no
    record of the file (i.e. install never ran).
    """
    key = f"{settings_path.as_posix()}::plugin.claude.settings"
    entry: ManifestEntry | None = manifest.generated.get(key)
    if entry is None:
        return None
    return entry.hash


#: Clears the wiring an older install left in ``settings.json``: the
#: re-install drops every eawf hook entry and rewrites ``__eawf_managed``.
_UPDATE_FIX: str = "eawf plugin update claude"

#: Clears a project install's hooks doubling the marketplace plugin's.
_DOUBLE_LOAD_FIX: str = (
    "keep one install mode: run /plugin uninstall eawf@eawf in Claude Code, "
    "or remove the project install's eawf entries from .claude/settings.json hooks"
)


def doctor_plugin(target_dir: Path, *, marketplace_installed: bool = False) -> DoctorReport:
    """Inspect the Claude plugin tree under *target_dir*.

    Args:
        target_dir: Workspace root that hosts ``.claude/``.
        marketplace_installed: The eawf plugin is also installed for the
            user from the marketplace, so its ``hooks/hooks.json`` loads
            beside the project ``settings.json`` hooks.

    Returns:
        :class:`DoctorReport` summarising every file Eä installs.

    The check is byte-equality on the rendered files (skills/agents/
    hooks). For ``settings.json`` we compare the on-disk bytes against
    the manifest's recorded hash, because the user owns most of the
    file and may have legitimately changed unrelated keys; only a hand
    edit *to the rendered bytes themselves* counts as drift. When the
    manifest is absent, ``settings.json`` is reported as ``"missing"``
    so the operator gets a single coherent "rerun install" recipe.
    """
    target_dir = Path(target_dir).resolve()
    manifest = _load_manifest(target_dir)
    paths, settings_path = expected_paths(target_dir)
    # Drift is byte-equality, so the comparison must render with the same
    # tool grant the install resolved — anchored at the same target_dir.
    extra_tools = resolve_agent_extra_tools(target_dir)

    ok: list[DoctorEntry] = []
    drifted: list[DoctorEntry] = []
    missing: list[DoctorEntry] = []

    for region_id, path in paths.items():
        expected = _expected_bytes_for_region(region_id, extra_tools=extra_tools)
        expected_hash = _hash_bytes(expected)
        if not path.exists():
            missing.append(
                DoctorEntry(
                    region_id=region_id,
                    path=path,
                    kind="missing",
                    on_disk_hash=None,
                    expected_hash=expected_hash,
                )
            )
            continue
        live = path.read_bytes()
        live_hash = _hash_bytes(live)
        if live_hash == expected_hash:
            ok.append(
                DoctorEntry(
                    region_id=region_id,
                    path=path,
                    kind="ok",
                    on_disk_hash=live_hash,
                    expected_hash=expected_hash,
                )
            )
        else:
            drifted.append(
                DoctorEntry(
                    region_id=region_id,
                    path=path,
                    kind="drifted",
                    on_disk_hash=live_hash,
                    expected_hash=expected_hash,
                )
            )

    # settings.json compared against the manifest-recorded hash.
    expected_settings_hash = _settings_managed_hash_from_manifest(manifest, settings_path)
    if expected_settings_hash is None:
        missing.append(
            DoctorEntry(
                region_id="plugin.claude.settings",
                path=settings_path,
                kind="missing",
                on_disk_hash=None,
                expected_hash="",
            )
        )
    else:
        if not settings_path.exists():
            missing.append(
                DoctorEntry(
                    region_id="plugin.claude.settings",
                    path=settings_path,
                    kind="missing",
                    on_disk_hash=None,
                    expected_hash=expected_settings_hash,
                )
            )
        else:
            live = settings_path.read_bytes()
            live_hash = _hash_bytes(live)
            kind = "ok" if live_hash == expected_settings_hash else "drifted"
            entry = DoctorEntry(
                region_id="plugin.claude.settings",
                path=settings_path,
                kind=kind,
                on_disk_hash=live_hash,
                expected_hash=expected_settings_hash,
            )
            (ok if kind == "ok" else drifted).append(entry)
            document = _settings_document(live)
            for region_id in _stale_settings_regions(document):
                drifted.append(
                    DoctorEntry(
                        region_id=region_id,
                        path=settings_path,
                        kind="drifted",
                        on_disk_hash=live_hash,
                        expected_hash=expected_settings_hash,
                        fix=_UPDATE_FIX,
                    )
                )
            if marketplace_installed and _doubled_hook_events(document):
                drifted.append(
                    DoctorEntry(
                        region_id="plugin.claude.hooks.double_load",
                        path=settings_path,
                        kind="drifted",
                        on_disk_hash=live_hash,
                        expected_hash=expected_settings_hash,
                        fix=_DOUBLE_LOAD_FIX,
                    )
                )
            # A concurrency plan Claude Code's own scheduler never reads is
            # prose, so an unwritten fan-out key is drift on its own.
            if unwritten_plan_keys("claude", document):
                drifted.append(
                    DoctorEntry(
                        region_id="plugin.claude.fan_out",
                        path=settings_path,
                        kind="drifted",
                        on_disk_hash=live_hash,
                        expected_hash=expected_settings_hash,
                    )
                )

    logger.info(
        f"doctor_plugin target={target_dir} ok={len(ok)} drifted={len(drifted)} "
        f"missing={len(missing)}"
    )
    return DoctorReport(target_dir=target_dir, ok=ok, drifted=drifted, missing=missing)


def _settings_document(settings_bytes: bytes) -> dict[str, Any]:
    """Parse ``settings.json``; a file that is not a JSON object reads as empty."""
    try:
        document = json.loads(settings_bytes)
    except UnicodeDecodeError, json.JSONDecodeError:
        return {}
    return document if isinstance(document, dict) else {}


def _eawf_hook_events(document: Mapping[str, Any]) -> set[str]:
    """Return the Claude Code events whose ``hooks`` entries run an eawf wrapper."""
    hooks = document.get("hooks")
    events: set[str] = set()
    for event, entries in hooks.items() if isinstance(hooks, dict) else ():
        for entry in entries if isinstance(entries, list) else ():
            commands = entry.get("hooks") if isinstance(entry, dict) else None
            if any(
                isinstance(command, dict)
                and _HOOK_COMMAND_MARKER in str(command.get("command", ""))
                for command in (commands if isinstance(commands, list) else ())
            ):
                events.add(event)
    return events


def _stale_settings_regions(document: Mapping[str, Any]) -> list[str]:
    """Return a region per piece of wiring an older install left in ``settings.json``.

    The rc2 install nested its hook list under ``__eawf_managed.hooks``,
    which Claude Code 2.1.310 refuses, and wired the session-end wrapper
    under ``Stop``, whose payload the router no longer maps, so the session
    end was never stamped. Any eawf wrapper under an event the current
    install does not wire is the same defect.
    """
    regions: list[str] = []
    managed = document.get(_MANAGED_KEY)
    if isinstance(managed, dict) and "hooks" in managed:
        regions.append("plugin.claude.settings.managed_hooks")
    regions.extend(
        f"plugin.claude.settings.hook.{event}"
        for event in sorted(_eawf_hook_events(document) - set(_eawf_settings_hooks()))
    )
    return regions


def _doubled_hook_events(document: Mapping[str, Any]) -> set[str]:
    """Return the events the project hooks and the marketplace plugin both wire."""
    return _eawf_hook_events(document) & set(build_plugin_hooks_json()["hooks"])


def doctor_plugin_strict(target_dir: Path, *, timeout: float = 5.0) -> DoctorReport:
    """Run :func:`doctor_plugin` under the shared sync/doctor advisory lock.

    The ``--strict`` drift gate is byte-for-byte identical to the default
    sweep; the difference is the *lock-scope*. Holding
    :func:`plugin_sync_lock` for the duration of the sweep guarantees no
    concurrent ``plugin sync`` (or managed-file edit mediated through the
    same lock) interleaves with the checksum read, so a green strict
    doctor means the on-disk tree matched the renderer at a single
    serialised instant — the property CI needs to fail a PR on a stale
    ``build/<runtime>-plugin/`` tree without flaking.

    Args:
        target_dir: Workspace root that hosts ``.claude/`` and ``.ea/``.
        timeout: Seconds to wait for the shared lock before raising
            :class:`~eawf.runtime.lock.portalock.LockTimeout`.

    Returns:
        :class:`DoctorReport` — same shape as :func:`doctor_plugin`.

    Raises:
        LockTimeout: When the shared lock cannot be acquired in *timeout*.
    """
    target_dir = Path(target_dir).resolve()
    with plugin_sync_lock(target_dir, timeout=timeout):
        report = doctor_plugin(target_dir)
    logger.info(
        f"doctor_plugin_strict target={target_dir} clean={report.clean} "
        f"drifted={len(report.drifted)} missing={len(report.missing)}"
    )
    return report


def _expected_bytes_for_region(
    region_id: str,
    *,
    extra_tools: Mapping[str, Sequence[str]] | None = None,
) -> bytes:
    """Wrapper around :func:`plugin_install._expected_bytes_for` (re-exported)."""
    # Indirection so tests can monkeypatch a single resolution point.
    if region_id.startswith("plugin.claude.hook."):
        # Resolve via the shared helper which knows the HookEventType lookup.
        _event_type_for(region_id.removeprefix("plugin.claude.hook."))
    return _expected_bytes_for(region_id, extra_tools=extra_tools)


__all__ = [
    "DoctorEntry",
    "DoctorReport",
    "doctor_plugin",
    "doctor_plugin_strict",
    "plugin_sync_lock",
    "plugin_sync_lock_path",
]
