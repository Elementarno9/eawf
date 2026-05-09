"""Shared helpers for the six core skill subclasses (Phase 4 W02).

The W02 skills all follow the same shape::

    1. Probe instruments via :func:`eawf.install.instrument_probe.probe`.
    2. Resolve the active state path (env / workspace flag / pwd upward).
    3. Execute the §14 algorithm steps; each step appends one ``EVENT``
       envelope to ``store/event.jsonl`` via
       :func:`eawf.store.append.append_envelope`.
    4. Optionally mutate state via
       :func:`eawf.cli._mutation.state_transaction`.
    5. Populate the per-skill body model from :mod:`eawf.skills.bodies`
       and return the corresponding :class:`SkillResult`.

This module factors the boilerplate so each skill module focuses on its
algorithm. No heavy LLM-fanout step is implemented in v0.1 — the
algorithms persist a placeholder envelope and degrade to
``status=needs_user`` with a typed :class:`UserQuestion` when downstream
work needs human input (per the design spec §14 degrade pattern).
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eawf.install.instrument_probe import (
    INSTRUMENT_REQUIREMENTS,
    PROBE_VERSION,
    ProbeReport,
    ProbeResult,
)
from eawf.install.instrument_probe import probe as run_probe
from eawf.render.envelope import (
    EnvelopeWarning,
    InstrumentStatus,
)
from eawf.skills.engine import ProbeOutcome
from eawf.state.enums import StoreKind
from eawf.state.resolve import resolve_with_reason
from eawf.store.append import append_envelope
from eawf.store.envelope import Envelope
from eawf.store.paths import store_path

logger = logging.getLogger(__name__)


_DEFAULT_PROFILE_IDS: tuple[str, ...] = ("core",)


def _project_status(spec_status: str) -> InstrumentStatus:
    """Project a probe-result status onto the envelope :class:`InstrumentStatus`.

    The envelope's frozen literal allows ``ok | missing | degraded``. Probe
    results emit ``ok | warn | fail``; we map ``warn → degraded`` and
    ``fail → missing`` so the envelope header surfaces a coherent value.
    """
    if spec_status == "ok":
        return "ok"
    if spec_status == "warn":
        return "degraded"
    # ``fail`` only ever applies to a hard tool that is absent.
    return "missing"


def _probe_cache_path(state_path: Path) -> Path:
    """Return the canonical probe-cache path next to ``state.json``.

    The probe-cache override env var (``EA_INSTRUMENT_PROBE``) is honoured
    by :func:`~eawf.install.instrument_probe.probe` itself, so we only
    need to supply a sensible default here.
    """
    return state_path.parent / "instrument-probe.json"


def _stub_report(profile_ids: list[str]) -> ProbeReport:
    """Build a synthetic ``ok``-only report for callers that bypass probing.

    Used when :func:`probe_skill_instruments` cannot run the real probe
    (e.g. the cache parent does not exist) — the fallback degrades the
    envelope's ``instrument_probe`` map into a single ``probe: degraded``
    entry so downstream callers can spot the bypass.
    """
    return ProbeReport(
        probe_version=PROBE_VERSION,
        profile_ids=list(profile_ids),
        results=[
            ProbeResult(
                name=spec.name,
                kind=spec.kind,
                status="warn",
                detail="probe bypassed (cache parent missing)",
            )
            for pid in profile_ids
            for spec in INSTRUMENT_REQUIREMENTS.get(pid, [])
        ],
    )


def probe_skill_instruments(
    *,
    profile_ids: Iterable[str] = _DEFAULT_PROFILE_IDS,
    state_path: Path | None = None,
) -> ProbeOutcome:
    """Run the canonical instrument probe and project onto a :class:`ProbeOutcome`.

    Hard-tool absence drives ``ok=False`` so the engine short-circuits to a
    ``status=blocked`` envelope; soft warnings flow through as a
    :class:`EnvelopeWarning` while leaving ``ok=True``. The returned outcome's
    ``instrument_probe`` map is keyed by tool name; the engine copies it
    verbatim into the envelope header.

    Args:
        profile_ids: Profile ids whose instrument-spec lists feed the probe.
            Defaults to ``("core",)`` — every Eä install requires git +
            python + uv.
        state_path: Optional state path used to derive the probe-cache
            location. ``None`` resolves the canonical ``.ea/state.json``
            position via :func:`eawf.state.resolve.resolve_with_reason`.
    """
    pids = list(profile_ids)
    if state_path is None:
        state_path, _ = resolve_with_reason(workspace=None)
    cache = _probe_cache_path(state_path)
    cache_parent_exists = cache.parent.exists()
    try:
        if not cache_parent_exists:
            report = _stub_report(pids)
        else:
            report = run_probe(pids, cache_path=cache, reprobe=False)
    except Exception as exc:
        # ``probe`` raises ``InstrumentMissing`` on a hard fail and only
        # surfaces other exceptions on truly broken environments. Either
        # way we want the engine to surface a blocked envelope rather
        # than crash.
        logger.warning(f"probe_skill_instruments: probe raised: {exc}")
        repair = ["eawf doctor --reprobe"]
        # Best-effort rebuild of the probe map: assume every spec is
        # missing so the envelope still carries an actionable map.
        instrument_probe: dict[str, InstrumentStatus] = {}
        for pid in pids:
            for spec in INSTRUMENT_REQUIREMENTS.get(pid, []):
                instrument_probe[spec.name] = "missing"
        return ProbeOutcome(
            ok=False,
            instrument_probe=instrument_probe,
            repair_commands=repair,
            warnings=[
                EnvelopeWarning(
                    code="instrument_probe_failed",
                    detail=f"instrument probe raised: {exc}",
                )
            ],
        )

    instrument_probe = {r.name: _project_status(r.status) for r in report.results}
    warnings: list[EnvelopeWarning] = []
    repair_commands: list[str] = []
    hard_fails: list[ProbeResult] = []
    for r in report.results:
        if r.status == "fail" and r.kind == "hard":
            hard_fails.append(r)
        elif r.status == "warn":
            warnings.append(
                EnvelopeWarning(
                    code="instrument_degraded",
                    detail=f"{r.name}: {r.detail or 'soft probe failed'}",
                )
            )

    if hard_fails:
        for r in hard_fails:
            warnings.append(
                EnvelopeWarning(
                    code="instrument_missing",
                    detail=f"{r.name}: {r.detail or 'hard probe failed'}",
                )
            )
            repair_commands.append(f"install {r.name} (see eawf doctor for hints)")
        return ProbeOutcome(
            ok=False,
            instrument_probe=instrument_probe,
            repair_commands=repair_commands,
            warnings=warnings,
        )

    return ProbeOutcome(
        ok=True,
        instrument_probe=instrument_probe,
        warnings=warnings,
    )


def emit_event(
    *,
    state_path: Path,
    scope_id: str,
    event_type: str,
    summary: str,
    payload: dict[str, Any] | None = None,
) -> str:
    """Append a single ``EVENT`` envelope to ``store/event.jsonl``.

    Returns the freshly minted envelope id so the caller can fold it into
    its persisted-store-records list.

    The append routes through :func:`eawf.store.append.append_envelope`,
    which acquires the events.jsonl sibling lock; this is independent of
    any state-mutation transaction the caller might be holding (the two
    locks are on distinct files).
    """
    events_path = store_path(state_path, StoreKind.EVENT)
    now = datetime.now(UTC)
    event_id = f"EV-{uuid.uuid4().hex[:12]}"
    envelope = Envelope(
        schema_version="1.0",
        id=event_id,
        kind=StoreKind.EVENT,
        scope_id=scope_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload={
            "timestamp": now.isoformat(),
            "event_type": event_type,
            "actor": "skill",
            "command": event_type,
            "args_hash": "",
            "before_state_version": None,
            "after_state_version": None,
            "status": "ok",
            "message": summary,
            **(payload or {}),
        },
        blob_refs=[],
        artifact_ids=[],
    )
    try:
        append_envelope(events_path, envelope)
    except Exception as exc:  # pragma: no cover - degrade gracefully
        # Skills must not crash on append failure; the engine wraps action
        # bodies so an exception flips the envelope to ``failed`` with a
        # traceback. We log and re-raise so the engine can surface it.
        logger.warning(f"emit_event: append failed for {events_path}: {exc}")
        raise
    return event_id


def resolve_active_state_path(workspace: Path | None = None) -> Path:
    """Return the resolved path of ``state.json`` for the active scope."""
    path, _reason = resolve_with_reason(workspace=workspace)
    return path


def _coerce_str_arg(value: Any, default: str) -> str:
    """Stable coercion of an args-dict value into a string.

    Skills accept stdin-piped JSON args; values arriving as ``int`` /
    ``bool`` / ``None`` are normalised so the body schema validator
    receives a string field.
    """
    if value is None:
        return default
    return str(value)


def has_research_profile(state_path: Path) -> bool:
    """Return True when the layered config enables the ``research`` profile.

    The layered-config merge is deferred until call time so the helper does
    not impose an import-time dependency on the profile machinery (which
    pulls Yaml + Pydantic loaders).
    """
    try:
        from eawf.config.layered import merge_config
    except Exception:  # pragma: no cover - defensive only
        return False
    anchor = state_path.parent.parent
    try:
        merged, _sources = merge_config(repo=anchor, workspace=anchor)
    except Exception as exc:
        logger.debug(f"has_research_profile: merge_config raised: {exc}")
        return False
    profiles = merged.get("profiles") if isinstance(merged, dict) else None
    if not isinstance(profiles, dict):
        return False
    enabled = profiles.get("enabled") or []
    if not isinstance(enabled, list):
        return False
    return "research" in enabled


def env_or(default: str, *names: str) -> str:
    """Return the first non-empty environment value for *names*, else *default*.

    Used by skill action handlers to honour scope-related env vars without
    pulling in the full CLI flag plumbing.
    """
    for name in names:
        v = os.environ.get(name)
        if v:
            return v
    return default


__all__ = [
    "emit_event",
    "env_or",
    "has_research_profile",
    "probe_skill_instruments",
    "resolve_active_state_path",
]
