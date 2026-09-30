"""The questions and pauses a tree holds, read for the decision overlays that draw them.

A question a host asked and the pause over the Run that waits on it live on the run
ledger, and a question a Campaign filed lives in the document; neither is a row the
Attention register carries in full. Both are read beside the register through the
daemon's own reads, each record arriving with the situation the daemon's projection put
it in, so the question and pause details draw what the daemon computed rather than a
guess. They are read on the Attention route, where a question is answered from its row
and the pause over its Run opens from the row beside it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from eawf.runtime.daemon.methods.pause import PAUSE_READ_METHOD, PausesAnswer
from eawf.runtime.daemon.methods.question import QUESTION_READ_METHOD, QuestionsAnswer
from eawf.surfaces.tui.console.decisions import PauseRecord, QuestionRecord

if TYPE_CHECKING:
    from eawf.surfaces.tui.console.live_reads import LiveReadHost

logger = logging.getLogger(__name__)

#: The address the Attention route reads the tree's records under: the whole tree.
TREE_ADDRESS: Final = "tree"

#: The route whose rows a question is answered from.
ATTENTION_ROUTE: Final = "attention"

#: The name the read is owed under, on the Attention route, where a question is answered
#: from its row and the pause over its Run opens from the row beside it.
OPEN_RECORDS_READ: Final = "open_records.attention"


@dataclass(frozen=True, slots=True, kw_only=True)
class HeldOpenRecords:
    """The questions and pauses of a tree, as last read.

    Attributes:
        questions: Every question, each with the situation the daemon projected.
        pauses: Every pause, each with the situation the daemon projected.
        run_states: The state of each Run a question or pause names, by Run key.
    """

    questions: tuple[QuestionRecord, ...] = ()
    pauses: tuple[PauseRecord, ...] = ()
    run_states: Mapping[str, str] = field(default_factory=dict)


async def fetch_open_records(host: LiveReadHost, address: str) -> HeldOpenRecords:
    """Read every question and pause of the tree, projected for the console's principal.

    Args:
        host: The seam, which knows who the console acts as.
        address: What the read is about; the whole tree either way.
    """
    operator = host.operator
    params = {"principal": operator.principal} if operator is not None else {}
    questions = QuestionsAnswer.model_validate(await host.call(QUESTION_READ_METHOD, params))
    pauses = PausesAnswer.model_validate(await host.call(PAUSE_READ_METHOD, {}))
    logger.debug(
        f"fetch_open_records address={address} questions={len(questions.questions)} "
        f"pauses={len(pauses.pauses)}"
    )
    return HeldOpenRecords(
        questions=tuple(
            QuestionRecord.of_question(
                item.question, asked_by_run=item.asked_by_run, situation=item.situation
            )
            for item in questions.questions
        ),
        pauses=tuple(
            PauseRecord.of_pause(item.pause, situation=item.situation) for item in pauses.pauses
        ),
        run_states={**questions.run_states, **pauses.run_states},
    )


def attention_address(host: LiveReadHost) -> str | None:
    """Return the tree, once the Attention register the records sit beside is held."""
    return TREE_ADDRESS if host.projection_for(ATTENTION_ROUTE) is not None else None


__all__ = [
    "ATTENTION_ROUTE",
    "OPEN_RECORDS_READ",
    "TREE_ADDRESS",
    "HeldOpenRecords",
    "attention_address",
    "fetch_open_records",
]
