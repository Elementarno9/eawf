"""Install and diagnose the Claude Code ``statusLine`` entry that runs ``eawf cc statusline``.

No plugin can supply a status line (Claude Code keeps only ``agent`` and
``subagentStatusLine`` from plugin settings), and a project file holds it for
one checkout only, so the line lives once in the user-level
``~/.claude/settings.json``. The installer sets only the ``statusLine`` key,
keeps every other key in its order and the file's mode, and swaps the file in
by renaming a temporary file, so a reader never sees half a file. It never
replaces an entry it finds unless asked to.

The diagnosis reads the four settings layers in Claude Code's precedence
(managed, local, project, user) and says which entry wins, whether it is
eawf's, and whether the command resolves and renders.

Public surface:

- :data:`STATUSLINE_ENTRY` -- the entry the installer writes.
- :func:`global_settings_path`, :func:`settings_path` -- target files.
- :func:`read_settings` -- parse a settings file.
- :func:`statusline_line` -- the line an operator adds by hand.
- :func:`names_eawf`, :func:`is_eawf_project_leftover` -- entry predicates.
- :func:`install_statusline` -- write the entry; :class:`SettingsNotWritable`.
- :func:`diagnose_statusline` -- the ``eawf statusline doctor`` report.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.harness.host_keys import require_recorded

logger = logging.getLogger(__name__)

STATUSLINE_KEY: Final = "statusLine"
"""The Claude Code settings key that names the status line command."""

STATUSLINE_COMMAND: Final = "eawf cc statusline"
"""The command Claude Code runs on each redraw."""

STATUSLINE_ENTRY: Final[dict[str, Any]] = {
    "type": "command",
    "command": STATUSLINE_COMMAND,
    "refreshInterval": 30,
}
"""The entry the installer writes; the refresh redraws an idle session's limit rows."""

_LEGACY_PROJECT_ENTRY: Final[dict[str, Any]] = {"type": "command", "command": STATUSLINE_COMMAND}
"""The entry earlier project installs wrote, which shadows the user's line."""

_DISABLE_ALL_HOOKS_KEY: Final = "disableAllHooks"
"""Claude Code turns the custom status line off with the hooks."""

_NEW_FILE_MODE: Final = 0o644
"""Mode of a settings file the installer creates."""

RENDER_BUDGET_MS: Final = 300
"""Cold render budget: Claude Code debounces status line updates at 300 ms."""

_PROBE_TIMEOUT_S: Final = 10.0
"""Upper bound of one doctor probe, so a hung command cannot hang doctor."""

type StatuslineScope = Literal["user", "project", "local"]
type StatuslineLayer = Literal["managed", "local", "project", "user"]
type StatuslineVerdict = Literal["ok", "missing", "shadowed", "broken"]
type InstallStatus = Literal["installed", "already", "differs"]


def global_settings_path() -> Path:
    """Return the user-level Claude settings file, ``~/.claude/settings.json``."""
    return Path.home() / ".claude" / "settings.json"


def _managed_settings_path() -> Path:
    """Return the platform's managed Claude settings file, which outranks every other layer."""
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    if sys.platform == "win32":
        return Path("C:/Program Files/ClaudeCode/managed-settings.json")
    return Path("/etc/claude-code/managed-settings.json")


def project_root(cwd: Path) -> Path:
    """Return the nearest ancestor of *cwd* holding ``.git`` (a file in a worktree), else *cwd*."""
    for directory in (cwd, *cwd.parents):
        if (directory / ".git").exists():
            return directory
    return cwd


def settings_path(scope: StatuslineScope, *, cwd: Path) -> Path:
    """Return the settings file *scope* names.

    Args:
        scope: ``user`` (``~/.claude/settings.json``), ``project``
            (``<repo>/.claude/settings.json``) or ``local``
            (``<repo>/.claude/settings.local.json``).
        cwd: The directory the repository root is searched from.

    Returns:
        The settings file path; it may not exist yet.
    """
    if scope == "user":
        return global_settings_path()
    name = "settings.local.json" if scope == "local" else "settings.json"
    return project_root(cwd) / ".claude" / name


def read_settings(path: Path) -> dict[str, Any]:
    """Read *path* as a JSON settings object, keeping its key order.

    Args:
        path: Settings file; a missing or blank file reads as ``{}``.

    Returns:
        The parsed settings mapping.

    Raises:
        ValueError: The content is not JSON, or not a JSON object.
        OSError: The file exists but cannot be read.
    """
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"settings is not valid json: {path!r}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"settings must be a json object: {path!r}")
    return dict(parsed)


