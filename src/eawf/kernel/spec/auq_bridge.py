"""Cross-runtime AUQ (ask-user-question) bridge + multi-wave frontier drain.

Two pure halves that turn a ``needs_user`` signal into an operator
decision and walk a dependency frontier surfacing one question per wave
that needs confirmation. Both are vendor-neutral and pure (no I/O, no
spawn, no state mutation) -- they mirror the injected-seam discipline the
rest of :mod:`eawf.kernel.spec` follows.

The cross-runtime AUQ bridge -- the projection
----------------------------------------------
:class:`AUQRequest` is the single vendor-neutral ask-user-question: a
question, an optional header, an :class:`~eawf.kernel.state.enums.Urgency`,
and 2..4 typed :class:`AUQOption` rows (the project-wide AskUserQuestion-only
approval policy fixes the floor at two options -- a single-option "question"
is not a choice -- and the four-option cap keeps the prompt scannable). The
request PROJECTS to each runtime's native confirm shape via
:meth:`AUQRequest.project_for_runtime`, mirroring how the dispatch envelope
/ role contract projects per-runtime rather than carrying one runtime's
shape as the canonical one. The three projection targets are the canonical
runtime ids (:data:`~eawf.runtime.runtimes.manifest.RuntimeId`):

* ``claude-code`` -> :class:`ClaudeAUQProjection`: an ``AskUserQuestion``
  multi-option prompt (a ``question`` + header + a list of ``{label,
  description}`` options). Claude renders this as its native single-select
  question card -- the project's canonical approval surface.
* ``codex`` -> :class:`CodexAUQProjection`: ``codex exec`` has no native
  multi-select widget, so the defensible mapping is a numbered text prompt
  -- the question followed by enumerated ``1) ...`` / ``2) ...`` option
  lines, the operator replying with the option key. This generalizes the
  ``y/N`` confirm convention to an N-way choice.
* ``opencode`` -> :class:`OpenCodeAUQProjection`: ``opencode run`` likewise
  has no native select, so the mapping is a bracket-keyed text prompt -- the
  question followed by ``[key] label`` option lines, the operator replying
  with the bracket key.

The operator's selection parses back through :meth:`AUQRequest.parse_answer`
into a typed :class:`AUQAnswer` (the selected option key, validated against
the request's own option set). A ``needs_user`` outcome from the jury
reducer (:class:`~eawf.observability.eval.jury.JuryAggregate`) or the
EviBound rung-3 convener (:class:`~eawf.workflow.evidence.rung3.Rung3Outcome`)
converts into an :class:`AUQRequest` via :func:`needs_user_to_auq` so an
unresolvable vote routes straight to the operator-pause surface instead of
falling through as a silent pass.

The multi-wave frontier drain -- the driver
-------------------------------------------
:func:`compute_ready_frontier` reduces a typed read-only view of the wave
graph (:class:`WaveFrontierItem` rows -- id, status, deps, iter) into the
dependency frontier: the PENDING waves whose deps are all CLOSED and that
are not blocked by a lower-numbered ready sibling under the same iter. The
predicate mirrors the claim-time gate
(:func:`eawf.workflow.lifecycle.wave.claim_wave`) but is computed purely off
the injected view -- it never constructs or mutates a
:class:`~eawf.kernel.state.models.State`. :func:`drain_frontier` is the
driver: it walks the ready frontier in claim order and, for each wave a
caller-injected predicate marks as needing operator confirmation, yields one
:class:`FrontierDrainStep` carrying that wave's :class:`AUQRequest`. The
confirmation predicate is injected (:data:`NeedsConfirmationFn`) so the
driver spawns nothing and a test drives it with a recording stub.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.state.enums import Urgency, WaveStatus
from eawf.kernel.state.ids import natural_key
from eawf.observability.eval.jury import JuryAggregate, JuryAggregateOutcome
from eawf.runtime.runtimes.manifest import RuntimeId
from eawf.workflow.evidence.rung3 import Rung3Outcome

logger = logging.getLogger(__name__)

#: Minimum number of options an :class:`AUQRequest` carries. The
#: project-wide AskUserQuestion-only approval policy fixes the floor at two:
#: a single-option "question" is a notice, not a choice, so the operator
#: surface always offers at least an accept / reject pair.
MIN_AUQ_OPTIONS: int = 2

#: Maximum number of options an :class:`AUQRequest` carries. The cap keeps
#: the projected prompt scannable in a single screen across all three native
#: confirm surfaces; a decision needing more than four branches is
#: decomposed into successive questions rather than one wide prompt.
MAX_AUQ_OPTIONS: int = 4


class AUQOption(BaseModel):
    """One selectable option on an :class:`AUQRequest`.

    Attributes:
        key: Stable short token the operator's selection is keyed by (e.g.
            ``"approve"`` / ``"reject"``). Non-empty, bounded so it stays a
            scannable token rather than a sentence; unique within a request
            (enforced by :meth:`AUQRequest._options_well_formed`).
        label: The one-line option label shown on the native confirm
            surface. Non-empty, bounded to one scannable line.
        description: Optional longer explanation rendered under the label on
            surfaces that support a secondary line (claude's
            ``AskUserQuestion`` card); ``None`` omits it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(min_length=1, max_length=48)
    label: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=280)


