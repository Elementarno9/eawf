"""``/flow`` skill — composite controller running the six core skills in order.

Per `docs/architecture/workflow.md` ``/flow`` drives a one-click ADD iteration:
research → prep → execute (includes audit) → ship. ``/flow`` runs all six
core skills sequentially
(research → prep → audit → ship → review → polish), accumulating per-step
envelopes under :attr:`FlowBody.steps`.

Short-circuit semantics (the W03 acceptance contract):

- The flow runs each core skill in order.
- After each step the flow inspects ``env.header.status``. Anything other
  than ``ok`` (``needs_user``, ``blocked``, ``failed``, ``partial``)
  triggers an immediate short-circuit. The flow's terminal envelope
  inherits the failing step's ``status`` and ``footer.repair_commands``.
- If every step returns ``ok``, the flow's terminal envelope is ``ok``
  and the body's ``terminal_status`` mirrors the last step's status.

The flow does **not** literally call :func:`eawf.skills.engine.run_skill`
on each subskill — instead it constructs a fresh :class:`SkillContext`
copy and routes through the engine so the per-step envelope is fully
populated (header status, instrument probe, footer mutations). This
keeps the short-circuit decision focused on the canonical envelope
shape rather than the action-side return type.

Honoured ``ctx.args`` keys:

- ``topic`` — free-form description recorded on :attr:`FlowBody.topic`.
- ``stop_after`` — short-circuit before the named step (matches §14's
  ``--stop-after`` flag). v0.1 honours the canonical names
  ``research|prep|audit|ship|review|polish``.
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

import orjson
from pydantic import ValidationError

from eawf.cli import errors as cli_errors
from eawf.render.envelope import OutputEnvelope, SkillName
from eawf.skills.audit import AuditSkill
from eawf.skills.bodies.flow import FlowBody
from eawf.skills.engine import (
    ActionRun,
    Skill,
    SkillAction,
    SkillContext,
    SkillResult,
    run_skill,
)
from eawf.skills.polish import PolishSkill
from eawf.skills.prep import PrepSkill
from eawf.skills.registry import register
from eawf.skills.research import ResearchSkill
from eawf.skills.review import ReviewSkill
from eawf.skills.ship import ShipSkill
from eawf.state.enums import FlowStatus, StoreKind
from eawf.store.append import append_envelope
from eawf.store.envelope import Envelope
from eawf.store.kinds.flow import FlowCheckpointPayload, FlowPayload
from eawf.store.paths import store_path

logger = logging.getLogger(__name__)


# Canonical core-skill order for ``/flow`` per §14 + v0.1 plan §4 W03.
# The list lives at module level so tests can iterate it without copying
# the order from the docstring.
_CORE_FLOW_ORDER: tuple[tuple[SkillName, type[Skill]], ...] = (
    ("/research", ResearchSkill),
    ("/prep", PrepSkill),
    ("/audit", AuditSkill),
    ("/ship", ShipSkill),
    ("/review", ReviewSkill),
    ("/polish", PolishSkill),
)


# Hard cap per git invocation. ``flow`` is operator-driven so a 5 s
# budget is well above the happy path while still bounding a hung daemon.
_GIT_TIMEOUT_SECONDS: float = 5.0


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
    :mod:`eawf.runtimes.claude.statusline_modules.git`: any non-zero exit,
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

    Reuses :func:`eawf.config.layered.merge_config` against the workspace
    root so the fixture-friendly cases (no config files) still produce a
    stable empty list. Any unexpected exception collapses to ``[]`` so
    drift detection sees "no profile change" rather than crashing.
    """
    try:
        from eawf.config.layered import merge_config
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


# ---- Checkpoint emission ---------------------------------------------------


def _flow_jsonl_path(state_path: Path) -> Path:
    """Return ``<state>/store/flow.jsonl``."""
    return store_path(state_path, StoreKind.FLOW)


