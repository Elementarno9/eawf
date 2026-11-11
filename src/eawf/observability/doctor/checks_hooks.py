"""``eawf doctor hooks``: run each installed plugin bundle's hooks against the hook contract.

A host runs whatever wrapper the installed bundle ships, whichever eawf it
finds, in whatever directory the session stands in. This module runs that
wrapper the way the host does, through ``bash`` and the manifest command, in
throwaway repositories and a throwaway ``XDG_RUNTIME_DIR``, across six
contexts, every subscribed event and a set of payloads, and checks each cell
against the contract every hook keeps:

- the exit status is 0;
- stdout is empty or one JSON document of the shape the host reads for the
  event;
- stderr is empty outside a managed repository;
- no daemon socket appears, except one a managed session start starts.

It also checks the bundle itself: a ``timeout`` on every manifest entry, the
``bash "${CLAUDE_PLUGIN_ROOT}/hooks/eawf-hook.sh" <event>`` command form, no
event subscribed twice, no skill shadowed by a copy under ``.claude/skills``,
``skill-classes.tsv`` equal to the catalogue, and the bundle's version equal
to the eawf its wrapper launches.

The bundle's own ``hooks.json`` and ``plugin.json`` are eawf renders and are
parsed through closed models. The host's plugin registry belongs to the host,
which adds keys freely, so it is read leniently, as the ``plugin_version``
check reads it.

The ``hook_health`` check of ``eawf doctor`` lives here too: it counts the
failures the hook health record holds and points at ``eawf doctor hooks``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.observability.doctor.hook_matrix import (
    CONTEXTS,
    MANAGED_CONTEXTS,
    MatrixEvent,
    Payload,
    StaleDaemon,
    allowed_shape_error,
    matrix_events,
    payloads_for,
)
from eawf.observability.doctor.models import CheckResult
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.hooks.health import HEALTH_RECORD, read_health_rows

HOOK_HEALTH_CHECK: Final = "hook_health"

#: The manifest command form every 0.7.2 entry takes.
_COMMAND_FORM: Final = re.compile(r'bash "\$\{CLAUDE_PLUGIN_ROOT\}/hooks/eawf-hook\.sh" ([a-z_]+)')

#: How long one cell may run; it covers the default permission wait, the
#: longest any cell's hook holds, since a throwaway repository sets no wait.
_CELL_TIMEOUT_S: Final = 30.0

#: How long a daemon a managed session start launched may take to come up.
_START_GRACE_S: Final = 10.0

#: How often the runtime directories are swept for such a daemon.
_SWEEP_INTERVAL_S: Final = 0.5

#: Cells run side by side; each is one short process, mostly interpreter start.
_WORKERS: Final = 2

#: The PATH of a host session with no runner: the system directories, plus
#: a directory holding only uv, which a host may well have on its own.
_SYSTEM_PATH: Final = "/usr/bin:/bin"

#: Environment variables that would point a cell at the caller's own tree or daemon.
_DROPPED_ENV: Final = (
    "EA_STATE",
    "EAWF_RUNTIME_DIR",
    "EAWF_HOOKS",
    "CLAUDE_PLUGIN_ROOT",
    # A doctor run from a session in a managed repository would otherwise let
    # every unmanaged cell's guard reach that repository.
    "CLAUDE_PROJECT_DIR",
)


class _HookCommand(BaseModel):
    """One command entry of a manifest group."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["command"]
    command: str
    timeout: int | None = None


class _HookGroup(BaseModel):
    """One matcher group of a manifest event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    matcher: str = ""
    hooks: tuple[_HookCommand, ...]


class _HooksManifest(BaseModel):
    """A bundle's ``hooks/hooks.json``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hooks: dict[str, tuple[_HookGroup, ...]]


class _PluginAuthor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str


