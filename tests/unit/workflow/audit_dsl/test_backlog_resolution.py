"""The ``backlog_resolution`` close-gate kind keeps its registered id."""

from __future__ import annotations

from eawf.workflow.audit_dsl.kinds.backlog_resolution import (
    BACKLOG_RESOLUTION_KIND,
)


def test_kind_constant_is_stable() -> None:
    # The registered kind string is the stable id the registry + the close-gate
    # view + the wired-on sweep all key on.
    assert BACKLOG_RESOLUTION_KIND == "backlog_resolution"
