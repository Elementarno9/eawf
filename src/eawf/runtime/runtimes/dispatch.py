"""Skill -> adapter handshake.

The contract boundary where a workflow skill is matched to a concrete
runtime adapter by the daemon. The **skill never picks the runtime** —
it declares the set of runtimes that can host it via
:attr:`SkillManifest.runtime`, and the daemon picks the
highest-preference runtime that is *both* listed by the skill *and* has
a resolvable adapter.

This module owns the pure resolution half of that handshake: given a
:class:`~eawf.runtime.runtimes.plugin_manifest.SkillManifest`, a wave's
``runtime_preference`` list, and an optional caller override, it returns
the chosen adapter plus the dispatch ``session_policy`` resolved off the
manifest. The live subprocess spawn lives in the daemon dispatch router;
this module deliberately does no I/O.

Failure modes:

* A caller-supplied ``override`` runtime that the skill's manifest does
  not list is rejected fast with :class:`AdapterManifestMismatchError`.
  The daemon refuses rather than silently honouring an off-manifest
  runtime, so an audit can reconstruct *why* a skill never ran on the
  requested runtime.

The complementary half — what happens when adapter *resolution* fails
mid-ladder — lives in :mod:`eawf.runtime.runtimes.fallback`.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict

from eawf.runtime.runtimes.adapter import RuntimeAdapter
from eawf.runtime.runtimes.plugin_manifest import SkillManifest
from eawf.runtime.runtimes.selector import select_adapter

logger = logging.getLogger(__name__)


#: Dispatch session policy resolved off a skill manifest. Mirrors
#: :data:`eawf.runtime.daemon.methods.agent.SessionPolicy`; kept as a local
#: literal so this module does not import the daemon (the daemon imports
#: the handshake, not the other way round).
DEFAULT_SESSION_POLICY = "hybrid"


class AdapterManifestMismatchError(ValueError):
    """Raised when a caller cites a runtime the skill manifest forbids.

    The off-manifest failure mode: a workflow command requests an
    explicit ``runtime`` that is not in the skill's
    :attr:`~eawf.runtime.runtimes.plugin_manifest.SkillManifest.runtime` list.
    The daemon is the single point of runtime policy and refuses the
    off-manifest request rather than honouring it. Subclasses
    :class:`ValueError` so the daemon's JSON-RPC layer maps it to
    ``-32602 invalid params`` without a bespoke catch.
    """


class AdapterResolutionError(ValueError):
    """Raised when no manifest-listed runtime yields a usable adapter.

    Emitted by :func:`resolve_adapter` when every candidate runtime (the
    manifest list intersected with the wave preference order) fails to
    resolve to a concrete adapter. This is the terminal rung of the
    resolution ladder — the daemon catches it and surfaces a
    ``runtime_unavailable`` envelope. The mid-ladder *single*-adapter
    resolution failure is handled by the V8 fall-through in
    :mod:`eawf.runtime.runtimes.fallback`; this exception fires only when the
    ladder is exhausted.
    """


class AdapterHandshake(BaseModel):
    """Resolved skill -> adapter handshake result.

    The typed product of :func:`resolve_adapter`: the canonical runtime
    id the daemon will dispatch on, plus the dispatch ``session_policy``
    resolved off the skill manifest (an explicit caller policy wins; the
    manifest ``dispatch.session_policy`` is next; :data:`DEFAULT_SESSION_POLICY`
    is the floor). The adapter instance itself is *not* carried on the
    model (it is not JSON-serialisable); callers re-resolve it via
    :func:`~eawf.runtime.runtimes.selector.select_adapter` using
    :attr:`runtime_id`, or read it off :attr:`adapter` on the in-process
    return value below.

    Attributes:
        runtime_id: Canonical runtime id chosen for this dispatch
            (e.g. ``"claude-code"``). Always a member of the skill
            manifest ``runtime`` list.
        session_policy: Effective dispatch session policy
            (``"fresh"`` / ``"continue"`` / ``"hybrid"``).
        considered: The candidate runtime ids the resolver walked, in
            preference order, up to and including the chosen one. Lets an
            audit reconstruct *why* a lower-preference runtime won (the
            higher ones failed to resolve).
    """

    model_config = ConfigDict(extra="forbid")
    runtime_id: str
    session_policy: str
    considered: list[str]


def candidate_runtimes(
    *,
    manifest: SkillManifest,
    preference: list[str] | None,
) -> list[str]:
    """Order the skill-hostable runtimes by wave preference.

    The handshake candidate list is the skill manifest ``runtime`` set
    *projected onto* the wave's ``runtime_preference`` order: a runtime
    must be hostable by the skill (manifest) AND wanted by the wave
    (preference) to be a candidate. Manifest order is the tie-breaker
    fallback when the wave declares no preference, so a skill that lists
    ``["claude-code", "codex"]`` on a preference-less wave still resolves
    deterministically.

    Args:
        manifest: The skill's manifest; :attr:`SkillManifest.runtime` is
            the closed set of runtimes that can host the skill.
        preference: ``Wave.runtime_preference`` — the operator's ordered
            runtime list. ``None`` / empty falls back to manifest order.

    Returns:
        Candidate runtime ids in resolution order (highest preference
        first). Never empty when the manifest is valid (the manifest
        rejects an empty ``runtime`` list at load time), unless the wave
        preference and manifest list are disjoint.
    """
    hostable: list[str] = list(manifest.runtime)
    if not preference:
        return hostable
    hostable_set = set(hostable)
    ordered: list[str] = [rt for rt in preference if rt in hostable_set]
    # Append any skill-hostable runtimes the wave preference omitted, in
    # manifest order, so a preference that lists only a subset still
    # falls through to the remaining hostable runtimes rather than
    # halting the ladder early.
    ordered.extend(rt for rt in hostable if rt not in set(preference))
    return ordered


def resolve_adapter(
    *,
    manifest: SkillManifest,
    preference: list[str] | None,
    override: str | None = None,
    session_policy: str | None = None,
) -> tuple[RuntimeAdapter, AdapterHandshake]:
    """Run the skill -> adapter handshake.

    Picks the highest-preference runtime that is both hostable by the
    skill manifest and resolvable to a concrete adapter, walking the
    candidate ladder so a higher-preference runtime that fails to
    resolve (uninstalled adapter) yields to the next candidate. The
    returned :class:`AdapterHandshake` records the runtimes considered so
    the dispatch annotation can explain a lower-preference win.

    Args:
        manifest: The skill's manifest declaring its hostable runtimes
            and default ``dispatch.session_policy``.
        preference: ``Wave.runtime_preference`` ordered runtime list.
        override: Optional caller-supplied runtime id. When set it must
            be in the manifest ``runtime`` list and is the only
            candidate the resolver tries.
        session_policy: Optional explicit session policy that wins over
            the manifest default.

    Returns:
        A ``(adapter, handshake)`` tuple: the live adapter instance plus
        the JSON-serialisable :class:`AdapterHandshake` describing the
        decision.

    Raises:
        AdapterManifestMismatchError: *override* names a runtime the
            skill manifest does not list.
        AdapterResolutionError: No candidate runtime resolves to an
            adapter — the ladder is exhausted.
    """
    effective_policy = session_policy or _manifest_session_policy(manifest)

    if override is not None:
        if override not in manifest.runtime:
            raise AdapterManifestMismatchError(
                f"runtime {override!r} not in skill manifest runtime list "
                f"{list(manifest.runtime)!r} for skill {manifest.name!r}"
            )
        candidates: list[str] = [override]
    else:
        candidates = candidate_runtimes(manifest=manifest, preference=preference)

    considered: list[str] = []
    for runtime_id in candidates:
        considered.append(runtime_id)
        try:
            adapter = select_adapter(runtime_id)
        except ValueError:
            logger.info(
                f"resolve_adapter skill={manifest.name!r} runtime={runtime_id!r} "
                "resolution=failed advancing=ladder"
            )
            continue
        handshake = AdapterHandshake(
            runtime_id=runtime_id,
            session_policy=effective_policy,
            considered=considered,
        )
        logger.info(
            f"resolve_adapter skill={manifest.name!r} runtime={runtime_id!r} "
            f"policy={effective_policy!r} considered={considered!r}"
        )
        return adapter, handshake

    raise AdapterResolutionError(
        f"no resolvable adapter for skill {manifest.name!r}: "
        f"considered {considered!r} (manifest={list(manifest.runtime)!r})"
    )


def _manifest_session_policy(manifest: SkillManifest) -> str:
    """Read the dispatch session policy off a skill manifest.

    The manifest carries dispatch knobs in the open
    :attr:`SkillManifest.dispatch` mapping. When ``session_policy`` is
    present it must be a string; anything else is a
    manifest authoring error and is rejected fast. A missing key falls
    back to :data:`DEFAULT_SESSION_POLICY`.

    Args:
        manifest: The skill manifest.

    Returns:
        The session policy string.

    Raises:
        AdapterManifestMismatchError: ``dispatch.session_policy`` is
            present but not a string.
    """
    raw = manifest.dispatch.get("session_policy")
    if raw is None:
        return DEFAULT_SESSION_POLICY
    if not isinstance(raw, str):
        raise AdapterManifestMismatchError(
            f"dispatch.session_policy must be a string; got {type(raw).__name__} "
            f"for skill {manifest.name!r}"
        )
    return raw


__all__ = [
    "DEFAULT_SESSION_POLICY",
    "AdapterHandshake",
    "AdapterManifestMismatchError",
    "AdapterResolutionError",
    "candidate_runtimes",
    "resolve_adapter",
]
