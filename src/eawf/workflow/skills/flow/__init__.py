"""``/flow`` skill — composite controller running the delivery stages in order.

``/flow`` runs research → prep → audit → polish → ship sequentially,
accumulating per-step envelopes under :attr:`FlowBody.steps`. The PR-review
pass remains inside ship.

Short-circuit semantics (the W03 acceptance contract):

- The flow runs each core skill in order.
- After each step the flow inspects ``env.header.status``. Anything other
  than ``ok`` (``needs_user``, ``blocked``, ``failed``, ``partial``)
  triggers an immediate short-circuit. The flow's terminal envelope
  inherits the failing step's ``status`` and ``footer.repair_commands``.
- If every step returns ``ok``, the flow's terminal envelope is ``ok``
  and the body's ``terminal_status`` mirrors the last step's status.

The flow does **not** literally call :func:`eawf.workflow.skills.engine.run_skill`
on each subskill — instead it constructs a fresh :class:`SkillContext`
copy and routes through the engine so the per-step envelope is fully
populated (header status, instrument probe, footer mutations). This
keeps the short-circuit decision focused on the canonical envelope
shape rather than the action-side return type.

Honoured ``ctx.args`` keys:

- ``topic`` — free-form description recorded on :attr:`FlowBody.topic`.
- ``stop_after`` — short-circuit before the named step (matches §14's
  ``--stop-after`` flag). Recognised names are
  ``research|prep|audit|polish|ship``.
- ``args_per_step`` — optional dict of ``skill_name → ctx.args`` to
  forward to specific steps; absent steps inherit the flow's own args.
- ``resume_from`` — Phase 5 W02. Optional :class:`FlowCheckpointPayload`
  dict carrying the replay anchor; when present, the runner skips the
  prefix ``flow_order[:step_index + 1]`` and starts at step ``step_index
  + 1``.

Phase 5 W02 also adds:

- After every step boundary, the runner appends a
  :class:`FlowCheckpointPayload` envelope to ``flow.jsonl`` (under the
  per-file portalock + fsync).
- ``--resume`` reads the latest ``last_safe=True`` checkpoint and
  computes drift (state.json sha, git HEAD, profile id list, args hash).
  Drift refuses with exit ``INTEGRITY_VIOLATION`` and a populated
  ``drift`` body field.

The implementation is intentionally explicit: each subskill is an
attribute on the flow class so a test can monkey-patch a single subskill
without rewriting the registry.

This module keeps the historical flat import surface
(``from eawf.workflow.skills.flow import FlowSkill, compute_drift, ...``) intact.
The append-only ``flow.jsonl`` store readers / writers
(``load_flow_records`` and friends, plus ``abort_flow_record`` and the
checkpoint / record emitters) live in the sibling
:mod:`eawf.workflow.skills.flow.store_io` and are re-exported here. The
drift-detection helpers (``_current_git_head`` / ``_current_profile_ids``
/ ``_state_hash`` / ``compute_drift``) and the :class:`FlowSkill` runner
stay in this module so the test-suite monkeypatch seam
(``monkeypatch.setattr(flow, "_current_git_head", ...)``) keeps resolving
against the same module object the runner and ``compute_drift`` read.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from eawf.kernel.state.enums import FlowStatus
from eawf.kernel.store.kinds.flow import FlowCheckpointPayload
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.render.envelope import EnvelopeWarning, OutputEnvelope, SkillName
from eawf.workflow.skills.audit import AuditSkill
from eawf.workflow.skills.bodies.flow import FlowBody
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from eawf.workflow.skills.engine import (
    ActionRun,
    Skill,
    SkillAction,
    SkillContext,
    SkillResult,
    run_skill,
)
from eawf.workflow.skills.flow.store_io import (
    _emit_checkpoint,
    _emit_flow_record,
    abort_flow_record,
    in_progress_flow_ids,
    latest_active_flow_id,
    load_flow_records,
    load_latest_records_per_flow,
    load_latest_safe_checkpoint,
)
from eawf.workflow.skills.polish import PolishSkill
from eawf.workflow.skills.prep import PrepSkill
from eawf.workflow.skills.registry import register
from eawf.workflow.skills.research import ResearchSkill
from eawf.workflow.skills.ship import ShipSkill

logger = logging.getLogger(__name__)


# Canonical core-skill order for ``/flow`` per §14 + v0.1 plan §4 W03.
# The list lives at module level so tests can iterate it without copying
# the order from the docstring.
_CORE_FLOW_ORDER: tuple[tuple[SkillName, type[Skill]], ...] = (
    ("/research", ResearchSkill),
    ("/prep", PrepSkill),
    ("/audit", AuditSkill),
    ("/polish", PolishSkill),
    ("/ship", ShipSkill),
)


# Hard cap per git invocation. ``flow`` is operator-driven so a 5 s
# budget is well above the happy path while still bounding a hung daemon.
_GIT_TIMEOUT_SECONDS: float = 5.0


# ---- Runtime-option resolution --------------------------------

#: Recognised ``--caps`` axes. A cap ceiling on any other axis records an
#: advisory warning and is dropped (an idle knob is worse than an honest gap).
_CAP_KEYS: frozenset[str] = frozenset({"eu", "usd", "tokens"})

#: Built-in default for ``--max-repair-cycles`` when neither the flag nor the
#: ``flow.max_repair_cycles`` config leaf resolves.
_DEFAULT_MAX_REPAIR_CYCLES: int = 3


def _parse_caps(raw: Any, warnings: list[EnvelopeWarning]) -> dict[str, float]:
    """Parse ``--caps eu=<f>,usd=<f>,tokens=<n>`` into a ceiling map.

    Accepts the comma-separated string form or an already-parsed dict. An
    out-of-set axis records an advisory ``unknown_cap`` warning; a malformed
    number records ``invalid_cap`` — both are dropped rather than aborting so
    one bad token cannot fail the pipeline. Enforcement stays honest-empty
    until EU-capture spend rows land; the parsed ceilings are recorded so the
    contract is visible, not idle.

    Args:
        raw: The ``ctx.args["caps"]`` value (string, dict, or ``None``).
        warnings: Accumulator for advisory parse warnings.

    Returns:
        The ceiling map keyed by axis (empty when uncapped).
    """
    if raw is None:
        return {}
    pairs: list[tuple[str, Any]] = []
    if isinstance(raw, dict):
        pairs = list(raw.items())
    elif isinstance(raw, str):
        for part in raw.split(","):
            token = part.strip()
            if not token:
                continue
            if "=" not in token:
                warnings.append(
                    EnvelopeWarning(
                        code="invalid_cap",
                        detail=f"ignored --caps segment {token!r}: expected axis=value",
                    )
                )
                continue
            axis, _, value = token.partition("=")
            pairs.append((axis, value))
    else:
        return {}
    caps: dict[str, float] = {}
    for axis, value in pairs:
        key = str(axis).strip().lower()
        if key not in _CAP_KEYS:
            warnings.append(
                EnvelopeWarning(
                    code="unknown_cap",
                    detail=f"ignored --caps axis {key!r}: expected one of {sorted(_CAP_KEYS)}",
                )
            )
            continue
        try:
            caps[key] = float(value)
        except TypeError, ValueError:
            warnings.append(
                EnvelopeWarning(
                    code="invalid_cap",
                    detail=f"ignored --caps {key}={value!r}: not a number",
                )
            )
    return caps


def _config_max_repair_cycles(state_path: Path) -> int:
    """Read the ``flow.max_repair_cycles`` leaf (default 3) from layered config."""
    from eawf.kernel.config.layered import merge_config

    workspace_root = _workspace_root_for_state(state_path)
    try:
        merged, _sources = merge_config(repo=workspace_root, workspace=workspace_root)
    except Exception as exc:  # pragma: no cover - defensive only
        logger.debug(f"_config_max_repair_cycles merge_error={exc!r}")
        return _DEFAULT_MAX_REPAIR_CYCLES
    flow_cfg = merged.get("flow") if isinstance(merged, dict) else None
    if isinstance(flow_cfg, dict):
        raw = flow_cfg.get("max_repair_cycles")
        if isinstance(raw, bool):
            # bool is an int subclass; a stray True/False is not a valid count.
            return _DEFAULT_MAX_REPAIR_CYCLES
        if isinstance(raw, int) and raw >= 0:
            return raw
    return _DEFAULT_MAX_REPAIR_CYCLES


def _resolve_max_repair_cycles(
    args: dict[str, Any], state_path: Path, warnings: list[EnvelopeWarning]
) -> int:
    """Resolve ``--max-repair-cycles`` (default from ``flow.max_repair_cycles``).

    An explicit flag wins; a non-integer or negative value records an advisory
    ``invalid_max_repair_cycles`` warning and falls back to the config leaf
    (built-in default 3) rather than aborting.
    """
    raw = args.get("max_repair_cycles")
    if raw is None:
        return _config_max_repair_cycles(state_path)
    if isinstance(raw, bool):
        warnings.append(
            EnvelopeWarning(
                code="invalid_max_repair_cycles",
                detail=f"ignored --max-repair-cycles {raw!r}: expected a non-negative int",
            )
        )
        return _config_max_repair_cycles(state_path)
    try:
        value = int(raw)
    except TypeError, ValueError:
        warnings.append(
            EnvelopeWarning(
                code="invalid_max_repair_cycles",
                detail=f"ignored --max-repair-cycles {raw!r}: not an integer",
            )
        )
        return _config_max_repair_cycles(state_path)
    if value < 0:
        warnings.append(
            EnvelopeWarning(
                code="invalid_max_repair_cycles",
                detail=f"ignored --max-repair-cycles {value}: must be >= 0",
            )
        )
        return _config_max_repair_cycles(state_path)
    return value


def _resolve_advance_after(
    args: dict[str, Any], state_path: Path, warnings: list[EnvelopeWarning]
) -> dict[str, bool]:
    """Resolve deterministic flow transition gates from config and CLI compatibility."""
    from eawf.kernel.config.layered import merge_config

    stages = ("research", "prep", "audit", "polish")
    resolved = dict.fromkeys(stages, False)
    workspace_root = _workspace_root_for_state(state_path)
    try:
        merged, _sources = merge_config(repo=workspace_root, workspace=workspace_root)
    except Exception as exc:  # pragma: no cover - defensive only
        logger.debug(f"_resolve_advance_after merge_error={exc!r}")
        merged = {}
    flow = merged.get("flow") if isinstance(merged, dict) else None
    configured = flow.get("advance_after") if isinstance(flow, dict) else None
    if isinstance(configured, dict):
        for stage in stages:
            value = configured.get(stage)
            if isinstance(value, bool):
                resolved[stage] = value

    raw = args.get("advance_after", args.get("auto_accept"))
    if raw is None:
        return resolved
    if raw is True:
        return dict.fromkeys(stages, True)
    tokens = _transition_override_tokens(raw)
    if tokens is None:
        warnings.append(
            EnvelopeWarning(
                code="invalid_advance_after",
                detail=f"ignored flow transition override {raw!r}",
            )
        )
        return resolved
    for token in tokens:
        if token not in resolved:
            warnings.append(
                EnvelopeWarning(
                    code="invalid_advance_after",
                    detail=f"ignored unknown flow stage {token!r}",
                )
            )
            continue
        resolved[token] = True
    return resolved


def _transition_override_tokens(raw: Any) -> list[str] | None:
    """Normalize CLI transition overrides; return ``None`` for invalid shapes."""
    if isinstance(raw, str):
        return [token.strip().lower() for token in raw.split(",") if token.strip()]
    if isinstance(raw, list):
        return [str(token).strip().lower() for token in raw]
    return None


def _stop_after_short_name(skill_name: SkillName) -> str:
    """Strip the leading ``/`` so ``stop_after`` can be a bare name.

    Mirrors the §14 ``--stop-after`` flag values: ``research|prep|audit|
    ship|review|polish`` (no leading slash) — the flow honours either
    form so operators don't have to escape the slash in shells.
    """
    return skill_name.lstrip("/")


def _resolve_stop_after(raw: Any) -> str | None:
    """Return a normalised ``stop_after`` short name or ``None``.

    Empty / unrecognised values yield ``None`` (the flow runs the full
    pipeline). Recognised values match a member of :data:`_CORE_FLOW_ORDER`.
    """
    if raw is None:
        return None
    candidate = str(raw).strip().lower().lstrip("/")
    if not candidate:
        return None
    valid = {_stop_after_short_name(name) for name, _ in _CORE_FLOW_ORDER}
    if candidate not in valid:
        return None
    return candidate


def short_circuit_terminal_status(statuses: list[str]) -> str:
    """Compute the flow's terminal status from a sequence of step statuses.

    Contract (mirrored by the test_skill_flow property test):

    - Empty input → ``"ok"`` (the flow ran nothing; nothing failed).
    - First non-``ok`` status wins (short-circuit); the rest of the list
      is ignored.
    - All-``ok`` input → ``"ok"`` (terminal status mirrors the last step).

    Returns:
        The terminal status string.
    """
    for s in statuses:
        if s != "ok":
            return s
    return "ok"


# ---- Drift detection helpers ------------------------------------------------


def _workspace_root_for_state(state_path: Path) -> Path:
    """Return the workspace root containing *state_path*.

    The canonical layout is ``<workspace>/.ea/state.json``; the workspace
    root is therefore ``state_path.parent.parent`` when the parent
    directory is named ``.ea``. Falls back to the parent so test fixtures
    that drop ``state.json`` directly under a temp dir still resolve.
    """
    ea_dir = state_path.parent
    return ea_dir.parent if ea_dir.name == ".ea" else ea_dir


def _run_git(args: list[str], cwd: Path) -> str | None:
    """Return stripped git stdout, or ``None`` on any failure.

    Mirrors the safe-degrade pattern in
    :mod:`eawf.runtime.runtimes.claude.statusline_modules.git`: any non-zero exit,
    missing binary, or timeout collapses to ``None`` so callers can
    distinguish "not a git repo / git unavailable" from a real value.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
        OSError,
    ) as exc:
        logger.debug(f"_run_git failed args={' '.join(args)!r} exc={exc}")
        return None
    return proc.stdout.strip()


