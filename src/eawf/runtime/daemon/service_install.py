"""Cross-OS service install/uninstall/status for the eawfd daemon.

Wraps the three native service surfaces:

- **Linux**: systemd user unit at ``~/.config/systemd/user/eawfd.service``,
  driven by ``systemctl --user enable --now`` / ``disable --now``.
- **macOS**: launchd LaunchAgent plist at
  ``~/Library/LaunchAgents/dev.eawf.eawfd.plist``, driven by
  ``launchctl bootstrap`` / ``enable`` / ``kickstart`` / ``bootout``.
- **Windows**: pywin32 ``win32serviceutil`` framework around the
  :class:`eawf.runtime.daemon.win_service.EawfdService` subclass.

The verbs are operator-facing and idempotent on the disable path:
disabling a never-installed service must succeed without raising.
Each enable call waits up to ten seconds for the daemon to answer
``daemon.ping`` so the operator gets a synchronous "service is up"
signal.

The NSSM fallback is *documented* but not shipped; the install verb
relies on pywin32 being importable. Operators can manually layer NSSM
atop the same service binary if pywin32 fails post-install.
"""

from __future__ import annotations

import importlib
import logging
import os
import plistlib
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from eawf.runtime.daemon.runtime_dir import pid_path, runtime_dir
from eawf.runtime.daemon.spawn import DaemonSpawnTimeoutError, wait_for_daemon_ready

logger = logging.getLogger(__name__)

if sys.platform != "win32":  # pragma: no cover - platform import guard
    import pwd
else:  # pragma: no cover - win32-only branch
    pwd: Any = None


# Service-template directories, in resolution order. ``__file__``
# resolves to ``.../eawf/runtime/daemon/service_install.py``, so
# ``parents[2]`` is the installed ``eawf`` package root and ``parents[4]``
# is the repo root in an editable checkout.
#
# - Editable / source checkout: use the version-controlled repo-root
#   ``templates/`` directory even when a prior local build left a
#   generated ``_data/`` tree behind.
# - Wheel / PyPI install: ``tools/bundle_data.py`` copies the templates
#   into ``eawf/_data/service_templates/`` at build time; that copy is
#   the only one present when there is no repo checkout.
_BUNDLED_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "_data" / "service_templates"
_REPO_TEMPLATE_DIR = Path(__file__).resolve().parents[4] / "templates"


def _template_dir() -> Path:
    """Resolve the service-template directory for the active install.

    Returns:
        The first existing of the bundled wheel copy
        (:data:`_BUNDLED_TEMPLATE_DIR`) and the repo-root source dir
        (:data:`_REPO_TEMPLATE_DIR`).

    Raises:
        ServiceInstallError: When neither candidate directory exists.
    """
    for candidate in (_REPO_TEMPLATE_DIR, _BUNDLED_TEMPLATE_DIR):
        if candidate.is_dir():
            return candidate
    raise ServiceInstallError(
        f"template dir missing: tried {_BUNDLED_TEMPLATE_DIR} and {_REPO_TEMPLATE_DIR}"
    )


_SYSTEMD_TEMPLATE = "eawfd.service.j2"
_LAUNCHD_TEMPLATE = "dev.eawf.eawfd.plist.j2"

_SYSTEMD_UNIT_NAME = "eawfd.service"
_LAUNCHD_LABEL = "dev.eawf.eawfd"
_WINDOWS_SERVICE_NAME = "eawfd"

# Per-OS readiness wait window (seconds).
_PID_WAIT_TIMEOUT_SECONDS = 10.0
_PID_WAIT_POLL_INTERVAL_SECONDS = 0.25


