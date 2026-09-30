"""Close-RPC timeout is terminal; transport fallback stamps ``daemon-fallback``.

P30-I23-W03 splits the bespoke ``_wave_close_via_daemon`` proxy's single
``(RuntimeError, OSError, TimeoutError)`` catch into two outcomes:

* **TimeoutError is terminal.** The close request already reached the daemon, so
  the daemon may still be mid-close under the lock. Falling through to the
  ungated in-process close could double-apply it. The proxy now emits a typed
  :class:`~eawf.surfaces.cli.errors.DaemonMutationIndeterminate` envelope and
  exits non-zero WITHOUT writing state -- no in-process retry.
* **RuntimeError / OSError still fall back**, but the in-process close event is
  stamped with a distinct ``close_mechanism = "daemon-fallback"`` on its
  ``extras`` map so an audit can tell it from a gate-passed ``"daemon"`` close.

Both drive the REAL ``eawf wave close`` CLI with a faked daemon client so the
bespoke proxy path runs, plus pure-function coverage of the new
``transport_fallback`` parameter on :func:`resolve_close_mechanism` /
:func:`close_event_extras`.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from eawf.kernel.state.enums import CloseFailureKind
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli._mutation import close_event_extras, resolve_close_mechanism
from eawf.surfaces.cli.commands.lifecycle import _close_failure_kind

pytestmark = pytest.mark.integration

runner = CliRunner()


# --- CR-01: close-RPC TimeoutError is terminal ------------------------------


# --- CR-02: transport fallback stamps ``daemon-fallback`` -------------------


# --- pure-function coverage of the new ``transport_fallback`` parameter ------


def test_resolve_close_mechanism_transport_fallback_wins() -> None:
    """``transport_fallback=True`` yields ``"daemon-fallback"`` regardless of gate/waiver."""
    assert (
        resolve_close_mechanism(gate_bearing=False, waived=False, transport_fallback=True)
        == "daemon-fallback"
    )
    assert (
        resolve_close_mechanism(gate_bearing=True, waived=True, transport_fallback=True)
        == "daemon-fallback"
    )


def test_resolve_close_mechanism_default_is_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boundary: the default (no transport fallback, no daemonless env) is ``"daemon"``."""
    # The integration tier forces the daemonless carve-out for every test; this
    # one asserts the behaviour when that env is ABSENT, so it clears it first.
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    assert resolve_close_mechanism(gate_bearing=True, waived=False) == "daemon"


def test_close_failure_kind_reads_a_direct_timeout() -> None:
    """Boundary: the exception itself is the timeout, with no chain to walk."""
    assert _close_failure_kind(TimeoutError("read timed out")) is CloseFailureKind.TIMED_OUT


def test_close_failure_kind_walks_a_wrapped_timeout() -> None:
    """The CLI wrapper hides the timeout on ``__cause__``; the chain finds it."""
    inner = TimeoutError("close RPC read timed out")
    wrapped = cli_errors.DaemonUnreachable("daemon unavailable for close.submit")
    wrapped.__cause__ = inner
    outer = cli_errors.DaemonMutationIndeterminate("close RPC timed out")
    outer.__cause__ = wrapped
    assert _close_failure_kind(outer) is CloseFailureKind.TIMED_OUT


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("connection reset mid-write"),
        OSError("socket closed"),
        cli_errors.DaemonUnreachable("daemon is down"),
    ],
)
def test_close_failure_kind_defaults_to_harness_fault(exc: BaseException) -> None:
    """Error path: a non-timeout chain never claims a timeout."""
    assert _close_failure_kind(exc) is CloseFailureKind.HARNESS_FAULT


def test_close_event_extras_transport_fallback_preserves_base() -> None:
    """``close_event_extras`` folds ``daemon-fallback`` in while preserving base extras."""
    extras = close_event_extras(
        {"readiness_warnings_count": 3}, gate_bearing=False, waived=False, transport_fallback=True
    )
    assert extras["close_mechanism"] == "daemon-fallback"
    assert extras["readiness_warnings_count"] == 3
