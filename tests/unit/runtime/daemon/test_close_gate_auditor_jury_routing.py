"""The configured juror ceiling reaches every limit that bounds a close.

A juror ceiling raised in the repo config must survive the config-to-block
path, stretch the daemon's mutation watchdog, and outlive the CLI's wire
timeout; a junk value falls back to the default.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import State
from eawf.platform.profiles.models import VerifyBlock
from eawf.runtime.daemon.limits import (
    cli_mutation_timeout_for,
    configured_juror_wall_clock,
    mutation_hard_limit_for,
)

pytestmark = pytest.mark.unit

_T0 = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)
_WAVE = "P30-I23-W07"


def _state_payload() -> dict[str, Any]:
    """A minimal State with one CLAIMED verdict-always (L-bucket) wave."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": _T0.isoformat(),
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "ABC",
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ABC",
        },
        "current": {"project_code": "ABC"},
        "workspace": None,
        "phases": {
            "P30": {
                "id": "P30",
                "scope_id": "ABC",
                "track_id": None,
                "title": "P30",
                "status": "active",
                "iter_ids": ["P30-I23"],
                "outcome_ids": [],
                "opened_at": _T0.isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P30-I23": {
                "id": "P30-I23",
                "phase_id": "P30",
                "title": "I23",
                "status": "active",
                "wave_ids": [_WAVE],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _T0.isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE: {
                "id": _WAVE,
                "iter_id": "P30-I23",
                "title": "close the jury OR-fold single-auditor bypass",
                "status": "claimed",
                "file_scopes": ["src/eawf/runtime/daemon/methods/state.py"],
                "success_criteria": [],
                "gates": [],
                "effort_bucket": "L",
                "agent_role": "executor",
                "opened_at": _T0.isoformat(),
                "sessions": {},
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


# --- P30-I25-W35: the configured ceiling must survive the config->block path ---


def test_repo_config_file_ceiling_reaches_the_close_path_loader(tmp_path: Path) -> None:
    """A ceiling in `.ea/config.yaml` must reach the block THE CLOSE PATH resolves.

    Drives `load_active_verify_block` -- the function the close gate itself calls
    (`daemon/methods/state.py` -> `_load_active_verify_block` -> the overlay). Not
    the overlay helper, and not a monkeypatched block.

    This is the THIRD attempt at this test, and the first two were both green over
    a dead path -- the exact defect class the phase exists to kill:

    - W32's asserted the ceiling on a synthetic block it had monkeypatched in,
      while the overlay silently dropped the leaf. The auditor kept spawning at
      600s, was killed mid-audit, wrote no verdict, and the wave could not close.
    - W35's called `_overlay_repo_verify_leaves` directly, reaching PAST the loader
      whose call site is the thing that could go missing. Deleting that call site
      left it green.

    So: delete the overlay call site in the loader and this test fails.
    """
    from eawf.workflow.verify.readiness import load_active_verify_block

    ea = tmp_path / ".ea"
    ea.mkdir()
    (ea / "config.yaml").write_text(
        "schema_version: '1.0'\n"
        "profiles:\n  enabled:\n    - core\n    - python\n"
        "verify:\n  juror_wall_clock_seconds: 1800.0\n",
        encoding="utf-8",
    )
    state = State.model_validate(_state_payload())
    wave_id = next(iter(state.waves))

    resolved = load_active_verify_block(wave_id, state, repo_root=tmp_path, config_root=tmp_path)

    assert resolved is not None
    # Off the FILE, through the loader the close path calls, onto the block.
    assert resolved.juror_wall_clock_seconds == 1800.0
    assert resolved.juror_wall_clock_seconds != VerifyBlock().juror_wall_clock_seconds


def test_repo_verify_overlay_ignores_a_junk_wall_clock() -> None:
    from eawf.workflow.verify.readiness import _overlay_repo_verify_leaves

    for junk in ("1800", True, 0, -5):
        resolved = _overlay_repo_verify_leaves(
            VerifyBlock(), {"verify": {"juror_wall_clock_seconds": junk}}
        )
        assert resolved is not None
        assert resolved.juror_wall_clock_seconds == VerifyBlock().juror_wall_clock_seconds


def test_watchdog_hard_limit_accommodates_a_raised_juror_ceiling() -> None:
    """An auditor allowed 1800s inside a mutation watched at 900s is killed anyway.

    The watchdog exists to catch a HUNG mutation, not to cancel a long one that
    is working. Raising the juror ceiling without raising the watchdog would make
    the close strictly worse than the timeout it was raised to escape.
    """
    from eawf.runtime.daemon.main import (
        _MUTATION_HARD_LIMIT_SECONDS,
        mutation_hard_limit_for,
    )

    assert mutation_hard_limit_for(1800.0) > 1800.0
    assert mutation_hard_limit_for(1800.0) == 2100.0
    # The default ceiling leaves the historical limit untouched.
    assert mutation_hard_limit_for(600.0) == _MUTATION_HARD_LIMIT_SECONDS
    assert mutation_hard_limit_for(None) == _MUTATION_HARD_LIMIT_SECONDS


def test_cli_wire_timeout_outlives_the_daemon_hard_limit(tmp_path: Path) -> None:
    """W48: the CLI must not give up before the daemon does.

    A gated close spawns a fresh-context auditor INSIDE the watched mutation, so
    the daemon may legitimately hold the request for the juror ceiling plus the
    commit margin. The CLI's wire timeout was a 30s constant -- less than a
    THIRTIETH of the daemon's own floor -- so every gated close raised
    ``DaemonMutationIndeterminate`` ("the write may or may not have applied")
    while the daemon went on to apply it. It misreported every close in P30-I25.

    The ceiling is read off the repo's real config file, through the same helper
    the CLI calls, so deleting the wiring cannot leave this green.
    """
    from eawf.surfaces.cli._daemon_client import DEFAULT_CALL_TIMEOUT_SECONDS

    ea = tmp_path / ".ea"
    ea.mkdir()
    (ea / "config.yaml").write_text(
        "schema_version: '1.0'\nverify:\n  juror_wall_clock_seconds: 1800.0\n",
        encoding="utf-8",
    )

    ceiling = configured_juror_wall_clock(tmp_path)

    assert ceiling == 1800.0
    # The old constant could not even cover the daemon's DEFAULT limit, let alone
    # a raised ceiling -- that is the whole bug, in one line.
    assert mutation_hard_limit_for(None) > DEFAULT_CALL_TIMEOUT_SECONDS
    assert cli_mutation_timeout_for(ceiling) > mutation_hard_limit_for(ceiling)
    assert cli_mutation_timeout_for(None) > mutation_hard_limit_for(None)
