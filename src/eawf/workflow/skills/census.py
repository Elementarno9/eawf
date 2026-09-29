"""The epoch-1 noun census over shipped skill text and a rendered bundle.

After the cutover the lifecycle is Track, Milestone, Batch, Task and Run.
A shipped page, manifest or hook that still says phase, iter or wave -- or
spells a ``P01-I02-W03`` identifier -- sends a model or an operator after an
entity that no longer exists, so the census finds every such mention and
the packagers refuse to ship past one.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

#: The epoch-1 lifecycle nouns and the identifier shape built from them.
EPOCH1_NOUN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:phases?|iters?|waves?)\b|\bP\d{2,}-I\d{2,}(?:-W\d{2,})?\b", re.IGNORECASE
)


class CensusHit(BaseModel):
    """One epoch-1 noun found in one bundle file.

    Attributes:
        path: The file, relative to the bundle root, POSIX-spelled.
        line: The 1-based line the noun is on.
        noun: The text that matched.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    line: int
    noun: str


def epoch1_nouns(text: str) -> tuple[str, ...]:
    """Return every distinct epoch-1 noun in *text*, in first-seen order.

    Args:
        text: Any shipped text.

    Returns:
        The matched spellings, deduplicated; empty when there are none.
    """
    return tuple(dict.fromkeys(match.group(0) for match in EPOCH1_NOUN_PATTERN.finditer(text)))


def bundle_census(root: Path) -> tuple[CensusHit, ...]:
    """Census every text file under *root* for epoch-1 nouns.

    Args:
        root: The root of a rendered bundle.

    Returns:
        One hit per noun occurrence, in path then line order. A file that is
        not UTF-8 text carries no noun a model or operator would read.
    """
    hits: list[CensusHit] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(root).as_posix()
        for number, line in enumerate(text.splitlines(), start=1):
            hits.extend(
                CensusHit(path=relative, line=number, noun=match.group(0))
                for match in EPOCH1_NOUN_PATTERN.finditer(line)
            )
    logger.debug(f"bundle_census root={root.name} hits={len(hits)}")
    return tuple(hits)


__all__ = ["EPOCH1_NOUN_PATTERN", "CensusHit", "bundle_census", "epoch1_nouns"]
