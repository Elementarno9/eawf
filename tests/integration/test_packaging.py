"""Packaging guarantees for the single-source version + wheel bundling.

Covers the three load-bearing guarantees of P27-W27:

- **Single-source version** — ``src/eawf/_version.py`` is the one literal;
  ``eawf.__version__`` re-exports it and Hatchling reads the same value at
  build time (asserted via the built wheel's METADATA ``Version:`` line).
- **version_bump grammar** — ``tools/version_bump.py`` bumps the semver
  core and attaches / advances PEP-440 pre-release segments; boundary +
  error paths are covered.
- **Wheel-size gate** — a real ``uv build --wheel`` lands the per-OS
  service templates under ``eawf/_data/service_templates/`` and stays
  under the ``[tool.eawf.bundle] wheel_max_bytes`` ceiling. Skipped
  cleanly when the build environment is unavailable; the assertions are
  real whenever the wheel builds.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest

import eawf

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VERSION_FILE = _REPO_ROOT / "src" / "eawf" / "_version.py"
_BUMP_PATH = _REPO_ROOT / "tools" / "version_bump.py"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"


def _load_bump() -> Any:
    """Import ``tools/version_bump.py`` as a module."""
    tool_dir = _BUMP_PATH.parent
    if str(tool_dir) not in sys.path:
        sys.path.insert(0, str(tool_dir))
    spec = importlib.util.spec_from_file_location("version_bump", _BUMP_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["version_bump"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bump() -> Any:
    return _load_bump()


def _bundle_config() -> dict[str, Any]:
    with open(_PYPROJECT, "rb") as handle:
        return tomllib.load(handle)["tool"]["eawf"]["bundle"]


# --- Single-source version --------------------------------------------------


def test_version_module_is_the_single_source(bump: Any) -> None:
    """``eawf.__version__`` is re-exported from ``_version.py``."""
    from eawf import _version

    assert eawf.__version__ == _version.__version__
    assert bump.read_current(_VERSION_FILE) == _version.__version__


def test_pyproject_uses_dynamic_version() -> None:
    """pyproject declares ``dynamic = ["version"]`` and points hatch at the file."""
    with open(_PYPROJECT, "rb") as handle:
        data = tomllib.load(handle)
    assert "version" in data["project"]["dynamic"]
    assert "version" not in data["project"]
    assert data["tool"]["hatch"]["version"]["path"] == "src/eawf/_version.py"


# --- version_bump grammar ---------------------------------------------------


def test_parse_version_final(bump: Any) -> None:
    assert bump.parse_version("0.2.0") == (0, 2, 0, None, None)


def test_parse_version_prerelease(bump: Any) -> None:
    assert bump.parse_version("0.3.0a1") == (0, 3, 0, "a", 1)


def test_parse_version_rejects_garbage(bump: Any) -> None:
    with pytest.raises(ValueError, match="unsupported version string"):
        bump.parse_version("0.3")


def test_parse_version_rejects_four_component(bump: Any) -> None:
    with pytest.raises(ValueError, match="unsupported version string"):
        bump.parse_version("0.3.0.1")


def test_parse_version_rejects_unknown_phase(bump: Any) -> None:
    with pytest.raises(ValueError, match="unsupported version string"):
        bump.parse_version("0.3.0dev1")


def test_bump_minor_resets_patch_and_drops_pre(bump: Any) -> None:
    assert bump.bump_version("0.2.4a3", dimension="minor", pre_phase=None) == "0.3.0"


def test_bump_major_resets_minor_and_patch(bump: Any) -> None:
    assert bump.bump_version("0.2.4", dimension="major", pre_phase=None) == "1.0.0"


def test_bump_patch(bump: Any) -> None:
    assert bump.bump_version("0.2.0", dimension="patch", pre_phase=None) == "0.2.1"


def test_bump_minor_with_pre_attaches_fresh_counter(bump: Any) -> None:
    assert bump.bump_version("0.2.0", dimension="minor", pre_phase="a") == "0.3.0a1"


def test_bump_pre_only_advances_existing_counter(bump: Any) -> None:
    assert bump.bump_version("0.3.0a1", dimension=None, pre_phase="a") == "0.3.0a2"


def test_bump_pre_only_switches_phase_resets_counter(bump: Any) -> None:
    assert bump.bump_version("0.3.0a2", dimension=None, pre_phase="rc") == "0.3.0rc1"


def test_bump_pre_only_attaches_when_no_existing_segment(bump: Any) -> None:
    assert bump.bump_version("0.3.0", dimension=None, pre_phase="b") == "0.3.0b1"


def test_bump_nothing_raises(bump: Any) -> None:
    with pytest.raises(ValueError, match="nothing to bump"):
        bump.bump_version("0.2.0", dimension=None, pre_phase=None)


def test_round_trip_format_parse(bump: Any) -> None:
    assert bump.format_version(0, 3, 0, "rc", 2) == "0.3.0rc2"
    assert bump.parse_version("0.3.0rc2") == (0, 3, 0, "rc", 2)


def test_format_pre_without_counter_raises(bump: Any) -> None:
    with pytest.raises(ValueError, match="requires a counter"):
        bump.format_version(0, 3, 0, "a", None)


def test_bump_write_round_trip(bump: Any, tmp_path: Path) -> None:
    """A real write rewrites the literal and reads back the new value."""
    target = tmp_path / "_version.py"
    target.write_text('from __future__ import annotations\n\n__version__ = "0.2.0"\n')
    new = bump.bump_version(bump.read_current(target), dimension="minor", pre_phase="a")
    bump.write_version(target, new)
    assert bump.read_current(target) == "0.3.0a1"
    assert "from __future__ import annotations" in target.read_text()


def test_main_dry_run_does_not_mutate(bump: Any, tmp_path: Path) -> None:
    target = tmp_path / "_version.py"
    target.write_text('__version__ = "0.2.0"\n')
    rc = bump.main(["--minor", "--dry-run", "--file", str(target)])
    assert rc == 0
    assert bump.read_current(target) == "0.2.0"


def test_main_no_args_is_usage_error(bump: Any, tmp_path: Path) -> None:
    target = tmp_path / "_version.py"
    target.write_text('__version__ = "0.2.0"\n')
    assert bump.main(["--file", str(target)]) == 2


# --- Wheel-size gate --------------------------------------------------------


def _build_wheel(out_dir: Path) -> Path | None:
    """Run ``uv build --wheel`` into *out_dir*; return the ``.whl`` or None.

    Returns ``None`` only when the build environment is genuinely
    unavailable — the ``EAWF_SKIP_WHEEL_BUILD`` opt-out is set or ``uv``
    is not on ``PATH`` (``FileNotFoundError``). A build that actually
    runs but fails (non-zero exit) or produces no wheel raises
    :class:`AssertionError` so the test REDS rather than green-skipping
    a real packaging regression.
    """
    if os.environ.get("EAWF_SKIP_WHEEL_BUILD"):
        return None
    try:
        proc = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(out_dir)],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        return None
    assert proc.returncode == 0, (
        f"uv build --wheel failed (exit {proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    wheels = sorted(out_dir.glob("*.whl"))
    assert wheels, "uv build --wheel produced no .whl artifact"
    return wheels[0]


def test_wheel_bundles_service_templates_under_size_ceiling(tmp_path: Path) -> None:
    """The built wheel ships the service templates and stays under budget."""
    wheel = _build_wheel(tmp_path / "dist")
    if wheel is None:
        pytest.skip("uv build unavailable in this environment")

    config = _bundle_config()
    ceiling = config["wheel_max_bytes"]
    size = wheel.stat().st_size
    assert size <= ceiling, f"wheel {size} bytes exceeds ceiling {ceiling}"

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    for template in config["service_templates"]:
        member = f"eawf/_data/service_templates/{template}"
        assert member in names, f"missing bundled template: {member}"


def test_wheel_metadata_version_matches_single_source(tmp_path: Path) -> None:
    """Hatchling stamps the wheel METADATA from ``_version.py``."""
    wheel = _build_wheel(tmp_path / "dist")
    if wheel is None:
        pytest.skip("uv build unavailable in this environment")

    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(n for n in archive.namelist() if n.endswith("METADATA"))
        metadata = archive.read(metadata_name).decode()
    version_line = next(line for line in metadata.splitlines() if line.startswith("Version:"))
    assert version_line == f"Version: {eawf.__version__}"


# --- Wheel-gate skip/fail discrimination ------------------------------------


def _fake_proc(returncode: int) -> subprocess.CompletedProcess[str]:
    """Return a stand-in ``CompletedProcess`` for a monkeypatched build run."""
    return subprocess.CompletedProcess(
        args=["uv", "build", "--wheel"],
        returncode=returncode,
        stdout="build log",
        stderr="error detail" if returncode else "",
    )


def test_build_wheel_skips_when_uv_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``uv`` not on PATH (``FileNotFoundError``) is a legit skip (returns None)."""
    monkeypatch.delenv("EAWF_SKIP_WHEEL_BUILD", raising=False)

    def _raise_missing(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("uv")

    monkeypatch.setattr(subprocess, "run", _raise_missing)
    assert _build_wheel(tmp_path / "dist") is None


def test_build_wheel_reds_on_failed_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real build failure (non-zero exit) must FAIL, not green-skip."""
    monkeypatch.delenv("EAWF_SKIP_WHEEL_BUILD", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_proc(1))
    with pytest.raises(AssertionError, match="uv build --wheel failed"):
        _build_wheel(tmp_path / "dist")


def test_build_wheel_reds_when_no_artifact_produced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean exit that yields no ``.whl`` is also a failure, not a skip."""
    monkeypatch.delenv("EAWF_SKIP_WHEEL_BUILD", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_proc(0))
    with pytest.raises(AssertionError, match="produced no"):
        _build_wheel(tmp_path / "dist")


def test_build_wheel_skips_when_opt_out_env_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``EAWF_SKIP_WHEEL_BUILD`` opt-out short-circuits to a skip (None)."""
    monkeypatch.setenv("EAWF_SKIP_WHEEL_BUILD", "1")
    assert _build_wheel(tmp_path / "dist") is None


# --- Windows extra --------------------------------------------


def test_windows_extra_pins_pywin32_in_pyproject() -> None:
    """The ``[windows]`` optional extra pins pywin32 for the pipe transport.

    ``pip install eawf[windows]`` must resolve pywin32 (the named-pipe
    transport backs the Windows daemon). This pins the source-of-truth in
    pyproject so the extra cannot silently drop the dependency.
    """
    with open(_PYPROJECT, "rb") as handle:
        data = tomllib.load(handle)
    extras = data["project"]["optional-dependencies"]
    assert "windows" in extras, "missing [windows] optional extra"
    assert any(req.startswith("pywin32") for req in extras["windows"]), extras["windows"]


def test_wheel_metadata_declares_windows_extra(tmp_path: Path) -> None:
    """The built wheel METADATA advertises the ``windows`` extra + pywin32.

    The install-resolve smoke: a built wheel must carry
    ``Provides-Extra: windows`` and a ``Requires-Dist`` for pywin32 gated
    on that extra, so ``pip install eawf[windows]`` resolves pywin32.
    """
    wheel = _build_wheel(tmp_path / "dist")
    if wheel is None:
        pytest.skip("uv build unavailable in this environment")

    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(n for n in archive.namelist() if n.endswith("METADATA"))
        metadata = archive.read(metadata_name).decode()
    assert "Provides-Extra: windows" in metadata
    requires = [line for line in metadata.splitlines() if line.startswith("Requires-Dist:")]
    pywin32_lines = [line for line in requires if "pywin32" in line]
    assert pywin32_lines, requires
    # PEP 508 allows either quote style for the marker; uv's build backend emits
    # single quotes (`extra == 'windows'`), so accept both rather than pinning one.
    assert any(
        'extra == "windows"' in line or "extra == 'windows'" in line for line in pywin32_lines
    ), pywin32_lines


@pytest.mark.skipif(sys.platform != "win32", reason="win32-only ctypes binding")
def test_cancel_io_ex_argtypes_set_once_at_module_load() -> None:
    """``CancelIoEx.argtypes`` is bound at import (W05 contract).

    The streaming teardown calls ``CancelIoEx`` per disconnect; binding
    ``argtypes`` once at module load (not per call) keeps the handle
    marshalling correct and cheap. Asserts the binding is present after a
    bare import of the win32-only transport module.
    """
    import ctypes

    from eawf.runtime.daemon import windows_pipe

    assert windows_pipe._CancelIoEx.argtypes == [ctypes.c_void_p, ctypes.c_void_p]
    assert windows_pipe._CancelIoEx.restype is ctypes.c_bool
