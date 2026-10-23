"""The epoch-1 noun census over shipped skill text and a rendered bundle.

After the cutover the lifecycle is Track, Milestone, Batch, Task and Run.
A shipped page, manifest or hook that still says phase, iter or wave --
bare or joined into an identifier such as ``wave_close`` --, spells a
``P<NN>-I<NN>-W<NN>`` identifier or invokes a retired skill such as ``/prep``
sends a model or an operator after an entity that no longer exists, so the
census finds every such mention and the packagers refuse to ship past one.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

from eawf.workflow.skills.catalog import SKILL_CATALOG

logger = logging.getLogger(__name__)

_RETIRED_IDS: Final[str] = "|".join(
    re.escape(row.skill_id)
    for row in sorted(SKILL_CATALOG.retired, key=lambda row: -len(row.skill_id))
)

#: Identifiers built on an epoch-1 noun that name something else, matched
#: whole and case-blind. Python's iteration protocol names its generators
#: this way; a Run control, a tool or host call, a transcript event and an
#: integration lock each move through typed stages, and a release version
#: carries a pre-release segment. None of them is an epoch-1 lifecycle
#: entity, so the census skips them instead of reporting noise.
_HOMONYMS: Final[tuple[str, ...]] = (
    "iter_agent_reports",
    "iter_child_nodes",
    "iter_modules",
    "iter_rows",
    "iter_subscriber_pushes",
    "_declared_phase_dispositions",
    "_disposition_belongs_to_the_phase",
    "_fields_match_the_phase",
    "_host_tool_phases",
    "_outcome_belongs_to_the_result_phase",
    "_payload_and_phase_match_the_kind",
    "_payload_phases",
    "_state_phase",
    "_status_belongs_to_the_terminal_phase",
    "_summary_belongs_to_the_summarized_phase",
    "compile_phase_dispositions",
    "compile_phase_locks",
    "gate_phase",
    "locked_phase",
    "missing_phase_rows",
    "missing_phases",
    "phase_dispositions",
    "phase_locks",
    "phase_order",
    "pre_phase",
    "state_tool_phase",
    "tool_phase_kinds",
    "unlocked_phases",
)

#: A slash invocation of a retired skill, the epoch-1 lifecycle nouns and
#: the identifier shape built from them. A noun joined into an identifier by
#: underscores (``phase_open``, ``active_wave``) matches as the whole
#: identifier, while a longer word (``phaseless``), a path segment
#: (``docs/prep``), a call of Python's ``iter()`` and a :data:`_HOMONYMS`
#: identifier do not. The slash branch comes first so ``/wave-spec``
#: reports whole.
EPOCH1_NOUN_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"(?<![\w./-])/(?:{_RETIRED_IDS})(?![\w-])"
    rf"|\b(?!(?:{'|'.join(_HOMONYMS)})\b)(?!iter\()(?:\w+_)?(?:phases?|iters?|waves?)(?:_\w+)?\b"
    r"|\bP\d{2,}-I\d{2,}(?:-W\d{2,})?\b",
    re.IGNORECASE,
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
