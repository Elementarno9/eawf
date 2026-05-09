"""``eawf doctor`` check implementations.

Each public ``check_*`` function returns a :class:`CheckResult`. Three checks
ship with Wave W01 — together they answer the v0.1 plan §11 question "is
this install workable?".

- :func:`check_tools_available` — runs the instrument probe and surfaces its
  outcome (``ok``/``warn``/``fail``). On hard-tool failure the underlying
  :class:`eawf.cli.errors.InstrumentMissing` is allowed to propagate so the
  CLI maps it to exit code ``6`` (``INSTRUMENT_MISSING``). All other outcomes
  collapse to a non-fatal :class:`CheckResult`.
- :func:`check_state_present` — reports whether ``state.json`` resolves at
  the workspace anchor.
- :func:`check_config_resolves` — reports whether the layered config merge
  succeeds for the workspace.

The doctor command (`eawf.cli.commands.doctor`) consumes the list, formats it
via :mod:`eawf.doctor.report`, and selects the highest-severity status to
drive its exit code.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from eawf.config.layered import merge_config
from eawf.config.profile import KNOWN_PROFILES
from eawf.install.instrument_probe import probe
from eawf.state.resolve import resolve_with_reason

logger = logging.getLogger(__name__)


CheckStatus = Literal["ok", "warn", "fail"]


class CheckResult(BaseModel):
    """Single doctor check outcome.

    Attributes:
        name: Stable machine identifier (``"tools_available"``, ...).
        status: ``ok`` (everything fine), ``warn`` (functional but degraded),
            or ``fail`` (broken — the doctor surface still completes, but the
            CLI exits non-zero).
        detail: Short human message. ``None`` when the check has nothing
            interesting to add beyond ``status``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    status: CheckStatus
    detail: str | None = None


def _default_probe_cache(workspace: Path) -> Path:
    """Return the canonical cache file path under ``<workspace>/.ea/``."""
    return workspace / ".ea" / "instrument-probe.json"


def check_tools_available(
    *,
    workspace: Path,
    profile_ids: list[str] | None = None,
    reprobe: bool = False,
    cache_path: Path | None = None,
) -> CheckResult:
    """Run the instrument probe and project its results onto a single status.

    The result rolls every ``ProbeResult`` up to one status:

    - ``ok`` — every probe returned ``ok``.
    - ``warn`` — at least one soft probe failed (no hard fails).
    - ``fail`` — at least one hard probe failed.

    The function lets :class:`eawf.cli.errors.InstrumentMissing` escape so the
    CLI surface can map it to exit code ``6``. Callers that only want the
    snapshot view (no abort) should call :func:`eawf.install.instrument_probe.probe`
    directly with a guard.
    """
    if profile_ids is None:
        profile_ids = ["core"]
    cache = cache_path if cache_path is not None else _default_probe_cache(workspace)
    report = probe(profile_ids, cache_path=cache, reprobe=reprobe)

    statuses = {r.status for r in report.results}
    if "fail" in statuses:
        # Should be unreachable: a hard fail would have raised
        # InstrumentMissing inside ``probe``. Soft fails do not exist (they
        # are reclassified to ``warn``). Keep the branch for forward-compat.
        return CheckResult(
            name="tools_available",
            status="fail",
            detail="one or more hard tools missing",
        )
    if "warn" in statuses:
        soft_missing = [r.name for r in report.results if r.status == "warn"]
        return CheckResult(
            name="tools_available",
            status="warn",
            detail=f"soft tool(s) absent: {', '.join(soft_missing)}",
        )
    return CheckResult(
        name="tools_available",
        status="ok",
        detail=f"{len(report.results)} probes ok",
    )


def check_state_present(*, workspace: Path | None) -> CheckResult:
    """Return ``ok`` iff ``state.json`` resolves at *workspace*.

    *workspace* may be ``None`` to defer to the resolver's pwd-upward walk
    (which is the default behaviour when ``--workspace`` is not supplied).
    """
    state_path, reason = resolve_with_reason(workspace=workspace)
    if state_path.exists():
        return CheckResult(
            name="state_present",
            status="ok",
            detail=f"state.json found at {state_path} (via {reason})",
        )
    return CheckResult(
        name="state_present",
        status="warn",
        detail=f"no state.json at {state_path}",
    )


def check_config_resolves(*, workspace: Path | None) -> CheckResult:
    """Return ``ok`` when the layered config merge succeeds.

    The merge is best-effort: any unexpected exception collapses to ``warn``
    so the doctor surface stays useful even when a layer file is malformed.
    """
    repo = Path.cwd()
    try:
        merged, _sources = merge_config(repo=repo, workspace=workspace)
    except Exception as exc:
        return CheckResult(
            name="config_resolves",
            status="warn",
            detail=f"config merge failed: {exc}",
        )
    enabled_profiles: list[str] = []
    profiles_section = merged.get("profiles") if isinstance(merged, dict) else None
    if isinstance(profiles_section, dict):
        raw_enabled = profiles_section.get("enabled")
        if isinstance(raw_enabled, list):
            enabled_profiles = [p for p in raw_enabled if isinstance(p, str)]
    unknown = [p for p in enabled_profiles if p not in KNOWN_PROFILES]
    if unknown:
        return CheckResult(
            name="config_resolves",
            status="warn",
            detail=f"unknown profile(s) enabled: {', '.join(unknown)}",
        )
    return CheckResult(
        name="config_resolves",
        status="ok",
        detail=f"{len(enabled_profiles)} profile(s) enabled",
    )


def tools_available(
    *,
    workspace: Path,
    profile_ids: list[str] | None = None,
    reprobe: bool = False,
    cache_path: Path | None = None,
) -> CheckResult:
    """Public alias for :func:`check_tools_available` (plan §246 spelling)."""
    return check_tools_available(
        workspace=workspace,
        profile_ids=profile_ids,
        reprobe=reprobe,
        cache_path=cache_path,
    )


def state_present(*, workspace: Path | None) -> CheckResult:
    """Public alias for :func:`check_state_present`."""
    return check_state_present(workspace=workspace)


def config_resolves(*, workspace: Path | None) -> CheckResult:
    """Public alias for :func:`check_config_resolves`."""
    return check_config_resolves(workspace=workspace)


def run_all(
    *,
    workspace: Path | None,
    profile_ids: list[str] | None = None,
    reprobe: bool = False,
    cache_path: Path | None = None,
) -> list[CheckResult]:
    """Run every doctor check and return the result list.

    The instrument probe is the only check whose hard failure aborts the
    function; it raises :class:`eawf.cli.errors.InstrumentMissing` so the CLI
    can map it to exit code ``6``. State and config checks always return a
    :class:`CheckResult`.
    """
    workspace_for_probe = workspace if workspace is not None else Path.cwd()
    results = [
        check_tools_available(
            workspace=workspace_for_probe,
            profile_ids=profile_ids,
            reprobe=reprobe,
            cache_path=cache_path,
        ),
        check_state_present(workspace=workspace),
        check_config_resolves(workspace=workspace),
    ]
    return results
