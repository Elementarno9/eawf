"""Advisory Simplified Technical English (ASD-STE100) prose lint.

Rule, profile and skill text is read by agents, and a long sentence or a
semicolon is where a condition or an exception gets lost. The lint flags an
instruction sentence over 20 words, a description sentence over 25 words and
every sentence that carries a semicolon. It only reports: the exit status is
0 whatever it finds, so a commit is never blocked by prose style.

A YAML rule or profile source is read field by field: ``instruction`` holds
instructions, ``description``, ``rationale``, ``summary`` and ``when_to_use``
hold descriptions, and ``body_template`` holds markdown. In markdown, a list
item is an instruction and a paragraph is a description; fenced code,
headings, tables and HTML comments are skipped. Inline code counts as one
word and its semicolons are not prose.

Usage::

    uv run python -m eawf.platform.lint.ste_prose
    uv run python -m eawf.platform.lint.ste_prose PATH [PATH ...]
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict

INSTRUCTION_MAX_WORDS: Final = 20
DESCRIPTION_MAX_WORDS: Final = 25

SentenceKind = Literal["instruction", "description"]
FindingKind = Literal["instruction-length", "description-length", "semicolon"]

_LIMITS: Final[dict[SentenceKind, tuple[int, FindingKind]]] = {
    "instruction": (INSTRUCTION_MAX_WORDS, "instruction-length"),
    "description": (DESCRIPTION_MAX_WORDS, "description-length"),
}
_YAML_PROSE_KEYS: Final[dict[str, SentenceKind]] = {
    "instruction": "instruction",
    "description": "description",
    "rationale": "description",
    "summary": "description",
    "when_to_use": "description",
}
_YAML_MARKDOWN_KEYS: Final = frozenset({"body_template"})
_PLATFORM: Final = Path("src") / "eawf" / "platform"
_DEFAULT_GLOBS: Final = (
    _PLATFORM / "rules" / "data" / "modules" / "*.yaml",
    _PLATFORM / "profiles" / "data" / "*.yaml",
)

_INLINE_CODE: Final = re.compile(r"`[^`\n]*`")
_SENTENCE_END: Final = re.compile(r"(?<=[.!?])\s+(?=[^\sa-z])")
_WORD: Final = re.compile(r"\S*[A-Za-z0-9]\S*")
_LIST_ITEM: Final = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_SKIPPED_LINE: Final = re.compile(r"^\s*(?:#|\||<!--|>)")
_FENCE: Final = re.compile(r"^\s*(?:```|~~~)")


class SteFinding(BaseModel):
    """One advisory STE finding.

    Attributes:
        path: Where the text came from: a file path or ``skill:<name>``.
        line: The 1-based line the paragraph, item or field starts on.
        kind: Which limit the sentence breaks.
        detail: The measured value against the limit.
        snippet: The start of the offending sentence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    line: int
    kind: FindingKind
    detail: str
    snippet: str

    def render(self) -> str:
        """Return a ``path:line: kind detail: snippet`` one-liner."""
        return f"{self.path}:{self.line}: ste-{self.kind} {self.detail}: {self.snippet!r}"


def prose_findings(text: str, *, kind: SentenceKind, path: str, line: int) -> list[SteFinding]:
    """Check one block of running prose sentence by sentence.

    Args:
        text: The prose; line breaks inside it are treated as spaces.
        kind: Whether its sentences are instructions or descriptions.
        path: The source the findings name.
        line: The 1-based line the block starts on.

    Returns:
        One finding per over-long sentence and one per sentence with a
        semicolon, in sentence order; empty for empty text.
    """
    findings: list[SteFinding] = []
    flat = _INLINE_CODE.sub("CODE", " ".join(text.split()))
    for sentence in _SENTENCE_END.split(flat):
        if not sentence:
            continue
        snippet = sentence[:80]
        words = len(_WORD.findall(sentence))
        limit, length_kind = _LIMITS[kind]
        if words > limit:
            findings.append(
                SteFinding(
                    path=path,
                    line=line,
                    kind=length_kind,
                    detail=f"{words} words > {limit}",
                    snippet=snippet,
                )
            )
        if ";" in sentence:
            findings.append(
                SteFinding(
                    path=path,
                    line=line,
                    kind="semicolon",
                    detail=f"{sentence.count(';')} semicolon(s)",
                    snippet=snippet,
                )
            )
    return findings


