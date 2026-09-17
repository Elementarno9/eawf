"""A byte-reproducible tarball of the rendered plugin tree.

The source-host leg attaches the plugin bundle to the tag release, and
``SHA256SUMS`` plus the frozen manifest both pin its digest. A plain
``tar -czf`` cannot be pinned ahead of the tag: every archive member
carries the mtime of the checkout that rendered it, the owner of the
runner that packed it, and whatever order the filesystem listed it in,
and the gzip header adds the wall clock on top. Two renders of one
commit therefore never agree, and a manifest frozen from the dry run
describes a bundle the tag build does not produce.

This module removes every one of those inputs:

* members are written in path order, each directory before its
  children, so the listing order of the filesystem does not matter;
* every mtime is ``SOURCE_DATE_EPOCH``, the tagged commit's committer
  time, which is a property of the source rather than of the runner;
* owners and groups are numeric ``0`` with no names;
* modes collapse to ``0o755`` or ``0o644``, so a checkout's umask does
  not leak in while an executable hook stays executable;
* the gzip header carries no file name and a zero timestamp.

It is a ``python -m`` entry point for the same reason the receipt
producer is: its one caller is the release workflow, and a bundle built
from a laptop tree is not an artifact anyone should publish.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import logging
import os
import stat
import sys
import tarfile
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from eawf.workflow.release.reproducibility import SOURCE_DATE_EPOCH_ENV

logger = logging.getLogger(__name__)

#: Mode of every directory and of every file with any executable bit.
EXECUTABLE_MODE: Final[int] = 0o755

#: Mode of every file without an executable bit.
REGULAR_MODE: Final[int] = 0o644

#: Owner and group id every member is recorded under.
ROOT_ID: Final[int] = 0

#: Fixed so a Python whose default level changes still writes the same
#: deflate stream.
GZIP_LEVEL: Final[int] = 9

#: Pinned because the ``tarfile`` default has changed before, and a new
#: default would change the header bytes of every member.
TAR_FORMAT: Final[int] = tarfile.PAX_FORMAT

#: Exit code for a missing or malformed ``SOURCE_DATE_EPOCH``: the
#: environment is wrong, not the tree.
EXIT_USAGE: Final[int] = 2


def bundle_members(source: Path) -> tuple[str, ...]:
    """Return the POSIX path of every entry under *source*, in archive order.

    Hidden entries are included: the rendered plugin payload lives in
    dotdirs, and a walk that skipped them would bundle an empty shell.

    Args:
        source: Root of the rendered tree.

    Returns:
        Paths relative to *source*, ordered by path segments so every
        directory precedes its own children.
    """
    names: list[str] = []
    for dirpath, dirnames, filenames in os.walk(source):
        parent = Path(dirpath).relative_to(source)
        names.extend((parent / entry).as_posix() for entry in (*dirnames, *filenames))
    return tuple(sorted(names, key=lambda name: name.split("/")))


def _member_info(path: Path, *, name: str, epoch: int) -> tarfile.TarInfo:
    """Return the normalised header for the entry at *path*.

    The header is built field by field rather than through
    ``TarFile.gettarinfo``, which records hard links and host owner
    names that differ between two checkouts of one commit.

    Raises:
        ValueError: When *path* is neither a directory nor a regular
            file. A symlink would publish a pointer into the builder's
            filesystem, and a device or pipe has no content to ship.
    """
    status = path.lstat()
    info = tarfile.TarInfo(name)
    info.mtime = epoch
    info.uid = info.gid = ROOT_ID
    info.uname = info.gname = ""
    if stat.S_ISDIR(status.st_mode):
        info.type = tarfile.DIRTYPE
        info.mode = EXECUTABLE_MODE
    elif stat.S_ISREG(status.st_mode):
        info.type = tarfile.REGTYPE
        info.size = status.st_size
        executable = status.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        info.mode = EXECUTABLE_MODE if executable else REGULAR_MODE
    else:
        raise ValueError(f"cannot bundle {name!r}: only directories and regular files are allowed")
    return info


def build_bundle(source: Path, destination: Path, *, epoch: int) -> str:
    """Write the reproducible gzip tarball of *source* to *destination*.

    Args:
        source: Root of the rendered plugin tree; its entries become the
            archive's top-level members.
        destination: Bundle file to write. Its parent must exist.
        epoch: ``SOURCE_DATE_EPOCH`` every member's mtime is pinned to.

    Returns:
        The hex SHA-256 of the written bundle.

    Raises:
        ValueError: When *epoch* is negative, *source* is not a
            directory or holds nothing, *destination* lies inside
            *source* (the walk would archive the half-written bundle),
            or the tree holds an entry that is not a directory or a
            regular file.
        OSError: When a member cannot be read or the bundle written.
    """
    if epoch < 0:
        raise ValueError(f"{SOURCE_DATE_EPOCH_ENV} must be non-negative, got {epoch}")
    if not source.is_dir():
        raise ValueError(f"plugin tree {source} is not a directory")
    if destination.resolve().is_relative_to(source.resolve()):
        raise ValueError(f"bundle {destination} must be written outside the tree it archives")
    names = bundle_members(source)
    if not names:
        raise ValueError(f"plugin tree {source} is empty; refusing to bundle nothing")
    with (
        destination.open("wb") as raw,
        gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, compresslevel=GZIP_LEVEL, mtime=0
        ) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=TAR_FORMAT) as archive,
    ):
        for name in names:
            path = source / name
            info = _member_info(path, name=name, epoch=epoch)
            if info.isreg():
                with path.open("rb") as content:
                    archive.addfile(info, content)
            else:
                archive.addfile(info)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    logger.info(
        f"build_bundle members={len(names)} epoch={epoch} "
        f"bundle={destination.name!r} sha256={digest[:12]!r}"
    )
    return digest


def main(argv: Sequence[str] | None = None) -> int:
    """Bundle the tree named on the command line under ``SOURCE_DATE_EPOCH``.

    Args:
        argv: Argument vector; defaults to the process arguments.

    Returns:
        ``0`` after printing the bundle's ``sha256sum`` line,
        :data:`EXIT_USAGE` when ``SOURCE_DATE_EPOCH`` is unset or not an
        integer, ``1`` when the tree cannot be bundled. The epoch is
        never defaulted to the wall clock, which is the one value that
        guarantees the bundle cannot be reproduced.
    """
    parser = argparse.ArgumentParser(prog="eawf-plugin-bundle")
    parser.add_argument("source", help="rendered plugin tree to archive")
    parser.add_argument("destination", help="bundle file to write")
    args = parser.parse_args(argv)
    raw_epoch = os.environ.get(SOURCE_DATE_EPOCH_ENV, "")
    try:
        epoch = int(raw_epoch)
    except ValueError:
        print(
            f"plugin bundle needs {SOURCE_DATE_EPOCH_ENV} set to an integer, got {raw_epoch!r}",
            file=sys.stderr,
        )
        return EXIT_USAGE
    destination = Path(args.destination)
    try:
        digest = build_bundle(Path(args.source), destination, epoch=epoch)
    except (OSError, ValueError) as exc:
        print(f"plugin bundle failed: {exc}", file=sys.stderr)
        return 1
    print(f"{digest}  {destination.name}")
    return 0


__all__ = [
    "EXECUTABLE_MODE",
    "EXIT_USAGE",
    "GZIP_LEVEL",
    "REGULAR_MODE",
    "ROOT_ID",
    "TAR_FORMAT",
    "build_bundle",
    "bundle_members",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
