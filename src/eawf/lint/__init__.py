"""Custom static-analysis rules for the eawf codebase.

Rules in this package are plain Python checks (not registered ruff
plugins — the ``[tool.ruff.lint] select`` list carries no ``EAWF``
prefix). Each rule module exposes a small, importable, unit-testable
surface; wiring rules into ``pre-commit`` / a ``eawf hook`` dispatcher
is a separate concern owned by downstream waves.
"""

from __future__ import annotations
