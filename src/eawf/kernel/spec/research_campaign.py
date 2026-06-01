"""Typed ``research:`` profile block + the plan-only Level-1 campaign runner.

A *research campaign* is a multi-domain ``/research`` sweep: one topic
fanned out across several research domains, each domain tuned by its own
depth / budget / focus. This module owns two halves of that surface:

- the **typed config** — :class:`ResearchProfileBlock` is the
  ``research:`` block a profile body mounts (per-domain config via
  :class:`ResearchDomainConfig`), composing with the canonical
  :class:`~eawf.kernel.spec.research.ResearchDepth` ladder so the
  campaign and the single-question ``/research`` surface resolve depth
  against the same source; and
- the **Level-1 runner** — :func:`stage_campaign` is a *plan-only*
  stager. It expands a topic + a :class:`ResearchProfileBlock` into a
  :class:`StagedCampaign` of typed :class:`StagedDispatch` envelopes and
  stops there. It NEVER spawns a subprocess, opens a runtime session, or
  touches an adapter — the staged envelopes are the hand-off a later
  level (the live dispatcher) consumes.

The "Level-1" name marks the rung on the autonomy ladder: Level-0 is the
single-question ``/research`` body, Level-1 stages a whole campaign
without execution, and a higher level (out of this wave's scope) drives
the live spawn. Keeping staging pure means the campaign shape is unit-
testable without a runtime and a campaign can be staged, reviewed, and
persisted (as a :class:`~eawf.kernel.store.kinds.research_campaign.ResearchCampaignPayload`
store record) before any tokens are spent.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.spec.research import DEFAULT_RESEARCH_DEPTH, ResearchDepth

#: Default read-only agent role staged dispatches carry. Campaign staging
#: is an investigation, so every staged envelope is a read-only researcher
#: dispatch unless a domain overrides the role.
DEFAULT_CAMPAIGN_ROLE: str = "researcher"


class ResearchDomainConfig(BaseModel):
    """Per-domain tuning for one research domain in a campaign.

    Mounted under :attr:`ResearchProfileBlock.domains` keyed by the domain
    name (e.g. ``"market-structure"``). Each field narrows how the
    Level-1 runner stages that domain's dispatch; an absent field defers
    to the block-level default so a domain only declares what it changes.

    Attributes:
        depth: Per-domain override of the survey depth. ``None`` defers to
            :attr:`ResearchProfileBlock.default_depth`. Reuses the
            canonical :class:`~eawf.kernel.spec.research.ResearchDepth`
            ladder so the campaign and ``/research`` agree on depth tokens.
        focus: Optional one-line focus prompt appended to the staged
            dispatch prompt for this domain. ``None`` stages the bare
            topic for the domain.
        agent_role: Read-only role the staged dispatch carries. Defaults
            to :data:`DEFAULT_CAMPAIGN_ROLE`; named per AGENTS rule 17
            (``agent_role`` on a dispatch-bearing field).
    """

    model_config = ConfigDict(extra="forbid")

    depth: ResearchDepth | None = None
    focus: str | None = Field(default=None, max_length=280)
    agent_role: str = DEFAULT_CAMPAIGN_ROLE

    def resolved_depth(self, default: ResearchDepth) -> ResearchDepth:
        """Return this domain's depth, falling back to *default*.

        Args:
            default: The block-level default depth to use when this
                domain declares no :attr:`depth` override.

        Returns:
            The per-domain :attr:`depth` when set, else *default*.
        """
        return self.depth if self.depth is not None else default


class ResearchProfileBlock(BaseModel):
    """Typed ``research:`` profile block — the per-domain campaign config.

    A profile body mounts this block under its ``research:`` leaf (the
    profile-side field wiring lands in
    :class:`eawf.platform.profiles.models.ProfileBody`; see this wave's
    follow-up note). The block carries a campaign-wide default depth plus
    a map of per-domain overrides; :func:`stage_campaign` reads it to
    decide how many dispatches to stage and how each is tuned.

    Attributes:
        default_depth: Campaign-wide survey depth a domain inherits when
            it declares no per-domain :attr:`ResearchDomainConfig.depth`.
            Defaults to the canonical
            :data:`~eawf.kernel.spec.research.DEFAULT_RESEARCH_DEPTH`.
        domains: Per-domain config keyed by domain name. An empty map
            means the block declares no domains — :func:`stage_campaign`
            then stages an empty campaign (no dispatches), which is the
            boundary case a caller stages before any domain is configured.
    """

    model_config = ConfigDict(extra="forbid")

    default_depth: ResearchDepth = DEFAULT_RESEARCH_DEPTH
    domains: dict[str, ResearchDomainConfig] = Field(default_factory=dict)


class StagedDispatch(BaseModel):
    """One staged — NOT spawned — research dispatch envelope.

    Produced by :func:`stage_campaign`, one per configured domain. The
    envelope describes a read-only dispatch the live dispatcher *would*
    run; staging it does not run anything. ``read_only`` is fixed
    ``True`` and there is deliberately no PID / exit-status / token field
    — those belong on the live-spawn
    :class:`~eawf.runtime.runtimes.adapter.SpawnResult`, never on a
    plan-only stage.

    Attributes:
        domain: The research domain this dispatch covers (the map key
            from :attr:`ResearchProfileBlock.domains`).
        agent_role: Read-only role the dispatch carries (AGENTS rule 17).
        depth: Resolved survey depth for the domain (per-domain override
            or block default).
        prompt: The staged prompt — topic, optionally suffixed with the
            domain's focus line.
        read_only: Always ``True``; a staged research dispatch never
            mutates state.
    """

    model_config = ConfigDict(extra="forbid")

    domain: str = Field(min_length=1)
    agent_role: str = Field(min_length=1)
    depth: ResearchDepth
    prompt: str = Field(min_length=1)
    read_only: Literal[True] = True


class StagedCampaign(BaseModel):
    """Output of the plan-only Level-1 runner :func:`stage_campaign`.

    Carries the staged dispatch plan for a multi-domain campaign. The
    ``spawned`` field is a fixed ``Literal[False]`` discriminator: it is
    the type-level proof that this object came out of the *plan-only*
    runner and that no subprocess / runtime session was opened. A live
    runner (a higher level, out of this wave's scope) would emit a
    distinct result type; the closed ``False`` here means a reviewer (or
    a test) can assert plan-only-ness from the value alone.

    Attributes:
        topic: The campaign topic fanned out across the domains.
        spawned: Always ``False`` — the plan-only discriminator.
        dispatches: One :class:`StagedDispatch` per configured domain, in
            sorted domain-name order for deterministic output. Empty when
            the block declared no domains.
    """

    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1)
    spawned: Literal[False] = False
    dispatches: list[StagedDispatch] = Field(default_factory=list)

    @property
    def domain_count(self) -> int:
        """Number of staged dispatches (one per configured domain)."""
        return len(self.dispatches)


def stage_campaign(
    topic: str,
    block: ResearchProfileBlock,
) -> StagedCampaign:
    """Stage a research campaign WITHOUT spawning — the Level-1 runner.

    Expands *topic* across every domain declared in *block* into a
    :class:`StagedCampaign` of read-only :class:`StagedDispatch`
    envelopes. This is a pure function: it allocates no subprocess, opens
    no runtime session, and imports no adapter. The returned campaign's
    :attr:`StagedCampaign.spawned` flag is fixed ``False`` so the
    plan-only contract is visible in the value.

    Domains are staged in sorted name order so the output is
    deterministic regardless of dict insertion order. A *block* with no
    domains stages an empty campaign (no dispatches) — the boundary case
    a caller stages before configuring any domain.

    Args:
        topic: The campaign topic. Must be a non-empty string.
        block: The typed ``research:`` profile block carrying the
            campaign-wide default depth and the per-domain config map.

    Returns:
        A :class:`StagedCampaign` with one :class:`StagedDispatch` per
        configured domain, sorted by domain name.

    Raises:
        ValueError: when *topic* is empty or whitespace-only.
    """
    if not topic.strip():
        raise ValueError(f"campaign topic must be non-empty: {topic!r}")

    dispatches: list[StagedDispatch] = []
    for domain in sorted(block.domains):
        domain_config = block.domains[domain]
        depth = domain_config.resolved_depth(block.default_depth)
        if domain_config.focus is None:
            prompt = topic
        else:
            prompt = f"{topic}\n\nFocus: {domain_config.focus}"
        dispatches.append(
            StagedDispatch(
                domain=domain,
                agent_role=domain_config.agent_role,
                depth=depth,
                prompt=prompt,
            )
        )

    return StagedCampaign(topic=topic, dispatches=dispatches)


#: Bound on the number of staged dispatches a single campaign payload
#: records — keeps a persisted campaign row scannable.
MAX_STAGED_DISPATCHES: Annotated[int, Field(ge=1)] = 64


__all__ = [
    "DEFAULT_CAMPAIGN_ROLE",
    "MAX_STAGED_DISPATCHES",
    "ResearchDomainConfig",
    "ResearchProfileBlock",
    "StagedCampaign",
    "StagedDispatch",
    "stage_campaign",
]