class _PluginManifest(BaseModel):
    """A bundle's ``.claude-plugin/plugin.json``, as eawf renders it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_url: str | None = Field(default=None, alias="$schema")
    name: str
    version: str
    description: str | None = None
    author: _PluginAuthor | None = None
    homepage: str | None = None
    repository: str | None = None
    license: str | None = None
    keywords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CellFailure:
    """One contract criterion one cell missed.

    Attributes:
        context: The repository context the wrapper ran in.
        event: The hook event.
        payload: The payload label.
        criterion: ``exit``, ``stdout``, ``stderr`` or ``daemon``.
        expected: What the contract asks for.
        got: What the cell produced, cut to one short line.
    """

    context: str
    event: str
    payload: str
    criterion: str
    expected: str
    got: str

    def line(self) -> str:
        """Return ``<context> <event> <payload>: <criterion> expected <x> got <y>``."""
        return (
            f"{self.context} {self.event} {self.payload}: "
            f"{self.criterion} expected {self.expected} got {self.got}"
        )


@dataclass(frozen=True, slots=True)
class BundleReport:
    """What ``eawf doctor hooks`` found in one bundle.

    Attributes:
        root: The bundle directory.
        version: The version its ``plugin.json`` names; ``None`` when unreadable.
        findings: One ``<check>: expected <x> got <y>`` line per bundle defect.
        cells: How many matrix cells ran.
        failures: The failing cells' criteria, in matrix order.
    """

    root: Path
    version: str | None
    findings: tuple[str, ...]
    cells: int
    failures: tuple[CellFailure, ...]

    @property
    def ok(self) -> bool:
        """Whether the bundle has no defect and every cell passed."""
        return not self.findings and not self.failures


def installed_bundles(home: Path) -> list[Path]:
    """Return the install directory of every eawf plugin the Claude Code registry records.

    Args:
        home: The home directory the registry lives under.

    Returns:
        The existing install directories, in registry order, each once.
    """
    try:
        document = json.loads(
            (home / ".claude" / "plugins" / "installed_plugins.json").read_text(encoding="utf-8")
        )
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return []
    plugins = document.get("plugins") if isinstance(document, dict) else None
    roots: list[Path] = []
    for key, records in plugins.items() if isinstance(plugins, dict) else ():
        if key.partition("@")[0] != "eawf" or not isinstance(records, list):
            continue
        for record in records:
            path = record.get("installPath") if isinstance(record, dict) else None
            if isinstance(path, str) and Path(path).is_dir() and Path(path) not in roots:
                roots.append(Path(path))
    return roots


def _load_manifest(root: Path) -> _HooksManifest | str:
    """Return the bundle's manifest, or the finding that it cannot be read."""
    try:
        return _HooksManifest.model_validate_json((root / "hooks" / "hooks.json").read_bytes())
    except OSError as exc:
        return f"manifest: expected hooks/hooks.json got {type(exc).__name__}"
    except ValidationError as exc:
        return f"manifest: expected an eawf hooks.json got {exc.errors()[0]['msg']}"


def _bundle_version(root: Path) -> str | None:
    """Return the version the bundle's ``plugin.json`` names, or ``None``."""
    try:
        raw = (root / ".claude-plugin" / "plugin.json").read_bytes()
        return _PluginManifest.model_validate_json(raw).version
    except OSError, ValidationError:
        return None


def _subscriptions(manifest: _HooksManifest) -> dict[str, list[_HookCommand]]:
    """Return every catch-all entry's command, keyed by the host event it is under."""
    return {
        cc_event: [hook for group in groups if group.matcher in {"", "*"} for hook in group.hooks]
        for cc_event, groups in manifest.hooks.items()
    }


def _manifest_findings(manifest: _HooksManifest, events: tuple[MatrixEvent, ...]) -> list[str]:
    """Return the timeout, command form and double subscription findings."""
    findings: list[str] = []
    for cc_event, groups in manifest.hooks.items():
        for group in groups:
            for hook in group.hooks:
                if hook.timeout is None:
                    findings.append(f"timeout {cc_event}: expected a timeout got none")
                if _COMMAND_FORM.fullmatch(hook.command) is None:
                    findings.append(
                        f"command {cc_event}: expected bash ... eawf-hook.sh <event> "
                        f"got {hook.command}"
                    )
    subscriptions = _subscriptions(manifest)
    for event in events:
        count = len(subscriptions.get(event.cc_event, []))
        if count != 1:
            findings.append(f"subscription {event.value}: expected 1 entry got {count}")
    return findings


