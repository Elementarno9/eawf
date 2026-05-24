"""Per-kind callables for the audit-check DSL (C03 W02 expansion).

Each module under :mod:`eawf.audit_dsl.kinds` ships one check-kind
callable matching :data:`~eawf.audit_dsl.registry.CheckFn`. The kinds
package is imported by :mod:`eawf.audit_dsl.registry` which binds the
callables into :data:`~eawf.audit_dsl.registry.CHECK_REGISTRY`.

The legacy five kinds (``file_exists``, ``path_glob_nonempty``,
``regex_in_file``, ``state_field_equals``, ``command_exit_zero``)
remain inlined in :mod:`eawf.audit_dsl.registry` for now; new kinds
introduced from C03 onward live here (``verify_implements``,
``criterion_in_diff``) so each kind's dependencies (e.g.
:mod:`eawf.kernel.spec`) load lazily.
"""

from __future__ import annotations

from eawf.audit_dsl.kinds.criterion_in_diff import check_criterion_in_diff
from eawf.audit_dsl.kinds.verify_implements import check_verify_implements

__all__ = ["check_criterion_in_diff", "check_verify_implements"]
