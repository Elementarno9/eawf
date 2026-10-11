"""``eawf help`` topics ship as package data and render from a wheel install.

A wheel carries no ``docs/`` tree, so the build hook copies ``docs/help``
into ``eawf/_data/help`` and ``eawf help`` falls back to that copy when no
repository checkout is around it. The wheel tests build a real wheel,
inspect the archive, install it into a fresh virtual environment and run
every topic through the installed console script.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tomllib
import types
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import help as help_cmd

_REPO_ROOT = Path(__file__).resolve().parents[4]
_HELP_DIR = _REPO_ROOT / "docs" / "help"
_BUNDLE_HOOK = _REPO_ROOT / "tools" / "bundle_data.py"

runner = CliRunner()


def _first_heading(topic: str) -> str:
    """Return the first markdown heading line of the authored *topic*."""
    text = (_HELP_DIR / f"{topic}.md").read_text(encoding="utf-8")
    return next(line for line in text.splitlines() if line.startswith("#"))


# --- Built and installed wheel ----------------------------------------------


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the wheel once for this module; skip only when ``uv`` is absent."""
    if os.environ.get("EAWF_SKIP_WHEEL_BUILD"):
        pytest.skip("wheel build opted out via EAWF_SKIP_WHEEL_BUILD")
    out_dir = tmp_path_factory.mktemp("dist")
    try:
        proc = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(out_dir)],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        pytest.skip("uv unavailable in this environment")
    assert proc.returncode == 0, f"uv build --wheel failed:\n{proc.stderr}"
    wheels = sorted(out_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"
    return wheels[0]


@pytest.fixture(scope="module")
def installed_eawf(built_wheel: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Install *built_wheel* into a fresh virtual environment; return its ``eawf``."""
    venv = tmp_path_factory.mktemp("venv") / "env"
    subprocess.run(
        ["uv", "venv", "--quiet", "--python", sys.executable, str(venv)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    python = venv / "bin" / "python"
    install = ["uv", "pip", "install", "--quiet", "--no-config", "--python", str(python)]
    # The uv cache usually already holds every dependency; go online only
    # when it does not, so a warm machine needs no network.
    proc = subprocess.run(
        [*install, "--offline", str(built_wheel)], capture_output=True, text=True, timeout=300
    )
    if proc.returncode != 0:
        proc = subprocess.run(
            [*install, str(built_wheel)], capture_output=True, text=True, timeout=600
        )
    assert proc.returncode == 0, f"installing the wheel failed:\n{proc.stderr}"
    return venv / "bin" / "eawf"


def test_wheel_bundles_every_help_topic(built_wheel: Path) -> None:
    """The archive carries ``eawf/_data/help/<topic>.md`` for every topic."""
    with zipfile.ZipFile(built_wheel) as archive:
        names = set(archive.namelist())
        for topic in help_cmd.TOPICS:
            member = f"eawf/_data/help/{topic}.md"
            assert member in names, f"missing bundled help topic: {member}"
            assert archive.read(member) == (_HELP_DIR / f"{topic}.md").read_bytes()


@pytest.mark.parametrize("topic", help_cmd.TOPICS)
def test_installed_wheel_renders_every_topic(
    installed_eawf: Path, topic: str, tmp_path: Path
) -> None:
    """The installed console script prints each topic's first heading, exit 0."""
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "VIRTUAL_ENV"}}
    env["EAWF_NO_PAGER"] = "1"
    proc = subprocess.run(
        [str(installed_eawf), "help", topic],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0] == _first_heading(topic)


# --- Topic resolution --------------------------------------------------------


