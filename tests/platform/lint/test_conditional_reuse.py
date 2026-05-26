"""Pin the W15 reuse contract: ``changed_files`` is reachable from
:mod:`eawf.workflow.audit_dsl.registry`.

The W15 hardening of the audit_dsl runner reaches into
:func:`eawf.platform.lint._conditional.changed_files` to resolve the
``scope`` kwarg into a concrete file set. The helper is already public
(it lives in :data:`~eawf.platform.lint._conditional.__all__`) but the
test pins the contract so a future rename in ``_conditional.py`` cannot
silently break the audit_dsl runner.
"""

from __future__ import annotations


def test_changed_files_is_public_export() -> None:
    """``changed_files`` is listed in the module's __all__."""
    from eawf.platform.lint import _conditional

    assert "changed_files" in _conditional.__all__


def test_changed_files_reachable_from_audit_dsl_registry() -> None:
    """The registry imports it under the same symbol name (no rename)."""
    from eawf.platform.lint._conditional import changed_files as platform_changed_files
    from eawf.workflow.audit_dsl.registry import changed_files as registry_changed_files

    assert platform_changed_files is registry_changed_files


def test_changed_files_signature_matches_w15_contract() -> None:
    """Pinned signature: ``(base=DEFAULT_DIFF_BASE, *, cwd=None, timeout=5.0)``.

    The audit_dsl runner threads ``cwd=`` as a kwarg, so the helper must
    keep ``cwd`` as a keyword-only parameter.
    """
    import inspect

    from eawf.platform.lint._conditional import changed_files

    sig = inspect.signature(changed_files)
    params = sig.parameters
    assert "base" in params
    assert "cwd" in params
    assert params["cwd"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "timeout" in params
    assert params["timeout"].kind is inspect.Parameter.KEYWORD_ONLY
