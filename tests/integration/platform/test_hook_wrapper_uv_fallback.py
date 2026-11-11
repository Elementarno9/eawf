"""Run rendered hook wrappers under bash against a stub ``uv`` in a managed repository.

The wrapper runs the host project's own ``.venv`` eawf (``uv run --project
<root> --no-sync``) whenever the project has one, so a global eawf of another
version never answers; it passes ``--no-project`` only when the project has no
eawf and one is on PATH, and otherwise runs nothing. The stub ``uv`` records
one argv line per invocation and exits with a chosen status, which also pins
that the wrapper never retries and never passes that status on.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from eawf.runtime.hooks.event import HookEventType
from eawf.surfaces.render.hooks import render_hook_sh

_BASH: str | None = shutil.which("bash")

# Directories the wrapper's PATH bootstrap appends regardless of HOME; an eawf
# there would make the "off PATH" case unreachable on this machine.
_BOOTSTRAP_DIRS: tuple[Path, ...] = (
    Path("/usr/bin"),
    Path("/bin"),
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
)

_ARG_SEP: str = "\x1f"

_UV_STUB: str = """#!/bin/sh
printf '%s\\037' "$@" >> "$STUB_UV_LOG"
printf '\\n' >> "$STUB_UV_LOG"
cat > "$STUB_UV_STDIN"
printf '%s' "${STUB_UV_STDOUT-}"
exit "${STUB_UV_EXIT:-0}"
"""

pytestmark = pytest.mark.skipif(_BASH is None, reason="bash is not installed")


def _write_exe(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _managed_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    if not project.exists():
        project.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        (project / ".ea").mkdir()
        (project / ".ea" / "state.json").write_text("{}\n", encoding="utf-8")
    return project.resolve()


def _run_wrapper(
    tmp_path: Path,
    event_type: HookEventType,
    runtime: str,
    *,
    eawf_on_path: bool,
    payload: str,
    project_venv: bool = False,
    uv_exit: int = 0,
    uv_stdout: str = "",
) -> tuple[subprocess.CompletedProcess[str], list[list[str]], str]:
    """Render and run one wrapper; return the result, uv argvs and uv stdin.

    Args:
        tmp_path: Scratch directory holding the stubs, wrapper and logs.
        event_type: Hook event whose wrapper is rendered.
        runtime: ``claude`` or ``codex``.
        eawf_on_path: Whether a stub ``eawf`` executable is put on PATH.
        payload: JSON document fed to the wrapper on stdin.
        project_venv: Whether the working directory's ``.venv`` holds eawf.
        uv_exit: Exit status the stub ``uv`` returns.
        uv_stdout: Text the stub ``uv`` prints.

    Returns:
        The completed wrapper process, one argv list per ``uv`` invocation,
        and the stdin the last ``uv`` invocation received.
    """
    if not eawf_on_path:
        found = [d for d in _BOOTSTRAP_DIRS if (d / "eawf").exists()]
        if found:
            pytest.skip(f"an eawf executable is installed in {found[0]}")
    uv_dir = tmp_path / "uv-bin"
    _write_exe(uv_dir / "uv", _UV_STUB)
    path_dirs = [str(uv_dir)]
    if eawf_on_path:
        eawf_dir = tmp_path / "eawf-bin"
        _write_exe(eawf_dir / "eawf", "#!/bin/sh\nexit 0\n")
        path_dirs.append(str(eawf_dir))
    path_dirs += ["/usr/bin", "/bin"]
    wrapper = tmp_path / f"{event_type.value}.sh"
    wrapper.write_text(render_hook_sh(event_type, runtime=runtime), encoding="utf-8")
    project = _managed_project(tmp_path)
    if project_venv:
        _write_exe(project / ".venv" / "bin" / "eawf", "#!/bin/sh\nexit 0\n")
    log = tmp_path / "uv-argv.log"
    stdin_file = tmp_path / "uv-stdin.txt"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": os.pathsep.join(path_dirs),
        "HOME": str(home),
        "TMPDIR": str(tmp_path),
        "STUB_UV_LOG": str(log),
        "STUB_UV_STDIN": str(stdin_file),
        "STUB_UV_EXIT": str(uv_exit),
        "STUB_UV_STDOUT": uv_stdout,
    }
    assert _BASH is not None
    result = subprocess.run(
        [_BASH, str(wrapper)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        cwd=project,
        check=False,
        timeout=30,
    )
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    argvs = [line.split(_ARG_SEP)[:-1] for line in lines]
    received = stdin_file.read_text(encoding="utf-8") if stdin_file.exists() else ""
    return result, argvs, received


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("event_type", list(HookEventType))
def test_wrapper_passes_no_project_only_when_eawf_is_on_path(
    tmp_path: Path, event_type: HookEventType, runtime: str
) -> None:
    payload = '{"hook_event_name":"X"}'
    result, argvs, received = _run_wrapper(
        tmp_path, event_type, runtime, eawf_on_path=True, payload=payload
    )
    project = _managed_project(tmp_path)
    assert result.returncode == 0, result.stderr
    assert argvs == [
        [
            "run",
            "--no-project",
            "eawf",
            "-w",
            str(project),
            "hook",
            "run",
            event_type.value,
            "--runtime",
            runtime,
        ]
    ]
    assert received == payload


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_wrapper_without_any_eawf_runs_nothing_and_exits_zero(tmp_path: Path, runtime: str) -> None:
    result, argvs, _ = _run_wrapper(
        tmp_path, HookEventType.PRE_TOOL_USE, runtime, eawf_on_path=False, payload="{}"
    )
    assert (result.returncode, result.stdout) == (0, "")
    assert argvs == []


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("uv_exit", [1, 2, 9, 127])
def test_a_failing_child_exits_zero_once_without_retry(
    tmp_path: Path, runtime: str, uv_exit: int
) -> None:
    """A non-zero child is recorded, never retried and never passed to the host."""
    result, argvs, _ = _run_wrapper(
        tmp_path,
        HookEventType.PRE_TOOL_USE,
        runtime,
        eawf_on_path=True,
        payload='{"tool_name":"Bash","tool_input":{"command":"rm -rf x"}}',
        uv_exit=uv_exit,
        uv_stdout='{"decision":"block"}',
    )
    assert (result.returncode, result.stdout) == (0, "")
    assert len(argvs) == 1


def test_pre_tool_success_forwards_the_hook_output(tmp_path: Path) -> None:
    result, argvs, _ = _run_wrapper(
        tmp_path,
        HookEventType.PRE_TOOL_USE,
        "claude",
        eawf_on_path=True,
        payload='{"tool_name":"Read"}',
        uv_stdout='{"decision":"allow"}',
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == '{"decision":"allow"}\n'
    assert len(argvs) == 1


def test_codex_discards_the_stdout_of_an_observing_event(tmp_path: Path) -> None:
    result, _argvs, _ = _run_wrapper(
        tmp_path,
        HookEventType.SESSION_END,
        "codex",
        eawf_on_path=True,
        payload="{}",
        uv_stdout='{"envelope":1}',
    )
    assert (result.returncode, result.stdout) == (0, "")


def test_empty_stdin_forwards_an_empty_payload(tmp_path: Path) -> None:
    result, argvs, received = _run_wrapper(
        tmp_path, HookEventType.SESSION_START, "claude", eawf_on_path=True, payload=""
    )
    assert result.returncode == 0, result.stderr
    assert argvs[0][:2] == ["run", "--no-project"]
    assert received == ""


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("eawf_on_path", [True, False], ids=["eawf-on-path", "eawf-off-path"])
def test_project_venv_eawf_beats_a_global_eawf(
    tmp_path: Path, runtime: str, eawf_on_path: bool
) -> None:
    """A project ``.venv`` eawf runs even when another eawf is on PATH."""
    result, argvs, _ = _run_wrapper(
        tmp_path,
        HookEventType.PRE_TOOL_USE,
        runtime,
        eawf_on_path=eawf_on_path,
        project_venv=True,
        payload='{"tool_name":"Read"}',
    )
    assert result.returncode == 0, result.stderr
    project = _managed_project(tmp_path)
    assert len(argvs) == 1
    assert argvs[0][:4] == ["run", "--project", str(project), "--no-sync"]
    assert argvs[0][4:9] == ["eawf", "-w", str(project), "hook", "run"]