class ServiceStatus(StrEnum):
    """Operational state of the eawfd service on the host.

    Members:
        RUNNING: Service is registered and actively running (PID-file
            present + the supervisor reports active).
        ENABLED: Service is registered but not currently running
            (start-on-boot configured; supervisor reports inactive).
        DISABLED: Service is registered but disabled at the supervisor
            level (will not start at boot; no running process).
        NOT_INSTALLED: Service is not registered with the supervisor.
        UNSUPPORTED: The host OS has no service-install recipe (BSD,
            other niche platforms).
    """

    RUNNING = "running"
    ENABLED = "enabled"
    DISABLED = "disabled"
    NOT_INSTALLED = "not-installed"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ServiceEnvelope:
    """Structured result emitted by service install / disable verbs.

    Attributes:
        event_type: Short identifier (e.g. ``daemon_service_enabled``)
            suitable for log routing and future event-bus emission.
        platform: ``linux`` / ``darwin`` / ``win32``.
        unit: Per-OS unit identifier (systemd unit name, launchd
            label, or Windows service short name).
        pid: Daemon PID when the post-install PID-file wait succeeded;
            ``None`` for disable verbs or when the wait timed out
            (in which case the caller raised before constructing the
            envelope).
    """

    event_type: str
    platform: str
    unit: str
    pid: int | None = None


class ServiceInstallError(RuntimeError):
    """An install / disable / status step failed unrecoverably.

    The daemon translates this into a non-zero CLI exit code; tests
    assert on the message substring when the failure path is part of
    the public contract.
    """


def _resolve_eawfd_binary() -> list[str]:
    """Resolve the argv to invoke the eawfd daemon entry point.

    Prefers the installed ``eawfd`` console script (``[project.scripts]``
    entry) so the OS supervisor shows ``eawfd`` as the process name in
    macOS App Background Activity / Linux ``systemctl`` / Windows
    Services. Falls back to ``<python> -m eawf.runtime.daemon.main`` when the
    console script is not on ``PATH`` (uncommon, but possible in
    bare-checkout dev shells).

    Returns:
        Argv list ready to be embedded in a service template's
        ``ProgramArguments`` / ``ExecStart``. Always at least one
        element; the first element is the program name the supervisor
        will display.
    """
    binary = shutil.which("eawfd")
    if binary is not None:
        return [binary]
    return [sys.executable, "-m", "eawf.runtime.daemon.main"]


def _render_template(name: str, *, runtime_dir_value: Path) -> str:
    """Render *name* with template variables substituted.

    Variables exposed to the template:

    - ``runtime_dir``: Resolved runtime directory path.
    - ``eawfd_argv``: Argv list for the daemon entry point; the first
      element is the program name the supervisor displays.
    - ``eawfd_program``: First element of ``eawfd_argv`` for plist
      ``Program`` keys that take a single string.

    Args:
        name: Template filename under the resolved template directory
            (see :func:`_template_dir`).
        runtime_dir_value: Resolved runtime directory path.

    Returns:
        Rendered template body as text.

    Raises:
        ServiceInstallError: When the template directory is missing or
            the template fails to render (the StrictUndefined catches
            typo'd variables).
    """
    env = Environment(
        loader=FileSystemLoader(str(_template_dir())),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,  # systemd / launchd templates are not HTML
    )
    template = env.get_template(name)
    eawfd_argv = _resolve_eawfd_binary()
    return template.render(
        runtime_dir=str(runtime_dir_value),
        eawfd_argv=eawfd_argv,
        eawfd_program=eawfd_argv[0],
    )


def _systemd_unit_path() -> Path:
    """Return the install path for the systemd user unit."""
    base = Path.home() / ".config" / "systemd" / "user"
    return base / _SYSTEMD_UNIT_NAME


def _invoking_uid() -> int:
    """Return the uid of the user who invoked the install.

    Resolves to ``SUDO_UID`` when present (the verb was run via
    ``sudo`` so ``os.getuid()`` would report root's 0) and falls back
    to the live ``os.getuid()`` otherwise. A user LaunchAgent under
    ``~/Library/LaunchAgents`` must bootstrap into the invoking user's
    ``gui/<uid>`` domain, never root's, so a resolved uid of 0 is a
    hard error.

    Returns:
        The invoking user's numeric uid.

    Raises:
        ServiceInstallError: When the resolved uid is 0 (running as
            root with no ``SUDO_UID`` to recover the real user).
    """
    uid = int(os.environ.get("SUDO_UID") or os.getuid())
    if uid == 0:
        raise ServiceInstallError(
            "refusing to install user LaunchAgent as root; run as your normal user"
        )
    return uid


