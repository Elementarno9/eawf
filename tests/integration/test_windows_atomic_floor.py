"""WIN-P1 import + atomic-write floor.

Windows has no ``os.O_DIRECTORY`` (parent-dir fsync), no ``os.killpg`` /
``os.getpgid`` (process-group cancel), and no ``signal.SIGKILL``. Before
this wave every atomic-write idiom and the process-group cancel module
read those attributes at module top, so the daemon module graph could
not even import on Windows.

Two kinds of coverage live here:

* The win32-marked case (``skipif sys.platform != "win32"``) runs only on
  the Windows CI job: it asserts the guarded paths IMPORT and an
  atomic write lands a file on real Windows semantics.

* The cross-platform cases prove the guard CONTRACT without a Windows
  host: :func:`fsync_parent_dir` skips cleanly when ``os.O_DIRECTORY``
  is absent, and the whole daemon module graph re-imports in a
  subprocess whose ``os`` / ``signal`` / ``socket`` have had the
  POSIX-only attributes stripped (the closest a POSIX dev host gets to
  proving the Windows import path).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from eawf.kernel.fsync import fsync_parent_dir


def test_fsync_parent_dir_skips_when_o_directory_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``os.O_DIRECTORY`` (Windows) the dir-fsync is a clean no-op."""
    import os

    target = tmp_path / "state.json"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)
    # Must not raise AttributeError nor attempt the directory open.
    fsync_parent_dir(target)


def test_fsync_parent_dir_fsyncs_when_o_directory_present(tmp_path: Path) -> None:
    """On POSIX the helper opens + fsyncs the parent directory."""
    import os

    if not hasattr(os, "O_DIRECTORY"):
        pytest.skip("O_DIRECTORY absent on this platform")
    target = tmp_path / "state.json"
    target.write_text("{}", encoding="utf-8")
    # Real call against a real directory: succeeds and closes the fd.
    fsync_parent_dir(target)


#: The daemon module graph plus every W01-guarded atomic writer. Imported
#: in a child interpreter whose POSIX-only ``os`` / ``signal`` / ``socket``
#: attributes have been stripped, so an unguarded module-top read would
#: raise ``AttributeError`` before the import completed.
_DAEMON_GRAPH_MODULES = (
    "eawf.kernel.fsync",
    "eawf.kernel.state.writer",
    "eawf.kernel.state.io",
    "eawf.kernel.config.profile",
    "eawf.platform.backup.store",
    "eawf.runtime.daemon.wal",
    "eawf.runtime.daemon.methods.config",
    "eawf.surfaces.cli.commands.config",
    "eawf.surfaces.render._atomic",
    "eawf.surfaces.render.manifest",
    "eawf.runtime.runtimes.cancel",
    "eawf.runtime.daemon.dispatch_runner",
    "eawf.runtime.daemon.server",
    "eawf.runtime.daemon.main",
)


def test_daemon_graph_imports_with_posix_only_attrs_stripped() -> None:
    """The daemon graph imports when POSIX-only os/signal attrs are absent.

    Runs the import in a child interpreter that deletes ``os.O_DIRECTORY``,
    ``os.killpg``, ``os.getpgid``, ``os.fork``, ``os.setsid``,
    ``os.geteuid``, ``signal.SIGKILL``, and ``socket.AF_UNIX`` BEFORE the
    import — the same shape the modules see on a real Windows host. A
    surviving module-top read of any of those would crash the child with a
    non-zero exit, so a clean exit is the import-guard proof.
    """
    imports = "\n".join(f"import {mod}" for mod in _DAEMON_GRAPH_MODULES)
    strip_lines = "\n".join(
        f"if hasattr(os, {attr!r}): delattr(os, {attr!r})"
        for attr in ("O_DIRECTORY", "killpg", "getpgid", "fork", "setsid", "geteuid", "getuid")
    )
    script = (
        "import os, signal, socket\n"
        f"{strip_lines}\n"
        "if hasattr(signal, 'SIGKILL'): delattr(signal, 'SIGKILL')\n"
        "if hasattr(socket, 'AF_UNIX'): delattr(socket, 'AF_UNIX')\n"
        f"{imports}\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"daemon graph import failed with POSIX-only attrs stripped:\n{result.stderr}"
    )
    assert result.stdout.strip() == "ok"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only atomic floor")
def test_win32_guarded_paths_import_and_atomic_write(tmp_path: Path) -> None:
    """On Windows the guarded writers import and an atomic write lands.

    Exercised only on the windows-latest CI job. Asserts the
    import-guarded atomic-write helper writes through the
    ``fsync_parent_dir`` no-op branch (no ``O_DIRECTORY`` on Windows) and
    the bytes are durable on disk.
    """
    from eawf.kernel.state import writer

    target = tmp_path / "state.json"
    writer.atomic_write_json(target, {"platform": "win32"})
    import json

    assert json.loads(target.read_text(encoding="utf-8")) == {"platform": "win32"}
    # No leftover tempfile after a successful write.
    leftovers = [p for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert leftovers == []