def statusline_line() -> str:
    """Return the ``"statusLine": {...}`` line an operator adds to a settings file by hand."""
    return f"{json.dumps(STATUSLINE_KEY)}: {json.dumps(STATUSLINE_ENTRY)}"


def names_eawf(entry: object) -> bool:
    """Return ``True`` when *entry* is a command entry running ``eawf cc statusline``.

    The executable may be spelled as an absolute path; only its name counts.
    """
    if not isinstance(entry, dict) or entry.get("type") != "command":
        return False
    command = entry.get("command")
    if not isinstance(command, str):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return len(argv) == 3 and Path(argv[0]).name == "eawf" and argv[1:] == ["cc", "statusline"]


def is_eawf_project_leftover(entry: object) -> bool:
    """Return ``True`` when *entry* equals an entry an eawf install wrote, and nothing more."""
    return entry in (_LEGACY_PROJECT_ENTRY, STATUSLINE_ENTRY)


class SettingsNotWritable(Exception):  # noqa: N818 - names the condition the CLI reports
    """The settings file cannot be read, parsed or replaced; it is left as it was.

    Attributes:
        path: The settings file.
        reason: Why it was refused, for the operator.
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


@dataclass(frozen=True, slots=True)
class InstallResult:
    """What :func:`install_statusline` found and did.

    Attributes:
        status: ``installed`` (written), ``already`` (the same entry was
            present, nothing written) or ``differs`` (another entry was
            present and kept).
        path: The settings file.
        current: The entry found before the call; ``None`` when absent.
    """

    status: InstallStatus
    path: Path
    current: object


def _refuse_read_only(path: Path) -> None:
    """Raise :class:`SettingsNotWritable` when *path* exists and is not writable.

    The rename that replaces the file needs only the directory to be
    writable, so a read-only file would be replaced silently without this.
    """
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return
    if not mode & stat.S_IWUSR or not os.access(path, os.W_OK):
        raise SettingsNotWritable(path, f"file is read-only (mode {stat.S_IMODE(mode):o})")


def _replace_file(path: Path, text: str) -> None:
    """Write *text* to a temporary file beside *path*, keep *path*'s mode, rename it into place."""
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else _NEW_FILE_MODE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


_DEFAULT_INDENT: Final = 2