def _launchd_plist_path() -> Path:
    """Return the install path for the launchd LaunchAgent plist.

    Derives the LaunchAgents base from the invoking user's home so the
    plist location agrees with the ``gui/<uid>`` domain target even
    under ``sudo -E`` (where ``HOME`` may be the real user's but the
    euid is root's).

    Raises:
        ServiceInstallError: When the invoking uid resolves to root
            (delegated to :func:`_invoking_uid`).
    """
    uid = _invoking_uid()
    if pwd is None:  # pragma: no cover - guarded by darwin dispatch
        raise ServiceInstallError("pwd module unavailable on this platform")
    base = Path(pwd.getpwuid(uid).pw_dir) / "Library" / "LaunchAgents"
    return base / f"{_LAUNCHD_LABEL}.plist"


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run *cmd* as a subprocess, capturing stdout / stderr.

    Args:
        cmd: Argv to execute.
        check: When True, raise :class:`ServiceInstallError` on non-zero
            exit; when False, return the completed process for the
            caller to inspect.

    Returns:
        The :class:`subprocess.CompletedProcess` regardless of *check*
        (raised paths short-circuit before return).

    Raises:
        ServiceInstallError: When *check* is True and the command
            exited non-zero.
    """
    logger.info(f"_run cmd={cmd!r}")
    result = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise ServiceInstallError(
            f"command failed cmd={cmd!r} rc={result.returncode} stderr={result.stderr.strip()!r}"
        )
    return result


def _wait_for_pid_file(timeout_seconds: float = _PID_WAIT_TIMEOUT_SECONDS) -> int:
    """Block until the daemon writes its PID file or *timeout* elapses.

    Args:
        timeout_seconds: Wait window in seconds; rejected when zero
            or negative because the caller always wants a positive
            grace period after enable.

    Returns:
        The PID parsed from the freshly written file.

    Raises:
        ServiceInstallError: When the file is not written within the
            wait window or the file content cannot be parsed.
    """
    if timeout_seconds <= 0:
        raise ServiceInstallError(f"timeout must be positive, got {timeout_seconds!r}")
    pid_file = pid_path()
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if pid_file.exists():
            try:
                first_line = pid_file.read_text(encoding="utf-8").splitlines()[0]
                return int(first_line.strip())
            except (OSError, ValueError, IndexError) as exc:
                raise ServiceInstallError(
                    f"pid file unreadable path={pid_file} reason={exc!s}"
                ) from exc
        time.sleep(_PID_WAIT_POLL_INTERVAL_SECONDS)
    raise ServiceInstallError(
        f"daemon did not write pid file within {timeout_seconds:.1f}s path={pid_file}"
    )


def _wait_for_daemon_ready(timeout_seconds: float = _PID_WAIT_TIMEOUT_SECONDS) -> int:
    """Block until the daemon answers ``daemon.ping`` or *timeout* elapses.

    Args:
        timeout_seconds: Wait window in seconds; rejected when zero
            or negative because the caller always wants a positive
            grace period after enable.

    Returns:
        The PID reported by the ready daemon.

    Raises:
        ServiceInstallError: When readiness does not arrive in time.
    """
    if timeout_seconds <= 0:
        raise ServiceInstallError(f"timeout must be positive, got {timeout_seconds!r}")
    try:
        return wait_for_daemon_ready(runtime_dir(), timeout_seconds=timeout_seconds)
    except DaemonSpawnTimeoutError as exc:
        raise ServiceInstallError(
            f"daemon did not answer daemon.ping within {timeout_seconds:.1f}s"
        ) from exc


# --- Linux (systemd user) ---------------------------------------------


def _enable_systemd() -> ServiceEnvelope:
    """Render the systemd unit, reload, enable + start, await RPC readiness."""
    unit_path = _systemd_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = _render_template(_SYSTEMD_TEMPLATE, runtime_dir_value=runtime_dir())
    unit_path.write_text(rendered, encoding="utf-8")
    logger.info(f"_enable_systemd wrote unit={unit_path}")

    _run(["systemctl", "--user", "daemon-reload"])
    _run(["systemctl", "--user", "enable", "--now", _SYSTEMD_UNIT_NAME])
    pid = _wait_for_daemon_ready()
    return ServiceEnvelope(
        event_type="daemon_service_enabled",
        platform="linux",
        unit=_SYSTEMD_UNIT_NAME,
        pid=pid,
    )


def _disable_systemd() -> ServiceEnvelope:
    """Stop + disable the unit and unlink the rendered file.

    Idempotent: the ``disable --now`` call swallows non-zero exits on
    already-disabled / unloaded units, and the unlink path tolerates
    a missing file.
    """
    _run(
        ["systemctl", "--user", "disable", "--now", _SYSTEMD_UNIT_NAME],
        check=False,
    )
    unit_path = _systemd_unit_path()
    try:
        unit_path.unlink()
        logger.info(f"_disable_systemd removed unit={unit_path}")
    except FileNotFoundError:
        logger.info(f"_disable_systemd unit-absent path={unit_path}")
    _run(["systemctl", "--user", "daemon-reload"], check=False)
    return ServiceEnvelope(
        event_type="daemon_service_disabled",
        platform="linux",
        unit=_SYSTEMD_UNIT_NAME,
    )


def _status_systemd() -> ServiceStatus:
    """Map ``systemctl --user is-active``/``is-enabled`` to ServiceStatus."""
    if not _systemd_unit_path().exists():
        return ServiceStatus.NOT_INSTALLED
    active = _run(
        ["systemctl", "--user", "is-active", _SYSTEMD_UNIT_NAME],
        check=False,
    )
    enabled = _run(
        ["systemctl", "--user", "is-enabled", _SYSTEMD_UNIT_NAME],
        check=False,
    )
    if active.stdout.strip() == "active":
        return ServiceStatus.RUNNING
    if enabled.stdout.strip() == "enabled":
        return ServiceStatus.ENABLED
    return ServiceStatus.DISABLED


# --- macOS (launchd) --------------------------------------------------


def _launchd_uid_target() -> str:
    """Return the ``gui/<uid>`` domain target for the invoking user.

    Raises:
        ServiceInstallError: When the invoking uid resolves to root
            (delegated to :func:`_invoking_uid`).
    """
    return f"gui/{_invoking_uid()}"


def _enable_launchd() -> ServiceEnvelope:
    """Render the plist, bootstrap + enable + kickstart, await RPC readiness."""
    plist_path = _launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = _render_template(_LAUNCHD_TEMPLATE, runtime_dir_value=runtime_dir())
    plist_path.write_text(rendered, encoding="utf-8")
    logger.info(f"_enable_launchd wrote plist={plist_path}")

    uid_target = _launchd_uid_target()
    # Tear down any already-loaded instance first: re-running bootstrap
    # against a loaded agent fails rc=5 (EIO, "already bootstrapped"),
    # and the bootout also picks up plist changes (e.g. updated
    # ProgramArguments) on upgrade. The non-zero "not loaded" exit on a
    # fresh install is expected and swallowed.
    _run(["launchctl", "bootout", f"{uid_target}/{_LAUNCHD_LABEL}"], check=False)
    _run(["launchctl", "bootstrap", uid_target, str(plist_path)])
    _run(["launchctl", "enable", f"{uid_target}/{_LAUNCHD_LABEL}"])
    _run(["launchctl", "kickstart", f"{uid_target}/{_LAUNCHD_LABEL}"])
    pid = _wait_for_daemon_ready()
    return ServiceEnvelope(
        event_type="daemon_service_enabled",
        platform="darwin",
        unit=_LAUNCHD_LABEL,
        pid=pid,
    )


def _disable_launchd() -> ServiceEnvelope:
    """Tear down the LaunchAgent and unlink the plist.

    Idempotent: ``launchctl bootout`` exits non-zero when the agent
    is not loaded; we swallow that path.
    """
    uid_target = _launchd_uid_target()
    _run(
        ["launchctl", "bootout", f"{uid_target}/{_LAUNCHD_LABEL}"],
        check=False,
    )
    plist_path = _launchd_plist_path()
    try:
        plist_path.unlink()
        logger.info(f"_disable_launchd removed plist={plist_path}")
    except FileNotFoundError:
        logger.info(f"_disable_launchd plist-absent path={plist_path}")
    return ServiceEnvelope(
        event_type="daemon_service_disabled",
        platform="darwin",
        unit=_LAUNCHD_LABEL,
    )


def _status_launchd() -> ServiceStatus:
    """Map ``launchctl print`` exit code to a :class:`ServiceStatus`."""
    if not _launchd_plist_path().exists():
        return ServiceStatus.NOT_INSTALLED
    target = f"{_launchd_uid_target()}/{_LAUNCHD_LABEL}"
    result = _run(["launchctl", "print", target], check=False)
    if result.returncode != 0:
        return ServiceStatus.DISABLED
    # ``launchctl print`` emits a multi-line description; ``state =
    # running`` indicates an active PID. Anything else (waiting,
    # stopped) maps to ENABLED.
    body = result.stdout.lower()
    if "state = running" in body or "pid = " in body:
        return ServiceStatus.RUNNING
    return ServiceStatus.ENABLED


# --- Windows (pywin32) ------------------------------------------------


def _enable_windows() -> ServiceEnvelope:
    """Install + start the pywin32 service and await RPC readiness.

    Uses :func:`win32serviceutil.InstallService` directly rather than
    shelling out to ``python -m eawf.runtime.daemon.win_service install`` so
    the subprocess hop is avoided and the SCM install error path
    surfaces as a Python exception we can wrap.

    Raises:
        ServiceInstallError: When pywin32 is not importable, the SCM
            install call fails, or the daemon does not answer
            ``daemon.ping`` within the wait window.
    """
    try:  # pragma: no cover - win32-only branch
        win32serviceutil: Any = importlib.import_module("win32serviceutil")
    except ImportError as exc:  # pragma: no cover - win32-only branch
        raise ServiceInstallError(
            "pywin32 is required to install the eawfd Windows service; "
            "install with `pip install eawf[windows]` or use NSSM fallback"
        ) from exc

    service_module = "eawf.runtime.daemon.win_service.EawfdService"
    try:  # pragma: no cover - win32-only branch
        win32serviceutil.InstallService(
            pythonClassString=service_module,
            serviceName=_WINDOWS_SERVICE_NAME,
            displayName="eawf coordinator daemon",
            startType=win32serviceutil.win32service.SERVICE_AUTO_START,
        )
        win32serviceutil.StartService(_WINDOWS_SERVICE_NAME)
    except Exception as exc:  # pragma: no cover - win32-only branch
        raise ServiceInstallError(f"windows install failed: {exc!s}") from exc

    pid = _wait_for_daemon_ready()
    return ServiceEnvelope(
        event_type="daemon_service_enabled",
        platform="win32",
        unit=_WINDOWS_SERVICE_NAME,
        pid=pid,
    )


def _disable_windows() -> ServiceEnvelope:
    """Stop + remove the pywin32 service.

    Idempotent: both ``StopService`` and ``RemoveService`` swallow the
    "service does not exist" error path so a never-installed state
    completes cleanly.
    """
    try:  # pragma: no cover - win32-only branch
        win32serviceutil: Any = importlib.import_module("win32serviceutil")
    except ImportError as exc:  # pragma: no cover - win32-only branch
        raise ServiceInstallError("pywin32 not importable") from exc

    for step in ("StopService", "RemoveService"):  # pragma: no cover - win32-only
        fn = getattr(win32serviceutil, step)
        try:
            fn(_WINDOWS_SERVICE_NAME)
        except Exception as exc:
            # SCM raises when the service is already stopped / absent;
            # we treat that as success per the idempotent contract.
            logger.info(f"_disable_windows step={step} swallowed={exc!s}")
    return ServiceEnvelope(
        event_type="daemon_service_disabled",
        platform="win32",
        unit=_WINDOWS_SERVICE_NAME,
    )


def _status_windows() -> ServiceStatus:
    """Map ``QueryServiceStatus`` state to a :class:`ServiceStatus`."""
    try:  # pragma: no cover - win32-only branch
        win32service: Any = importlib.import_module("win32service")
        win32serviceutil: Any = importlib.import_module("win32serviceutil")
    except ImportError:  # pragma: no cover - win32-only branch
        return ServiceStatus.UNSUPPORTED

    try:  # pragma: no cover - win32-only branch
        state_tuple = win32serviceutil.QueryServiceStatus(_WINDOWS_SERVICE_NAME)
    except Exception:
        return ServiceStatus.NOT_INSTALLED

    current_state = state_tuple[1]
    if current_state == win32service.SERVICE_RUNNING:
        return ServiceStatus.RUNNING
    if current_state == win32service.SERVICE_STOPPED:
        return ServiceStatus.ENABLED
    return ServiceStatus.DISABLED


# --- Public dispatch ---------------------------------------------------


def enable_service() -> ServiceEnvelope:
    """Install + start the eawfd service on the current OS.

    Returns:
        :class:`ServiceEnvelope` summarising the install (platform,
        unit name, daemon PID).

    Raises:
        ServiceInstallError: When the install fails — template
            missing, supervisor command exit non-zero, daemon readiness
            timeout, or platform unsupported.
    """
    if sys.platform.startswith("linux"):
        return _enable_systemd()
    if sys.platform == "darwin":
        return _enable_launchd()
    if sys.platform == "win32":
        return _enable_windows()
    raise ServiceInstallError(f"service install unsupported on {sys.platform!r}")


def disable_service() -> ServiceEnvelope:
    """Stop + uninstall the eawfd service on the current OS.

    Idempotent on every OS: disabling a never-installed service must
    succeed without raising.

    Returns:
        :class:`ServiceEnvelope` summarising the disable.

    Raises:
        ServiceInstallError: When the platform itself is unsupported.
    """
    if sys.platform.startswith("linux"):
        return _disable_systemd()
    if sys.platform == "darwin":
        return _disable_launchd()
    if sys.platform == "win32":
        return _disable_windows()
    raise ServiceInstallError(f"service disable unsupported on {sys.platform!r}")


def service_status() -> ServiceStatus:
    """Return the current service state on this host.

    Returns:
        :class:`ServiceStatus` enum value. ``UNSUPPORTED`` for hosts
        with no install recipe; ``NOT_INSTALLED`` for supported hosts
        without a registered unit.
    """
    if sys.platform.startswith("linux"):
        return _status_systemd()
    if sys.platform == "darwin":
        return _status_launchd()
    if sys.platform == "win32":
        return _status_windows()
    return ServiceStatus.UNSUPPORTED


# --- Supervised-agent management (launchd / systemd) ------------------
#
# A daemon under a launchd LaunchAgent (macOS, KeepAlive) or a systemd user
# unit (Linux, Restart=) auto-restarts on exit, so a plain ``daemon.shutdown``
# RPC is undone the moment it lands. The ``daemon stop --evict-service`` verb
# boots the agent out first; ``daemon run`` defers to a loaded agent instead of
# forking a rival; and the doctor surface reports the agent state. All three
# route through the injectable :data:`_service_runner` seam so the lifecycle
# suite captures + orders every supervisor call without touching the host.


#: Runner seam: a callable that runs an argv and returns the completed process.
_ServiceRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _default_service_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run *cmd* capturing output; never raise on non-zero or missing binary.

    Unlike :func:`_run`, this never raises. A non-zero exit is a signal the
    caller interprets (an unloaded agent, an inactive unit) rather than a
    failure, and a missing supervisor binary (``launchctl`` absent on Linux,
    ``systemctl`` absent in a minimal container) collapses to a synthetic
    ``rc=127`` so detection degrades to "no supervised agent" instead of
    crashing the doctor surface.

    Args:
        cmd: Argv to execute.

    Returns:
        The completed process, or a synthetic ``rc=127`` result when the
        binary is not on ``PATH``.
    """
    logger.info(f"_default_service_runner cmd={cmd!r}")
    try:
        return subprocess.run(cmd, check=False, capture_output=True, text=True)
    except FileNotFoundError:
        return subprocess.CompletedProcess(args=cmd, returncode=127, stdout="", stderr="")