def _skill_findings(root: Path, project: Path | None, home: Path) -> list[str]:
    """Return the duplicate skill and skill class findings."""
    from eawf.workflow.skills.catalog import render_skill_classes_tsv

    findings: list[str] = []
    skills = root / "skills"
    names = sorted(p.name for p in skills.iterdir() if p.is_dir()) if skills.is_dir() else []
    shadows = [base for base in (project, home) if base is not None]
    for base in shadows:
        for name in names:
            if (base / ".claude" / "skills" / name / "SKILL.md").is_file():
                findings.append(
                    f"duplicate skill {name}: expected only the plugin's got a copy "
                    f"under {'the project' if base == project else 'the user'} .claude/skills"
                )
    classes = root / "hooks" / "skill-classes.tsv"
    try:
        shipped = classes.read_text(encoding="utf-8")
    except OSError:
        shipped = None
    if shipped != render_skill_classes_tsv():
        got = "no file" if shipped is None else "a different table"
        findings.append(f"skill-classes.tsv: expected the catalogue got {got}")
    return findings


def launcher_version(env: Mapping[str, str]) -> str | None:
    """Return the version of the eawf the wrapper launches from *env*'s ``PATH``.

    Args:
        env: The environment the wrapper runs under.

    Returns:
        The version ``eawf --version`` prints; ``None`` when no eawf is on the
        path or it does not answer.
    """
    eawf = shutil.which("eawf", path=env.get("PATH"))
    if eawf is None:
        return None
    try:
        completed = subprocess.run(
            [eawf, "--version"],
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=_CELL_TIMEOUT_S,
            check=False,
            **no_window_kwargs(),
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    text = completed.stdout.strip()
    return text.split()[-1] if completed.returncode == 0 and text else None


def bundle_findings(
    root: Path, *, project: Path | None, home: Path, env: Mapping[str, str]
) -> list[str]:
    """Return every defect of the bundle at *root* the matrix does not exercise.

    Args:
        root: The bundle directory.
        project: The repository the doctor runs in, whose ``.claude/skills``
            would shadow the plugin's; ``None`` outside one.
        home: The home directory whose ``.claude/skills`` would too.
        env: The environment the wrapper runs under, which picks its eawf.

    Returns:
        One ``<check>: expected <x> got <y>`` line per defect.
    """
    manifest = _load_manifest(root)
    findings = [manifest] if isinstance(manifest, str) else []
    if not isinstance(manifest, str):
        findings += _manifest_findings(manifest, matrix_events())
    findings += _skill_findings(root, project, home)
    version, launched = _bundle_version(root), launcher_version(env)
    # A local label (``+dev.g<sha>``) marks a build of the same release.
    if version is None or launched is None or version != launched.partition("+")[0]:
        findings.append(f"version: expected the launcher's eawf {launched} got bundle {version}")
    return findings


@dataclass(frozen=True, slots=True)
class _Contexts:
    """The throwaway directories of one matrix run."""

    base: Path
    dirs: Mapping[str, Path]
    main: Path
    shims: Path


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=eawf", "-c", "user.email=doctor@eawf.invalid", *args],
        cwd=cwd,
        capture_output=True,
        check=True,
        timeout=_CELL_TIMEOUT_S,
        **no_window_kwargs(),
    )


