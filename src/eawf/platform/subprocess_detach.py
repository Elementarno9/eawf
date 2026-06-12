"""Shared subprocess-detachment kwargs for TUI-spawned child processes.

Every blocking subprocess the live TUI fans out to (the instrument
version-probes, the per-wave ``git log`` drift scan, the daemon cold-spawn)
runs while the parent's fd 0 is the controlling TTY. ``stdin=DEVNULL`` stops a
child from *reading* that TTY, but the child still SHARES the parent's
controlling terminal (same session / process-group). On a graphics terminal a
child's mere presence or output can provoke a terminal escape-reply (a
Device-Attributes / capability response such as ``\\x1b[?62;1;...c``) written
back onto the shared TTY -- where the live App's Textual stdin reader parses
the embedded digits as synthetic digit-mode-switch keypresses.

Detaching each child from the controlling terminal closes that leak at the
source: a child with NO controlling TTY can neither provoke nor receive a
terminal reply on the shared session.

- POSIX: ``start_new_session=True`` makes the child a new session leader, which
  has no controlling terminal. This is the same primitive the runtime spawn
  seam already relies on (see :mod:`eawf.runtime.runtimes.claude.adapter`).
- win32: ``CREATE_NO_WINDOW`` runs the child without allocating a console
  window. The flag is read off the win32 build of the stdlib ``subprocess``
  module via :func:`getattr` so a POSIX host (where the constant is absent) is
  unaffected.

The kwargs are deliberately minimal -- they carry ONLY the detach knobs.
Callers keep threading ``stdin=subprocess.DEVNULL`` + ``capture_output=True``
themselves so the dead-stdin isolation and output-capture intent stay visible
at each call site.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any


def detached_subprocess_kwargs() -> dict[str, Any]:
    """Return the platform-appropriate detach kwargs for a TUI-spawned child.

    On POSIX this is ``{"start_new_session": True}`` -- the child becomes a
    session leader with no controlling terminal. On win32 it is
    ``{"creationflags": subprocess.CREATE_NO_WINDOW}`` so the child never
    allocates a console window. The win32 flag is resolved via :func:`getattr`
    with a ``0`` fallback so importing this module on POSIX never raises for the
    missing constant (the win32 branch only runs on win32 regardless).

    Returns:
        A kwargs dict suitable for splatting into ``subprocess.run`` /
        ``subprocess.Popen``. Empty platform branches return an empty dict so
        the splat is a no-op rather than a syntax requirement.
    """
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


__all__ = ["detached_subprocess_kwargs"]