class AUQRequest(BaseModel):
    """A vendor-neutral ask-user-question routed to a runtime's confirm UI.

    The single typed request the bridge produces. It carries the question,
    an optional header, the :class:`~eawf.kernel.state.enums.Urgency` ranking
    how soon the operator must answer, and 2..4 typed :class:`AUQOption`
    rows. :meth:`project_for_runtime` renders it into each runtime's native
    confirm shape and :meth:`parse_answer` validates the operator's selection
    back into a typed :class:`AUQAnswer`.

    Attributes:
        question: The question text put to the operator. Non-empty, bounded
            so it stays a scannable prompt.
        options: The 2..4 selectable :class:`AUQOption` rows (the
            :data:`MIN_AUQ_OPTIONS` / :data:`MAX_AUQ_OPTIONS` bounds encode
            the AskUserQuestion-only floor + the scannable cap). Option keys
            are unique within the request.
        header: Optional short header / category shown above the question on
            surfaces that support one (claude's ``AskUserQuestion`` header);
            ``None`` omits it.
        urgency: The shared :class:`~eawf.kernel.state.enums.Urgency` ladder
            ranking how soon the operator must answer. Defaults to
            :attr:`~eawf.kernel.state.enums.Urgency.NORMAL`; a needs_user
            conversion raises it to :attr:`~eawf.kernel.state.enums.Urgency.URGENT`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str = Field(min_length=1, max_length=500)
    options: tuple[AUQOption, ...] = Field(min_length=MIN_AUQ_OPTIONS, max_length=MAX_AUQ_OPTIONS)
    header: str | None = Field(default=None, max_length=120)
    urgency: Urgency = Urgency.NORMAL

    @model_validator(mode="after")
    def _options_well_formed(self) -> AUQRequest:
        """Reject duplicate option keys within the request.

        The 2..4 count is enforced by the ``min_length`` / ``max_length``
        bounds on :attr:`options`; this validator adds the uniqueness
        invariant the bounds cannot express, so :meth:`parse_answer` can
        resolve a selected key to exactly one option.

        Returns:
            The validated request (unchanged).

        Raises:
            ValueError: when two options share a ``key``.
        """
        keys = [opt.key for opt in self.options]
        if len(set(keys)) != len(keys):
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            raise ValueError(f"duplicate option keys: {dupes}")
        return self

    @property
    def option_keys(self) -> tuple[str, ...]:
        """The option keys, in declared order."""
        return tuple(opt.key for opt in self.options)

    def project_for_runtime(self, runtime_id: str) -> RuntimeAUQProjection:
        """Render the request into ``runtime_id``'s native confirm shape.

        Dispatches on the canonical runtime id to the per-runtime projection
        (claude ``AskUserQuestion`` card / codex numbered prompt / opencode
        bracket-keyed prompt). The request itself is runtime-neutral; this
        method is the single seam that knows each runtime's confirm
        convention, mirroring how the dispatch envelope projects per-runtime.

        Args:
            runtime_id: Canonical runtime id -- one of ``claude-code`` /
                ``codex`` / ``opencode``.

        Returns:
            The per-runtime projection: :class:`ClaudeAUQProjection`,
            :class:`CodexAUQProjection`, or :class:`OpenCodeAUQProjection`.

        Raises:
            ValueError: when *runtime_id* is not one of the three canonical
                runtime ids.
        """
        if runtime_id == "claude-code":
            return self._project_claude()
        if runtime_id == "codex":
            return self._project_codex()
        if runtime_id == "opencode":
            return self._project_opencode()
        raise ValueError(f"unknown runtime: {runtime_id!r}")

    def _project_claude(self) -> ClaudeAUQProjection:
        """Project onto claude-code's ``AskUserQuestion`` multi-option card."""
        options = tuple(
            ClaudeAUQOption(label=opt.label, description=opt.description or opt.label)
            for opt in self.options
        )
        return ClaudeAUQProjection(
            question=self.question,
            header=self.header or "Confirm",
            options=options,
        )

    def _project_codex(self) -> CodexAUQProjection:
        """Project onto a codex numbered text prompt."""
        lines = [self.question, ""]
        choices: list[CodexNumberedChoice] = []
        for index, opt in enumerate(self.options, start=1):
            suffix = f" -- {opt.description}" if opt.description else ""
            lines.append(f"{index}) {opt.label}{suffix} [{opt.key}]")
            choices.append(CodexNumberedChoice(number=index, key=opt.key, label=opt.label))
        lines.append("")
        lines.append("Reply with the option number or its key.")
        return CodexAUQProjection(prompt="\n".join(lines), choices=tuple(choices))

    def _project_opencode(self) -> OpenCodeAUQProjection:
        """Project onto an opencode bracket-keyed text prompt."""
        lines = [self.question, ""]
        for opt in self.options:
            suffix = f" -- {opt.description}" if opt.description else ""
            lines.append(f"[{opt.key}] {opt.label}{suffix}")
        lines.append("")
        lines.append("Reply with the bracket key.")
        return OpenCodeAUQProjection(prompt="\n".join(lines), keys=self.option_keys)

    def parse_answer(self, selected_key: str) -> AUQAnswer:
        """Parse the operator's selection into a typed :class:`AUQAnswer`.

        Validates *selected_key* against this request's own option set so a
        stray key (a typo, a stale option from a re-rendered prompt) fails
        fast rather than silently selecting nothing.

        Args:
            selected_key: The option key the operator chose (the ``key`` of
                one :class:`AUQOption`).

        Returns:
            The typed :class:`AUQAnswer` carrying the selected option.

        Raises:
            ValueError: when *selected_key* is not one of this request's
                option keys.
        """
        for opt in self.options:
            if opt.key == selected_key:
                logger.debug(f"parse_answer selected={selected_key!r}")
                return AUQAnswer(selected_key=opt.key, selected_label=opt.label)
        raise ValueError(
            f"unknown option key: {selected_key!r}; expected one of {list(self.option_keys)}"
        )


