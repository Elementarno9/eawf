"""Typed ingestion of the pinned epoch-1 backlog corpus slice.

The backlog rules are evaluated against identifier populations that come
from four different places in the epoch-1 tree (waves, backlog, the
audits collection and the audit ledger). This module reads them as one
validated document so a rule test never has to reach into a raw dict.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from eawf.kernel.migration.epoch2.backlog import ResolutionCorpus
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel

logger = logging.getLogger(__name__)


class Epoch1BacklogCorpus(StrictMigrationModel):
    """The pinned epoch-1 slice the backlog rules are evaluated against.

    Attributes:
        source_revision: The repository revision the slice was taken at,
            so a measured count can always be reproduced.
        source_schema_version: The epoch-1 schema version at that
            revision.
        backlog: Every backlog row, keyed by id.
        wave_ids: Every canonical wave id.
        audit_document_ids: Ids in the ``audits`` document collection.
        audit_ledger_ids: Ids in the audit ledger.
    """

    source_revision: Annotated[str, Field(pattern=r"^[0-9a-f]{7,40}$")]
    source_schema_version: Annotated[str, Field(min_length=1)]
    backlog: dict[str, dict[str, Any]]
    wave_ids: tuple[str, ...]
    audit_document_ids: tuple[str, ...]
    audit_ledger_ids: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> Epoch1BacklogCorpus:
        """Read and validate the corpus slice at ``path``.

        Args:
            path: Location of the corpus JSON document.

        Returns:
            The validated corpus.

        Raises:
            FileNotFoundError: When ``path`` does not exist.
            json.JSONDecodeError: When the file is not valid JSON.
            ValidationError: When a key is unknown or a field is mis-shaped.
        """
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def audit_union_ids(self) -> frozenset[str]:
        """Return the audit population: the union of the document and the ledger.

        A row present in both counts once; a row present only in the
        ledger still resolves, because the ledger is part of the source
        and not a cache of the document.
        """
        return frozenset(self.audit_document_ids) | frozenset(self.audit_ledger_ids)

    def closed_rows(self) -> dict[str, dict[str, Any]]:
        """Return the backlog rows whose source status is ``closed``."""
        return {
            row_id: row for row_id, row in self.backlog.items() if row.get("status") == "closed"
        }

    def resolution_corpus(self) -> ResolutionCorpus:
        """Return the identifier populations the classifier resolves against.

        Raises:
            ValueError: When a wave id is not canonically shaped.
        """
        return ResolutionCorpus.build(
            wave_ids=self.wave_ids,
            backlog_ids=self.backlog.keys(),
            audit_ids=self.audit_union_ids(),
        )
