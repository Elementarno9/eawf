"""``release.candidate``: freeze the manifest and record the CANDIDATE.

``release.create`` opens a DRAFT and ``release.approve`` binds an
approval to a manifest digest. Between them sits the one edge nothing
drove: pinning the artifact set. The dev2 walk filled the gap with a
throwaway script, which meant the manifest the approval bound was
produced outside every guard the rest of the path runs under.

This verb is that edge. It reads the stored DRAFT, freezes the manifest
from the publication receipts of the tag's jobs
(:func:`~eawf.workflow.release.candidate.freeze_manifest`), resolves the
tree of the pinned commit out of the checkout the daemon's state root
names, and records the successor through
:func:`~eawf.workflow.release.candidate.pin_candidate` under the
``manifest_complete`` guard.

Two orderings carry weight. The manifest is frozen *before* anything is
written, so a receipts directory missing a leg or naming another version
leaves the record collection untouched -- the DRAFT stays a DRAFT and the
operator retries with the right download. And the record is persisted
before the reply is built, because a candidate only one RPC response ever
carried is a candidate no later verb could act on.

The frozen manifest is returned rather than written: the daemon owns
records, not artifacts, and the operator needs the document on disk
anyway to hand to ``eawf release observe --manifest``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.spec.release import ReleaseStatus, release_key
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.methods.release_context import require_state_path, resolve_config
from eawf.workflow.release.candidate import (
    CandidateFreezeError,
    freeze_manifest,
    pin_candidate,
    resolve_source_tree,
)
from eawf.workflow.release.lifecycle import ReleaseTransitionError
from eawf.workflow.release.records import (
    read_release_record,
    record_envelope_id,
    record_release,
)

logger = logging.getLogger(__name__)


class CandidateParams(BaseModel):
    """Params for :func:`candidate`.

    Attributes:
        version: Normalized checkpoint version to pin, e.g.
            ``0.7.0.dev3``.
        receipts_dir: Directory the tag's publication receipts were
            downloaded into, one file per declared target.
        source_sha: Commit the published artifacts were built from. The
            tree is read from the checkout rather than accepted here, so
            the two halves of the pin cannot disagree.
        manifest_ref: Pointer to where the frozen manifest is stored.
            ``None`` derives a digest-bearing reference, which names the
            exact document even before it has a durable home.
    """

    model_config = ConfigDict(extra="forbid")
    version: str
    receipts_dir: str = Field(min_length=1)
    source_sha: str = Field(min_length=1)
    manifest_ref: str | None = None


@register("release.candidate")
async def candidate(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Freeze the manifest of one checkpoint and record its CANDIDATE.

    Args:
        ctx: Server context; its state root supplies the stored DRAFT,
            the checkout the tree is resolved in, and the collection the
            candidate is recorded into.
        params: JSON-RPC params per :class:`CandidateParams`.

    Returns:
        The serialized candidate record, the id of the collection row
        carrying it, the frozen manifest document and its digest, the
        reference the record pins it under, and the predecessor the
        record supersedes (``None`` at the head of the ladder).

    Raises:
        DaemonValidationError: When the daemon has no state root, no
            configuration is authored for the version, no record is
            stored for it, the receipts do not freeze a manifest, the
            source commit has no tree here, or the pin is denied -- the
            message leads with the named code
            (``release_manifest_incomplete`` when the manifest does not
            carry every required target).
    """
    args = CandidateParams.model_validate(params)
    state_path = require_state_path(ctx)
    config = resolve_config(args.version)
    key = release_key(args.version)
    draft = read_release_record(state_path, key)
    if draft is None:
        raise DaemonValidationError(
            f"validation_failed: no release record is stored for {key!r}; open the "
            f"checkpoint with `eawf release create {args.version}` first"
        )
    try:
        manifest = freeze_manifest(config, receipts_dir=Path(args.receipts_dir))
        tree_sha = resolve_source_tree(state_path.parent.parent, args.source_sha)
        pinned = pin_candidate(
            draft,
            config,
            manifest,
            source_sha=args.source_sha,
            source_tree_sha=tree_sha,
            manifest_ref=args.manifest_ref or f"manifest://{key}/{manifest.digest}",
        )
    except CandidateFreezeError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    except ReleaseTransitionError as exc:
        raise DaemonValidationError(f"validation_failed: {exc.code.value}: {exc}") from exc
    except ValueError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    record_release(
        state_path,
        pinned,
        recorded_at=datetime.now(UTC),
        summary=f"candidate {pinned.key}: {ReleaseStatus.CANDIDATE.value}",
    )
    logger.info(
        f"candidate key={pinned.key!r} revision={pinned.revision} "
        f"source_sha={args.source_sha!r} manifest_digest={manifest.digest!r}"
    )
    return {
        "release": pinned.model_dump(mode="json"),
        "release_record_id": record_envelope_id(pinned),
        "manifest": manifest.model_dump(mode="json"),
        "manifest_digest": manifest.digest,
        "manifest_ref": pinned.manifest_ref,
        "source_tree_sha": tree_sha,
        "supersedes_release_ref": pinned.supersedes_release_ref,
    }


__all__ = ["CandidateParams", "candidate"]