#: Injectable subprocess runner for the supervised-agent management calls
#: (``launchctl print`` / ``bootout``, ``systemctl is-active`` / ``show`` /
#: ``stop``). The default shells out; the lifecycle suite swaps this for a
#: recording stub so no launchctl / systemctl call ever touches the host under
#: test. Keeping the seam at module scope lets the parameter-free Typer daemon
#: verbs still route through an injectable point.
_service_runner: _ServiceRunner = _default_service_runner


def _current_platform() -> str:
    """Return the running platform id.

    A one-line seam so the lifecycle + doctor suites can force ``darwin`` /
    ``linux`` regardless of the host they run on, without mutating the global
    :mod:`sys` module.
    """
    return sys.platform


@dataclass(frozen=True)
class SupervisedAgentReport:
    """Detection snapshot for the OS-supervised eawfd agent.

    Attributes:
        supervisor: ``launchd`` (macOS), ``systemd`` (Linux user session), or
            ``none`` (Windows / unsupported / no install recipe).
        label: launchd label or systemd unit name; empty when ``supervisor``
            is ``none``.
        installed: A plist / unit file is present on disk.
        loaded: The supervisor reports the agent loaded (launchd) or active
            (systemd) -- i.e. it will (re)start the daemon on exit.
        program: The program the unit is configured to exec (launchd
            ``ProgramArguments[0]`` / systemd ``ExecStart[0]``); ``None`` when
            the unit file is absent or unparseable.
        drift: ``installed`` and ``program`` differs from the currently
            resolved eawfd binary -- the unit points at a stale binary from a
            pre-upgrade ``service-enable`` that never re-ran.
        rival_pid: A live daemon PID recorded in the PID file that the
            supervisor did NOT report (a manually spawned rival alongside the
            supervised daemon); ``None`` when no rival is detected.
    """

    supervisor: str
    label: str
    installed: bool
    loaded: bool
    program: str | None
    drift: bool
    rival_pid: int | None