def _indent_of(path: Path) -> int | str:
    """Return the indentation *path* is written with: its first indented line's.

    A run of spaces is returned as its width and any other run (tabs) as the
    run itself, the two forms :func:`json.dumps` takes. A missing file, or one
    with no indented line, gets :data:`_DEFAULT_INDENT`.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return _DEFAULT_INDENT
    for line in lines[1:]:
        body = line.lstrip(" \t")
        run = line[: len(line) - len(body)]
        if run and body:
            return len(run) if set(run) == {" "} else run
    return _DEFAULT_INDENT


def install_statusline(path: Path, *, replace: bool = False) -> InstallResult:
    """Set the ``statusLine`` key of *path* to :data:`STATUSLINE_ENTRY`.

    Every other key keeps its value and its order; an existing key is set in
    place, a new one goes last. A symlinked file is written through the link.

    Args:
        path: The settings file; created with its parent when missing.
        replace: Replace another ``statusLine`` entry instead of keeping it.

    Returns:
        The :class:`InstallResult`; ``differs`` writes nothing.

    Raises:
        SettingsNotWritable: The file cannot be read, is not a JSON object,
            is read-only, or cannot be replaced; it is left unchanged.
        UnrecordedHostKeyError: ``statusLine`` has no observed host effect.
    """
    target = path.resolve() if path.is_symlink() else path
    try:
        settings = read_settings(target)
    except ValueError as exc:
        raise SettingsNotWritable(path, "not a valid JSON object") from exc
    except OSError as exc:
        raise SettingsNotWritable(path, exc.strerror or str(exc)) from exc
    current = settings.get(STATUSLINE_KEY)
    if current == STATUSLINE_ENTRY:
        return InstallResult(status="already", path=path, current=current)
    if STATUSLINE_KEY in settings and not replace:
        return InstallResult(status="differs", path=path, current=current)
    require_recorded("claude_settings", (STATUSLINE_KEY,))
    _refuse_read_only(target)
    settings[STATUSLINE_KEY] = dict(STATUSLINE_ENTRY)
    text = json.dumps(settings, indent=_indent_of(target), ensure_ascii=False) + "\n"
    try:
        _replace_file(target, text)
    except OSError as exc:
        raise SettingsNotWritable(path, exc.strerror or str(exc)) from exc
    logger.info(f"install_statusline wrote-statusline path={path!r}")
    return InstallResult(status="installed", path=path, current=current)


@dataclass(frozen=True, slots=True)
class StatuslineDiagnosis:
    """The ``eawf statusline doctor`` report.

    Attributes:
        layer: The first layer (managed, local, project, user) whose entry
            runs eawf's status line; ``none`` when no layer does.
        command: The command of the entry Claude Code uses, eawf's or not.
        shadowed_by: The layer whose entry or ``disableAllHooks`` hides
            eawf's line, as ``<layer>`` or ``<layer>:disableAllHooks``.
        resolves: Where the entry's executable resolves on ``PATH``.
        version: The resolved executable's ``--version`` output.
        render_ms: Wall time of one render of a minimal payload.
        budget_ms: The render budget, :data:`RENDER_BUDGET_MS`.
        refresh_interval: The entry's ``refreshInterval`` in seconds.
        disable_all_hooks: The winning ``disableAllHooks`` value.
        verdict: ``ok``, ``missing``, ``shadowed`` or ``broken``.
        fix: What to do about a verdict other than ``ok``.
    """

    layer: StatuslineLayer | Literal["none"]
    command: str | None
    shadowed_by: str | None
    resolves: str | None
    version: str | None
    render_ms: int | None
    budget_ms: int
    refresh_interval: int | None
    disable_all_hooks: bool
    verdict: StatuslineVerdict
    fix: str | None


@dataclass(frozen=True, slots=True)
class _RenderProbe:
    """One probe of the resolved executable: its version and one render."""

    version: str | None
    render_ms: int | None
    error: str | None


def _first_line(text: str) -> str:
    """Return the first non-blank line of *text*, stripped."""
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _probe_render(executable: str, argv: list[str], cwd: Path) -> _RenderProbe:
    """Run ``<executable> --version`` and one render of a payload with no session id.

    With no session id the render writes no cache entry, so the probe
    leaves nothing behind.
    """
    payload = json.dumps(
        {"model": {"display_name": "doctor"}, "workspace": {"current_dir": str(cwd)}}
    )
    try:
        shown = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            check=False,
            **no_window_kwargs(),
        )
        started = time.perf_counter()
        rendered = subprocess.run(
            [executable, *argv[1:]],
            input=payload,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            cwd=cwd,
            check=False,
            **no_window_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return _RenderProbe(None, None, f"timed out after {_PROBE_TIMEOUT_S:.0f} s")
    except OSError as exc:
        return _RenderProbe(None, None, exc.strerror or str(exc))
    render_ms = round((time.perf_counter() - started) * 1000)
    version = _first_line(shown.stdout) or None
    if rendered.returncode != 0:
        return _RenderProbe(
            version, render_ms, f"exit {rendered.returncode}: {_first_line(rendered.stderr)}"
        )
    if rendered.stderr.strip():
        return _RenderProbe(version, render_ms, f"stderr: {_first_line(rendered.stderr)}")
    return _RenderProbe(version, render_ms, None)


def _layer_files(*, cwd: Path) -> list[tuple[StatuslineLayer, Path]]:
    """Return the settings files in Claude Code's precedence, highest first."""
    root = project_root(cwd)
    return [
        ("managed", _managed_settings_path()),
        ("local", root / ".claude" / "settings.local.json"),
        ("project", root / ".claude" / "settings.json"),
        ("user", global_settings_path()),
    ]


def _readable_settings(path: Path) -> dict[str, Any]:
    """Return *path*'s settings, or ``{}`` when unreadable: such a layer counts as unset."""
    try:
        return read_settings(path)
    except ValueError, OSError:
        return {}


def _missing_fix() -> str:
    """Return the fix for a missing entry, naming the hand edit when the user file is read-only."""
    path = global_settings_path()
    try:
        _refuse_read_only(path)
    except SettingsNotWritable as exc:
        return f"add {statusline_line()} to {path} ({exc.reason})"
    return "run eawf statusline install"


