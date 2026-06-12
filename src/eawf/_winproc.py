"""Windows console-window suppression for daemon-side subprocess spawns.

The daemon is spawned under ``pythonw.exe`` (GUI subsystem, no console -- see
:func:`eawf.runtime.daemon.spawn._spawn_windows`). On Windows, a GUI-subsystem
parent that spawns a console-subsystem child (``claude``, ``git``, ``node``)
*without* explicit creation flags makes the OS allocate a fresh console window
for that child -- which flashes open and closed as short-lived helpers run.
``CREATE_NO_WINDOW`` suppresses that allocation.

Every subprocess the daemon spawns (directly or transitively) must thread these
kwargs through. The helper returns an empty mapping off-Windows, and is a
harmless no-op when the parent already owns a console, so call sites stay
platform-agnostic.
"""

from __future__ import annotations

import subprocess
import sys

__all__ = ["no_window_kwargs"]


def no_window_kwargs() -> dict[str, int]:
    """Return the ``creationflags`` kwarg that suppresses console-window flashing.

    Spread into a ``subprocess.run`` / ``subprocess.Popen`` /
    ``asyncio.create_subprocess_exec`` call as ``**no_window_kwargs()``.

    Returns:
        ``{"creationflags": subprocess.CREATE_NO_WINDOW}`` on Windows, else an
        empty mapping.
    """
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}