def _pid_is_alive(pid: int) -> bool:
    """Return True when *pid* refers to a live process (POSIX ``kill(0)``)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_recorded_daemon_pid() -> int | None:
    """Return the PID recorded in ``<runtime_dir>/eawfd.pid``, or ``None``."""
    try:
        head = pid_path().read_text(encoding="utf-8").splitlines()[0]
        return int(head.strip())
    except OSError, ValueError, IndexError:
        return None


def _detect_rival_pid(supervised_pid: int | None) -> int | None:
    """Return a live rival daemon PID distinct from *supervised_pid*.

    The supervised daemon writes its own PID into ``eawfd.pid``; a PID-file
    entry that differs from the supervisor-reported PID and is still alive
    means a second (manually spawned) daemon is running alongside the
    supervised one. Any parse / liveness failure yields ``None`` -- the signal
    is advisory and must never crash detection.
    """
    if supervised_pid is None:
        return None
    recorded = _read_recorded_daemon_pid()
    if recorded is None or recorded == supervised_pid:
        return None
    return recorded if _pid_is_alive(recorded) else None


def _parse_launchctl_pid(stdout: str) -> int | None:
    """Return the ``pid = N`` value from ``launchctl print`` output, or None."""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("pid ="):
            _key, _sep, value = stripped.partition("=")
            try:
                pid = int(value.strip())
            except ValueError:
                return None
            return pid if pid > 0 else None
    return None


def _plist_program(path: Path) -> str | None:
    """Return ``ProgramArguments[0]`` from the launchd plist at *path*."""
    try:
        with path.open("rb") as handle:
            data = plistlib.load(handle)
    except OSError, ValueError, ExpatError:
        return None
    argv = data.get("ProgramArguments") if isinstance(data, dict) else None
    if isinstance(argv, list) and argv:
        return str(argv[0])
    return None


def _systemd_program(path: Path) -> str | None:
    """Return the ``ExecStart=`` program token from the systemd unit at *path*."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("ExecStart="):
            _key, _sep, rhs = stripped.partition("=")
            tokens = rhs.split()
            return tokens[0] if tokens else None
    return None