def _lay_contexts(base: Path) -> _Contexts:
    """Create the managed, unmanaged and plain directories the cells run in."""
    from eawf.platform.install.epoch2_birth import bear_epoch2_tree

    main = base / "managed"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    (main / ".ea").mkdir()
    (main / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    bear_epoch2_tree(main / ".ea", project_code="DH", project_title="", born_at=datetime.now(UTC))
    (main / "src").mkdir()
    _git(main, "commit", "-q", "--allow-empty", "-m", "doctor hooks")
    worktree = main / ".ea" / "worktrees" / "doctor"
    _git(main, "worktree", "add", "-q", str(worktree), "-b", "doctor-hooks")
    unmanaged = base / "unmanaged"
    unmanaged.mkdir()
    _git(unmanaged, "init", "-q", "-b", "main")
    plain = base / "plain"
    plain.mkdir()
    shims = base / "shims"
    shims.mkdir()
    uv = shutil.which("uv")
    if uv is not None:
        (shims / "uv").symlink_to(uv)
    dirs = {
        "managed-root": main,
        "managed-subfolder": main / "src",
        "managed-worktree": worktree,
        "unmanaged-git": unmanaged,
        "no-git": plain,
        "runner-missing": main,
    }
    return _Contexts(base=base, dirs=dirs, main=main, shims=shims)


def _cell_env(bundle: Path, contexts: _Contexts, context: str, runtime: Path) -> dict[str, str]:
    """Return the environment one cell's wrapper runs under."""
    env = {k: v for k, v in os.environ.items() if k not in _DROPPED_ENV}
    env["CLAUDE_PLUGIN_ROOT"] = str(bundle)
    env["XDG_RUNTIME_DIR"] = str(runtime)
    env["TMPDIR"] = str(contexts.base)
    env["GIT_CEILING_DIRECTORIES"] = str(contexts.base)
    if context == "runner-missing":
        # No eawf on the path, no active environment uv could run one from,
        # and a home whose install directories hold nothing either.
        env["PATH"] = f"{contexts.shims}{os.pathsep}{_SYSTEM_PATH}"
        env["HOME"] = str(contexts.base / "home")
        env.pop("VIRTUAL_ENV", None)
        # uv would otherwise fetch an interpreter into that empty home.
        env["UV_PYTHON_DOWNLOADS"] = "never"
        env["UV_OFFLINE"] = "1"
    return env


def _daemon_sockets(runtime: Path, stub: Path | None) -> list[str]:
    """Return every daemon trace under *runtime*, relative to it.

    A socket other than the stand-in's is one a daemon bound; a pid file is
    the trace of a daemon that took over the stand-in's path.
    """
    sockets = [path for path in runtime.rglob("*.sock") if path != stub and path.is_socket()]
    return sorted(
        str(path.relative_to(runtime)) for path in [*sockets, *runtime.rglob("eawfd.pid")]
    )


def _stop_daemons(runtime: Path) -> None:
    """Ask every daemon a cell started under *runtime* to shut down."""
    from eawf.runtime.daemon.spawn import request_daemon_shutdown

    for pid_file in runtime.rglob("eawfd.pid"):
        try:
            request_daemon_shutdown(pid_file.parent, drain=False, timeout_seconds=0)
        except RuntimeError, OSError:
            continue


def _judge(
    cell: tuple[str, MatrixEvent, Payload],
    completed: subprocess.CompletedProcess[str] | None,
    sockets: list[str],
) -> list[CellFailure]:
    """Return the criteria one cell missed."""
    context, event, payload = cell

    def fail(criterion: str, expected: str, got: str) -> CellFailure:
        return CellFailure(context, event.value, payload.label, criterion, expected, got[:120])

    if completed is None:
        return [fail("exit", "0", f"no exit within {_CELL_TIMEOUT_S:g} s")]
    failures: list[CellFailure] = []
    if completed.returncode != 0:
        failures.append(fail("exit", "0", str(completed.returncode)))
    shape_error = allowed_shape_error(event.cc_event, completed.stdout)
    if shape_error is not None:
        failures.append(fail("stdout", f"empty or one {event.cc_event} document", shape_error))
    elif payload.deny_when_managed and context in MANAGED_CONTEXTS:
        decision = _permission_decision(completed.stdout)
        if decision != "deny":
            failures.append(fail("guard", "deny", decision or "no decision"))
    if context not in MANAGED_CONTEXTS and context != "runner-missing" and completed.stderr:
        failures.append(fail("stderr", "empty", completed.stderr.strip().splitlines()[0]))
    allowed = event.value == "session_start" and context in MANAGED_CONTEXTS
    if sockets and not allowed:
        failures.append(fail("daemon", "no socket", ", ".join(sockets)))
    return failures


def _permission_decision(stdout: str) -> str | None:
    """Return the ``permissionDecision`` of a pre-tool answer already shaped as one document."""
    if not stdout.strip():
        return None
    specific = json.loads(stdout).get("hookSpecificOutput")
    decision = specific.get("permissionDecision") if isinstance(specific, dict) else None
    return decision if isinstance(decision, str) else None


def _run_cell(
    bundle: Path,
    command: str,
    contexts: _Contexts,
    cell: tuple[str, MatrixEvent, Payload],
) -> list[CellFailure]:
    """Run one cell in a runtime directory of its own and judge it."""
    context, _event, payload = cell
    runtime = Path(tempfile.mkdtemp(prefix="xdg-", dir=contexts.base))
    cwd = contexts.dirs[context]
    try:
        with _daemon_stand_in(payload, runtime, contexts.main) as stub:
            try:
                # The host's shell, not the cell's PATH, finds bash.
                completed: subprocess.CompletedProcess[str] | None = subprocess.run(
                    [shutil.which("bash") or "/bin/bash", "-c", command],
                    input=payload.stdin(cwd),
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                    env=_cell_env(bundle, contexts, context, runtime),
                    timeout=_CELL_TIMEOUT_S,
                    check=False,
                    **no_window_kwargs(),
                )
            except subprocess.TimeoutExpired:
                completed = None
            sockets = _daemon_sockets(runtime, stub)
        return _judge(cell, completed, sockets)
    finally:
        _stop_daemons(runtime)


@contextmanager
def _daemon_stand_in(payload: Payload, runtime: Path, main: Path) -> Iterator[Path | None]:
    """Put a dead socket or a stale-version stub where the cell's hook dials its daemon."""
    from eawf.runtime.daemon.runtime_dir import TREES_DIRNAME, tree_key

    if payload.daemon == "none":
        yield None
        return
    directory = runtime / "eawfd" / TREES_DIRNAME / tree_key(main / ".ea" / "state.json")
    directory.mkdir(parents=True)
    with StaleDaemon(directory / "eawfd.sock", serve=payload.daemon == "stale") as socket_path:
        yield socket_path


def _cells(context_names: tuple[str, ...]) -> list[tuple[str, MatrixEvent, Payload]]:
    return [
        (context, event, payload)
        for context in context_names
        for event in matrix_events()
        for payload in payloads_for(event, managed=context in MANAGED_CONTEXTS)
    ]


def run_matrix(bundle: Path) -> tuple[int, list[CellFailure]]:
    """Run the bundle's wrapper across every context, event and payload.

    Args:
        bundle: The bundle directory, holding ``hooks/hooks.json``.

    Returns:
        How many cells ran and the criteria they missed, in matrix order. An
        event the manifest does not subscribe runs no cell; the bundle
        findings name it.
    """
    manifest = _load_manifest(bundle)
    if isinstance(manifest, str):
        return 0, []
    subscriptions = _subscriptions(manifest)
    base = Path(tempfile.mkdtemp(prefix="eawf-doctor-hooks-"))
    try:
        contexts = _lay_contexts(base)
        cells = [
            (cell, subscriptions[cell[1].cc_event][0].command)
            for cell in _cells(CONTEXTS)
            if subscriptions.get(cell[1].cc_event)
        ]
        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            judged = list(
                pool.map(lambda item: _run_cell(bundle, item[1], contexts, item[0]), cells)
            )
        return len(cells), [failure for failures in judged for failure in failures]
    finally:
        _settle_background_starts(base)
        shutil.rmtree(base, ignore_errors=True)


def _settle_background_starts(base: Path) -> None:
    """Stop every daemon a managed session start launched in the background.

    Such a daemon comes up after its cell has been judged, so the cell's own
    cleanup can miss it; for :data:`_START_GRACE_S` every runtime directory is
    swept again, leaving no daemon behind once the directories are removed.
    """
    deadline = time.monotonic() + _START_GRACE_S
    while time.monotonic() < deadline:
        for runtime in base.glob("xdg-*"):
            _stop_daemons(runtime)
        time.sleep(_SWEEP_INTERVAL_S)


def doctor_bundle(root: Path, *, project: Path | None, home: Path) -> BundleReport:
    """Check one bundle and run its matrix.

    Args:
        root: The bundle directory.
        project: The repository the doctor runs in; ``None`` outside one.
        home: The home directory.

    Returns:
        The bundle's report.
    """
    root = root.resolve()
    findings = bundle_findings(root, project=project, home=home, env=os.environ)
    cells, failures = run_matrix(root)
    return BundleReport(
        root=root,
        version=_bundle_version(root),
        findings=tuple(findings),
        cells=cells,
        failures=tuple(failures),
    )


@dataclass(frozen=True, slots=True)
class HooksReport:
    """What ``eawf doctor hooks`` found across the bundles it checked.

    Attributes:
        bundles: One report per bundle.
        findings: Defects of the install as a whole, such as two installed
            bundles subscribing every event twice.
    """

    bundles: tuple[BundleReport, ...]
    findings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Whether nothing failed anywhere."""
        return not self.findings and all(bundle.ok for bundle in self.bundles)

    def lines(self) -> list[str]:
        """Return the text report: one heading per bundle, then one line per failure."""
        lines = list(self.findings)
        if not self.bundles:
            lines.append("no eawf plugin bundle installed; nothing to check")
        for bundle in self.bundles:
            failing = len(bundle.findings) + len(bundle.failures)
            verdict = "ok" if bundle.ok else f"{failing} failing"
            lines.append(
                f"bundle {_display(bundle.root)} (eawf {bundle.version}): "
                f"{bundle.cells} cells, {verdict}"
            )
            lines += [f"  {finding}" for finding in bundle.findings]
            lines += [f"  {failure.line()}" for failure in bundle.failures]
        return lines

    def payload(self) -> dict[str, object]:
        """Return the report as the JSON body ``--json`` prints."""
        return {
            "ok": self.ok,
            "findings": list(self.findings),
            "bundles": [
                {
                    "root": _display(bundle.root),
                    "version": bundle.version,
                    "cells": bundle.cells,
                    "findings": list(bundle.findings),
                    "failures": [failure.line() for failure in bundle.failures],
                }
                for bundle in self.bundles
            ],
        }


def _display(path: Path) -> str:
    """Return *path* with the home directory written as ``~``."""
    try:
        return f"~/{path.relative_to(Path.home()).as_posix()}"
    except ValueError:
        return str(path)


def doctor_hooks(bundles: list[Path] | None, *, cwd: Path, home: Path) -> HooksReport:
    """Check the named bundles, or every installed one.

    Args:
        bundles: The bundle directories to check; ``None`` checks every
            bundle the Claude Code registry under *home* records.
        cwd: Where the doctor runs; the git working tree holding it, if any,
            is the project whose ``.claude/skills`` could shadow the plugin's.
        home: The home directory.

    Returns:
        The report. Two installed bundles are a finding of their own: the
        host runs every hook once per bundle.
    """
    from eawf.runtime.hooks.context import resolve_hook_context

    context = resolve_hook_context(cwd)
    project = None if context.kind == "no-git" else context.top
    roots = installed_bundles(home) if bundles is None else bundles
    findings: list[str] = []
    if bundles is None and len(roots) > 1:
        findings.append(f"subscription: expected 1 installed eawf plugin got {len(roots)}")
    reports = tuple(doctor_bundle(root, project=project, home=home) for root in roots)
    return HooksReport(bundles=reports, findings=tuple(findings))


def check_hook_health(*, workspace: Path | None) -> CheckResult:
    """Report the failures the hook health record of *workspace* holds.

    Args:
        workspace: The workspace anchor; ``None`` when no anchor exists.

    Returns:
        ``warn`` naming how many failures the record holds, the most frequent
        code and ``eawf doctor hooks``; ``ok`` when it holds none.
    """
    if workspace is None:
        return CheckResult(name=HOOK_HEALTH_CHECK, status="ok", detail="skipped: no workspace")
    try:
        rows = read_health_rows(workspace / ".ea").rows
    except OSError as exc:
        return CheckResult(
            name=HOOK_HEALTH_CHECK,
            status="warn",
            detail=f".ea/{HEALTH_RECORD.as_posix()} unreadable ({type(exc).__name__})",
        )
    if not rows:
        return CheckResult(name=HOOK_HEALTH_CHECK, status="ok", detail="no hook failures recorded")
    codes: dict[str, int] = {}
    for row in rows:
        codes[row.code] = codes.get(row.code, 0) + 1
    code, count = max(codes.items(), key=lambda item: item[1])
    return CheckResult(
        name=HOOK_HEALTH_CHECK,
        status="warn",
        detail=f"{len(rows)} hook failures recorded (top: {code} x{count}); run eawf doctor hooks",
    )


__all__ = [
    "HOOK_HEALTH_CHECK",
    "BundleReport",
    "CellFailure",
    "HooksReport",
    "bundle_findings",
    "check_hook_health",
    "doctor_bundle",
    "doctor_hooks",
    "installed_bundles",
    "launcher_version",
    "run_matrix",
]
