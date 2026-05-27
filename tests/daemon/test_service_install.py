"""End-to-end coverage for the W04 service-install verbs.

Each install path is gated on the platform that owns it:

- ``test_enable_systemd_*`` only runs on Linux.
- ``test_enable_launchd_*`` only runs on macOS (darwin).
- ``test_enable_windows_*`` only runs on Windows (win32).
- The disable + status mocks run on any host that has the matching
  platform's recipe — we monkeypatch ``sys.platform`` for branch
  coverage where the spec calls for it, but never invoke real
  ``systemctl`` / ``launchctl`` / ``win32serviceutil`` from non-native
  runners.

The suite uses :func:`monkeypatch.setattr` on the ``subprocess.run``
symbol the install module imports so we exercise the full argument
sequencing without touching the host's actual service supervisor.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from eawf.runtime.daemon import service_install
from eawf.runtime.daemon.service_install import (
    ServiceEnvelope,
    ServiceInstallError,
    ServiceStatus,
    disable_service,
    enable_service,
    service_status,
)

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


class _StubProcess:
    """Minimal stand-in for :class:`subprocess.CompletedProcess`."""

    def __init__(
        self,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _RunRecorder:
    """Capture subprocess.run invocations + return queued stubs.

    The recorder feeds successive stub results to the test target so
    the install verb can drive multiple supervisor calls (e.g.
    ``daemon-reload`` then ``enable --now``) within one test.
    """

    def __init__(self, responses: list[_StubProcess]) -> None:
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(
        self,
        cmd: list[str],
        check: bool = False,
        capture_output: bool = False,
        text: bool = False,
    ) -> _StubProcess:
        self.calls.append(list(cmd))
        if not self._responses:
            return _StubProcess()
        return self._responses.pop(0)


def _seed_pid_file(tmp_runtime: Path, pid: int = 4242) -> Path:
    """Write a fake PID file the install wait-loop will discover."""
    pid_file = tmp_runtime / "eawfd.pid"
    pid_file.write_text(f"{pid}\n1\n2026-05-19T00:00:00+00:00\n", encoding="utf-8")
    return pid_file


@pytest.fixture
def stub_runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the daemon runtime dir into a per-test temp tree."""
    target = tmp_path / "eawfd-runtime"
    target.mkdir()
    monkeypatch.setattr(
        "eawf.runtime.daemon.service_install.runtime_dir",
        lambda: target,
    )
    monkeypatch.setattr(
        "eawf.runtime.daemon.service_install.pid_path",
        lambda: target / "eawfd.pid",
    )
    return target


@pytest.fixture
def stub_systemd_unit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the systemd unit install path into the temp tree."""
    unit_path = tmp_path / "unit" / "eawfd.service"
    monkeypatch.setattr(
        "eawf.runtime.daemon.service_install._systemd_unit_path",
        lambda: unit_path,
    )
    return unit_path


@pytest.fixture
def stub_launchd_plist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the launchd plist install path into the temp tree."""
    plist_path = tmp_path / "agents" / "dev.eawf.eawfd.plist"
    monkeypatch.setattr(
        "eawf.runtime.daemon.service_install._launchd_plist_path",
        lambda: plist_path,
    )
    return plist_path


def _install_run_recorder(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[_StubProcess] | None = None,
) -> _RunRecorder:
    """Patch :func:`subprocess.run` inside the install module."""
    recorder = _RunRecorder(responses or [])
    monkeypatch.setattr(
        "eawf.runtime.daemon.service_install.subprocess.run",
        recorder,
    )
    return recorder


# ---------------------------------------------------------------------
# Linux — enable
# ---------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only systemd path")
def test_enable_service_linux_renders_unit_and_invokes_systemctl(
    monkeypatch: pytest.MonkeyPatch,
    stub_runtime_dir: Path,
    stub_systemd_unit: Path,
) -> None:
    """``enable_service`` writes the unit + runs the expected commands."""
    recorder = _install_run_recorder(
        monkeypatch,
        responses=[_StubProcess(), _StubProcess()],
    )
    _seed_pid_file(stub_runtime_dir, pid=9876)
    monkeypatch.setattr(service_install, "_wait_for_daemon_ready", lambda: 9876)

    envelope = enable_service()

    assert envelope.event_type == "daemon_service_enabled"
    assert envelope.platform == "linux"
    assert envelope.unit == "eawfd.service"
    assert envelope.pid == 9876

    assert stub_systemd_unit.exists()
    body = stub_systemd_unit.read_text(encoding="utf-8")
    assert str(stub_runtime_dir) in body
    assert "ExecStart=" in body
    assert "--foreground" in body

    assert recorder.calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "eawfd.service"],
    ]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only systemd path")