def _detect_launchd(runner: _ServiceRunner) -> SupervisedAgentReport:
    """Build the launchd :class:`SupervisedAgentReport` (macOS)."""
    label = _LAUNCHD_LABEL
    try:
        target = f"{_launchd_uid_target()}/{label}"
    except ServiceInstallError:
        # Running as root with no SUDO_UID: the gui/<uid> domain is
        # unresolvable, so report the agent absent rather than raise.
        return SupervisedAgentReport(
            supervisor="launchd",
            label=label,
            installed=False,
            loaded=False,
            program=None,
            drift=False,
            rival_pid=None,
        )
    installed = False
    program: str | None = None
    try:
        plist_path = _launchd_plist_path()
        installed = plist_path.exists()
        if installed:
            program = _plist_program(plist_path)
    except ServiceInstallError, KeyError, OSError:
        installed = False
    printed = runner(["launchctl", "print", target])
    loaded = printed.returncode == 0
    supervised_pid = _parse_launchctl_pid(printed.stdout) if loaded else None
    drift = installed and program is not None and program != _resolve_eawfd_binary()[0]
    return SupervisedAgentReport(
        supervisor="launchd",
        label=label,
        installed=installed,
        loaded=loaded,
        program=program,
        drift=drift,
        rival_pid=_detect_rival_pid(supervised_pid),
    )