class AUQAnswer(BaseModel):
    """The operator's typed answer to an :class:`AUQRequest`.

    Produced only by :meth:`AUQRequest.parse_answer` -- never hand-built on
    the call path -- so the selected key is guaranteed to name a real option
    on the originating request.

    Attributes:
        selected_key: The :attr:`AUQOption.key` the operator chose.
        selected_label: The chosen option's label, carried through for the
            audit / provenance trail so a downstream reader sees the choice
            without re-resolving the key against the request.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_key: str = Field(min_length=1)
    selected_label: str = Field(min_length=1)


class ClaudeAUQOption(BaseModel):
    """One option on a claude-code ``AskUserQuestion`` projection.

    Attributes:
        label: The option label shown on the question card.
        description: The secondary line under the label. Always populated on
            the projection (falls back to the label when the source option
            carried no description) because the claude card renders a
            two-line option.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    description: str


class ClaudeAUQProjection(BaseModel):
    """An :class:`AUQRequest` projected onto claude-code's ``AskUserQuestion``.

    The native claude approval surface: a header + a question + a list of
    two-line options the operator single-selects.

    Attributes:
        question: The question text.
        header: The short header / category above the question (defaults to
            ``"Confirm"`` when the request carried none).
        options: The two-line :class:`ClaudeAUQOption` rows.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    question: str
    header: str
    options: tuple[ClaudeAUQOption, ...]


class CodexNumberedChoice(BaseModel):
    """One numbered choice on a codex text-prompt projection.

    Attributes:
        number: The 1-based option number the operator may reply with.
        key: The stable option key (the alternative reply token).
        label: The option label.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    number: int = Field(ge=1)
    key: str
    label: str


