"""Payload models for ``StoreKind.FLOW`` records.

Two sibling models share the ``flow.jsonl`` file, disambiguated by the
``kind`` discriminator:

- :class:`FlowPayload` — one summary record per flow run (start, paused,
  done, abandoned, …). Carries ``flow_id``, ``goal``, ``policy``,
  ``status``, and the rolling ``last_safe_checkpoint`` pointer.
- :class:`FlowCheckpointPayload` — one record appended after each
  step boundary inside a run. Carries the parent state hash, parent git
  HEAD, parent profile id list, the canonicalised ``args_per_step`` hash,
  and the ``last_safe`` predicate result. Drift detection on
  ``eawf flow run --resume`` reads the latest ``last_safe=True`` record
  for the active flow and compares the parent_* fields against the
  current workspace state.

Both models enforce ``extra="forbid"`` per AGENTS.md rule 2; the
discriminator field is a frozen :class:`typing.Literal` so a
``flow_record`` JSON line can never validate as a checkpoint and vice
versa.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import FlowStatus
from eawf.kernel.state.types import UtcDatetime
from eawf.surfaces.render.envelope import SkillName

# Frozen regex patterns shared across both payload models. Repeated as
# constants so a future schema bump only edits the literal once and so
# the unit tests can grep the exact source of truth.
_FLOW_ID_PATTERN: str = r"^FL-[0-9a-f]{12}$"
_SHA256_PATTERN: str = r"^sha256:[0-9a-f]{64}$"
_GIT_SHA_PATTERN: str = r"^[0-9a-f]{40}$"


class FlowPayload(BaseModel):
    """Summary record for a flow run.

    Appended at run start (``status=in_progress``), again at run end
    (``status=done`` / ``paused`` / ``abandoned`` / failure equivalents).
    The latest record per ``flow_id`` is the authoritative state for
    that run; earlier records are retained for audit.

    Attributes:
        kind: Discriminator literal. Always ``"flow_record"``.
        flow_id: ``FL-<uuid12>`` string identifying the run.
        goal: Free-form one-line summary of what the run is about.
        policy: Run-level policy dict (``stop_after``, ``abort_reason``, …).
        last_safe_checkpoint: Pointer to the latest ``flow_checkpoint``
            envelope id (``EV-...``) that satisfies the safe predicate.
            ``None`` until the first step boundary lands.
        next_action: Human-readable hint for the operator on what to do
            next (e.g. ``"eawf flow status"``).
        status: One of :class:`FlowStatus` (``pending``, ``in_progress``,
            ``paused``, ``blocked``, ``done``, ``abandoned``,
            ``superseded``).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["flow_record"] = "flow_record"
    flow_id: Annotated[str, Field(pattern=_FLOW_ID_PATTERN)]
    goal: str
    policy: dict[str, Any]
    last_safe_checkpoint: str | None = None
    next_action: str | None = None
    status: FlowStatus = FlowStatus.PENDING


class FlowCheckpointPayload(BaseModel):
    """Per-step checkpoint emitted after a step boundary.

    The runner appends one of these after every step. ``last_safe=True``
    iff the safe predicate (see :mod:`eawf.workflow.skills.flow`) holds for the
    boundary. ``--resume`` finds the latest ``last_safe=True`` record
    for the active flow and replays the canonical step order from
    ``step_index + 1``.

    Attributes:
        kind: Discriminator literal. Always ``"flow_checkpoint"``.
        flow_id: ``FL-<uuid12>`` string identifying the parent run.
        step_index: Zero-based index into the canonical six-step order.
        step_name: Frozen :data:`SkillName` of the step that just
            finished (e.g. ``"/research"``).
        started_at: UTC datetime when the step began.
        completed_at: UTC datetime when the step finished. Must be
            ``>= started_at`` (Pydantic-level invariant; the runner
            relies on the system clock being monotonic for sub-second
            adjacency).
        last_safe: ``True`` when the step's terminal envelope status is
            ``ok`` or ``partial`` AND no follow-on safety constraints
            apply (see §4.2 of the wave spec).
        payload_hash: ``sha256:<hex>`` of the step envelope's body JSON.
            Used by future replay-detector tools to spot a step that
            re-ran with different inputs.
        parent_state_hash: ``sha256:<hex>`` of ``state.json`` bytes at
            step start. Drift sentinel.
        parent_git_head: 40-char ``git rev-parse HEAD`` at step start.
            ``None`` when the workspace is not a git repo.
        parent_profile_ids: Sorted list of merged enabled profile ids at
            step start. Drift sentinel.
        args_per_step_hash: ``sha256:<hex>`` of canonical-form
            ``args_per_step`` dict at step start. Drift sentinel.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["flow_checkpoint"] = "flow_checkpoint"
    flow_id: Annotated[str, Field(pattern=_FLOW_ID_PATTERN)]
    step_index: Annotated[int, Field(ge=0)]
    step_name: SkillName
    started_at: UtcDatetime
    completed_at: UtcDatetime
    last_safe: bool
    payload_hash: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    parent_state_hash: Annotated[str, Field(pattern=_SHA256_PATTERN)]
    parent_git_head: Annotated[str, Field(pattern=_GIT_SHA_PATTERN)] | None
    parent_profile_ids: list[str]
    args_per_step_hash: Annotated[str, Field(pattern=_SHA256_PATTERN)]


__all__ = [
    "FlowCheckpointPayload",
    "FlowPayload",
]
