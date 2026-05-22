"""event_jsonl source adapter — reader for the canonical eawf JSONL stores.

The projector ingests three canonical JSONL stores under ``<state_dir>/store``
(C09 §5.9.4): ``event.jsonl`` (typed :class:`~eawf.store.kinds.event.EventPayload`
envelopes), ``audit.jsonl`` (audit records), and the per-role report stores
(``<role>_report.jsonl``). This adapter is the single reader for all three: it
discovers the files under a project root and yields each line as a validated
:class:`~eawf.store.envelope.Envelope`.

A line that fails JSON parsing or :class:`Envelope` validation is **skipped**
with a logged ``WARNING`` carrying the file and 1-based line number, and the
scan continues with the next line (C09 §6 F3 — corrupt mid-line recovery).
A wholly missing store file is not an error: :meth:`EventJsonlSource.discover`
simply omits it, so a project that has never emitted a given store is skipped
without noise.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from pydantic import ValidationError

from eawf.state.enums import StoreKind
from eawf.store.envelope import Envelope
from eawf.store.paths import store_path

logger = logging.getLogger(__name__)

_ROLE_REPORT_KINDS: tuple[StoreKind, ...] = (
    StoreKind.RESEARCHER_REPORT,
    StoreKind.PLANNER_REPORT,
    StoreKind.EXECUTOR_REPORT,
    StoreKind.AUDITOR_REPORT,
    StoreKind.REVIEWER_REPORT,
    StoreKind.POLISHER_REPORT,
    StoreKind.OPERATOR_REPORT,
    StoreKind.DOMAIN_SPECIALIST_REPORT,
)

_STORE_KINDS: tuple[StoreKind, ...] = (StoreKind.EVENT, StoreKind.AUDIT, *_ROLE_REPORT_KINDS)


class EventJsonlSource:
    """Reader for the canonical eawf event / audit / role-report JSONL stores.

    Implements the :class:`~eawf.telemetry.sources.base.SessionSource` protocol
    over :class:`~eawf.store.envelope.Envelope` rows.
    """

    source_name = "event_jsonl"

    def discover(self, root: Path) -> Iterator[Path]:
        """Yield the existing canonical store files under *root*.

        *root* is a project state path (the ``.ea/state.json`` file or the
        ``.ea`` directory's state path); :func:`~eawf.store.paths.store_path`
        derives each ``<state_dir>/store/<kind>.jsonl`` location. Only files
        that exist are yielded — a never-emitted store is silently skipped
        (C09 §6 F2).
        """
        for kind in _STORE_KINDS:
            path = store_path(root, kind)
            if path.is_file():
                yield path

    def iter_rows(self, path: Path) -> Iterator[Envelope]:
        """Yield each line of *path* as a validated :class:`Envelope`.

        Lines that fail JSON parsing or :class:`Envelope` validation are
        skipped with a logged ``WARNING`` (file + 1-based line number) and the
        scan continues (C09 §6 F3). A missing *path* yields nothing.
        """
        if not path.is_file():
            return
        with path.open(encoding="utf-8") as handle:
            for line_no, raw in enumerate(handle, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    yield Envelope.model_validate_json(line)
                except ValidationError as exc:
                    logger.warning(
                        f"iter_rows source={self.source_name} path={str(path)!r} "
                        f"line={line_no} errors={exc.error_count()} skipped corrupt envelope"
                    )


__all__ = ["EventJsonlSource"]