class CodexAUQProjection(BaseModel):
    """An :class:`AUQRequest` projected onto a codex numbered text prompt.

    ``codex exec`` has no native multi-select widget, so the request renders
    as a numbered prompt the operator answers by number or key.

    Attributes:
        prompt: The fully-rendered multi-line prompt text passed to
            ``codex exec``.
        choices: The structured :class:`CodexNumberedChoice` rows backing the
            rendered numbered lines (so a caller can map a reply back without
            re-parsing the prompt text).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str
    choices: tuple[CodexNumberedChoice, ...]


class OpenCodeAUQProjection(BaseModel):
    """An :class:`AUQRequest` projected onto an opencode bracket-keyed prompt.

    ``opencode run`` likewise has no native select, so the request renders as
    a bracket-keyed prompt the operator answers with the bracket key.

    Attributes:
        prompt: The fully-rendered multi-line prompt text passed to
            ``opencode run``.
        keys: The bracket keys offered, in order (so a caller can validate a
            reply against the offered set without re-parsing the prompt).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str
    keys: tuple[str, ...]


#: Union of the three per-runtime confirm projections returned by
#: :meth:`AUQRequest.project_for_runtime`. The concrete type is fixed by the
#: runtime id the caller passed, so a caller that knows the runtime narrows
#: the union by ``isinstance``.
type RuntimeAUQProjection = ClaudeAUQProjection | CodexAUQProjection | OpenCodeAUQProjection


#: The two-option adjudicate / override pair a needs_user conversion offers by
#: default. The operator either pauses to resolve the unresolved signal by
#: hand (adjudicate) or overrides it to let the run proceed (override).
_NEEDS_USER_OPTIONS: tuple[AUQOption, ...] = (
    AUQOption(
        key="adjudicate",
        label="Adjudicate now",
        description="Pause and resolve the unresolved signal by hand.",
    ),
    AUQOption(
        key="override",
        label="Override and proceed",
        description="Accept the risk and let the run continue past the pause.",
    ),
)


def needs_user_to_auq(
    signal: JuryAggregate | Rung3Outcome,
    *,
    question: str | None = None,
    header: str = "Unresolved -- needs you",
) -> AUQRequest:
    """Convert a ``needs_user`` signal into an operator :class:`AUQRequest`.

    Both needs_user producers feed this one converter: a jury aggregate whose
    outcome is
    :attr:`~eawf.observability.eval.jury.JuryAggregateOutcome.NEEDS_USER` (a
    split with no veto, or a high-variance graded vote) and a rung-3 outcome
    whose :attr:`~eawf.workflow.evidence.rung3.Rung3Outcome.needs_user` bit is
    set (the same aggregate seen through the EviBound chain). An unresolvable
    signal becomes an :class:`AUQRequest` at
    :attr:`~eawf.kernel.state.enums.Urgency.URGENT` so it routes straight to
    the operator-pause surface rather than falling through as a silent pass --
    the refute-first contract the jury / rung-3 modules already hold.

    Args:
        signal: The needs_user producer -- a
            :class:`~eawf.observability.eval.jury.JuryAggregate` or a
            :class:`~eawf.workflow.evidence.rung3.Rung3Outcome`.
        question: Optional override for the question text. ``None`` derives a
            question from the signal's reasons.
        header: The :attr:`AUQRequest.header` for the pause. Defaults to a
            generic unresolved-pause header.

    Returns:
        An :class:`AUQRequest` at :attr:`~eawf.kernel.state.enums.Urgency.URGENT`
        offering the adjudicate / override pair.

    Raises:
        ValueError: when *signal* does not actually carry a needs_user state
            (a resolved jury aggregate, or a rung-3 outcome with
            ``needs_user`` unset) -- converting a resolved signal to an
            operator pause would be a category error.
    """
    reasons = _needs_user_reasons(signal)
    derived_question = question if question is not None else _needs_user_question(reasons)
    logger.info(
        f"needs_user_to_auq signal={type(signal).__name__} reasons={len(reasons)} "
        f"urgency={Urgency.URGENT.value}"
    )
    return AUQRequest(
        question=derived_question,
        options=_NEEDS_USER_OPTIONS,
        header=header,
        urgency=Urgency.URGENT,
    )