def test_checkout_renders_upgrade_topic() -> None:
    """In a repository checkout the authored ``docs/help`` source renders."""
    result = runner.invoke(app, ["help", "upgrade-from-0.6"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == _first_heading("upgrade-from-0.6")


def test_topic_path_prefers_checkout_source() -> None:
    """With ``docs/help`` reachable the source file wins over package data."""
    path = help_cmd._topic_path("daemon")
    assert path is not None
    assert Path(str(path)) == _HELP_DIR / "daemon.md"


def _fake_package(root: Path, topics: dict[str, str]) -> Path:
    """Lay out an ``eawf`` package dir holding ``_data/help`` with *topics*."""
    help_dir = root / "_data" / "help"
    help_dir.mkdir(parents=True)
    for name, body in topics.items():
        (help_dir / f"{name}.md").write_text(body, encoding="utf-8")
    return root


def test_topic_path_falls_back_to_package_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a checkout the bundled ``_data/help`` copy is read."""
    package = _fake_package(tmp_path, {"daemon": "# Bundled daemon\n"})
    monkeypatch.setattr(help_cmd, "_topics_dir", lambda: None)
    monkeypatch.setattr(help_cmd, "files", lambda _name: package)

    path = help_cmd._topic_path("daemon")

    assert path is not None
    assert path.read_text(encoding="utf-8") == "# Bundled daemon\n"


def test_topic_path_falls_back_when_checkout_lacks_topic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrelated ``docs/help`` without the topic does not mask package data."""
    unrelated = tmp_path / "docs" / "help"
    unrelated.mkdir(parents=True)
    package = _fake_package(tmp_path / "pkg", {"urns": "# Bundled urns\n"})
    monkeypatch.setattr(help_cmd, "_topics_dir", lambda: unrelated)
    monkeypatch.setattr(help_cmd, "files", lambda _name: package)

    path = help_cmd._topic_path("urns")

    assert path is not None
    assert path.read_text(encoding="utf-8") == "# Bundled urns\n"


def test_topic_path_none_when_neither_location_has_topic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered topic missing everywhere resolves to ``None``."""
    package = _fake_package(tmp_path, {})
    monkeypatch.setattr(help_cmd, "_topics_dir", lambda: None)
    monkeypatch.setattr(help_cmd, "files", lambda _name: package)

    assert help_cmd._topic_path("streaming") is None
    result = runner.invoke(app, ["help", "streaming"])
    assert result.exit_code != 0
    assert "help topic source missing" in result.stdout


@pytest.mark.parametrize("topic", ["", "nope", "daemon.md", "../daemon"])
def test_topic_path_rejects_unregistered_names(topic: str) -> None:
    """Unregistered names (empty, unknown, suffixed, traversal) never resolve."""
    assert help_cmd._topic_path(topic) is None


# --- Build hook --------------------------------------------------------------


@pytest.fixture()
def bundle_hook(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Import ``tools/bundle_data.py`` with a stub for the build-only hatchling."""
    interface = types.ModuleType("hatchling.builders.hooks.plugin.interface")
    interface.BuildHookInterface = type("BuildHookInterface", (), {})  # type: ignore[attr-defined]
    for name in (
        "hatchling",
        "hatchling.builders",
        "hatchling.builders.hooks",
        "hatchling.builders.hooks.plugin",
    ):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, interface.__name__, interface)
    spec = importlib.util.spec_from_file_location("bundle_data_under_test", _BUNDLE_HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module


def _hook_tree(root: Path, topics: list[str]) -> dict[str, Any]:
    """Lay out a minimal repo under *root*; return its bundle config."""
    (root / "templates").mkdir()
    (root / "templates" / "unit.j2").write_text("unit\n", encoding="utf-8")
    help_dir = root / "docs" / "help"
    help_dir.mkdir(parents=True)
    for name in topics:
        (help_dir / name).write_text(f"# {name}\n", encoding="utf-8")
    return {
        "service_templates_dir": "templates",
        "service_templates": ["unit.j2"],
        "help_dir": "docs/help",
    }


def test_hook_config_declares_the_help_source() -> None:
    """``[tool.eawf.bundle] help_dir`` names the authored ``docs/help`` tree."""
    with (_REPO_ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)["tool"]["eawf"]["bundle"]
    assert config["help_dir"] == "docs/help"


def test_hook_copies_a_single_topic(bundle_hook: Any, tmp_path: Path) -> None:
    """One topic lands under ``_data/help`` next to the service template."""
    config = _hook_tree(tmp_path, ["only.md"])
    written = bundle_hook.populate_data_tree(tmp_path, config)
    data = tmp_path / "src" / "eawf" / "_data"
    assert written == [data / "service_templates" / "unit.j2", data / "help" / "only.md"]
    assert (data / "help" / "only.md").read_text(encoding="utf-8") == "# only.md\n"


def test_hook_copies_only_markdown_and_drops_stale_topics(bundle_hook: Any, tmp_path: Path) -> None:
    """Non-markdown files are skipped and a prior build's topic is removed."""
    config = _hook_tree(tmp_path, ["a.md", "b.md", "notes.txt"])
    stale = tmp_path / "src" / "eawf" / "_data" / "help" / "retired.md"
    stale.parent.mkdir(parents=True)
    stale.write_text("old\n", encoding="utf-8")

    bundle_hook.populate_data_tree(tmp_path, config)

    help_out = tmp_path / "src" / "eawf" / "_data" / "help"
    assert sorted(p.name for p in help_out.iterdir()) == ["a.md", "b.md"]


def test_hook_fails_fast_on_an_empty_help_dir(bundle_hook: Any, tmp_path: Path) -> None:
    """A help directory with no topic reds the build instead of shipping none."""
    config = _hook_tree(tmp_path, [])
    with pytest.raises(FileNotFoundError, match="bundle help topics not found"):
        bundle_hook.populate_data_tree(tmp_path, config)


def test_hook_fails_fast_on_a_missing_help_dir(bundle_hook: Any, tmp_path: Path) -> None:
    """A misnamed help directory is the same failure as an empty one."""
    config = _hook_tree(tmp_path, ["a.md"])
    config["help_dir"] = "docs/missing"
    with pytest.raises(FileNotFoundError, match="bundle help topics not found"):
        bundle_hook.populate_data_tree(tmp_path, config)


def test_hook_requires_the_help_dir_key(bundle_hook: Any, tmp_path: Path) -> None:
    """A bundle config without ``help_dir`` is a schema mismatch (KeyError)."""
    config = _hook_tree(tmp_path, ["a.md"])
    del config["help_dir"]
    with pytest.raises(KeyError, match="help_dir"):
        bundle_hook.populate_data_tree(tmp_path, config)
