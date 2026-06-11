"""Per-kind callables for the audit-check DSL (C03 W02 expansion).

Each module under :mod:`eawf.workflow.audit_dsl.kinds` ships one check-kind
callable matching :data:`~eawf.workflow.audit_dsl.registry.CheckFn`. The kinds
package is imported by :mod:`eawf.workflow.audit_dsl.registry` which binds the
callables into :data:`~eawf.workflow.audit_dsl.registry.CHECK_REGISTRY`.

The legacy five kinds (``file_exists``, ``path_glob_nonempty``,
``regex_in_file``, ``state_field_equals``, ``command_exit_zero``)
remain inlined in :mod:`eawf.workflow.audit_dsl.registry` for now; new kinds
introduced from C03 onward live here (``verify_implements``,
``criterion_in_diff``, ``schema_validate``, ``affordance_parity``,
``transition_coverage``, ``svg_well_formed``, ``svg_pixel_diff``) so each
kind's dependencies (e.g. :mod:`eawf.kernel.spec`, the TUI snapshot
harness, the lifecycle FSM tables, the ``xmllint`` / ``resvg`` CLIs) load
lazily.
"""

from __future__ import annotations

from eawf.workflow.audit_dsl.kinds.affordance_parity import check_affordance_parity
from eawf.workflow.audit_dsl.kinds.criterion_in_diff import check_criterion_in_diff
from eawf.workflow.audit_dsl.kinds.journal_chain import check_journal_chain
from eawf.workflow.audit_dsl.kinds.schema_validate import check_schema_validate
from eawf.workflow.audit_dsl.kinds.svg_pixel_diff import check_svg_pixel_diff
from eawf.workflow.audit_dsl.kinds.svg_well_formed import check_svg_well_formed
from eawf.workflow.audit_dsl.kinds.transition_coverage import check_transition_coverage
from eawf.workflow.audit_dsl.kinds.verify_implements import check_verify_implements

__all__ = [
    "check_affordance_parity",
    "check_criterion_in_diff",
    "check_journal_chain",
    "check_schema_validate",
    "check_svg_pixel_diff",
    "check_svg_well_formed",
    "check_transition_coverage",
    "check_verify_implements",
]
