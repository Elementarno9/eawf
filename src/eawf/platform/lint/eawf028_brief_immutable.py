"""EAWF028 — the brief-immutability guard: a committed research brief keeps its wording.

A research brief under ``.ea/artifacts/research/`` is evidence: decisions and audits cite
it, so once committed its words stay as they were read. A later finding goes into a new,
dated brief that supersedes the old one. Format repairs are still welcome, because they
change how the brief renders and not what it says.

This lint holds that line on every staged edit. It compares each modified or renamed brief
in the index against its ``HEAD`` version by the hash of its word sequence, so these pass:

- rewrapping, trailing spaces, line endings and runs of blank lines;
- a language tag added to or changed on an opening code fence;
- table pipes escaped, unescaped or realigned, and the table delimiter row redrawn.

Any other change to the word sequence is refused with ``brief_immutable``, naming the file,
the first changed words and the remediation. A new brief is not an edit, and a removal is
the deletion rule's concern, so neither is checked here.

The production call-site is ``eawf hook eawf028-brief-immutable``.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from eawf.kernel.spec.common import ARTIFACT_KIND_SUBDIR

RULE_CODE = "EAWF028"

#: The refusal code a wording edit carries.
REFUSAL_CODE: Final = "brief_immutable"

#: Repo-relative directory the committed research briefs live beneath.
BRIEF_ROOT: Final = f".ea/artifacts/{ARTIFACT_KIND_SUBDIR['research']}/"

#: What the refusal tells the author to do instead.
REMEDIATION: Final = (
    f"file a superseding brief (a new dated file under {BRIEF_ROOT}) "
    "instead of editing a committed one"
)

#: The info string of an opening code fence; the fence itself is kept.
_FENCE_INFO_RE: Final = re.compile(r"^([ \t]*(?:```|~~~))[^\n`]*$", re.MULTILINE)
#: A table delimiter row such as ``|---|:--:|``.
_DELIMITER_ROW_RE: Final = re.compile(r"^[ \t]*\|?(?:[ \t]*:?-+:?[ \t]*\|)+[ \t]*:?-*:?[ \t]*$")
#: A table pipe, escaped or not.
_PIPE_RE: Final = re.compile(r"\\?\|")

#: How many changed words a finding quotes on each side.
_EXCERPT_WORDS: Final = 8


def words(text: str) -> list[str]:
    """Return the word sequence of a brief, with its format stripped.

    Args:
        text: The brief's markdown.

    Returns:
        The whitespace-separated words left once opening-fence info strings, table
        delimiter rows and table pipes are removed.
    """
    kept = (line for line in text.splitlines() if not _DELIMITER_ROW_RE.match(line))
    stripped = _FENCE_INFO_RE.sub(r"\1", "\n".join(kept))
    return _PIPE_RE.sub(" ", stripped).split()


def wording_hash(text: str) -> str:
    """Return the SHA-256 of a brief's word sequence, which a format repair leaves unchanged."""
    return hashlib.sha256(" ".join(words(text)).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BriefFinding:
    """One committed brief whose wording a staged edit changes.

    Attributes:
        path: The brief's path in the index.
        before: The first removed words, or empty when words were only added.
        after: The first added words, or empty when words were only removed.
    """

    path: str
    before: str
    after: str

    def render(self) -> str:
        """Return the ``path: CODE refusal`` diagnostic with its remediation."""
        return (
            f"{self.path}: {RULE_CODE} {REFUSAL_CODE}: wording changed "
            f"({self.before!r} -> {self.after!r}); {REMEDIATION}"
        )


def check_brief(path: str, before: str, after: str) -> BriefFinding | None:
    """Return the finding for one edited brief, or ``None`` for a format-only repair.

    Args:
        path: The name the finding carries.
        before: The committed text.
        after: The edited text.

    Returns:
        ``None`` when both texts share a wording hash; otherwise a finding quoting the
        first changed span of words.
    """
    if wording_hash(before) == wording_hash(after):
        return None
    old, new = words(before), words(after)
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    _, i1, i2, j1, j2 = next(op for op in matcher.get_opcodes() if op[0] != "equal")
    return BriefFinding(
        path=path,
        before=" ".join(old[i1:i2][:_EXCERPT_WORDS]),
        after=" ".join(new[j1:j2][:_EXCERPT_WORDS]),
    )


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo_root, check=True, capture_output=True, text=True
    ).stdout


def staged_edits(repo_root: Path) -> list[tuple[str, str]]:
    """Return each staged modified or renamed brief as ``(HEAD path, index path)``.

    Raises:
        subprocess.CalledProcessError: ``repo_root`` is not a git work tree.
    """
    fields = _git(
        repo_root,
        "diff",
        "--cached",
        "--name-status",
        "-z",
        "-M",
        "--diff-filter=MR",
        "--",
        BRIEF_ROOT,
    ).split("\0")
    edits: list[tuple[str, str]] = []
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index]
        if status.startswith("R"):
            old, new = fields[index + 1], fields[index + 2]
            index += 3
        else:
            old = new = fields[index + 1]
            index += 2
        if new.endswith(".md"):
            edits.append((old, new))
    return edits


def staged_texts(repo_root: Path, old: str, new: str) -> tuple[str, str]:
    """Return a staged brief's committed text at ``old`` and its index text at ``new``.

    Raises:
        subprocess.CalledProcessError: Either path is absent from its revision.
    """
    return _git(repo_root, "show", f"HEAD:{old}"), _git(repo_root, "show", f":{new}")


__all__ = [
    "BRIEF_ROOT",
    "REFUSAL_CODE",
    "REMEDIATION",
    "RULE_CODE",
    "BriefFinding",
    "check_brief",
    "staged_edits",
    "staged_texts",
    "wording_hash",
    "words",
]
