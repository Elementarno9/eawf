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
- **npm dist-tag derivation** — ``dist_tag_for_version`` routes every
  prerelease to ``next`` and only a final release to ``latest``, so the
  plugin-release publish cannot hand a prerelease to the default
  ``npm install``.
- **Support classification** — the README support table, the
  ``pyproject.toml`` trove classifiers, ``requires-python``, and the CI
  matrix in ``.github/workflows/ci.yaml`` name the same platforms and the
  same interpreter, so the documented support promise cannot drift from
  what is actually gated.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml

import eawf
from eawf.kernel.release.signals import (
    ReleaseSignalContext,
    ReleaseSignalName,
    ReleaseSignalStatus,
)
from eawf.kernel.spec.release_config import load_release_config
from eawf.platform.install.dist_tag import (
    DIST_TAG_LATEST,
    DIST_TAG_NEXT,
    dist_tag_for_version,
    npm_version_for,
)
from eawf.workflow.release.train import DEV1_RELEASE_CONFIG_YAML, V07_TRAIN
from eawf.workflow.verify.release_probes import TagPreflightInputs, build_tag_probes

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VERSION_FILE = _REPO_ROOT / "src" / "eawf" / "_version.py"
_BUMP_PATH = _REPO_ROOT / "tools" / "version_bump.py"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_README = _REPO_ROOT / "README.md"
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yaml"


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


# --- npm dist-tag derivation ------------------------------------------------


@pytest.mark.parametrize(
    "version",
    ["0.7.0a1", "0.7.0b2", "0.7.0rc1", "0.7.0.dev1", "0.7.0rc1.dev4", "0a1", "1.0rc9"],
)
def test_dist_tag_for_version_routes_prereleases_to_next(version: str) -> None:
    """Any pre-release or ``.dev`` build publishes under the opt-in channel."""
    assert dist_tag_for_version(version) == DIST_TAG_NEXT == "next"


@pytest.mark.parametrize("version", ["0.7.0", "1.0", "1", "0.7.0.post1", "10.20.30"])
def test_dist_tag_for_version_routes_stable_to_latest(version: str) -> None:
    """A final release (post-releases included) owns the default install."""
    assert dist_tag_for_version(version) == DIST_TAG_LATEST == "latest"


@pytest.mark.parametrize(
    "version",
    ["", " ", "0.7.0 ", "v0.7.0", "0.7.0dev1", "0.7.0.dev", "0.7.0rc", "0.7.0-rc1", "latest"],
)
def test_dist_tag_for_version_rejects_unparsable_versions(version: str) -> None:
    """An unclassifiable version raises rather than defaulting to ``latest``."""
    with pytest.raises(ValueError, match="unsupported version string"):
        dist_tag_for_version(version)


def test_dist_tag_for_version_rejects_non_string() -> None:
    """A non-``str`` version is a caller bug, not a version-grammar miss."""
    with pytest.raises(TypeError, match="version must be a str"):
        dist_tag_for_version(None)  # type: ignore[arg-type]


def test_dist_tag_for_version_classifies_the_shipped_version() -> None:
    """The version this repo ships resolves to exactly one of the two tags."""
    assert dist_tag_for_version(eawf.__version__) in {DIST_TAG_LATEST, DIST_TAG_NEXT}


# --- npm version spelling ---------------------------------------------------
#
# npm speaks SemVer and rejects PEP 440 outright, so the checkpoint PyPI
# carries as ``0.7.0.dev1`` reaches the registry as ``0.7.0-dev.1``. Two
# callers need that spelling: the plugin-release publish step, which
# writes it into the synthesized ``package.json``, and the
# version-consistency readiness row, which reports it so the operator
# can see what each channel will actually receive.


@pytest.mark.parametrize(
    ("version", "npm_version"),
    [
        ("0.7.0.dev1", "0.7.0-dev.1"),
        ("0.7.0rc1", "0.7.0-rc.1"),
        ("0.7.0", "0.7.0"),
        ("0.7.0.dev0", "0.7.0-dev.0"),
        ("0.7.0.dev10", "0.7.0-dev.10"),
        ("0.7.0a1", "0.7.0-a.1"),
        ("0.7.0b2", "0.7.0-b.2"),
        ("1", "1"),
        ("1.0", "1.0"),
        ("10.20.30", "10.20.30"),
        ("0.7.0rc01", "0.7.0-rc.1"),
    ],
)
def test_npm_version_for_spells_the_semver_form(version: str, npm_version: str) -> None:
    """Every publishable spelling maps onto the one npm will accept."""
    assert npm_version_for(version) == npm_version


@pytest.mark.parametrize(
    "version",
    ["", " ", "0.7.0 ", "v0.7.0", "0.7.0dev1", "0.7.0.dev", "0.7.0rc", "0.7.0-rc1", "latest"],
)
def test_npm_version_for_rejects_unparsable_versions(version: str) -> None:
    """A version the pipeline cannot classify is not one it may name."""
    with pytest.raises(ValueError, match="unsupported version string"):
        npm_version_for(version)