def _markdown_blocks(text: str) -> list[tuple[int, SentenceKind, str]]:
    """Return ``(line, kind, prose)`` for each list item and paragraph."""
    blocks: list[tuple[int, SentenceKind, str]] = []
    current: list[str] = []
    start = 0
    kind: SentenceKind = "description"
    in_fence = False

    def flush() -> None:
        if current:
            blocks.append((start, kind, " ".join(current)))
            current.clear()

    for number, line in enumerate(text.splitlines(), start=1):
        if _FENCE.match(line):
            flush()
            in_fence = not in_fence
            continue
        if in_fence or not line.strip() or _SKIPPED_LINE.match(line):
            flush()
            continue
        item = _LIST_ITEM.match(line)
        if item:
            flush()
            line = line[item.end() :]
        if not current:
            start, kind = number, "instruction" if item else "description"
        current.append(line)
    flush()
    return blocks


def markdown_findings(text: str, *, path: str, line: int = 1) -> list[SteFinding]:
    """Check markdown: list items as instructions, paragraphs as descriptions.

    Args:
        text: The markdown source.
        path: The source the findings name.
        line: The 1-based line the markdown starts on in that source.

    Returns:
        The findings of every item and paragraph, in source order.
    """
    return [
        finding
        for start, kind, prose in _markdown_blocks(text)
        for finding in prose_findings(prose, kind=kind, path=path, line=line + start - 1)
    ]


def yaml_findings(text: str, *, path: str) -> list[SteFinding]:
    """Check the prose fields of a YAML rule or profile source.

    Args:
        text: The YAML source.
        path: The source the findings name.

    Returns:
        The findings of every prose field, in document order.

    Raises:
        yaml.YAMLError: When *text* is not YAML.
    """
    root = yaml.compose(text)
    return [] if root is None else _node_findings(root, path=path)


def _node_findings(node: yaml.Node, *, path: str) -> list[SteFinding]:
    """Return the findings of every prose field under *node*, in order."""
    if isinstance(node, yaml.SequenceNode):
        return [finding for child in node.value for finding in _node_findings(child, path=path)]
    if not isinstance(node, yaml.MappingNode):
        return []
    findings: list[SteFinding] = []
    for key, value in node.value:
        name = key.value if isinstance(key, yaml.ScalarNode) else None
        if isinstance(value, yaml.ScalarNode) and name in _YAML_PROSE_KEYS:
            line = value.start_mark.line + 1
            findings.extend(
                prose_findings(value.value, kind=_YAML_PROSE_KEYS[name], path=path, line=line)
            )
        elif isinstance(value, yaml.ScalarNode) and name in _YAML_MARKDOWN_KEYS:
            # A block scalar's text starts on the line after its key.
            findings.extend(
                markdown_findings(value.value, path=path, line=value.start_mark.line + 2)
            )
        else:
            findings.extend(_node_findings(value, path=path))
    return findings


def file_findings(path: Path) -> list[SteFinding]:
    """Check one file: YAML by its prose fields, anything else as markdown.

    Args:
        path: A UTF-8 text file.

    Returns:
        The file's findings in source order.

    Raises:
        FileNotFoundError: When *path* does not exist.
        UnicodeDecodeError: When *path* is not UTF-8 text.
        yaml.YAMLError: When a ``.yaml`` or ``.yml`` file is not YAML.
    """
    text = path.read_text(encoding="utf-8")
    where = path.as_posix()
    if path.suffix in {".yaml", ".yml"}:
        return yaml_findings(text, path=where)
    return markdown_findings(text, path=where)


def default_source_findings() -> list[SteFinding]:
    """Check the rule modules, the profiles and the skill bodies.

    The rule and profile globs resolve under the working directory, which is
    the repository root when pre-commit runs the hook.

    Returns:
        Every finding, file sources first, then skill bodies in registry
        order.
    """
    from eawf.surfaces.render.skills.registry import SKILL_REGISTRY

    findings = [
        finding
        for pattern in _DEFAULT_GLOBS
        for path in sorted(Path().glob(pattern.as_posix()))
        for finding in file_findings(path)
    ]
    for spec in SKILL_REGISTRY:
        findings.extend(markdown_findings(spec.body, path=f"skill:{spec.skill_name}"))
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    """Print every finding; always return 0, because the lint is advisory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args(argv)
    if args.paths:
        findings = [finding for path in args.paths for finding in file_findings(path)]
    else:
        findings = default_source_findings()
    for finding in findings:
        print(finding.render())
    print(f"ste-prose: {len(findings)} advisory finding(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DESCRIPTION_MAX_WORDS",
    "INSTRUCTION_MAX_WORDS",
    "SentenceKind",
    "SteFinding",
    "default_source_findings",
    "file_findings",
    "main",
    "markdown_findings",
    "prose_findings",
    "yaml_findings",
]
