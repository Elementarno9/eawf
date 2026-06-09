"""Thin CLI over the sigil-totality gate.

The gate logic lives in :mod:`eawf.platform.lint.sigil_totality` (so it has a
production call-site under ``src/`` -- the ``eawf hook sigil-totality`` command
-- and is unit-testable by name). This module is the standalone CLI shim: it
delegates to :func:`~eawf.platform.lint.sigil_totality.check_sigil_totality` and
maps the typed :class:`~eawf.platform.lint.sigil_totality.GateResult` onto an
exit code.

Invocation:

    python3 tools/sigil_totality_gate.py

Exit codes:
- ``0`` -- every status value across every covered enum + FSM terminal resolves
  to a real glyph (no bare ``.value``, no ``?`` fallthrough).
- ``1`` -- at least one value did not resolve to a real glyph (the failures are
  named on stderr).
"""

from __future__ import annotations

import sys

from eawf.platform.lint.sigil_totality import check_sigil_totality


def main(argv: list[str] | None = None) -> int:
    """Run the check and map the result onto an exit code.

    Args:
        argv: Unused; accepted so the entry point matches the tool convention.

    Returns:
        ``0`` when the gate passes, ``1`` when at least one value did not
        resolve to a real glyph.
    """
    _ = argv
    result = check_sigil_totality()
    if result.passed:
        print(result.message)
        return 0
    print(result.message, file=sys.stderr)
    for miss in result.misses:
        print(f"  {miss}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
