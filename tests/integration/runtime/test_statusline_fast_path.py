"""The console entry point answers the plain statusline without importing the CLI.

Claude Code runs ``eawf cc statusline`` on every redraw. :mod:`eawf.__main__`
answers it without loading the Typer app: a cached line is printed at once, a
miss is rendered and cached exactly as the ``cc statusline`` command would,
and a stale entry is served and then rewritten by a detached background
render. Any other invocation still goes to the full CLI. The process-level
checks run a fresh interpreter, because the test process has the app imported.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import orjson
import pytest

from eawf.__main__ import _statusline_theme

_PROBE = (
    "import sys\n"
    "sys.argv = ['eawf', *sys.argv[1:]]\n"
    "from eawf.__main__ import main\n"
    "main()\n"
    "print('app-loaded' if 'eawf.surfaces.cli.app' in sys.modules else 'app-unloaded',"
    " file=sys.stderr)\n"
)


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["cc", "statusline"], (True, None)),
        (["cc", "statusline", "--theme", "ascii-fallback"], (True, "ascii-fallback")),
        (["cc", "statusline", "--theme=powerline"], (True, "powerline")),
        (["cc", "statusline", "--theme="], (True, "")),
        ([], (False, None)),
        (["cc"], (False, None)),
        (["cc", "statusline", "--theme"], (False, None)),
        (["cc", "statusline", "prewarm"], (False, None)),
        (["cc", "statusline", "install"], (False, None)),
        (["cc", "statusline", "--help"], (False, None)),
        (["cc", "statusline", "--theme", "a", "--theme", "b"], (False, None)),
        (["--workspace", "w", "cc", "statusline"], (False, None)),
        (["status"], (False, None)),
    ],
)
def test_only_the_plain_statusline_invocation_takes_the_fast_path(
    argv: list[str], expected: tuple[bool, str | None]
) -> None:
    assert _statusline_theme(argv) == expected


def _env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {
        **os.environ,
        "HOME": str(home),
        "EAWF_STATUSLINE_CACHE": str(tmp_path / "cache"),
        "EAWF_STATUSLINE_THEME": "ascii-fallback",
    }


#: Runs the same command through the Typer app, bypassing the entry point.
_CLI = (
    "import sys\n"
    "sys.argv = ['eawf', *sys.argv[1:]]\n"
    "from eawf.surfaces.cli.app import main\n"
    "main()\n"
)


def _run(
    tmp_path: Path, payload: dict[str, object], *args: str, program: str = _PROBE
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", program, "cc", "statusline", *args],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=_env(tmp_path),
        cwd=tmp_path,
        timeout=60,
        check=True,
    )


def _seed(tmp_path: Path, session_id: str, value: str, written_at: float) -> Path:
    path = tmp_path / "cache" / f"{session_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps({"value": value, "written_at": written_at}))
    return path


def test_a_fresh_hit_is_printed_without_loading_the_cli_app(tmp_path: Path) -> None:
    _seed(tmp_path, "ses-fast", "CACHED", written_at=time.time() + 3600)
    proc = _run(tmp_path, {"session_id": "ses-fast"})
    assert proc.stdout == "CACHED\n"
    assert proc.stderr.strip() == "app-unloaded"


def test_a_miss_is_rendered_and_cached_without_loading_the_cli_app(tmp_path: Path) -> None:
    proc = _run(tmp_path, {"session_id": "ses-miss", "model": "haiku"}, "--theme", "ascii-fallback")
    assert "model:haiku" in proc.stdout
    assert proc.stderr.strip() == "app-unloaded"
    cached = orjson.loads((tmp_path / "cache" / "ses-miss.json").read_bytes())
    assert cached["value"] == proc.stdout.rstrip("\n")


def test_a_miss_prints_and_caches_what_the_cli_command_would(tmp_path: Path) -> None:
    payload: dict[str, object] = {"session_id": "ses-same", "model": "haiku"}
    (tmp_path / "fast").mkdir()
    (tmp_path / "cli").mkdir()
    fast = _run(tmp_path / "fast", payload)
    cli = _run(tmp_path / "cli", payload, program=_CLI)
    assert fast.stdout == cli.stdout
    fast_entry = orjson.loads((tmp_path / "fast" / "cache" / "ses-same.json").read_bytes())
    cli_entry = orjson.loads((tmp_path / "cli" / "cache" / "ses-same.json").read_bytes())
    assert fast_entry["value"] == cli_entry["value"]


def test_a_payload_without_a_session_renders_uncached(tmp_path: Path) -> None:
    proc = _run(tmp_path, {"model": "haiku"})
    assert "model:haiku" in proc.stdout
    assert proc.stderr.strip() == "app-unloaded"
    cache = tmp_path / "cache"
    assert not cache.exists() or not any(cache.iterdir())


def test_an_empty_payload_still_renders_one_line(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, "cc", "statusline"],
        input="",
        capture_output=True,
        text=True,
        env=_env(tmp_path),
        cwd=tmp_path,
        timeout=60,
        check=True,
    )
    assert proc.stdout.count("\n") == 1
    assert proc.stderr.strip() == "app-unloaded"


def test_another_statusline_form_still_goes_to_the_cli(tmp_path: Path) -> None:
    proc = _run(tmp_path, {"session_id": "ses-warm", "model": "haiku"}, "prewarm")
    # The CLI app exits the process itself, so the probe's own marker never prints.
    assert "app-unloaded" not in proc.stderr
    cached = orjson.loads((tmp_path / "cache" / "ses-warm.json").read_bytes())
    assert "model:haiku" in cached["value"]


def test_a_stale_hit_is_served_then_refreshed_in_the_background(tmp_path: Path) -> None:
    path = _seed(tmp_path, "ses-stale", "OLD", written_at=0.0)
    proc = _run(tmp_path, {"session_id": "ses-stale", "model": "haiku"})
    assert proc.stdout == "OLD\n"
    assert proc.stderr.strip() == "app-unloaded"
    deadline = time.monotonic() + 45
    value = "OLD"
    while time.monotonic() < deadline:
        value = orjson.loads(path.read_bytes())["value"]
        if value != "OLD":
            break
        time.sleep(0.1)
    assert "model:haiku" in value
