"""Bump the single-source version literal in ``src/eawf/_version.py``.

The version grammar is the semver core ``MAJOR.MINOR.PATCH`` with an
optional PEP-440 pre-release segment (``a`` / ``b`` / ``rc`` + a
number), e.g. ``0.3.0`` or ``0.3.0a1``. This is the only rewrite path
for the version literal; the release pipeline drives it so the bumped
shape stays consistent and pip-resolvable.

Usage::

    uv run python tools/version_bump.py --minor
    uv run python tools/version_bump.py --minor --pre a   # 0.2.0 -> 0.3.0a1
    uv run python tools/version_bump.py --pre a           # 0.3.0a1 -> 0.3.0a2
    uv run python tools/version_bump.py --patch --dry-run

Exactly one bump dimension may be given; ``--pre`` is composable with a
dimension (bump then attach a fresh pre-release ``N1``) or used alone
(advance the existing pre-release counter, or attach ``N1`` when the
current version has no pre-release segment). Bumping a dimension
without ``--pre`` drops any existing pre-release segment, matching
semver's "a release supersedes its pre-releases" rule.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: Default location of the version module relative to the repo root.
_DEFAULT_VERSION_FILE = Path("src") / "eawf" / "_version.py"

#: PEP-440 pre-release phase tokens this bumper understands.
_PRE_PHASES: tuple[str, ...] = ("a", "b", "rc")

#: Grammar: ``MAJOR.MINOR.PATCH`` + optional ``(a|b|rc)N`` segment.
_VERSION_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:(?P<phase>a|b|rc)(?P<pre>\d+))?$"
)

#: Match the assignment line so the rewrite preserves surrounding text.
_ASSIGN_RE = re.compile(r'^(?P<prefix>__version__\s*=\s*")(?P<value>[^"]+)(?P<suffix>")$', re.M)


def parse_version(value: str) -> tuple[int, int, int, str | None, int | None]:
    """Parse a version string into its components.

    Args:
        value: A version string such as ``0.3.0`` or ``0.3.0a1``.

    Returns:
        A ``(major, minor, patch, phase, pre)`` tuple where ``phase`` is
        ``None`` for a final release and one of ``a`` / ``b`` / ``rc``
        otherwise, and ``pre`` is the pre-release counter (or ``None``).

    Raises:
        ValueError: When *value* does not match the supported grammar.
    """
    match = _VERSION_RE.match(value)
    if match is None:
        raise ValueError(f"unsupported version string: {value!r}")
    return (
        int(match["major"]),
        int(match["minor"]),
        int(match["patch"]),
        match["phase"],
        int(match["pre"]) if match["pre"] is not None else None,
    )


def format_version(major: int, minor: int, patch: int, phase: str | None, pre: int | None) -> str:
    """Render version components back to a PEP-440-compatible string.

    Args:
        major: Major component.
        minor: Minor component.
        patch: Patch component.
        phase: Pre-release phase (``a`` / ``b`` / ``rc``) or ``None``.
        pre: Pre-release counter, required when *phase* is set.

    Returns:
        The version string, e.g. ``0.3.0`` or ``0.3.0a1``.

    Raises:
        ValueError: When *phase* is set without a *pre* counter.
    """
    core = f"{major}.{minor}.{patch}"
    if phase is None:
        return core
    if pre is None:
        raise ValueError("pre-release phase requires a counter")
    return f"{core}{phase}{pre}"


def bump_version(
    current: str,
    *,
    dimension: str | None,
    pre_phase: str | None,
) -> str:
    """Compute the next version string from *current*.

    Args:
        current: The current version string.
        dimension: ``major`` / ``minor`` / ``patch`` or ``None`` when
            only the pre-release segment changes.
        pre_phase: ``a`` / ``b`` / ``rc`` to attach / advance, or
            ``None`` to produce a final release.

    Returns:
        The bumped version string.

    Raises:
        ValueError: When neither *dimension* nor *pre_phase* is given.
    """
    if dimension is None and pre_phase is None:
        raise ValueError("nothing to bump: pass a dimension and/or --pre")

    major, minor, patch, phase, pre = parse_version(current)

    if dimension == "major":
        major, minor, patch = major + 1, 0, 0
        phase, pre = None, None
    elif dimension == "minor":
        minor, patch = minor + 1, 0
        phase, pre = None, None
    elif dimension == "patch":
        patch += 1
        phase, pre = None, None

    if pre_phase is not None:
        if phase == pre_phase and dimension is None:
            pre = (pre or 0) + 1
        else:
            phase, pre = pre_phase, 1
    elif dimension is None:
        # ``--pre`` absent and no dimension: caller error caught above;
        # this branch is unreachable but keeps the type checker happy.
        raise ValueError("nothing to bump: pass a dimension and/or --pre")

    return format_version(major, minor, patch, phase, pre)


def read_current(version_file: Path) -> str:
    """Read the ``__version__`` literal from *version_file*.

    Args:
        version_file: Path to the version module.

    Returns:
        The current version string.

    Raises:
        FileNotFoundError: When *version_file* does not exist.
        ValueError: When no ``__version__`` assignment is found.
    """
    text = version_file.read_text()
    match = _ASSIGN_RE.search(text)
    if match is None:
        raise ValueError(f"no __version__ assignment in {version_file}")
    return match["value"]


def write_version(version_file: Path, new_version: str) -> None:
    """Rewrite the ``__version__`` literal in place.

    Args:
        version_file: Path to the version module.
        new_version: The version string to write.
    """
    text = version_file.read_text()
    updated = _ASSIGN_RE.sub(rf"\g<prefix>{new_version}\g<suffix>", text, count=1)
    version_file.write_text(updated)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="version_bump",
        description="Bump the single-source version in src/eawf/_version.py.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--major", action="store_true", help="Bump MAJOR (resets MINOR.PATCH).")
    group.add_argument("--minor", action="store_true", help="Bump MINOR (resets PATCH).")
    group.add_argument("--patch", action="store_true", help="Bump PATCH.")
    parser.add_argument(
        "--pre",
        choices=_PRE_PHASES,
        default=None,
        help="Attach / advance a PEP-440 pre-release segment (a | b | rc).",
    )
    parser.add_argument(
        "--file",
        type=Path,
        default=_DEFAULT_VERSION_FILE,
        help="Path to the version module (default: src/eawf/_version.py).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the bump without writing the file.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """CLI entrypoint: bump the version file and report the change.

    Args:
        argv: Argument vector without the program name.

    Returns:
        Process exit code (0 on success, 2 on usage / parse error).
    """
    args = _parse_args(argv)
    dimension = (
        "major" if args.major else "minor" if args.minor else "patch" if args.patch else None
    )
    if dimension is None and args.pre is None:
        print(
            "error: pass a bump dimension (--major/--minor/--patch) and/or --pre", file=sys.stderr
        )
        return 2

    try:
        current = read_current(args.file)
        new_version = bump_version(current, dimension=dimension, pre_phase=args.pre)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"{current} -> {new_version} (dry-run; {args.file} unchanged)")
        return 0

    write_version(args.file, new_version)
    print(f"{current} -> {new_version} (wrote {args.file})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
