"""Co-author trailer policy and VCS config validation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CoauthorMode = Literal["runtime", "project", "disabled"]

_COAUTHOR_LINE_RE = re.compile(r"^Co-Authored-By:\s+.+<[^>]+>\s*$", re.MULTILINE)
_RUNTIME_ALIASES: dict[str, str] = {
    "anthropic": "claude",
    "claude": "claude",
    "claude-code": "claude",
    "codex": "codex",
    "codex-cli": "codex",
    "openai": "codex",
}


class CoauthorPolicyError(ValueError):
    """Raised when co-author policy rejects a trailer operation."""


class CoauthorIdentity(BaseModel):
    """One renderable ``Co-Authored-By`` identity."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1)]
    email: Annotated[str, Field(pattern=r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")]

    def trailer(self) -> str:
        """Return the canonical git trailer line."""
        return f"Co-Authored-By: {self.name} <{self.email}>"


class CoauthorConfig(BaseModel):
    """Validated ``vcs.coauthor`` config block.

    ``mode=disabled`` is intentionally strict: any existing co-author trailer
    is rejected by :func:`resolve_coauthor_trailer` when message text is
    supplied. ``mode=runtime`` resolves through the runtime registry.
    ``mode=project`` uses the explicit project identity.
    """

    model_config = ConfigDict(extra="forbid")

    mode: CoauthorMode = "runtime"
    default_runtime: str = "claude"
    project: CoauthorIdentity | None = None
    trailers: dict[str, CoauthorIdentity] = Field(
        default_factory=lambda: {
            "claude": CoauthorIdentity(name="Claude", email="noreply@anthropic.com"),
            "codex": CoauthorIdentity(name="Codex", email="noreply@openai.com"),
        }
    )
    require_trailer: bool = True

    @field_validator("default_runtime")
    @classmethod
    def _default_runtime_non_empty(cls, value: str) -> str:
        runtime = _normalise_runtime(value)
        if not runtime:
            raise ValueError("default_runtime must not be empty")
        return runtime

    @field_validator("trailers")
    @classmethod
    def _trailer_keys_normalised(
        cls, value: dict[str, CoauthorIdentity]
    ) -> dict[str, CoauthorIdentity]:
        normalised: dict[str, CoauthorIdentity] = {}
        for key, identity in value.items():
            runtime = _normalise_runtime(key)
            if not runtime:
                raise ValueError("trailer runtime key must not be empty")
            normalised[runtime] = identity
        return normalised

    @model_validator(mode="after")
    def _mode_requirements(self) -> CoauthorConfig:
        if self.mode == "project" and self.project is None:
            raise ValueError("vcs.coauthor.project is required when mode='project'")
        if self.mode == "runtime" and _canonical_runtime(self.default_runtime) not in self.trailers:
            raise ValueError(f"default_runtime {self.default_runtime!r} has no configured trailer")
        return self


class VcsConfig(BaseModel):
    """Validated ``vcs`` config surface."""

    model_config = ConfigDict(extra="forbid")

    commit_template: str
    pr_template: str
    branch_pattern: str
    checkpoint_requires_commit: bool
    protected_branches: list[str]
    auto_commit: str
    auto_push: str
    pr_open: str
    pr_merge_method: str
    squash_allowed: bool
    delete_branch_after_merge: bool
    require_ci_green: bool
    require_review_before_merge: bool
    force_push: str
    coauthor: CoauthorConfig = Field(default_factory=CoauthorConfig)


def _normalise_runtime(value: str) -> str:
    return value.strip().casefold().replace("_", "-")


def _canonical_runtime(value: str) -> str:
    normalised = _normalise_runtime(value)
    return _RUNTIME_ALIASES.get(normalised, normalised)


def _runtime_from_env(env: Mapping[str, str]) -> str | None:
    """Resolve runtime from explicit opt-in env vars only (KISS-001).

    Pre-W12 this helper additionally sniffed ``CLAUDE*`` / ``CODEX*``
    env-var prefixes — that implicit detection path was removed per
    KISS-001 because operators could not reliably opt out (any
    stray ``CLAUDE_HOME`` in the parent shell would force a Claude
    trailer even when running under Codex). Detection now requires
    explicit opt-in via :data:`~eawf.runtime.runtimes.coauthor.COAUTHOR_RUNTIME_ENV_VAR`
    or its legacy alias :data:`~eawf.runtime.runtimes.coauthor.COAUTHOR_RUNTIME_LEGACY_ENV_VAR`.
    """
    from eawf.runtime.runtimes.coauthor import resolve_runtime_explicit

    resolved = resolve_runtime_explicit(env=env)
    if resolved is None:
        return None
    return _canonical_runtime(resolved)


def has_any_coauthor_trailer(text: str) -> bool:
    """Return whether *text* contains any visible co-author trailer."""
    return _COAUTHOR_LINE_RE.search(text) is not None


def resolve_coauthor_trailer(
    config: CoauthorConfig,
    *,
    runtime: str | None = None,
    env: Mapping[str, str] | None = None,
    message_text: str | None = None,
) -> str | None:
    """Resolve the trailer line for *config*.

    Args:
        config: Validated co-author config.
        runtime: Optional runtime id override.
        env: Optional environment used for runtime detection.
        message_text: Optional commit/PR text checked by disabled mode.

    Returns:
        The trailer line, or ``None`` when trailers are disabled or cannot be
        inferred.

    Raises:
        CoauthorPolicyError: When disabled mode sees an existing trailer or
            runtime mode cannot resolve a configured identity.
    """
    if config.mode == "disabled":
        if message_text is not None and has_any_coauthor_trailer(message_text):
            raise CoauthorPolicyError("co-author trailers are disabled")
        return None

    if config.mode == "project":
        if config.project is None:
            raise CoauthorPolicyError("project co-author identity is not configured")
        return config.project.trailer()

    detected = runtime
    if detected is None and env is not None:
        detected = _runtime_from_env(env)
    if detected is None:
        detected = config.default_runtime

    key = _canonical_runtime(detected)
    identity = config.trailers.get(key)
    if identity is None:
        if config.require_trailer:
            raise CoauthorPolicyError(f"no co-author trailer configured for runtime {key!r}")
        return None
    return identity.trailer()


__all__ = [
    "CoauthorConfig",
    "CoauthorIdentity",
    "CoauthorPolicyError",
    "VcsConfig",
    "has_any_coauthor_trailer",
    "resolve_coauthor_trailer",
]