def test_enable_service_linux_times_out_when_daemon_ping_absent(
    monkeypatch: pytest.MonkeyPatch,
    stub_runtime_dir: Path,
    stub_systemd_unit: Path,
) -> None:
    """The PID-file wait raises a structured error on timeout."""
    _install_run_recorder(
        monkeypatch,
        responses=[_StubProcess(), _StubProcess()],
    )

    def _fail_ready() -> int:
        raise ServiceInstallError("daemon did not answer daemon.ping within 0.2s")

    monkeypatch.setattr(service_install, "_wait_for_daemon_ready", _fail_ready)

    with pytest.raises(ServiceInstallError, match=r"daemon\.ping"):
        enable_service()


# ---------------------------------------------------------------------
# macOS — enable
# ---------------------------------------------------------------------


class _FakePwRecord:
    """Minimal stand-in for :class:`pwd.struct_passwd`."""

    def __init__(self, pw_dir: str) -> None:
        self.pw_dir = pw_dir


def _stub_invoking_user(
    monkeypatch: pytest.MonkeyPatch,
    *,
    uid: int,
    sudo_uid: str | None,
    home: Path,
) -> None:
    """Pin the invoking-uid resolution deterministically.

    Sets ``os.getuid`` → *uid*, manages ``SUDO_UID`` per *sudo_uid*
    (``None`` deletes it so a stray real env var cannot leak in), and
    redirects ``pwd.getpwuid`` to a fake record so the LaunchAgents
    home is derived from *home* and never a real user path.
    """
    monkeypatch.setattr("eawf.runtime.daemon.service_install.os.getuid", lambda: uid)
    if sudo_uid is None:
        monkeypatch.delenv("SUDO_UID", raising=False)
    else:
        monkeypatch.setenv("SUDO_UID", sudo_uid)
    monkeypatch.setattr(
        "eawf.runtime.daemon.service_install.pwd.getpwuid",
        lambda _uid: _FakePwRecord(str(home)),
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only launchd path")
def test_enable_service_macos_renders_plist_and_invokes_launchctl(
    monkeypatch: pytest.MonkeyPatch,
    stub_runtime_dir: Path,
    stub_launchd_plist: Path,
) -> None:
    """``enable_service`` writes the plist + runs the expected commands."""
    recorder = _install_run_recorder(
        monkeypatch,
        responses=[_StubProcess(), _StubProcess(), _StubProcess(), _StubProcess()],
    )
    _seed_pid_file(stub_runtime_dir, pid=11111)
    monkeypatch.setattr(service_install, "_wait_for_daemon_ready", lambda: 11111)
    _stub_invoking_user(
        monkeypatch,
        uid=501,
        sudo_uid=None,
        home=stub_launchd_plist.parent,
    )

    envelope = enable_service()

    assert envelope.event_type == "daemon_service_enabled"
    assert envelope.platform == "darwin"
    assert envelope.unit == "dev.eawf.eawfd"
    assert envelope.pid == 11111

    assert stub_launchd_plist.exists()
    body = stub_launchd_plist.read_text(encoding="utf-8")
    assert str(stub_runtime_dir) in body
    assert "<key>Label</key>" in body
    assert "<string>dev.eawf.eawfd</string>" in body
    assert "<string>--foreground</string>" in body

    assert recorder.calls[0] == [
        "launchctl",
        "bootout",
        "gui/501/dev.eawf.eawfd",
    ]
    assert recorder.calls[1][:3] == ["launchctl", "bootstrap", "gui/501"]
    assert recorder.calls[2] == [
        "launchctl",
        "enable",
        "gui/501/dev.eawf.eawfd",
    ]
    assert recorder.calls[3] == [
        "launchctl",
        "kickstart",
        "gui/501/dev.eawf.eawfd",
    ]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only launchd path")
def test_enable_service_macos_uses_sudo_uid_when_run_as_root(
    monkeypatch: pytest.MonkeyPatch,
    stub_runtime_dir: Path,
    tmp_path: Path,
) -> None:
    """Under sudo (getuid==0) the invoking SUDO_UID drives domain + home."""
    fake_home = tmp_path / "fake-home-501"
    plist_path = fake_home / "Library" / "LaunchAgents" / "dev.eawf.eawfd.plist"
    # Exercise the real _launchd_plist_path so the home derivation is
    # what's under test; do not stub it here.
    recorder = _install_run_recorder(
        monkeypatch,
        responses=[_StubProcess(), _StubProcess(), _StubProcess(), _StubProcess()],
    )
    _seed_pid_file(stub_runtime_dir, pid=22222)
    monkeypatch.setattr(service_install, "_wait_for_daemon_ready", lambda: 22222)
    _stub_invoking_user(
        monkeypatch,
        uid=0,
        sudo_uid="501",
        home=fake_home,
    )

    envelope = enable_service()

    assert envelope.platform == "darwin"
    assert envelope.pid == 22222
    assert plist_path.exists()
    assert recorder.calls[1][:3] == ["launchctl", "bootstrap", "gui/501"]
    assert recorder.calls[1][3] == str(plist_path)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only launchd path")
def test_enable_service_macos_refuses_root_without_sudo_uid(
    monkeypatch: pytest.MonkeyPatch,
    stub_runtime_dir: Path,
    stub_launchd_plist: Path,
) -> None:
    """getuid==0 with no SUDO_UID fails fast before touching launchctl."""
    _install_run_recorder(monkeypatch)
    _stub_invoking_user(
        monkeypatch,
        uid=0,
        sudo_uid=None,
        home=stub_launchd_plist.parent,
    )

    with pytest.raises(ServiceInstallError, match="root"):
        service_install._launchd_uid_target()

    with pytest.raises(ServiceInstallError, match="root"):
        service_install._enable_launchd()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only launchd path")
def test_enable_service_macos_boots_out_before_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    stub_runtime_dir: Path,
    stub_launchd_plist: Path,
) -> None:
    """Idempotent enable issues bootout BEFORE bootstrap in argv order."""
    recorder = _install_run_recorder(
        monkeypatch,
        responses=[_StubProcess(), _StubProcess(), _StubProcess(), _StubProcess()],
    )
    _seed_pid_file(stub_runtime_dir, pid=33333)
    monkeypatch.setattr(service_install, "_wait_for_daemon_ready", lambda: 33333)
    _stub_invoking_user(
        monkeypatch,
        uid=501,
        sudo_uid=None,
        home=stub_launchd_plist.parent,
    )

    service_install._enable_launchd()

    verbs = [call[1] for call in recorder.calls if call[0] == "launchctl"]
    assert verbs.index("bootout") < verbs.index("bootstrap")