@pytest.mark.parametrize("version", ["0.7.0.post1", "1.0.post12"])
def test_npm_version_for_rejects_a_post_release(version: str) -> None:
    """SemVer sorts ``0.7.0-post.1`` below the ``0.7.0`` it supersedes."""
    with pytest.raises(ValueError, match="no npm version for post-release"):
        npm_version_for(version)


@pytest.mark.parametrize("version", ["0.7.0rc1.dev4", "0.7.0a1.dev1"])
def test_npm_version_for_rejects_a_dev_build_of_a_prerelease(version: str) -> None:
    """SemVer sorts a dev build of rc1 above rc1; PEP 440 sorts it below."""
    with pytest.raises(ValueError, match="no npm version for"):
        npm_version_for(version)


def test_npm_version_for_rejects_non_string() -> None:
    """A non-``str`` version is a caller bug, not a version-grammar miss."""
    with pytest.raises(TypeError, match="version must be a str"):
        npm_version_for(None)  # type: ignore[arg-type]


def test_npm_version_for_names_the_shipped_version() -> None:
    """The version this repo ships has an npm spelling the publish can use."""
    assert npm_version_for(eawf.__version__)


def test_version_consistency_row_reports_the_npm_channel_spelling() -> None:
    """The readiness row names both spellings, so neither channel is a guess."""
    config = load_release_config(DEV1_RELEASE_CONFIG_YAML, train=V07_TRAIN)
    inputs = TagPreflightInputs(
        repo_root=_REPO_ROOT,
        version=config.version,
        tag=f"v{config.version}",
        package_version=config.version,
        remote="origin",
    )
    probe = build_tag_probes(inputs)[ReleaseSignalName.VERSION_CONSISTENCY]
    outcome = probe(
        ReleaseSignalContext(
            config=config,
            signal=ReleaseSignalName.VERSION_CONSISTENCY,
            observed_revision=None,
        )
    )
    assert outcome.status is ReleaseSignalStatus.PASS
    assert f"npm-version:{npm_version_for(config.version)}" in outcome.evidence_refs
    assert "npm-version:0.7.0-dev.1" in outcome.evidence_refs


# --- Support classification -------------------------------------------------
#
# Four artifacts each claim which platforms and which interpreter eawf
# supports: the README table an installer actually reads, the trove
# classifiers PyPI renders, ``requires-python`` the resolver enforces, and
# the CI matrix that is the only one of the four backed by a real run. The
# tests below pin all four to the CI matrix, so widening or narrowing
# support means editing every artifact or reddening this file.

# Runner-label prefix -> the trove classifier that promises that platform.
_RUNNER_OS_CLASSIFIER = {
    "ubuntu": "Operating System :: POSIX :: Linux",
    "macos": "Operating System :: MacOS :: MacOS X",
    "windows": "Operating System :: Microsoft :: Windows",
}
_PY_CLASSIFIER = re.compile(r"^Programming Language :: Python :: (\d+)\.(\d+)$")
_TABLE_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")


def _markdown_table_rows(markdown: str, *, heading: str) -> list[list[str]]:
    """Return the data rows of the first markdown table under ``## heading``.

    Args:
        markdown: Full markdown source to scan.
        heading: Level-two heading text, without the leading ``##``.

    Returns:
        One list of stripped cell strings per data row, header and
        separator rows dropped. An empty list when the table under the
        heading is header-only.

    Raises:
        KeyError: when *heading* is absent from *markdown*.
        ValueError: when no table follows the heading before the next
            level-two heading.
    """
    lines = markdown.splitlines()
    try:
        start = lines.index(f"## {heading}")
    except ValueError as exc:
        raise KeyError(f"no '## {heading}' section in the markdown source") from exc

    table: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith("## "):
            break
        if stripped.startswith("|"):
            table.append(stripped)
        elif table:
            break
    if not table:
        raise ValueError(f"no markdown table under '## {heading}'")

    rows = [[cell.strip() for cell in row.strip("|").split("|")] for row in table]
    return [row for row in rows[1:] if not all(_TABLE_SEPARATOR_CELL.match(c) for c in row)]


def _inline_code(cell: str) -> str:
    """Return the text inside a single backtick-quoted markdown cell.

    Raises:
        ValueError: when *cell* is not exactly one inline-code span.
    """
    match = re.fullmatch(r"`([^`]+)`", cell.strip())
    if match is None:
        raise ValueError(f"cell is not a single inline-code span: {cell!r}")
    return match.group(1)


def _ci_runner_labels() -> set[str]:
    """Return every concrete GitHub runner label the CI workflow uses.

    The ``test`` job templates ``runs-on`` from its matrix, so the matrix
    ``os`` list is read directly; every other job names its runner
    literally.
    """
    workflow = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    labels = set(jobs["test"]["strategy"]["matrix"]["os"])
    for job in jobs.values():
        runs_on = job.get("runs-on", "")
        if isinstance(runs_on, str) and runs_on and "${{" not in runs_on:
            labels.add(runs_on)
    return labels