def _current_git_head(workspace_root: Path) -> str | None:
    """Return the current ``git rev-parse HEAD`` SHA or ``None``."""
    head = _run_git(["rev-parse", "HEAD"], workspace_root)
    if head is None:
        return None
    # ``rev-parse`` may return short forms in odd configs; require the
    # canonical 40-char hex form so the pattern validator on the
    # checkpoint payload accepts the round-trip.
    if len(head) != 40:
        logger.debug(f"_current_git_head unexpected-length length={len(head)} head={head!r}")
        return None
    return head


def _state_hash(state_path: Path) -> str:
    """Return ``sha256:<hex>`` of the on-disk state.json bytes.

    Missing-file collapses to a synthetic ``sha256:<all-zeros>``-style
    sentinel — the canonical pattern still matches so the checkpoint
    payload validates, but a real state.json materialising between two
    appends would be flagged as drift.
    """
    try:
        data = state_path.read_bytes()
    except FileNotFoundError:
        # Empty bytes hash so the sentinel still fits the regex.
        data = b""
    digest = hashlib.sha256(data).hexdigest()
    return f"sha256:{digest}"


def _canonical_args_per_step_hash(args_per_step: dict[str, Any] | None) -> str:
    """Return ``sha256:<hex>`` of the canonical-form per-step args JSON.

    Canonicalisation: sorted keys, no whitespace. None / empty input
    hashes to the SHA of an empty JSON object (``{}``) so the absent and
    all-defaults cases are indistinguishable.

    The runner emits one checkpoint per step and hashes the **per-step**
    args dict (the value forwarded to that step's :class:`SkillContext`)
    so per-step arg mutations between resume and the original run are
    detected by :func:`compute_drift`. Hashing the whole multi-step
    mapping would yield the same value for every checkpoint and provide
    no per-step granularity.
    """
    payload = args_per_step or {}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(blob).hexdigest()
    return f"sha256:{digest}"


