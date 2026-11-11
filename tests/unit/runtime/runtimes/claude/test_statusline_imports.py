"""A plain statusline redraw keeps the CLI and the log scrubber off its import path.

Claude Code starts ``python -m eawf cc statusline`` on every redraw, so every
module that path imports is paid per redraw. Each probe runs in a fresh
interpreter: the pytest process has already imported most of the tree, so an
in-process ``sys.modules`` check would prove nothing.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess  # noqa: EAWF024 a fresh interpreter is the only honest sys.modules probe
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf import __main__ as entry
from eawf.observability.logging.scrub import SensitiveScrubber
from tests._epoch2_helpers import lay_epoch2_tree

#: Modules no redraw may load, in or out of a managed repository.
NEVER_LOADED: tuple[str, ...] = ("typer", "click", "eawf.observability.logging.scrub")

#: Modules a redraw outside a managed repository should not load either.
NOT_LOADED_OUTSIDE: tuple[str, ...] = ("yaml", "pydantic")

#: Runs ``eawf cc statusline`` through the console entry point, then writes
#: ``sys.modules`` to the file named by argv[2]. Mode ``raise`` makes the render
#: raise and keeps the refresh it would start from spawning a process.
_REDRAW_PROBE = """
import json, sys

mode, dump = sys.argv[1:3]
if mode == "raise":
    from eawf.runtime.runtimes.claude import statusline

    def failing_render(*args, **kwargs):
        raise RuntimeError("render failed on purpose")

    statusline.render_pipeline = failing_render
    statusline._spawn_refresh = lambda *args, **kwargs: None
sys.argv = ["eawf", "cc", "statusline"]
from eawf.__main__ import main
main()
sys.stdout.flush()
with open(dump, "w", encoding="utf-8") as handle:
    json.dump(sorted(sys.modules), handle)
