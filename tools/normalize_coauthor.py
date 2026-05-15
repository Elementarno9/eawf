"""Commit-msg hook: collapse co-author trailer variants + dedupe by email.

Many runtimes emit ``Co-Authored-By`` lines with non-canonical name
spellings (``claude``, ``Claude (claude.ai/code)``), mixed-case emails,
or duplicate trailers when multiple tools touch the same message. This
hook rewrites every recognized trailer into the canonical form from
:mod:`coauthor_policy` and drops duplicates that share an email.

Resolution rules
----------------

- Email comparison is case-insensitive (``Foo@Anthropic.com`` ==
  ``foo@anthropic.com``); the canonical lowercase form is emitted.
- When an email maps to a registered runtime (Anthropic or OpenAI
  domain in :data:`coauthor_policy.SUPPORTED_TRAILERS`), the line is
  replaced with the canonical trailer string verbatim.
- Unknown trailers (third-party co-authors) are normalised
  whitespace-wise but otherwise kept intact, and still deduped by
  case-folded email.
- Dedupe is **first-write-wins**: the first occurrence of an email in
  reading order survives, later occurrences are dropped. The surviving
  line is rewritten to canonical form when known.
- Trailers stay in their existing relative order so reviewers can
  audit by reading top-down.

Idempotency
-----------

Running the hook twice on the same message produces byte-identical
output: the second pass sees only canonical trailers, finds nothing to
collapse, and rewrites the file with the same bytes.

Failure modes
-------------

The hook NEVER blocks a commit. On parse error or a missing message
file, the hook logs a warning to stderr and exits zero so unrelated
trailers (or empty messages on rebase / squash) are not penalised.

Exit codes
----------

- ``0`` — message left unchanged, or trailers were rewritten in place.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from pathlib import Path

from coauthor_policy import SUPPORTED_TRAILERS

_TRAILER_RE = re.compile(
    r"^Co-Authored-By:\s*(?P<name>[^<]+?)\s*<(?P<email>[^>]+)>\s*$",
    re.IGNORECASE,
)

# Map case-folded email -> canonical trailer string for known runtimes.
_CANONICAL_BY_EMAIL: dict[str, str] = {}
for _line in SUPPORTED_TRAILERS:
    _match = _TRAILER_RE.match(_line)
    if _match is not None:
        _CANONICAL_BY_EMAIL[_match.group("email").casefold()] = _line


def _parse_trailer(line: str) -> tuple[str, str] | None:
    """Return ``(name, email)`` if *line* is a ``Co-Authored-By`` trailer.

    Returns ``None`` when *line* is not a valid trailer; callers preserve
    the line untouched in that case.
    """
    match = _TRAILER_RE.match(line)
    if match is None:
        return None
    name = match.group("name").strip()
    email = match.group("email").strip()
    if not name or not email:
        return None
    return name, email


def _canonical_trailer(name: str, email: str) -> str:
    """Return the canonical trailer line for *(name, email)*.

    Known runtime emails resolve to the registered canonical string from
    :data:`coauthor_policy.SUPPORTED_TRAILERS`. Unknown emails fall back
    to ``Co-Authored-By: <name> <lowercased-email>`` so capitalisation
    drift in the email host does not produce phantom duplicates.
    """
    canonical = _CANONICAL_BY_EMAIL.get(email.casefold())
    if canonical is not None:
        return canonical
    return f"Co-Authored-By: {name} <{email.lower()}>"


def _split_trailer_block(lines: list[str]) -> tuple[list[str], list[str]]:
    """Split *lines* into ``(body, trailers)`` at the trailing trailer run.

    Walks backwards from the end, skipping blank lines, then collects
    every consecutive trailer line. The remaining prefix is the body
    (subject + paragraphs + intervening comments). Comments inside the
    trailer block are treated as body — git's own ``interpret-trailers``
    requires trailers to be uninterrupted.
    """
    end = len(lines)
    while end > 0 and lines[end - 1].strip() == "":
        end -= 1
    trailer_start = end
    while trailer_start > 0:
        candidate = lines[trailer_start - 1]
        if _parse_trailer(candidate) is None:
            break
        trailer_start -= 1
    body = lines[:trailer_start]
    trailers = lines[trailer_start:end]
    return body, trailers


def normalize(text: str) -> str:
    """Return *text* with trailer variants collapsed and duped emails dropped.

    The function is pure: it does not touch the filesystem and is safe
    to call repeatedly. Idempotency: ``normalize(normalize(t)) ==
    normalize(t)``.
    """
    lines = text.splitlines()
    body, trailers = _split_trailer_block(lines)
    if not trailers:
        return text

    seen: set[str] = set()
    kept: list[str] = []
    for line in trailers:
        parsed = _parse_trailer(line)
        if parsed is None:
            # Should not happen given _split_trailer_block guarantees,
            # but tolerate it: keep the raw line.
            kept.append(line.rstrip())
            continue
        name, email = parsed
        key = email.casefold()
        if key in seen:
            continue
        seen.add(key)
        kept.append(_canonical_trailer(name, email))

    rebuilt = list(body)
    # Strip trailing blank lines from body so we control the separator.
    while rebuilt and rebuilt[-1].strip() == "":
        rebuilt.pop()
    if rebuilt:
        rebuilt.append("")
    rebuilt.extend(kept)
    output = "\n".join(rebuilt)
    # Preserve trailing newline when the source had one.
    if text.endswith("\n"):
        output += "\n"
    return output


def normalize_file(message_path: Path) -> bool:
    """Rewrite *message_path* in place if normalisation changes the bytes.

    Returns ``True`` when the file was modified, ``False`` otherwise.
    Never raises; on read errors the path is left alone and the caller
    decides what to do (the hook entrypoint swallows + warns).
    """
    original = message_path.read_text(encoding="utf-8")
    rewritten = normalize(original)
    if rewritten == original:
        return False
    message_path.write_text(rewritten, encoding="utf-8")
    return True


def _warn(parts: Iterable[str]) -> None:
    print("normalize-coauthor: " + " ".join(parts), file=sys.stderr)


def main(argv: list[str]) -> int:
    """Commit-msg hook entrypoint.

    ABI: ``argv[1]`` is the path to ``.git/COMMIT_EDITMSG``. The hook
    NEVER exits non-zero — broken inputs trigger a stderr warning and a
    zero exit so the commit proceeds untouched.
    """
    if len(argv) < 2:
        _warn(["missing commit-message path"])
        return 0
    message_path = Path(argv[1])
    if not message_path.exists():
        _warn([f"missing file: {str(message_path)!r}"])
        return 0
    try:
        normalize_file(message_path)
    except OSError as exc:
        _warn([f"io error: {exc!r}"])
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
