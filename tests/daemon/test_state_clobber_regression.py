"""The stale-cache clobber class (P30-I23-W21, CR-03).

The incident-map wipe this wave repairs happened because a daemon
mutation committed a state snapshot read BEFORE a CLI direct write
landed — the write between read and commit was silently lost. W09's
lock split re-reads state UNDER the commit lock and re-checks the
target wave row, so a daemon close now lands ON TOP of a concurrent
CLI backfill instead of wiping it. This suite pins that survival.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.state.enums import IncidentSeverity
from eawf.kernel.state.models import State
from eawf.runtime.daemon.methods.state import mutate
from eawf.workflow.evidence.incident import open_incident
from tests.daemon.test_close_lock_split import (
    _WAVE,
    _build_ctx,
    _close_mutation,
    _run,
    _write_state,
)

pytestmark = pytest.mark.integration

_CLI_INCIDENT = f"INC-CLI-{uuid.uuid4().hex[:6]}"


def _cli_direct_backfill(state_path: Path) -> None:
    """The CLI fallback write: load, mutate via the library mutator, rewrite."""
    state = State.model_validate_json(state_path.read_text(encoding="utf-8"))
    open_incident(
        state,
        incident_id=_CLI_INCIDENT,
        scope_id="ABC",
        severity=IncidentSeverity.MEDIUM,
        title="backfilled by the CLI while the daemon close was in flight",
    )
    state_path.write_text(state.model_dump_json(), encoding="utf-8")


def test_daemon_close_preserves_cli_write_landed_during_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CR-03: a CLI backfill in the pre-flight window survives the close.

    The CLI write lands AFTER the daemon's lock-free pre-flight read and
    BEFORE its locked commit — exactly the window the old whole-pipeline
    snapshot clobbered. The W09 under-lock re-read must preserve BOTH the
    incident (the CLI write) and the closed wave (the daemon write).
    """
    from eawf.runtime.daemon.methods import state as daemon_state

    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path)
    ctx = _build_ctx(tmp_path, state_path)

    real_preflight = daemon_state.run_close_preflight

    async def _preflight_then_cli_write(*args: Any, **kwargs: Any) -> Any:
        result = await real_preflight(*args, **kwargs)
        _cli_direct_backfill(state_path)
        return result

    monkeypatch.setattr(daemon_state, "run_close_preflight", _preflight_then_cli_write)

    async def body() -> None:
        await mutate(ctx, _close_mutation())

    _run(body)
    payload = orjson.loads(state_path.read_bytes())
    assert payload["waves"][_WAVE]["status"] == "closed", "the daemon write was lost"
    assert _CLI_INCIDENT in (payload.get("incidents") or {}), (
        "the CLI backfill was clobbered by the daemon commit — the W09 under-lock re-read regressed"
    )