"""

_SESSION = "ses-imports"

#: One payload with no clock-dependent field, so its rows are fixed.
_PAYLOAD: dict[str, Any] = {
    "session_id": _SESSION,
    "model": {"id": "claude-opus-4-7", "display_name": "Opus"},
    "cost": {"total_cost_usd": 1.25, "total_duration_ms": 90_000, "total_api_duration_ms": 30_000},
    "context_window": {
        "context_window_size": 200_000,
        "current_usage": {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 4000,
        },
    },
}

#: The rows, cache key and counter sidecar a redraw wrote for :data:`_PAYLOAD`
#: before the import path was trimmed.
_GOLDEN_ROWS = (
    "\x1b[97;45m Opus \x1b[35;46m\x1b[30;46m 1m · 33% active "
    "\x1b[36;42m\x1b[30;42m $1.25 API-equivalent \x1b[32;49m\x1b[0m\n"
    "\n"
    "\x1b[1;39;49mctx\x1b[0m \x1b[36;100m▌\x1b[100m                   \x1b[0m "
    "\x1b[1;39;49m  3%\x1b[0m \x1b[2;39;49m 80%c\x1b[0m \x1b[2;39;49m5K/200K\x1b[0m"
)
_GOLDEN_KEY = "config/config"
_GOLDEN_SIDECAR = (
    b'{"schema_version":"1.0","counters":{"api_duration_ms":30000,"total_duration_ms":90000,'
    b'"cost_usd":"1.25","input_tokens":1000,"output_tokens":200,'
    b'"cache_creation_input_tokens":0,"cache_read_input_tokens":4000,'
    b'"harness":"claude-code","model":"claude-opus-4-7","measure_version":101}}'
)


def _redraw(
    tmp_path: Path, cwd: Path, *, mode: str = "plain"
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Serve one redraw of :data:`_PAYLOAD` in a fresh interpreter started in *cwd*.

    Args:
        tmp_path: Holds the cache, the home directory and the module dump.
        cwd: The directory the redraw runs in.
        mode: ``plain`` or ``raise``, see :data:`_REDRAW_PROBE`.

    Returns:
        The finished process and the modules it had loaded after the redraw.
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in {"EA_STATE", "COLUMNS"} and not name.startswith("EAWF_")
    }
    env["HOME"] = str(tmp_path / "home")
    env["EAWF_STATUSLINE_CACHE"] = str(tmp_path / "cache")
    dump = tmp_path / f"modules-{mode}.json"
    proc = subprocess.run(
        [sys.executable, "-c", _REDRAW_PROBE, mode, str(dump)],
        input=json.dumps(_PAYLOAD),
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert dump.is_file(), f"the probe wrote no module list:\n{proc.stderr}"
    return proc, json.loads(dump.read_text(encoding="utf-8"))


def _loaded(modules: list[str], names: tuple[str, ...]) -> list[str]:
    return [name for name in names if name in modules]


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    directory = tmp_path / "plain-dir"
    directory.mkdir()
    return directory


@pytest.fixture
def managed(tmp_path: Path) -> Path:
    repo = tmp_path / "managed-repo"
    lay_epoch2_tree(repo)
    return repo


@pytest.mark.parametrize("where", ["outside", "managed"])
def test_redraw_import_set(tmp_path: Path, where: str, request: pytest.FixtureRequest) -> None:
    cwd: Path = request.getfixturevalue(where)
    proc, modules = _redraw(tmp_path, cwd)
    assert proc.stdout.strip(), "the redraw printed no line"
    assert _loaded(modules, NEVER_LOADED) == []


@pytest.mark.xfail(
    strict=True,
    reason=(
        "eawf.surfaces.render.statusline imports yaml and pydantic at module top, and "
        "every segment carries a pydantic truth field"
    ),
)
def test_redraw_outside_repo_loads_no_yaml_or_pydantic(tmp_path: Path, outside: Path) -> None:
    _, modules = _redraw(tmp_path, outside)
    assert _loaded(modules, NOT_LOADED_OUTSIDE) == []


def test_redraw_outputs_unchanged(tmp_path: Path, outside: Path) -> None:
    proc, _ = _redraw(tmp_path, outside)
    assert proc.stdout == f"{_GOLDEN_ROWS}\n"
    entry_body = orjson.loads((tmp_path / "cache" / f"{_SESSION}.json").read_bytes())
    assert entry_body["value"] == _GOLDEN_ROWS
    assert entry_body["key"] == _GOLDEN_KEY
    sidecar = tmp_path / "cache" / f"{_SESSION}.runtime-counters.json"
    assert sidecar.read_bytes() == _GOLDEN_SIDECAR


def test_redraw_raising_render_prints_model_name(tmp_path: Path, outside: Path) -> None:
    proc, modules = _redraw(tmp_path, outside, mode="raise")
    assert proc.stdout == "Opus\n"
    # The warning reaches stderr through the scrubbed sink's format, not as a
    # bare message, so the sink installed itself on that record.
    assert (
        " WARNING eawf.runtime.runtimes.claude.statusline serve_line render-raised" in proc.stderr
    )
    assert "eawf.observability.logging.scrub" in modules


def test_redraw_raising_render_prints_cached_line(tmp_path: Path, outside: Path) -> None:
    _redraw(tmp_path, outside)
    proc, _ = _redraw(tmp_path, outside, mode="raise")
    assert proc.stdout == f"{_GOLDEN_ROWS}\n"


@contextmanager
def _bare_root() -> Iterator[logging.Logger]:
    """Lend the root logger with no handlers, restoring its handlers and level after.

    Entered in the test body, since pytest attaches its capture handlers to
    the root logger after fixtures have run.
    """
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    for handler in handlers:
        root.removeHandler(handler)
    try:
        yield root
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)


def test_defer_cli_logging_installs_scrubbed_sink_on_first_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = logging.getLogger("eawf.probe")
    with _bare_root() as root:
        entry._defer_cli_logging()
        [stand_in] = root.handlers
        assert not any(isinstance(f, SensitiveScrubber) for f in stand_in.filters)
        assert root.level == logging.WARNING

        probe.warning("first under ~/notes")
        [sink] = root.handlers
        assert sink is not stand_in
        assert any(isinstance(f, SensitiveScrubber) for f in sink.filters)
        probe.warning("second")
        probe.info("below the sink level")

    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 2
    assert lines[0].endswith(" WARNING eawf.probe first under <scrubbed>")
    assert lines[1].endswith(" WARNING eawf.probe second")


def test_defer_cli_logging_leaves_a_configured_root_alone() -> None:
    existing = logging.NullHandler()
    with _bare_root() as root:
        root.addHandler(existing)
        root.setLevel(logging.DEBUG)
        entry._defer_cli_logging()
        assert root.handlers == [existing]
        assert root.level == logging.DEBUG