# ---------------------------------------------------------------------
# Disable — idempotent across never-installed state
# ---------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only systemd path")
def test_disable_service_linux_swallows_not_loaded_error(
    monkeypatch: pytest.MonkeyPatch,
    stub_systemd_unit: Path,
) -> None:
    """``disable_service`` swallows systemctl's non-zero "not loaded" exit.

    Tests the idempotent contract: disabling a never-installed unit
    must complete cleanly without raising.
    """
    recorder = _install_run_recorder(
        monkeypatch,
        responses=[
            _StubProcess(
                returncode=1,
                stderr="Failed to disable unit: Unit eawfd.service not loaded.",
            ),
            _StubProcess(),
        ],
    )
    # Unit file never existed; unlink path must tolerate the absence.
    assert not stub_systemd_unit.exists()

    envelope = disable_service()

    assert envelope.event_type == "daemon_service_disabled"
    assert envelope.platform == "linux"
    assert envelope.unit == "eawfd.service"
    # daemon-reload still runs at the end.
    assert recorder.calls[0][:3] == ["systemctl", "--user", "disable"]
    assert recorder.calls[-1] == ["systemctl", "--user", "daemon-reload"]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only launchd path")
def test_disable_service_macos_idempotent_when_plist_absent(
    monkeypatch: pytest.MonkeyPatch,
    stub_launchd_plist: Path,
) -> None:
    """Bootout's non-zero exit + missing plist are both swallowed."""
    recorder = _install_run_recorder(
        monkeypatch,
        responses=[
            _StubProcess(
                returncode=113,
                stderr="Could not find specified service.",
            ),
        ],
    )
    monkeypatch.setattr("eawf.runtime.daemon.service_install.os.getuid", lambda: 501)
    monkeypatch.delenv("SUDO_UID", raising=False)
    assert not stub_launchd_plist.exists()

    envelope = disable_service()

    assert envelope.event_type == "daemon_service_disabled"
    assert envelope.platform == "darwin"
    assert recorder.calls == [
        [
            "launchctl",
            "bootout",
            "gui/501/dev.eawf.eawfd",
        ]
    ]