def _resolve_step_args(
    args_per_step: dict[str, Any] | None,
    skill_name: SkillName,
) -> dict[str, Any]:
    """Return the per-step args dict for *skill_name*.

    Preference order: full skill name (``"/research"``), then short name
    (``"research"``). An explicit-key empty dict no longer collapses to
    the short-name fallback (an ``or`` chain dropped empty dicts because
    they are falsy).
    """
    mapping = args_per_step or {}
    short = _stop_after_short_name(skill_name)
    if skill_name in mapping:
        forwarded = mapping[skill_name]
    elif short in mapping:
        forwarded = mapping[short]
    else:
        return {}
    return dict(forwarded) if isinstance(forwarded, dict) else {}


def _payload_hash(body: Any) -> str:
    """Return ``sha256:<hex>`` of an envelope body's canonical JSON."""
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    digest = hashlib.sha256(blob).hexdigest()
    return f"sha256:{digest}"


def _current_profile_ids(state_path: Path) -> list[str]:
    """Return the sorted list of merged enabled profile ids.

    Reuses :func:`eawf.kernel.config.layered.merge_config` against the workspace
    root so the fixture-friendly cases (no config files) still produce a
    stable empty list. Any unexpected exception collapses to ``[]`` so
    drift detection sees "no profile change" rather than crashing.
    """
    try:
        from eawf.kernel.config.layered import merge_config
    except Exception as exc:  # pragma: no cover - defensive only
        logger.debug(f"_current_profile_ids import-failed exc={exc}")
        return []
    workspace_root = _workspace_root_for_state(state_path)
    try:
        merged, _sources = merge_config(repo=workspace_root, workspace=workspace_root)
    except Exception as exc:
        logger.debug(f"_current_profile_ids merge-config-raised exc={exc}")
        return []
    profiles = merged.get("profiles") if isinstance(merged, dict) else None
    if not isinstance(profiles, dict):
        return []
    enabled = profiles.get("enabled") or []
    if not isinstance(enabled, list):
        return []
    return sorted(str(p) for p in enabled if isinstance(p, str))