def _needs_user_reasons(signal: JuryAggregate | Rung3Outcome) -> tuple[str, ...]:
    """Return the signal's reasons, asserting it is genuinely needs_user.

    Raises:
        ValueError: when the signal is not in a needs_user state.
    """
    if isinstance(signal, JuryAggregate):
        if signal.outcome is not JuryAggregateOutcome.NEEDS_USER:
            raise ValueError(
                f"jury aggregate is not needs_user (outcome={signal.outcome.value!r}); "
                "cannot convert a resolved vote to an operator pause"
            )
        return signal.reasons
    if not signal.needs_user:
        raise ValueError(
            "rung-3 outcome does not need the operator (needs_user is false); "
            "cannot convert a resolved outcome to an operator pause"
        )
    return signal.reasons


def _needs_user_question(reasons: tuple[str, ...]) -> str:
    """Derive a question from a needs_user signal's reasons.

    A non-empty reason set is summarised into the question; an empty set
    falls back to a generic adjudication prompt. The result is clipped to the
    :attr:`AUQRequest.question` bound so a long reason chain never overflows.
    """
    if reasons:
        joined = "; ".join(reasons)
        body = f"An automated check could not resolve this: {joined}. How do you want to proceed?"
    else:
        body = "An automated check could not resolve this. How do you want to proceed?"
    return body[:500]


@dataclass(frozen=True)
class WaveFrontierItem:
    """One read-only row of the wave graph fed to the frontier compute.

    A slim, injected view of a :class:`~eawf.kernel.state.models.Wave` -- the
    four fields the claimability predicate needs -- so
    :func:`compute_ready_frontier` reduces the frontier purely off the view
    and never constructs or mutates a
    :class:`~eawf.kernel.state.models.State`. The caller projects the live
    state into these rows.

    Attributes:
        wave_id: Canonical wave id (e.g. ``P29-I04-W10``).
        iter_id: Parent iter id -- siblings share it, which the
            lower-numbered-sibling gate keys on.
        status: The wave's :class:`~eawf.kernel.state.enums.WaveStatus`.
        deps: The ids of the waves this wave depends on. The wave is
            dep-ready only when every dep is CLOSED in the same view.
    """

    wave_id: str
    iter_id: str
    status: WaveStatus
    deps: tuple[str, ...] = ()


@dataclass(frozen=True)
class DrainableFrontier:
    """The computed ready frontier of a wave graph.

    Produced only by :func:`compute_ready_frontier` -- never hand-built on
    the call path. Carries the ready waves in claim order plus the full
    indexed view so the driver can resolve a dep / sibling without a second
    pass.

    Attributes:
        ready: The ready :class:`WaveFrontierItem` rows in claim order
            (natural id order). Each is PENDING with all deps CLOSED and no
            lower-numbered ready sibling under the same iter.
        by_id: The whole injected view indexed by wave id (every row, not
            just the ready ones).
    """

    ready: tuple[WaveFrontierItem, ...]
    by_id: dict[str, WaveFrontierItem]

    @property
    def ready_ids(self) -> tuple[str, ...]:
        """The ready wave ids in claim order."""
        return tuple(item.wave_id for item in self.ready)

    @property
    def is_empty(self) -> bool:
        """Whether no wave is ready to claim this frontier."""
        return not self.ready