# ---------------------------------------------------------------------
# Status — enum mapping
# ---------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only systemd path")
def test_service_status_linux_not_installed(
    monkeypatch: pytest.MonkeyPatch,
    stub_systemd_unit: Path,
) -> None:
    """Missing unit file → ``NOT_INSTALLED``."""
    assert not stub_systemd_unit.exists()
    _install_run_recorder(monkeypatch)
    assert service_status() == ServiceStatus.NOT_INSTALLED


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only systemd path")
def test_service_status_linux_running(
    monkeypatch: pytest.MonkeyPatch,
    stub_systemd_unit: Path,
) -> None:
    """``is-active`` ⇒ ``active`` maps to ``RUNNING``."""
    stub_systemd_unit.parent.mkdir(parents=True, exist_ok=True)
    stub_systemd_unit.write_text("[Unit]\n", encoding="utf-8")
    _install_run_recorder(
        monkeypatch,
        responses=[
            _StubProcess(returncode=0, stdout="active\n"),
            _StubProcess(returncode=0, stdout="enabled\n"),
        ],
    )
    assert service_status() == ServiceStatus.RUNNING


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only systemd path")
def test_service_status_linux_enabled_but_inactive(
    monkeypatch: pytest.MonkeyPatch,
    stub_systemd_unit: Path,
) -> None:
    """Inactive + enabled ⇒ ``ENABLED``."""
    stub_systemd_unit.parent.mkdir(parents=True, exist_ok=True)
    stub_systemd_unit.write_text("[Unit]\n", encoding="utf-8")
    _install_run_recorder(
        monkeypatch,
        responses=[
            _StubProcess(returncode=3, stdout="inactive\n"),
            _StubProcess(returncode=0, stdout="enabled\n"),
        ],
    )
    assert service_status() == ServiceStatus.ENABLED


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only systemd path")
def test_service_status_linux_disabled(
    monkeypatch: pytest.MonkeyPatch,
    stub_systemd_unit: Path,
) -> None:
    """Inactive + disabled ⇒ ``DISABLED``."""
    stub_systemd_unit.parent.mkdir(parents=True, exist_ok=True)
    stub_systemd_unit.write_text("[Unit]\n", encoding="utf-8")
    _install_run_recorder(
        monkeypatch,
        responses=[
            _StubProcess(returncode=3, stdout="inactive\n"),
            _StubProcess(returncode=1, stdout="disabled\n"),
        ],
    )
    assert service_status() == ServiceStatus.DISABLED


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only launchd path")
def test_service_status_macos_running(
    monkeypatch: pytest.MonkeyPatch,
    stub_launchd_plist: Path,
) -> None:
    """``launchctl print`` exit 0 with ``state = running`` ⇒ ``RUNNING``."""
    stub_launchd_plist.parent.mkdir(parents=True, exist_ok=True)
    stub_launchd_plist.write_text("<plist/>", encoding="utf-8")
    monkeypatch.setattr("eawf.runtime.daemon.service_install.os.getuid", lambda: 501)
    monkeypatch.delenv("SUDO_UID", raising=False)
    _install_run_recorder(
        monkeypatch,
        responses=[
            _StubProcess(
                returncode=0,
                stdout="state = running\npid = 4242\n",
            ),
        ],
    )
    assert service_status() == ServiceStatus.RUNNING


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only launchd path")
def test_service_status_macos_not_installed(
    monkeypatch: pytest.MonkeyPatch,
    stub_launchd_plist: Path,
) -> None:
    """Missing plist ⇒ ``NOT_INSTALLED``."""
    monkeypatch.setattr("eawf.runtime.daemon.service_install.os.getuid", lambda: 501)
    monkeypatch.delenv("SUDO_UID", raising=False)
    _install_run_recorder(monkeypatch)
    assert service_status() == ServiceStatus.NOT_INSTALLED