def _detect_systemd(runner: _ServiceRunner) -> SupervisedAgentReport:
    """Build the systemd :class:`SupervisedAgentReport` (Linux user session)."""
    unit = _SYSTEMD_UNIT_NAME
    unit_path = _systemd_unit_path()
    installed = unit_path.exists()
    program = _systemd_program(unit_path) if installed else None
    active = runner(["systemctl", "--user", "is-active", unit])
    loaded = active.stdout.strip() == "active"
    supervised_pid: int | None = None
    if loaded:
        shown = runner(["systemctl", "--user", "show", "--property=MainPID", "--value", unit])
        try:
            main_pid = int(shown.stdout.strip())
        except ValueError:
            main_pid = 0
        supervised_pid = main_pid if main_pid > 0 else None
    drift = installed and program is not None and program != _resolve_eawfd_binary()[0]
    return SupervisedAgentReport(
        supervisor="systemd",
        label=unit,
        installed=installed,
        loaded=loaded,
        program=program,
        drift=drift,
        rival_pid=_detect_rival_pid(supervised_pid),
    )


def detect_supervised_agent(
    *,
    platform: str | None = None,
    runner: _ServiceRunner | None = None,
) -> SupervisedAgentReport:
    """Detect the OS-supervised eawfd agent (launchd / systemd).

    Never raises and never mutates: it reads the plist / unit file and asks the
    supervisor whether the agent is loaded. Windows and unsupported hosts
    report ``supervisor="none"`` without shelling out.

    Args:
        platform: Platform id override; defaults to :func:`_current_platform`.
            The suites force ``darwin`` / ``linux`` regardless of host.
        runner: Injected subprocess runner; defaults to the module seam
            (:data:`_service_runner`). Tests pass a recording stub.

    Returns:
        The :class:`SupervisedAgentReport` snapshot.
    """
    plat = platform if platform is not None else _current_platform()
    run = runner if runner is not None else _service_runner
    if plat == "darwin":
        return _detect_launchd(run)
    if plat.startswith("linux"):
        return _detect_systemd(run)
    return SupervisedAgentReport(
        supervisor="none",
        label="",
        installed=False,
        loaded=False,
        program=None,
        drift=False,
        rival_pid=None,
    )


