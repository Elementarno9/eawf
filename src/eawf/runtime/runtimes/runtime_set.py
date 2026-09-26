"""Normalize the runtimes a plugin verb will write, then refuse a second claim on any.

A runtime already served by an installed eawf plugin - the Claude Code
marketplace install, or a user-scope Codex or OpenCode install - is claimed.
Rendering a project-local tree for it as well makes the host load two copies
of every skill, agent and hook with undefined precedence, so the guard
refuses before any byte is written.

The guard runs over the normalized, fully expanded runtime set and never over
the flags as typed. Two escapes shaped this: one verb spelt the Claude runtime
``claude`` while another normalized it to ``claude-code``, so a gate matching
one spelling fell through for every call of the other; and a bare invocation
carries no runtime at all, which the renderer expands to every runtime, so
gating the given flags gated nothing. Normalizing once, here, closes both.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from eawf.runtime.runtimes.manifest import RuntimeId

logger = logging.getLogger(__name__)

__all__ = [
    "ALL_RUNTIMES",
    "RUNTIME_ALIASES",
    "ClaimDetector",
    "RuntimeClaim",
    "RuntimeSetConflictError",
    "claimed_runtimes",
    "guard_runtime_set",
    "normalize_runtime_set",
]

#: Every runtime a plugin renders for, in the order the renderers run.
ALL_RUNTIMES: Final[tuple[RuntimeId, ...]] = ("claude-code", "codex", "opencode")

#: Operator-facing spellings mapped to the canonical runtime id.
RUNTIME_ALIASES: Final[Mapping[str, RuntimeId]] = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex",
    "opencode": "opencode",
}

#: Returns the path of an installed eawf plugin serving one runtime, or ``None``.
ClaimDetector = Callable[[], Path | None]

_CLAIM_EFFECT: Final[Mapping[RuntimeId, str]] = {
    "claude-code": (
        "a Claude Code marketplace install of eawf at {path}; a project-local .claude/ "
        "render beside it makes Claude Code see every skill/agent/hook twice"
    ),
    "codex": (
        "a user-scope codex eawf install at {path}; a project-scope install beside it "
        "makes Codex resolve two 'eawf' plugins with undefined precedence"
    ),
    "opencode": (
        "a user-scope opencode eawf install at {path}; a project-scope install beside it "
        "makes OpenCode auto-load two 'eawf' plugins with undefined precedence"
    ),
}


@dataclass(frozen=True)
class RuntimeClaim:
    """An installed eawf plugin that already serves one runtime.

    Attributes:
        runtime: The canonical runtime the install serves.
        install_path: Where the host keeps that install.
    """

    runtime: RuntimeId
    install_path: Path

    def describe(self) -> str:
        """Return the operator-facing sentence naming the claim and its effect."""
        return _CLAIM_EFFECT[self.runtime].format(path=self.install_path)


class RuntimeSetConflictError(Exception):
    """Raised when a runtime the verb would write is already claimed by an install.

    Attributes:
        claims: Every claimed runtime in the set, in canonical order.
    """

    def __init__(self, claims: Sequence[RuntimeClaim]) -> None:
        self.claims: tuple[RuntimeClaim, ...] = tuple(claims)
        super().__init__("detected " + "; and ".join(claim.describe() for claim in self.claims))


def normalize_runtime_set(values: Sequence[str]) -> tuple[RuntimeId, ...]:
    """Return the canonical runtime set a verb will write.

    Args:
        values: Runtime spellings as the operator typed them. Empty means
            every runtime, which is what the renderers do with no selection.

    Returns:
        The canonical ids, de-duplicated, in :data:`ALL_RUNTIMES` order.

    Raises:
        TypeError: When *values* is a bare string rather than a sequence of
            runtime names, which would otherwise be read letter by letter.
        ValueError: When a value is not a known runtime spelling.
    """
    if isinstance(values, str):
        raise TypeError(f"runtime set must be a sequence of names, not the string {values!r}")
    if not values:
        return ALL_RUNTIMES
    requested: set[RuntimeId] = set()
    for value in values:
        canonical = RUNTIME_ALIASES.get(value)
        if canonical is None:
            raise ValueError(f"unknown runtime {value!r}; expected one of {sorted(ALL_RUNTIMES)}")
        requested.add(canonical)
    return tuple(runtime for runtime in ALL_RUNTIMES if runtime in requested)


def claimed_runtimes(
    runtime_set: Sequence[RuntimeId],
    *,
    scope: Literal["project", "user"],
    detectors: Mapping[RuntimeId, ClaimDetector],
) -> tuple[RuntimeClaim, ...]:
    """Return every runtime in *runtime_set* that an installed plugin already claims.

    Claude Code only renders project-local, so its marketplace install clashes
    at every scope. Codex and OpenCode clash only on a project-scope write: a
    user-scope write replaces the user-scope install instead of sitting
    beside it.

    Args:
        runtime_set: The output of :func:`normalize_runtime_set`.
        scope: The scope the verb writes to.
        detectors: One detector per runtime. A runtime with no detector is
            treated as unclaimed.

    Returns:
        The claims, in the order of *runtime_set*.
    """
    claims: list[RuntimeClaim] = []
    for runtime in runtime_set:
        if runtime != "claude-code" and scope != "project":
            continue
        detector = detectors.get(runtime)
        install_path = detector() if detector is not None else None
        if install_path is not None:
            claims.append(RuntimeClaim(runtime=runtime, install_path=install_path))
    return tuple(claims)


def guard_runtime_set(
    runtime_set: Sequence[RuntimeId],
    *,
    scope: Literal["project", "user"],
    detectors: Mapping[RuntimeId, ClaimDetector],
    force: bool,
) -> tuple[RuntimeClaim, ...]:
    """Refuse a write that would put a second install on a claimed runtime.

    Args:
        runtime_set: The output of :func:`normalize_runtime_set`.
        scope: The scope the verb writes to.
        detectors: One detector per runtime.
        force: The operator acknowledged the duplicate; claims are returned
            instead of refused.

    Returns:
        The claims bypassed under *force*; empty when nothing was claimed.

    Raises:
        RuntimeSetConflictError: When any runtime in the set is claimed and
            *force* is not set.
    """
    claims = claimed_runtimes(runtime_set, scope=scope, detectors=detectors)
    if claims and not force:
        raise RuntimeSetConflictError(claims)
    for claim in claims:
        logger.info(
            f"guard_runtime_set force-bypass runtime={claim.runtime} "
            f"install_path={claim.install_path}"
        )
    return claims