# ---------------------------------------------------------------------
# Template rendering — direct
# ---------------------------------------------------------------------


def test_render_systemd_template_substitutes_runtime_dir(tmp_path: Path) -> None:
    """The systemd template renders with the runtime dir substituted."""
    body = service_install._render_template(
        "eawfd.service.j2",
        runtime_dir_value=tmp_path / "runtime",
    )
    assert f'Environment="EAWF_RUNTIME_DIR={tmp_path / "runtime"}"' in body
    assert "[Unit]" in body and "[Service]" in body and "[Install]" in body


def test_render_launchd_template_substitutes_runtime_dir(tmp_path: Path) -> None:
    """The launchd template renders with the runtime dir substituted."""
    body = service_install._render_template(
        "dev.eawf.eawfd.plist.j2",
        runtime_dir_value=tmp_path / "runtime",
    )
    assert f"<string>{tmp_path / 'runtime'}/eawfd.log</string>" in body
    assert f"<string>{tmp_path / 'runtime'}/eawfd.err</string>" in body
    assert "<key>Label</key>" in body
    assert "<string>dev.eawf.eawfd</string>" in body
    assert "<string>--foreground</string>" in body
    assert "<key>SuccessfulExit</key>" in body
    assert "<false/>" in body
    assert "<key>KeepAlive</key>\n    <dict>" in body


# ---------------------------------------------------------------------
# Template-directory resolution (wheel vs editable checkout)
# ---------------------------------------------------------------------


def test_template_dir_prefers_repo_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editable checkouts use source templates when both dirs exist."""
    bundled = tmp_path / "bundled"
    repo = tmp_path / "repo"
    bundled.mkdir()
    repo.mkdir()
    monkeypatch.setattr(service_install, "_BUNDLED_TEMPLATE_DIR", bundled)
    monkeypatch.setattr(service_install, "_REPO_TEMPLATE_DIR", repo)
    assert service_install._template_dir() == repo


def test_template_dir_falls_back_to_repo_when_bundled_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editable checkouts (no bundled ``_data/``) resolve the repo dir."""
    bundled = tmp_path / "bundled"  # never created
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(service_install, "_BUNDLED_TEMPLATE_DIR", bundled)
    monkeypatch.setattr(service_install, "_REPO_TEMPLATE_DIR", repo)
    assert service_install._template_dir() == repo


def test_template_dir_raises_when_both_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither candidate present ⇒ ``ServiceInstallError``."""
    monkeypatch.setattr(service_install, "_BUNDLED_TEMPLATE_DIR", tmp_path / "bundled")
    monkeypatch.setattr(service_install, "_REPO_TEMPLATE_DIR", tmp_path / "repo")
    with pytest.raises(ServiceInstallError, match="template dir missing"):
        service_install._template_dir()


# ---------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------


def test_wait_for_pid_file_rejects_nonpositive_timeout() -> None:
    """The PID-file wait raises immediately on a non-positive timeout."""
    with pytest.raises(ServiceInstallError, match="timeout must be positive"):
        service_install._wait_for_pid_file(timeout_seconds=0)


def test_wait_for_daemon_ready_rejects_nonpositive_timeout() -> None:
    """The readiness wait raises immediately on a non-positive timeout."""
    with pytest.raises(ServiceInstallError, match="timeout must be positive"):
        service_install._wait_for_daemon_ready(timeout_seconds=0)


def test_wait_for_daemon_ready_wraps_ping_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn-layer ping timeout becomes a service-install error."""

    def _fail_ready(*_args: object, **_kwargs: object) -> int:
        raise service_install.DaemonSpawnTimeoutError("timeout")

    monkeypatch.setattr(service_install, "wait_for_daemon_ready", _fail_ready)
    with pytest.raises(ServiceInstallError, match=r"daemon\.ping"):
        service_install._wait_for_daemon_ready(timeout_seconds=0.1)