def compute_ready_frontier(items: Iterable[WaveFrontierItem]) -> DrainableFrontier:
    """Reduce a wave-graph view into its ready (claimable) frontier.

    Pure: the same view always yields the same frontier; no I/O, no mutation.
    A wave is on the ready frontier when it satisfies the claim-time gate
    (:func:`eawf.workflow.lifecycle.wave.claim_wave`), computed read-only off
    the injected view:

    1. its status is :attr:`~eawf.kernel.state.enums.WaveStatus.PENDING`;
    2. every id in its :attr:`WaveFrontierItem.deps` resolves in the view to
       a :attr:`~eawf.kernel.state.enums.WaveStatus.CLOSED` wave (an
       unresolved or open dep excludes it); and
    3. no lower-numbered sibling under the same iter is itself ready -- the
       monotonic claim order that keeps parallel runtimes from skipping
       ahead. "Lower-numbered" compares the wave ids by
       :func:`~eawf.kernel.state.ids.natural_key`.

    The ready rows are returned in claim order (natural id order) so the
    driver walks them deterministically.

    Args:
        items: The wave-graph view rows. Duplicate wave ids are rejected --
            the view must be a clean index.

    Returns:
        A :class:`DrainableFrontier` carrying the ready rows in claim order
        and the full indexed view.

    Raises:
        ValueError: when two rows share a ``wave_id`` -- the view cannot be
            a clean index, so dep / sibling resolution would be ambiguous.
    """
    item_list = list(items)
    by_id: dict[str, WaveFrontierItem] = {}
    for item in item_list:
        if item.wave_id in by_id:
            raise ValueError(f"duplicate wave id in frontier view: {item.wave_id!r}")
        by_id[item.wave_id] = item

    dep_ready = {
        item.wave_id
        for item in item_list
        if item.status is WaveStatus.PENDING and _deps_closed(item, by_id)
    }
    ready = tuple(
        sorted(
            (
                by_id[wave_id]
                for wave_id in dep_ready
                if not _lower_sibling_ready(by_id[wave_id], by_id, dep_ready)
            ),
            key=lambda item: natural_key(item.wave_id),
        )
    )
    logger.debug(
        f"compute_ready_frontier total={len(item_list)} dep_ready={len(dep_ready)} "
        f"ready={len(ready)}"
    )
    return DrainableFrontier(ready=ready, by_id=by_id)


def _deps_closed(item: WaveFrontierItem, by_id: dict[str, WaveFrontierItem]) -> bool:
    """Return whether every dep of *item* resolves to a CLOSED wave in the view.

    An unresolved dep id (not in the view) counts as not-closed -- the wave
    cannot be ready while a dependency is unaccounted for.
    """
    return all(
        by_id.get(dep_id) is not None and by_id[dep_id].status is WaveStatus.CLOSED
        for dep_id in item.deps
    )


def _lower_sibling_ready(
    item: WaveFrontierItem,
    by_id: dict[str, WaveFrontierItem],
    dep_ready: set[str],
) -> bool:
    """Return whether a lower-numbered sibling of *item* is itself dep-ready.

    A sibling shares *item*'s ``iter_id``; "lower-numbered" compares the wave
    ids by :func:`~eawf.kernel.state.ids.natural_key`. Mirrors
    :func:`eawf.workflow.lifecycle.wave._lower_w_sibling_pending` -- a wave is
    held off the frontier while a lower-numbered sibling under the same iter
    is ready to claim, so the monotonic claim order holds.
    """
    my_key = natural_key(item.wave_id)
    for other in by_id.values():
        if other.iter_id != item.iter_id:
            continue
        if other.wave_id == item.wave_id:
            continue
        if natural_key(other.wave_id) >= my_key:
            continue
        if other.wave_id in dep_ready:
            return True
    return False


