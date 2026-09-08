"""Stable failure codes the epoch-2 importer raises.

Each error carries a machine-readable ``code`` because the cutover plan
reports failures as codes, not as prose: an operator triaging a refused
import matches on the code and the surrounding tooling never has to
parse an English message.
"""

from __future__ import annotations

from typing import ClassVar


class MigrationRuleError(Exception):
    """Base class for a refusal raised by an importer rule.

    Attributes:
        code: The stable failure code reported in the cutover plan.
    """

    code: ClassVar[str] = "migration_rule_error"


class MigrationCountMismatchError(MigrationRuleError):
    """A source row reached no arm of a rule that must be total.

    Raised when a source status falls outside the closed status map, or
    when a closed backlog row reaches none of the four classifier arms.
    Either way the source census and the target census can no longer
    reconcile, so the plan fails rather than importing the row under a
    guessed state.
    """

    code: ClassVar[str] = "migration_count_mismatch"


class MigrationFabricationDetectedError(MigrationRuleError):
    """A rule was about to emit a record the source does not support.

    The importer preserves source facts; it never invents one to make a
    reference resolve or a field non-null.
    """

    code: ClassVar[str] = "migration_fabrication_detected"
