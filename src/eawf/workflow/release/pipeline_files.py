"""The file chore of the post-merge release pipeline.

It came out of walking a checkpoint by hand, and failed quietly the
first time rather than refusing.

``gh run download`` nests every artifact in a directory named after it
once more than one artifact is fetched, so a receipt uploaded as
``dependency-manifest`` lands at
``<dir>/dependency-manifest/dependency-manifest.json``. The readers in
:mod:`eawf.workflow.release.pipeline_receipts` and
:mod:`eawf.workflow.release.publication_receipt` look for the flat name
only, so a nested download reads as "the producer never ran".
:func:`flatten_artifacts` puts every expected file where the readers
look and names the ones the download did not carry.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)


def flatten_artifacts(raw_dir: Path, dest: Path, filenames: Iterable[str]) -> tuple[str, ...]:
    """Copy each of *filenames* found anywhere under *raw_dir* flat into *dest*.

    Args:
        raw_dir: Where ``gh run download`` unpacked the artifacts, nested
            or not.
        dest: The flat directory the receipt readers look in.
        filenames: The files expected, by basename.

    Returns:
        The basenames the download did not carry, in the order asked.

    Raises:
        ValueError: When one basename occurs more than once under
            *raw_dir*: two runs uploading the same receipt cannot be told
            apart, and picking one would pin an arbitrary leg.
    """
    dest.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []
    for name in filenames:
        found = sorted(raw_dir.rglob(name)) if raw_dir.is_dir() else []
        if not found:
            missing.append(name)
            continue
        if len(found) > 1:
            raise ValueError(
                f"{len(found)} downloaded files are named {name!r}; expected one per artifact"
            )
        shutil.copy2(found[0], dest / name)
    logger.info(f"flatten_artifacts dest={dest.name!r} missing={missing}")
    return tuple(missing)


__all__ = ["flatten_artifacts"]