def test_run_raises_service_install_error_on_check_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The internal ``_run`` helper wraps non-zero exits when ``check=True``."""

    def fake_run(*args: Any, **kwargs: Any) -> _StubProcess:
        return _StubProcess(returncode=2, stderr="boom")

    monkeypatch.setattr("eawf.runtime.daemon.service_install.subprocess.run", fake_run)
    with pytest.raises(ServiceInstallError, match="rc=2"):
        service_install._run(["false"])


# ---------------------------------------------------------------------
# Windows-only smoke (skipped on POSIX runners)
# ---------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SCM path")
def test_enable_service_windows_invokes_install_and_start(  # pragma: no cover - win32
    monkeypatch: pytest.MonkeyPatch,
    stub_runtime_dir: Path,
) -> None:
    """``enable_service`` calls InstallService + StartService on Windows."""
    install_calls: list[tuple[str, ...]] = []
    start_calls: list[str] = []

    class _StubServiceFramework:
        SERVICE_AUTO_START = 2

    class _FakeWin32ServiceUtil:
        win32service = _StubServiceFramework()

        @staticmethod
        def InstallService(  # noqa: N802 — pywin32 CamelCase API surface
            pythonClassString: str,  # noqa: N803 — pywin32 CamelCase API surface
            serviceName: str,  # noqa: N803
            displayName: str,  # noqa: N803
            startType: int,  # noqa: N803
        ) -> None:
            install_calls.append((pythonClassString, serviceName, displayName, str(startType)))

        @staticmethod
        def StartService(name: str) -> None:  # noqa: N802 — pywin32 CamelCase
            start_calls.append(name)

    monkeypatch.setitem(
        sys.modules,
        "win32serviceutil",
        _FakeWin32ServiceUtil,
    )
    _seed_pid_file(stub_runtime_dir, pid=7777)
    monkeypatch.setattr(service_install, "_wait_for_daemon_ready", lambda: 7777)

    envelope = enable_service()

    assert envelope.event_type == "daemon_service_enabled"
    assert envelope.platform == "win32"
    assert envelope.unit == "eawfd"
    assert envelope.pid == 7777
    assert install_calls and install_calls[0][1] == "eawfd"
    assert start_calls == ["eawfd"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only SCM path")
def test_disable_service_windows_idempotent(  # pragma: no cover - win32
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``disable_service`` swallows Stop / Remove errors on Windows."""

    class _FakeWin32ServiceUtil:
        @staticmethod
        def StopService(name: str) -> None:  # noqa: N802 — pywin32 CamelCase API
            raise OSError("service not running")

        @staticmethod
        def RemoveService(name: str) -> None:  # noqa: N802
            raise OSError("service does not exist")

    monkeypatch.setitem(
        sys.modules,
        "win32serviceutil",
        _FakeWin32ServiceUtil,
    )

    envelope = disable_service()
    assert envelope.event_type == "daemon_service_disabled"
    assert envelope.platform == "win32"


# ---------------------------------------------------------------------
# Envelope shape
# ---------------------------------------------------------------------


def test_service_envelope_is_frozen() -> None:
    """The envelope dataclass is frozen so consumers cannot mutate it."""
    envelope = ServiceEnvelope(
        event_type="daemon_service_enabled",
        platform="linux",
        unit="eawfd.service",
        pid=1,
    )
    with pytest.raises((AttributeError, Exception)):
        envelope.platform = "darwin"  # type: ignore[misc]


def test_service_status_enum_members_cover_spec_states() -> None:
    """All four states from the spec are enumerated on the public enum."""
    assert {member.value for member in ServiceStatus} == {
        "running",
        "enabled",
        "disabled",
        "not-installed",
        "unsupported",
    }