def _new_envelope_id(prefix: str = "EV") -> str:
    """Mint a fresh ``<prefix>-<uuid12>`` envelope id."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _emit_flow_record(
    *,
    state_path: Path,
    scope_id: str,
    flow_id: str,
    goal: str,
    policy: dict[str, Any],
    status: FlowStatus,
    last_safe_checkpoint: str | None,
    next_action: str | None,
) -> str:
    """Append a :class:`FlowPayload` (``flow_record``) envelope.

    Returns the freshly minted envelope id so the caller can fold it
    into ``persisted_store_records``.
    """
    payload = FlowPayload(
        flow_id=flow_id,
        goal=goal,
        policy=policy,
        last_safe_checkpoint=last_safe_checkpoint,
        next_action=next_action,
        status=status,
    )
    envelope_id = _new_envelope_id()
    envelope = Envelope(
        schema_version="1.0",
        id=envelope_id,
        kind=StoreKind.FLOW,
        scope_id=scope_id,
        created_at=datetime.now(UTC),
        updated_at=None,
        summary=f"flow: {flow_id} status={status.value}",
        payload=payload.model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(_flow_jsonl_path(state_path), envelope)
    return envelope_id


def abort_flow_record(
    state_path: Path,
    *,
    scope_id: str,
    previous: FlowPayload,
    reason: str | None = None,
) -> tuple[str, FlowPayload]:
    """Append an ``abandoned`` flow_record envelope.

    Builds a :class:`FlowPayload` derived from *previous* with
    ``status = ABANDONED`` (and ``policy['abort_reason'] = reason``
    when supplied), wraps it in an :class:`Envelope`, and appends to
    the flow JSONL via :func:`append_envelope`.

    Returns ``(envelope_id, new_payload)`` so the caller can surface
    the new status and envelope id without re-reading the store.
    """
    policy: dict[str, Any] = dict(previous.policy)
    if reason is not None:
        policy["abort_reason"] = reason
    new_payload = FlowPayload(
        flow_id=previous.flow_id,
        goal=previous.goal,
        policy=policy,
        last_safe_checkpoint=previous.last_safe_checkpoint,
        next_action=None,
        status=FlowStatus.ABANDONED,
    )
    envelope_id = _new_envelope_id()
    envelope = Envelope(
        schema_version="1.0",
        id=envelope_id,
        kind=StoreKind.FLOW,
        scope_id=scope_id,
        created_at=datetime.now(UTC),
        updated_at=None,
        summary=(f"flow: {previous.flow_id} abort previous={previous.status.value}"),
        payload=new_payload.model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(_flow_jsonl_path(state_path), envelope)
    return envelope_id, new_payload


def _emit_checkpoint(
    *,
    state_path: Path,
    scope_id: str,
    flow_id: str,
    step_index: int,
    step_name: SkillName,
    started_at: datetime,
    completed_at: datetime,
    last_safe: bool,
    payload_hash: str,
    parent_state_hash: str,
    parent_git_head: str | None,
    parent_profile_ids: list[str],
    args_per_step_hash: str,
) -> str:
    """Append a :class:`FlowCheckpointPayload` envelope.

    Returns the freshly minted envelope id (``EV-...``). The append
    routes through :func:`eawf.store.append.append_envelope`, so the
    line is fsynced before this function returns.
    """
    payload = FlowCheckpointPayload(
        flow_id=flow_id,
        step_index=step_index,
        step_name=step_name,
        started_at=started_at,
        completed_at=completed_at,
        last_safe=last_safe,
        payload_hash=payload_hash,
        parent_state_hash=parent_state_hash,
        parent_git_head=parent_git_head,
        parent_profile_ids=parent_profile_ids,
        args_per_step_hash=args_per_step_hash,
    )
    envelope_id = _new_envelope_id()
    envelope = Envelope(
        schema_version="1.0",
        id=envelope_id,
        kind=StoreKind.FLOW,
        scope_id=scope_id,
        created_at=completed_at,
        updated_at=None,
        summary=f"flow: {flow_id} checkpoint step_index={step_index} {step_name}",
        payload=payload.model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(_flow_jsonl_path(state_path), envelope)
    return envelope_id


# ---- flow.jsonl readers (read-only) ----------------------------------------


def load_flow_records(state_path: Path) -> list[tuple[str, dict[str, Any]]]:
    """Stream-parse ``flow.jsonl`` and return ``(envelope_id, payload)`` pairs.

    Records are returned in append order (oldest first). Malformed lines
    (orjson decode failure or non-flow kind) are skipped with a debug
    log so a partially-corrupted file still surfaces the parseable
    suffix.
    """
    path = _flow_jsonl_path(state_path)
    if not path.exists():
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    raw = path.read_text(encoding="utf-8")
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            decoded = orjson.loads(line)
        except orjson.JSONDecodeError as exc:
            logger.debug(f"load_flow_records skipping-malformed-line exc={exc}")
            continue
        if not isinstance(decoded, dict):
            continue
        envelope_id = decoded.get("id")
        payload = decoded.get("payload")
        if not isinstance(envelope_id, str) or not isinstance(payload, dict):
            continue
        out.append((envelope_id, payload))
    return out


def load_latest_records_per_flow(state_path: Path) -> dict[str, FlowPayload]:
    """Return a ``{flow_id: latest FlowPayload}`` mapping.

    Discriminator-aware: only ``kind == "flow_record"`` lines contribute
    to the mapping. A flow with no ``flow_record`` lines (only
    checkpoints) does not appear in the mapping.
    """
    out: dict[str, FlowPayload] = {}
    for _envelope_id, payload in load_flow_records(state_path):
        if payload.get("kind") != "flow_record":
            continue
        try:
            record = FlowPayload.model_validate(payload)
        except Exception as exc:
            logger.debug(f"load_latest_records_per_flow validation-failed exc={exc}")
            continue
        out[record.flow_id] = record
    return out


def load_latest_safe_checkpoint(
    state_path: Path,
    flow_id: str,
) -> tuple[str, FlowCheckpointPayload] | None:
    """Return the latest ``(envelope_id, payload)`` with ``last_safe=True``.

    Returns ``None`` when the flow has no safe checkpoints — the runner
    treats that as "nothing to resume to" and refuses with an
    ``INTEGRITY_VIOLATION`` exit code.
    """
    safe: tuple[str, FlowCheckpointPayload] | None = None
    for envelope_id, payload in load_flow_records(state_path):
        if payload.get("kind") != "flow_checkpoint":
            continue
        if payload.get("flow_id") != flow_id:
            continue
        try:
            ckpt = FlowCheckpointPayload.model_validate(payload)
        except Exception as exc:
            logger.debug(f"load_latest_safe_checkpoint validation-failed exc={exc}")
            continue
        if ckpt.last_safe:
            safe = (envelope_id, ckpt)
    return safe


def in_progress_flow_ids(state_path: Path) -> list[str]:
    """Return the ids of flows whose latest record status is ``in_progress``."""
    latest = load_latest_records_per_flow(state_path)
    return [fid for fid, record in latest.items() if record.status == FlowStatus.IN_PROGRESS]


def latest_active_flow_id(state_path: Path) -> str | None:
    """Return the flow_id of the most recently-appended ``flow_record`` envelope.

    Append order in ``flow.jsonl`` reflects chronological order (each
    append is single-writer), so the last seen ``flow_record`` flow_id
    is the most recently-active flow. Used by ``eawf flow status`` to
    pick a deterministic default when the operator did not pass
    ``--flow-id`` and no flow is in-progress.
    """
    out: str | None = None
    for _envelope_id, payload in load_flow_records(state_path):
        if payload.get("kind") != "flow_record":
            continue
        flow_id = payload.get("flow_id")
        if isinstance(flow_id, str):
            out = flow_id
    return out


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
    """

    topic: Any
    stop_after: str | None
    args_per_step: dict[str, Any]
    resume_from: FlowCheckpointPayload | None
    resume_from_id: str | None
    flow_id: str
    start_index: int


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
        return _FlowInputs(
            topic=args.get("topic"),
            stop_after=_resolve_stop_after(args.get("stop_after")),
            args_per_step=args_per_step_raw,
            resume_from=resume_from,
            resume_from_id=resume_from_id,
            flow_id=flow_id,
            start_index=0 if resume_from is None else resume_from.step_index + 1,
        )

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
            },
        )
        # Record the in-progress run summary so a kill leaves a trail for
        # ``--resume`` to find.
        policy: dict[str, Any] = {}
        if inputs.stop_after is not None:
            policy["stop_after"] = inputs.stop_after
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
        terminal_status = short_circuit_terminal_status(
            [s["header"]["status"] for s in outcome.steps]
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
            policy={"stop_after": inputs.stop_after} if inputs.stop_after is not None else {},
            status=_terminal_flow_status(terminal_status),
            last_safe_checkpoint=outcome.last_safe_checkpoint_id,
            next_action=None,
        )
        run.records.append(terminal_record_id)
        body = FlowBody(
            topic=str(inputs.topic) if inputs.topic is not None else None,
            steps=outcome.steps,
            terminal_status=terminal_status,
            user_question=None,
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