def evict_supervised_agent(
    *,
    platform: str | None = None,
    runner: _ServiceRunner | None = None,
) -> str:
    """Boot out / stop the supervised eawfd agent for the current stop window.

    Unloads the launchd agent (``launchctl bootout``) or stops the systemd unit
    (``systemctl --user stop``) so a KeepAlive / Restart directive cannot
    immediately respawn the daemon the operator just asked to stop. The plist /
    unit file is LEFT IN PLACE -- this evicts for the stop window only, unlike
    :func:`disable_service`, which uninstalls.

    Args:
        platform: Platform id override; defaults to :func:`_current_platform`.
        runner: Injected subprocess runner; defaults to the module seam.

    Returns:
        The evicted target (launchd ``gui/<uid>/<label>`` or the systemd unit
        name); empty string on Windows / unsupported hosts.

    Raises:
        ServiceInstallError: When the launchd ``gui/<uid>`` domain cannot be
            resolved (running as root with no ``SUDO_UID``).
    """
    plat = platform if platform is not None else _current_platform()
    run = runner if runner is not None else _service_runner
    if plat == "darwin":
        target = f"{_launchd_uid_target()}/{_LAUNCHD_LABEL}"
        run(["launchctl", "bootout", target])
        logger.info(f"evict_supervised_agent booted-out target={target!r}")
        return target
    if plat.startswith("linux"):
        run(["systemctl", "--user", "stop", _SYSTEMD_UNIT_NAME])
        logger.info(f"evict_supervised_agent stopped unit={_SYSTEMD_UNIT_NAME!r}")
        return _SYSTEMD_UNIT_NAME
    return ""
