"""Shared entry checks every ``release.*`` handler runs before it acts.

Four questions have to be answered the same way by every release verb,
whichever module the verb lives in: is there a state root to record
into, is the payload a record this train declares, is the caller holding
the current revision, and which authored checkpoint configuration
applies. Answering them differently in two handlers is how a verb ends
up accepting a record another one would have refused, so they live here
once and both handler modules import them.

This module imports no sibling handler module, which is what keeps the
release family's import graph a layer rather than a tangle.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from eawf.kernel.spec.release import Release, validate_release_against_train
from eawf.kernel.spec.release_config import (
    ReleaseConfig,
    ReleaseConfigError,
    load_release_config,
)
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.workflow.release.ledger import (
    StaleReleaseRevisionError,
    assert_fresh_revision,
)
from eawf.workflow.release.train import V07_TRAIN, checkpoint_config_yaml

logger = logging.getLogger(__name__)


def resolve_config(version: str) -> ReleaseConfig:
    """Return the loaded configuration for checkpoint *version*.

    Args:
        version: Normalized checkpoint version.

    Returns:
        The validated :class:`~eawf.kernel.spec.release_config.ReleaseConfig`.

    Raises:
        DaemonValidationError: When no configuration is authored for the
            version, or the authored one is rejected by the loader.
    """
    try:
        source = checkpoint_config_yaml(version)
    except KeyError as exc:
        raise DaemonValidationError(
            f"validation_failed: no release configuration for {version!r}"
        ) from exc
    try:
        return load_release_config(source, train=V07_TRAIN)
    except ReleaseConfigError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.code.value}: {exc}") from exc


def validated_release(payload: dict[str, Any]) -> Release:
    """Return the :class:`Release` in *payload*, placed on the train.

    Args:
        payload: Serialized release record.

    Returns:
        The validated record, whose epoch and membership agree with the
        rung the train declares for it.

    Raises:
        DaemonValidationError: When the payload fails the record schema,
            names a checkpoint the train does not declare, or
            contradicts the rung's declaration.
    """
    try:
        release = Release.model_validate(payload)
    except ValidationError as exc:
        raise DaemonValidationError(
            f"validation_failed: release payload invalid: {exc.error_count()} error(s)"
        ) from exc
    try:
        validate_release_against_train(release, V07_TRAIN)
    except (KeyError, ValueError) as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    return release


def require_state_path(ctx: MethodContext) -> Path:
    """Return the daemon's state path, or refuse the verb.

    Args:
        ctx: Server context.

    Returns:
        The bound ``state.json`` path.

    Raises:
        DaemonValidationError: When the daemon runs without on-disk
            state. A verb with nowhere to record what it did is worse
            than one that refuses.
    """
    if ctx.state_path is None:
        raise DaemonValidationError(
            "validation_failed: recording release verbs require an on-disk state root"
        )
    return Path(ctx.state_path)


def assert_revision(release: Release, expected_revision: int) -> None:
    """Refuse the verb when the caller holds a stale record.

    Args:
        release: The record being mutated.
        expected_revision: The revision the caller believes it holds.

    Raises:
        DaemonValidationError: With a ``stale_release_revision`` message.
    """
    try:
        assert_fresh_revision(release, expected_revision)
    except StaleReleaseRevisionError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc


__all__ = [
    "assert_revision",
    "require_state_path",
    "resolve_config",
    "validated_release",
]