def _ci_python_versions() -> list[str]:
    """Return the interpreter versions the CI test matrix pins."""
    workflow = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    return [str(v) for v in workflow["jobs"]["test"]["strategy"]["matrix"]["python"]]


def _requires_python_floor(spec: str) -> tuple[int, int]:
    """Return the ``(major, minor)`` floor of a ``>=X.Y`` requires-python spec.

    Raises:
        ValueError: when *spec* is not a bare ``>=`` lower bound — any other
            shape (a range, an exclusion, an empty string) means the support
            promise is no longer a single floor, and the callers' one-line
            comparison against it would silently mislead.
    """
    match = re.fullmatch(r">=\s*(\d+)\.(\d+)", spec.strip())
    if match is None:
        raise ValueError(f"unsupported requires-python spec: {spec!r}")
    return int(match.group(1)), int(match.group(2))


def _project_table() -> dict[str, Any]:
    """Return the ``[project]`` table of the repo's pyproject.toml."""
    with open(_PYPROJECT, "rb") as handle:
        return tomllib.load(handle)["project"]


def test_support_classification_readme_table_matches_ci_matrix() -> None:
    """Every README support row names a runner CI actually schedules."""
    rows = _markdown_table_rows(_README.read_text(encoding="utf-8"), heading="Support")
    assert rows, "the README '## Support' table must carry at least one platform row"
    documented = {_inline_code(row[1]) for row in rows}
    assert documented == _ci_runner_labels(), (
        "README support table and the CI runner set disagree; update README.md "
        "and .github/workflows/ci.yaml together"
    )


def test_support_classification_classifiers_match_ci_platforms() -> None:
    """The trove OS classifiers cover exactly the CI-gated platforms."""
    expected = set()
    for label in _ci_runner_labels():
        family = label.split("-", 1)[0]
        assert family in _RUNNER_OS_CLASSIFIER, f"unmapped CI runner family: {label!r}"
        expected.add(_RUNNER_OS_CLASSIFIER[family])
    declared = {c for c in _project_table()["classifiers"] if c.startswith("Operating System ::")}
    assert declared == expected


def test_support_classification_python_floor_agrees_across_sources() -> None:
    """requires-python, the classifiers, CI, and the README name one interpreter."""
    project = _project_table()
    requires_python = project["requires-python"]
    floor = _requires_python_floor(requires_python)

    classified = {
        (int(m.group(1)), int(m.group(2)))
        for m in (_PY_CLASSIFIER.match(c) for c in project["classifiers"])
        if m is not None
    }
    assert classified, "pyproject must classify at least one concrete Python minor"
    assert min(classified) == floor, "lowest Python classifier must be the requires-python floor"

    ci_versions = _ci_python_versions()
    assert {tuple(int(part) for part in v.split(".")) for v in ci_versions} == classified

    support = _README.read_text(encoding="utf-8").split("## Support", 1)[1]
    python_line = next(line for line in support.splitlines() if line.startswith("Python:"))
    assert f"`{requires_python}`" in python_line
    for version in ci_versions:
        assert f"`{version}`" in python_line


def test_support_classification_table_parser_rejects_absent_heading() -> None:
    """A missing section is a KeyError, not a silently empty row list."""
    with pytest.raises(KeyError, match="no '## Support' section"):
        _markdown_table_rows("# Title\n\nprose only\n", heading="Support")


def test_support_classification_table_parser_rejects_tableless_section() -> None:
    """A heading carrying prose but no table is a ValueError."""
    with pytest.raises(ValueError, match="no markdown table"):
        _markdown_table_rows("## Support\n\njust prose\n\n## Next\n", heading="Support")


def test_support_classification_table_parser_returns_empty_for_header_only_table() -> None:
    """A header-plus-separator table with no data rows yields no rows."""
    source = "## Support\n\n| Platform | Runner |\n| --- | --- |\n"
    assert _markdown_table_rows(source, heading="Support") == []


def test_support_classification_table_parser_reads_a_single_row() -> None:
    """The one-row boundary parses to exactly one stripped cell list."""
    source = "## Support\n\n| Platform | Runner |\n| --- | --- |\n| Linux | `ubuntu-24.04` |\n"
    assert _markdown_table_rows(source, heading="Support") == [["Linux", "`ubuntu-24.04`"]]


def test_support_classification_inline_code_rejects_plain_cell() -> None:
    """A runner cell that lost its backticks is a ValueError, not a silent pass."""
    with pytest.raises(ValueError, match="single inline-code span"):
        _inline_code("ubuntu-24.04")


@pytest.mark.parametrize("spec", ["", ">3.14", ">=3", "==3.14", ">=3.14,<4"])
def test_support_classification_requires_python_rejects_non_floor_specs(spec: str) -> None:
    """Only a bare ``>=X.Y`` lower bound is a floor this gate can compare."""
    with pytest.raises(ValueError, match="unsupported requires-python spec"):
        _requires_python_floor(spec)


def test_support_classification_requires_python_parses_the_shipped_floor() -> None:
    """The floor this repo ships parses to a concrete (major, minor) pair."""
    assert _requires_python_floor(_project_table()["requires-python"]) == (3, 14)
