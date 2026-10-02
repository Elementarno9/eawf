"""Print every leaf command path of the installed ``eawf`` CLI, one per line.

The retired-verb census compares a released command tree with HEAD, and
an old release cannot be imported beside HEAD, so its tree is captured
once into a committed fixture. Regenerate that fixture from a throwaway
worktree and venv of the release, run from the repository root::

    git worktree add --detach "$TMP/v068" v0.6.8
    uv venv "$TMP/venv068" --python 3.14
    VIRTUAL_ENV="$TMP/venv068" uv pip install --no-config -e "$TMP/v068"
    "$TMP/venv068/bin/python" tools/command_tree_paths.py \
        > tests/fixtures/cli/v068_command_paths.txt
    git worktree remove "$TMP/v068"

Hidden commands are listed too: a hidden verb still ran in that release.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import click
import typer


def leaf_paths(command: click.Command, path: tuple[str, ...] = ()) -> Iterator[str]:
    """Yield the path after ``eawf`` of every leaf below ``command``.

    Args:
        command: The group or command to walk.
        path: The words naming ``command`` itself.

    Yields:
        Each leaf command path, words separated by one space.
    """
    if isinstance(command, click.Group):
        for name, child in command.commands.items():
            yield from leaf_paths(child, (*path, name))
    else:
        yield " ".join(path)


def main() -> int:
    from eawf.surfaces.cli.app import app

    for path in sorted(leaf_paths(typer.main.get_command(app))):
        sys.stdout.write(f"{path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