def compute_drift(
    checkpoint: FlowCheckpointPayload,
    state_path: Path,
    *,
    args_per_step: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Compare a checkpoint's parent_* fields against the current workspace.

    Returns ``None`` when every dimension matches (no drift). Otherwise
    returns a dict whose keys identify each dimension that drifted; the
    values carry the ``checkpoint`` and ``current`` snapshots so the
    operator can see what changed.

    Drift dimensions:

    - ``state_json``: ``sha256:<hex>`` of ``state.json`` bytes.
    - ``git_head``: 40-char ``git rev-parse HEAD`` (``None`` when not a
      git repo on either side; both sides ``None`` is treated as a match).
    - ``profile_ids``: sorted list of merged enabled profile ids.
    - ``args_per_step``: ``sha256:<hex>`` of canonical-form
      ``args_per_step`` dict.
    """
    drift: dict[str, Any] = {}

    current_state = _state_hash(state_path)
    if current_state != checkpoint.parent_state_hash:
        drift["state_json"] = {
            "checkpoint": checkpoint.parent_state_hash,
            "current": current_state,
        }

    workspace_root = _workspace_root_for_state(state_path)
    current_head = _current_git_head(workspace_root)
    if current_head is None and checkpoint.parent_git_head is None:
        logger.debug(
            "compute_drift git-head-none-both-sides; workspace not a git repo "
            "or git unavailable, skipping git drift comparison"
        )
    elif current_head != checkpoint.parent_git_head:
        drift["git_head"] = {
            "checkpoint": checkpoint.parent_git_head,
            "current": current_head,
        }

    current_profiles = _current_profile_ids(state_path)
    if current_profiles != checkpoint.parent_profile_ids:
        drift["profile_ids"] = {
            "checkpoint": checkpoint.parent_profile_ids,
            "current": current_profiles,
        }

    current_step_args = _resolve_step_args(args_per_step, checkpoint.step_name)
    current_args_hash = _canonical_args_per_step_hash(current_step_args)
    if current_args_hash != checkpoint.args_per_step_hash:
        drift["args_per_step"] = {
            "checkpoint": checkpoint.args_per_step_hash,
            "current": current_args_hash,
        }

    return drift or None


# ---- Safe-step predicate ---------------------------------------------------


def is_safe_step_boundary(step_status: str, step_name: SkillName) -> bool:
    """Return True iff a step boundary is "safe" per the spec §4.2 predicate.

    Safe = the step's terminal envelope status is ``ok`` or ``partial``.
    The conservative ``/ship`` rule (the boundary becomes safe only after
    the terminal ok envelope is fully appended) reduces to the same
    condition in the runner — by the time we read ``step_envelope``,
    the envelope IS fully appended.

    ``failed``, ``blocked``, and ``needs_user`` are never safe — those
    statuses already exit the flow per the W03 short-circuit.
    """
    # ``step_name`` reserved for future per-skill conservatism (e.g.
    # marking ``/ship`` unsafe until a ``ship.pr_pushed`` event lands).
    # v0.1 just folds the ``/ship`` rule into the general ok-or-partial
    # predicate.
    del step_name
    return step_status in {"ok", "partial"}


# ---- Skill registration ----------------------------------------------------


_FLOW_NEXT_ACTIONS: tuple[str, ...] = ("eawf flow status", "eawf audit")


def _terminal_flow_status(terminal_status: str) -> FlowStatus:
    """Map a terminal envelope status onto a :class:`FlowStatus` for the record.

    ``ok`` / ``partial`` → DONE, ``needs_user`` → PAUSED, everything else
    (``blocked`` / ``failed`` / unrecognised) → BLOCKED so the operator
    always sees a non-success terminal record.
    """
    if terminal_status in {"ok", "partial"}:
        return FlowStatus.DONE
    if terminal_status == "needs_user":
        return FlowStatus.PAUSED
    return FlowStatus.BLOCKED


@dataclass
class _FlowInputs:
    """Resolved ``/flow`` inputs gathered before the pipeline runs.

    Attributes:
        topic: The free-form topic recorded on the body, or ``None``.
        stop_after: The normalised stop-after short name, or ``None``.
        args_per_step: The per-step args mapping.
        resume_from: The validated resume checkpoint, or ``None``.
        resume_from_id: The resumed checkpoint's envelope id, or ``None``.
        flow_id: The flow id (caller-supplied or freshly minted).
        start_index: The first step index to run (resume tail offset).
        caps: The parsed ``--caps`` spend ceilings keyed by axis (empty when
            uncapped); enforcement stays honest-empty until EU-capture lands.
        max_repair_cycles: The resolved ``--max-repair-cycles`` ceiling
            (default 3 via ``flow.max_repair_cycles``).
        warnings: Advisory warnings folded into the envelope footer.
    """

    topic: Any
    stop_after: str | None
    args_per_step: dict[str, Any]
    resume_from: FlowCheckpointPayload | None
    resume_from_id: str | None
    flow_id: str
    start_index: int
    caps: dict[str, float] = field(default_factory=dict)
    max_repair_cycles: int = _DEFAULT_MAX_REPAIR_CYCLES
    advance_after: dict[str, bool] = field(default_factory=dict)
    warnings: list[EnvelopeWarning] = field(default_factory=list)


@dataclass
class _StepResult:
    """The per-step outcome the loop folds into the run.

    Attributes:
        status: The step envelope's terminal status.
        envelope: The step's :class:`OutputEnvelope`.
        checkpoint_id: The appended checkpoint envelope id.
        last_safe: Whether the boundary was a safe checkpoint.
    """

    status: str
    envelope: OutputEnvelope
    checkpoint_id: str
    last_safe: bool


@dataclass
class _FlowRun:
    """The accumulated result of running the flow's step loop.

    Attributes:
        steps: The serialised per-step envelopes (post-resume tail only).
        repair_commands: The failing step's repair commands, or ``None``.
        last_safe_checkpoint_id: The id of the last safe checkpoint.
    """

    steps: list[dict[str, Any]] = field(default_factory=list)
    repair_commands: list[str] | None = None
    last_safe_checkpoint_id: str | None = None
    user_question: UserQuestion | None = None


@register
class FlowSkill(SkillAction):
    """Concrete ``/flow`` skill (Phase 4 W03 + Phase 5 W02 resume).

    Runs the six core skills sequentially. Short-circuits on the first
    non-``ok`` envelope status, propagating the failing step's
    ``repair_commands`` to the flow's own footer.

    Phase 5 W02 adds:
    - Per-step ``flow_checkpoint`` envelope appended to ``flow.jsonl``.
    - Optional ``resume_from`` ctx arg replays from a safe checkpoint.
    - ``flow_record`` envelopes (start, terminal) so ``status`` /
      ``--resume`` can locate the active run.
    """

    name: SkillName = "/flow"

    # Canonical step order — exposed so tests can read the sequence
    # without re-importing the module-private tuple.
    flow_order: tuple[tuple[SkillName, type[Skill]], ...] = _CORE_FLOW_ORDER

    def _gather(self, run: ActionRun) -> _FlowInputs:
        args = run.args
        args_per_step_raw = args.get("args_per_step") or {}
        if not isinstance(args_per_step_raw, dict):
            args_per_step_raw = {}
        resume_from, resume_from_id = self._parse_resume_from(args.get("resume_from"))
        flow_id_raw = args.get("flow_id")
        flow_id = (
            flow_id_raw
            if isinstance(flow_id_raw, str) and flow_id_raw
            else f"FL-{uuid.uuid4().hex[:12]}"
        )
        warnings: list[EnvelopeWarning] = []
        return _FlowInputs(
            topic=args.get("topic"),
            stop_after=_resolve_stop_after(args.get("stop_after")),
            args_per_step=args_per_step_raw,
            resume_from=resume_from,
            resume_from_id=resume_from_id,
            flow_id=flow_id,
            start_index=0 if resume_from is None else resume_from.step_index + 1,
            caps=_parse_caps(args.get("caps"), warnings),
            max_repair_cycles=_resolve_max_repair_cycles(args, run.state_path, warnings),
            advance_after=_resolve_advance_after(args, run.state_path, warnings),
            warnings=warnings,
        )

    def _flow_policy(self, inputs: _FlowInputs) -> dict[str, Any]:
        """Build the run-level policy dict recorded on every ``flow_record``.

        Surfaces the resolved runtime options (``stop_after`` +
        ``max_repair_cycles`` always, ``caps`` when set) so the flow record is
        an honest, queryable ledger of what the run was told to do.
        """
        policy: dict[str, Any] = {}
        if inputs.stop_after is not None:
            policy["stop_after"] = inputs.stop_after
        if inputs.caps:
            policy["caps"] = dict(inputs.caps)
        policy["max_repair_cycles"] = inputs.max_repair_cycles
        return policy

    def _parse_resume_from(
        self, resume_from_raw: Any
    ) -> tuple[FlowCheckpointPayload | None, str | None]:
        """Validate the ``resume_from`` arg into a checkpoint + envelope id.

        Raises:
            UserError: when the supplied checkpoint dict fails validation
                (``kind="InvalidInput"``).
        """
        if not isinstance(resume_from_raw, dict):
            return None, None
        resume_envelope_id = resume_from_raw.get("__envelope_id__")
        ckpt_dict = {k: v for k, v in resume_from_raw.items() if k != "__envelope_id__"}
        try:
            resume_from = FlowCheckpointPayload.model_validate(ckpt_dict)
        except ValidationError as exc:
            raise cli_errors.UserError(
                f"resume_from checkpoint failed validation: {exc.errors()[0].get('msg', exc)}",
                kind="InvalidInput",
            ) from exc
        resume_from_id = resume_envelope_id if isinstance(resume_envelope_id, str) else None
        return resume_from, resume_from_id

    def _validate(self, run: ActionRun, inputs: _FlowInputs) -> SkillResult | None:
        # The only up-front guard (resume-checkpoint validation) raises in
        # ``_gather``; nothing else gates before the loop.
        return None

    def _execute(self, run: ActionRun, inputs: _FlowInputs) -> _FlowRun:
        # Re-emit canonical events so observers can distinguish a fresh run
        # from a resume.
        if inputs.resume_from is None:
            self._emit_start(run, inputs)
        else:
            self._emit_resume_start(run, inputs)
        return self._run_steps(run, inputs)

    def _emit_start(self, run: ActionRun, inputs: _FlowInputs) -> None:
        self._trace(
            run,
            "flow.start",
            f"flow: start topic={inputs.topic!r} flow_id={inputs.flow_id}",
            {
                "topic": inputs.topic,
                "stop_after": inputs.stop_after,
                "step_count": len(self.flow_order),
                "flow_id": inputs.flow_id,
                "caps": inputs.caps,
                "max_repair_cycles": inputs.max_repair_cycles,
            },
        )
        # Record the in-progress run summary so a kill leaves a trail for
        # ``--resume`` to find.
        policy = self._flow_policy(inputs)
        record_id = _emit_flow_record(
            state_path=run.state_path,
            scope_id=run.scope_id,
            flow_id=inputs.flow_id,
            goal=str(inputs.topic) if inputs.topic is not None else "",
            policy=policy,
            status=FlowStatus.IN_PROGRESS,
            last_safe_checkpoint=None,
            next_action="eawf flow status",
        )
        run.records.append(record_id)

    def _emit_resume_start(self, run: ActionRun, inputs: _FlowInputs) -> None:
        assert inputs.resume_from is not None
        self._trace(
            run,
            "flow.resume_start",
            (
                f"flow: resume_start flow_id={inputs.flow_id} "
                f"from step_index={inputs.resume_from.step_index}"
            ),
            {
                "flow_id": inputs.flow_id,
                "resume_from_step_index": inputs.resume_from.step_index,
                "resume_from_step_name": inputs.resume_from.step_name,
                "resume_from_checkpoint_id": inputs.resume_from_id,
            },
        )

    def _run_steps(self, run: ActionRun, inputs: _FlowInputs) -> _FlowRun:
        flow_run = _FlowRun(last_safe_checkpoint_id=inputs.resume_from_id)
        for idx, (skill_name, skill_cls) in enumerate(self.flow_order):
            if idx < inputs.start_index:
                # Skipped step (resume prefix). ``body.steps`` reflects the
                # post-resume tail only; the resumed checkpoint id is recorded
                # separately on ``body.resume_from_checkpoint_id`` so consumers
                # can still tie the tail back to the original prefix.
                continue
            step = self._run_one_step(run, inputs, idx, skill_name, skill_cls)
            flow_run.steps.append(step.envelope.model_dump(mode="json"))
            if step.last_safe:
                flow_run.last_safe_checkpoint_id = step.checkpoint_id
            # Short-circuit on first non-ok status.
            if step.status != "ok":
                flow_run.repair_commands = list(step.envelope.footer.repair_commands or [])
                self._trace(
                    run,
                    "flow.short_circuit",
                    f"flow: short-circuit on {skill_name} ({step.status})",
                    {"skill": skill_name, "status": step.status, "flow_id": inputs.flow_id},
                )
                break
            # ``stop_after`` honours the §14 flag.
            if inputs.stop_after is not None and inputs.stop_after == _stop_after_short_name(
                skill_name
            ):
                self._trace(
                    run,
                    "flow.stop_after",
                    f"flow: stop-after {inputs.stop_after}",
                    {"stop_after": inputs.stop_after, "flow_id": inputs.flow_id},
                )
                break
            stage = _stop_after_short_name(skill_name)
            if (
                idx < len(self.flow_order) - 1
                and stage in inputs.advance_after
                and not inputs.advance_after[stage]
            ):
                next_skill = self.flow_order[idx + 1][0]
                flow_run.user_question = UserQuestion(
                    question=f"{skill_name} completed. Advance to {next_skill}?",
                    options=[
                        UserQuestionOption(
                            label="continue",
                            description="Resume from this safe checkpoint and run the next stage.",
                        ),
                        UserQuestionOption(
                            label="defer",
                            description="Leave the flow paused for a later resume.",
                        ),
                    ],
                )
                self._trace(
                    run,
                    "flow.transition_pause",
                    f"flow: pause after {skill_name} before {next_skill}",
                    {
                        "flow_id": inputs.flow_id,
                        "completed_skill": skill_name,
                        "next_skill": next_skill,
                    },
                )
                break
        return flow_run

    def _run_one_step(
        self,
        run: ActionRun,
        inputs: _FlowInputs,
        idx: int,
        skill_name: SkillName,
        skill_cls: type[Skill],
    ) -> _StepResult:
        # Build a per-step context using explicit-key precedence so an
        # empty-dict args entry isn't silently dropped to the short-name
        # fallback (the ``or`` chain it replaced did so because empty dicts
        # are falsy).
        step_args = _resolve_step_args(inputs.args_per_step, skill_name)
        step_ctx = SkillContext(
            scope=run.ctx.scope,
            session=run.ctx.session,
            instrument_probe=dict(run.ctx.instrument_probe),
            args=step_args,
            failure_repair_commands=run.ctx.failure_repair_commands,
        )
        # Snapshot drift sentinels BEFORE running the step. Hash the per-step
        # args dict (not the whole multi-step mapping) so the checkpoint
        # records which args this step actually consumed; ``compute_drift``
        # mirrors this on resume.
        workspace_root = _workspace_root_for_state(run.state_path)
        parent_state_hash = _state_hash(run.state_path)
        parent_git_head = _current_git_head(workspace_root)
        parent_profile_ids = _current_profile_ids(run.state_path)
        args_per_step_hash = _canonical_args_per_step_hash(step_args)
        started_at = datetime.now(UTC)

        self._trace(
            run,
            "flow.step_start",
            f"flow: step {skill_name} starting",
            {"skill": skill_name, "flow_id": inputs.flow_id, "step_index": idx},
        )
        step_envelope: OutputEnvelope = run_skill(skill_cls(), step_ctx)
        step_status = step_envelope.header.status
        run.records.extend(step_envelope.footer.persisted_store_records)
        run.mutations.extend(step_envelope.footer.state_mutations)
        run.evidence.extend(step_envelope.footer.evidence_refs)
        self._trace(
            run,
            "flow.step_end",
            f"flow: step {skill_name} -> {step_status}",
            {
                "skill": skill_name,
                "status": step_status,
                "flow_id": inputs.flow_id,
                "step_index": idx,
            },
        )
        # Append the per-step checkpoint.
        completed_at = datetime.now(UTC)
        last_safe = is_safe_step_boundary(step_status, skill_name)
        checkpoint_id = _emit_checkpoint(
            state_path=run.state_path,
            scope_id=run.scope_id,
            flow_id=inputs.flow_id,
            step_index=idx,
            step_name=skill_name,
            started_at=started_at,
            completed_at=completed_at,
            last_safe=last_safe,
            payload_hash=_payload_hash(step_envelope.body),
            parent_state_hash=parent_state_hash,
            parent_git_head=parent_git_head,
            parent_profile_ids=parent_profile_ids,
            args_per_step_hash=args_per_step_hash,
        )
        run.records.append(checkpoint_id)
        return _StepResult(
            status=step_status,
            envelope=step_envelope,
            checkpoint_id=checkpoint_id,
            last_safe=last_safe,
        )

    def _render(self, run: ActionRun, inputs: _FlowInputs, outcome: _FlowRun) -> SkillResult:
        terminal_status = (
            "needs_user"
            if outcome.user_question is not None
            else short_circuit_terminal_status([s["header"]["status"] for s in outcome.steps])
        )
        evt_type = "flow.resume_end" if inputs.resume_from is not None else "flow.end"
        self._trace(
            run,
            evt_type,
            f"flow: end terminal_status={terminal_status} flow_id={inputs.flow_id}",
            {
                "terminal_status": terminal_status,
                "steps_run": len(outcome.steps),
                "flow_id": inputs.flow_id,
            },
        )
        terminal_record_id = _emit_flow_record(
            state_path=run.state_path,
            scope_id=run.scope_id,
            flow_id=inputs.flow_id,
            goal=str(inputs.topic) if inputs.topic is not None else "",
            policy=self._flow_policy(inputs),
            status=_terminal_flow_status(terminal_status),
            last_safe_checkpoint=outcome.last_safe_checkpoint_id,
            next_action=None,
        )
        run.records.append(terminal_record_id)
        body = FlowBody(
            topic=str(inputs.topic) if inputs.topic is not None else None,
            steps=outcome.steps,
            terminal_status=terminal_status,
            user_question=outcome.user_question,
            resume_from_checkpoint_id=inputs.resume_from_id,
            drift=None,
        )
        return SkillResult(
            status=terminal_status,  # type: ignore[arg-type]
            body=body.model_dump(mode="json"),
            persisted_store_records=run.records,
            state_mutations=run.mutations,
            evidence_refs=run.evidence,
            next_valid_actions=list(_FLOW_NEXT_ACTIONS),
            warnings=inputs.warnings,
            repair_commands=outcome.repair_commands,
        )


__all__ = [
    "FlowSkill",
    "abort_flow_record",
    "compute_drift",
    "in_progress_flow_ids",
    "is_safe_step_boundary",
    "latest_active_flow_id",
    "load_flow_records",
    "load_latest_records_per_flow",
    "load_latest_safe_checkpoint",
    "short_circuit_terminal_status",
]