@dataclass(frozen=True)
class FrontierDrainStep:
    """One step of a frontier drain that surfaced an operator question.

    Produced by :func:`drain_frontier`, one per ready wave the injected
    confirmation predicate marked as needing the operator. A wave that needs
    no confirmation produces no step (it is claim-ready without a pause).

    Attributes:
        wave_id: The ready wave this step surfaces a question for.
        request: The :class:`AUQRequest` to put to the operator for that
            wave.
    """

    wave_id: str
    request: AUQRequest


#: A predicate the frontier driver calls once per ready wave to decide
#: whether that wave needs operator confirmation before it is claimed. It
#: returns the :class:`AUQRequest` to surface, or ``None`` when the wave is
#: claim-ready without a pause. Injected into :func:`drain_frontier` so the
#: driver carries no policy and a test drives it with a recording stub.
type NeedsConfirmationFn = Callable[[WaveFrontierItem], AUQRequest | None]


def drain_frontier(
    frontier: DrainableFrontier,
    needs_confirmation: NeedsConfirmationFn,
) -> tuple[FrontierDrainStep, ...]:
    """Walk the ready frontier surfacing one AUQ per wave needing confirmation.

    Pure driver: it walks :attr:`DrainableFrontier.ready` in claim order and,
    for each ready wave the injected *needs_confirmation* predicate returns a
    request for, emits one :class:`FrontierDrainStep`. A wave the predicate
    clears (returns ``None``) produces no step -- it is claim-ready without a
    pause. The driver spawns nothing and mutates nothing; the predicate is
    the only policy seam, so a test drives the whole walk with a recording
    stub.

    Args:
        frontier: The computed ready frontier from
            :func:`compute_ready_frontier`.
        needs_confirmation: The injected per-wave predicate -- returns the
            :class:`AUQRequest` to surface for a wave, or ``None`` when the
            wave needs no operator confirmation.

    Returns:
        The :class:`FrontierDrainStep` rows, in the ready frontier's claim
        order, one per wave that needed confirmation. Empty when no ready
        wave needed a pause (including an empty frontier).
    """
    steps: list[FrontierDrainStep] = []
    for item in frontier.ready:
        request = needs_confirmation(item)
        if request is not None:
            steps.append(FrontierDrainStep(wave_id=item.wave_id, request=request))
    logger.info(f"drain_frontier ready={len(frontier.ready)} surfaced={len(steps)}")
    return tuple(steps)


def drain_frontier_from_view(
    items: Sequence[WaveFrontierItem],
    needs_confirmation: NeedsConfirmationFn,
) -> tuple[DrainableFrontier, tuple[FrontierDrainStep, ...]]:
    """Compute the ready frontier from a view and drain it in one call.

    Convenience composition of :func:`compute_ready_frontier` +
    :func:`drain_frontier` for the common case where the caller has the raw
    wave-graph view and wants both the computed frontier and the surfaced
    questions.

    Args:
        items: The wave-graph view rows.
        needs_confirmation: The injected per-wave confirmation predicate.

    Returns:
        A ``(frontier, steps)`` pair: the computed :class:`DrainableFrontier`
        and the :class:`FrontierDrainStep` rows surfaced over its ready waves.

    Raises:
        ValueError: when the view has a duplicate wave id (propagated from
            :func:`compute_ready_frontier`).
    """
    frontier = compute_ready_frontier(items)
    steps = drain_frontier(frontier, needs_confirmation)
    return frontier, steps


__all__ = [
    "MAX_AUQ_OPTIONS",
    "MIN_AUQ_OPTIONS",
    "AUQAnswer",
    "AUQOption",
    "AUQRequest",
    "ClaudeAUQOption",
    "ClaudeAUQProjection",
    "CodexAUQProjection",
    "CodexNumberedChoice",
    "DrainableFrontier",
    "FrontierDrainStep",
    "NeedsConfirmationFn",
    "OpenCodeAUQProjection",
    "RuntimeAUQProjection",
    "RuntimeId",
    "WaveFrontierItem",
    "compute_ready_frontier",
    "drain_frontier",
    "drain_frontier_from_view",
    "needs_user_to_auq",
]
