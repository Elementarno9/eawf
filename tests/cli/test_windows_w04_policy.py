"""WIN-P4 policy: daemon routing default, phase-open in-process, UTF-8 console.

P30-I19-W04 ensures mutations route through the daemon by default on
Windows (the pipe transport landed in W02), keeps ``phase open``
in-process by design (gotcha 10 -- its scope_id is allocated DURING the
mutation so it cannot be marshalled across the wire), relaxes the
``eawf daemon run`` win32 refusal, and makes the console UTF-8-safe so the
``Eä`` brand never crashes on a cp1251 codepage.

These cases are platform-agnostic where possible (the routing decision +
the UTF-8 reconfigure are exercised with a simulated platform) so the
regression runs on the POSIX CI host too; the live windows-latest job
(W07) exercises the real pipe + console.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli import _dispatch
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.unit

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Temp workspace with ``EA_STATE`` inside; bootstrap runs daemonless."""
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    yield tmp_path


def test_proxy_enabled_is_not_platform_gated() -> None:
    """``_proxy_enabled`` defaults True regardless of platform.

    The daemon-default routing on Windows rides the same predicate the
    POSIX path uses -- there is no Windows carve-out that silently
    disables proxying. With no ``EAWF_DAEMONLESS`` env and the default
    config, the predicate is True so a mutation_kind verb routes through
    the daemon (over the W02 pipe on Windows).
    """
    from eawf.surfaces.cli._mutation import _proxy_enabled

    # Default config in a fresh cwd: proxy enabled (no daemonless env set).
    assert _proxy_enabled(None) is True


def test_phase_open_is_never_daemon_routed(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``phase open`` stays in-process by design (gotcha 10).

    Its scope_id is auto-allocated inside the mutator, so ``_run_mutation``
    is called with ``scope_id_factory`` (no eager ``mutation_kind``) and
    must NEVER marshal a mutation across ``_mutate_via_daemon`` -- the
    daemon proxy needs the scope upfront. We spy on the proxy shim and
    assert it is not called even with proxying enabled.
    """
    assert (
        runner.invoke(
            app, ["project", "init", "QR", "--title", "Quant", "--domains", "quant"]
        ).exit_code
        == 0
    )

    proxy_calls: list[str] = []
    real_mutate = _dispatch._mutate_via_daemon

    def _spy(*args: object, **kwargs: object) -> object:
        proxy_calls.append(str(kwargs.get("verb", "")))
        return real_mutate(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(_dispatch, "_mutate_via_daemon", _spy)
    # Enable proxying for this invocation (clear the daemonless env).
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)

    result = runner.invoke(app, ["phase", "open", "--auto", "--title", "P1"])
    assert result.exit_code == 0, result.output
    assert proxy_calls == [], f"phase open was daemon-routed: {proxy_calls}"


def test_daemon_run_no_longer_refuses_on_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    """``eawf daemon run`` no longer exits 2 with the win32 refusal.

    W04 removed the ``sys.platform.startswith('win')`` early-exit. We
    simulate win32 and stub the daemon ``run`` so the verb reaches the
    boot path instead of the old refusal. The stub returns rc=0 so the
    verb exits 0 -- proving the refusal is gone.
    """
    import eawf.surfaces.cli.commands.daemon as daemon_cmd

    monkeypatch.setattr(daemon_cmd.sys, "platform", "win32")

    booted: dict[str, bool] = {}

    def _fake_run(*, foreground: bool = True) -> int:
        booted["foreground"] = foreground
        return 0

    monkeypatch.setattr("eawf.runtime.daemon.main.run", _fake_run)
    result = runner.invoke(app, ["daemon", "run", "--foreground"])
    assert "not supported on windows" not in result.output
    assert result.exit_code == 0
    assert booted == {"foreground": True}


def test_ensure_utf8_console_reconfigures_on_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_ensure_utf8_console`` reconfigures stdout/stderr to UTF-8 on win32.

    A cp1251 console cannot encode the ``ä`` in the ``Eä`` brand; the
    helper must reconfigure both streams to UTF-8 with ``errors='replace'``
    so ``eawf --help`` never raises ``UnicodeEncodeError``. We assert it
    requests the reconfigure on a simulated win32 and is a no-op on POSIX.
    """
    import eawf.surfaces.cli.app as cli_app

    calls: list[dict[str, object]] = []

    class _Stream:
        def reconfigure(self, **kwargs: object) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(cli_app.sys, "platform", "win32")
    monkeypatch.setattr(cli_app.sys, "stdout", _Stream())
    monkeypatch.setattr(cli_app.sys, "stderr", _Stream())
    cli_app._ensure_utf8_console()
    assert calls == [
        {"encoding": "utf-8", "errors": "replace"},
        {"encoding": "utf-8", "errors": "replace"},
    ]


def test_ensure_utf8_console_is_noop_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The UTF-8 reconfigure never fires on a POSIX platform."""
    import eawf.surfaces.cli.app as cli_app

    calls: list[dict[str, object]] = []

    class _Stream:
        def reconfigure(self, **kwargs: object) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(cli_app.sys, "platform", "linux")
    monkeypatch.setattr(cli_app.sys, "stdout", _Stream())
    monkeypatch.setattr(cli_app.sys, "stderr", _Stream())
    cli_app._ensure_utf8_console()
    assert calls == []