def diagnose_statusline(*, cwd: Path, render: bool) -> StatuslineDiagnosis:
    """Report which ``statusLine`` entry Claude Code uses from *cwd* and whether it works.

    Verdicts: ``missing`` when no layer sets an entry; ``shadowed`` when the
    winning entry is not eawf's or ``disableAllHooks`` is on; ``broken`` when
    eawf's entry wins but its executable is not on ``PATH`` or, with
    *render*, a render exits non-zero or writes to stderr; else ``ok``.

    Args:
        cwd: The directory Claude Code runs in; its repository root holds
            the project and local layers.
        render: Run the executable once for the version and a timed render;
            ``False`` keeps the report to the files and ``PATH``.

    Returns:
        The :class:`StatuslineDiagnosis`.
    """
    layers = [(name, path, _readable_settings(path)) for name, path in _layer_files(cwd=cwd)]
    entries = [(name, path, s[STATUSLINE_KEY]) for name, path, s in layers if STATUSLINE_KEY in s]
    disabling = next(
        (
            (name, path, s[_DISABLE_ALL_HOOKS_KEY])
            for name, path, s in layers
            if _DISABLE_ALL_HOOKS_KEY in s
        ),
        None,
    )
    disable_all_hooks = disabling is not None and disabling[2] is True
    eawf_layer: StatuslineLayer | Literal["none"] = next(
        (name for name, _, entry in entries if names_eawf(entry)), "none"
    )
    winner: object = entries[0][2] if entries else None
    command = winner.get("command") if isinstance(winner, dict) else None
    refresh = winner.get("refreshInterval") if isinstance(winner, dict) else None
    verdict: StatuslineVerdict = "ok"
    shadowed_by: str | None = None
    fix: str | None = None
    resolves: str | None = None
    probe = _RenderProbe(None, None, None)
    if not entries:
        verdict, fix = "missing", _missing_fix()
    elif not names_eawf(winner):
        win_layer, win_path = entries[0][0], entries[0][1]
        verdict, shadowed_by = "shadowed", win_layer
        fix = (
            "run eawf statusline install --yes to replace it"
            if win_layer == "user"
            else f"remove statusLine from {win_path}"
        )
    elif disable_all_hooks and disabling is not None:
        verdict = "shadowed"
        shadowed_by = f"{disabling[0]}:{_DISABLE_ALL_HOOKS_KEY}"
        fix = f"remove {_DISABLE_ALL_HOOKS_KEY} from {disabling[1]}"
    else:
        verdict, fix, resolves, probe = _judge_eawf_command(str(command), cwd=cwd, render=render)
    return StatuslineDiagnosis(
        layer=eawf_layer,
        command=command if isinstance(command, str) else None,
        shadowed_by=shadowed_by,
        resolves=resolves,
        version=probe.version,
        render_ms=probe.render_ms,
        budget_ms=RENDER_BUDGET_MS,
        refresh_interval=refresh if isinstance(refresh, int) else None,
        disable_all_hooks=disable_all_hooks,
        verdict=verdict,
        fix=fix,
    )


def _judge_eawf_command(
    command: str, *, cwd: Path, render: bool
) -> tuple[StatuslineVerdict, str | None, str | None, _RenderProbe]:
    """Return verdict, fix, resolved executable and probe for eawf's winning *command*."""
    argv = shlex.split(command)
    resolves = shutil.which(argv[0])
    if resolves is None:
        if Path(argv[0]).is_absolute():
            fix = (
                f"{argv[0]} does not exist or is not executable; reinstall eawf "
                "or run eawf statusline install --yes to name the one on PATH"
            )
        else:
            fix = f"{argv[0]} is not on PATH; install eawf or name it by absolute path"
        return "broken", fix, None, _RenderProbe(None, None, None)
    if not render:
        return "ok", None, resolves, _RenderProbe(None, None, None)
    probe = _probe_render(resolves, argv, cwd)
    if probe.error is not None:
        return "broken", f"{command} failed: {probe.error}", resolves, probe
    return "ok", None, resolves, probe


__all__ = [
    "RENDER_BUDGET_MS",
    "STATUSLINE_COMMAND",
    "STATUSLINE_ENTRY",
    "STATUSLINE_KEY",
    "InstallResult",
    "SettingsNotWritable",
    "StatuslineDiagnosis",
    "diagnose_statusline",
    "global_settings_path",
    "install_statusline",
    "is_eawf_project_leftover",
    "names_eawf",
    "project_root",
    "read_settings",
    "settings_path",
    "statusline_line",
]
