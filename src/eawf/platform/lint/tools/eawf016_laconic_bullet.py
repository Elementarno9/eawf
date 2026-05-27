"""EAWF016 — reject generic or verbose release changelog bullets."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

RULE_CODE = "EAWF016"
MAX_BULLET_CHARS = 140

_GENERIC_WORDS = {
    "change",
    "changes",
    "cleanup",
    "cleanups",
    "fix",
    "fixes",
    "improvement",
    "improvements",
    "misc",
    "miscellaneous",
    "polish",
    "refactor",
    "refactors",
    "stuff",
    "tbd",
    "todo",
    "update",
    "updates",
    "various",
    "work",
    "wip",
}
_GENERIC_PHRASES = {
    "bug fixes",
    "minor fixes",
    "misc updates",
    "miscellaneous updates",
    "other changes",
    "various fixes",
    "various improvements",
    "various updates",
}
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+_-]*")


@dataclass(frozen=True)
class LaconicBulletViolation:
    """One EAWF016 finding."""

    lineno: int
    col_offset: int
    snippet: str
    reason: str

    @property
    def code(self) -> str:
        """Return the rule code."""
        return RULE_CODE

    def render(self) -> str:
        """Return a ``line:col: CODE reason`` style one-liner body."""
        return f"{self.lineno}:{self.col_offset}: {RULE_CODE} {self.reason}: {self.snippet!r}"


def _unreleased_bounds(lines: list[str]) -> tuple[int, int] | None:
    start = next(
        (index for index, line in enumerate(lines) if line.strip() == "## [Unreleased]"),
        None,
    )
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return start, end


def _normalised_words(text: str) -> list[str]:
    without_markup = text.replace("`", " ").replace("*", " ")
    return _WORD_RE.findall(without_markup.casefold())


def _is_generic_bullet(text: str) -> bool:
    words = _normalised_words(text)
    if not words:
        return True
    phrase = " ".join(words).strip()
    if phrase in _GENERIC_PHRASES:
        return True
    return all(word in _GENERIC_WORDS for word in words)


def _bullet_violation(lineno: int, line: str) -> LaconicBulletViolation | None:
    stripped = line.strip()
    content = stripped[2:].strip()
    if _is_generic_bullet(content):
        return LaconicBulletViolation(
            lineno=lineno,
            col_offset=line.find("- "),
            snippet=stripped[:100],
            reason="release bullet is generic; name the shipped behavior",
        )
    if len(content) > MAX_BULLET_CHARS:
        return LaconicBulletViolation(
            lineno=lineno,
            col_offset=line.find("- "),
            snippet=stripped[:100],
            reason=f"release bullet exceeds {MAX_BULLET_CHARS} characters",
        )
    return None


def check_source(source: str) -> list[LaconicBulletViolation]:
    """Return EAWF016 violations for ``CHANGELOG.md`` ``[Unreleased]`` bullets."""
    lines = source.splitlines()
    bounds = _unreleased_bounds(lines)
    if bounds is None:
        return []
    start, end = bounds
    violations: list[LaconicBulletViolation] = []
    previous_bullet_lineno: int | None = None
    for index in range(start + 1, end):
        line = lines[index]
        stripped = line.strip()
        lineno = index + 1
        if not stripped or stripped.startswith("### "):
            previous_bullet_lineno = None
            continue
        if line.startswith((" ", "\t")) and previous_bullet_lineno is not None:
            violations.append(
                LaconicBulletViolation(
                    lineno=lineno,
                    col_offset=len(line) - len(line.lstrip()),
                    snippet=stripped[:100],
                    reason=(
                        "release bullet spans multiple physical lines; keep one laconic line "
                        f"after line {previous_bullet_lineno}"
                    ),
                )
            )
            continue
        previous_bullet_lineno = None
        if not stripped.startswith("- "):
            continue
        previous_bullet_lineno = lineno
        violation = _bullet_violation(lineno, line)
        if violation is not None:
            violations.append(violation)
    return violations


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the EAWF016 gate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, default=[Path("CHANGELOG.md")])
    args = parser.parse_args(argv)
    rows: list[str] = []
    scanned = 0
    for path in args.paths:
        try:
            source = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError as exc:
            print(f"eawf016-laconic-bullet: cannot read {path}: {exc}", file=sys.stderr)
            return 1
        scanned += 1
        rows.extend(f"  {path}:{violation.render()}" for violation in check_source(source))
    if rows:
        print(f"eawf016-laconic-bullet: {len(rows)} violation(s) across {scanned} file(s)")
        print("\n".join(rows))
        return 1
    print(f"eawf016-laconic-bullet: clean ({scanned} file(s) scanned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MAX_BULLET_CHARS",
    "RULE_CODE",
    "LaconicBulletViolation",
    "check_source",
    "main",
]
