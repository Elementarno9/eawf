"""Prepare-commit-msg hook: append a recognized ``Co-Authored-By`` trailer.

The active harness trailer is appended whenever the commit message does
not already carry a recognized Claude or Codex trailer. If no harness is
detected, the message is left unchanged.

The hook deliberately stays silent on merge / squash / fixup / commit
``--amend -m`` invocations where the existing trailer set is what the
operator asked for. Pre-commit passes the commit-source as ``argv[2]``
when present.

Exit codes:

- ``0`` — message left as-is, or trailer appended.
- ``0`` even on missing message file (pre-commit invokes this on every
  commit; we never want to block via this hook).
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

from coauthor_policy import has_supported_trailer, select_trailer

_SKIP_SOURCES: frozenset[str] = frozenset({"merge", "squash", "commit"})


def _strip_comments(text: str) -> str:
    """Drop pre-commit-injected scissor / instruction lines from *text*."""
    keep: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        keep.append(line)
    return "\n".join(keep).rstrip()


def _trailer_present(text: str) -> bool:
    return has_supported_trailer(text)


def append_trailer(message_path: Path, env: Mapping[str, str] | None = None) -> bool:
    """Append the active harness trailer to *message_path* if absent.

    Returns ``True`` when the file was modified, ``False`` otherwise.
    """
    text = message_path.read_text(encoding="utf-8")
    if _trailer_present(text):
        return False
    trailer = select_trailer(os.environ if env is None else env)
    if trailer is None:
        return False
    body = _strip_comments(text)
    if not body:
        return False
    sep = "\n" if body.endswith("\n") else "\n\n"
    new_text = body + sep + trailer + "\n"
    message_path.write_text(new_text, encoding="utf-8")
    return True


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 0
    message_path = Path(argv[1])
    if not message_path.exists():
        return 0
    source = argv[2] if len(argv) > 2 else ""
    if source in _SKIP_SOURCES:
        return 0
    append_trailer(message_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
