"""Run rendered hook wrappers under bash against a stub ``uv``.

The wrapper passes ``--no-project`` to ``uv run`` only when an ``eawf``
executable is on PATH; otherwise it runs plain ``uv run eawf`` so an eawf
installed solely as the host project's dependency still resolves. The stub
``uv`` records one argv line per invocation and exits with a chosen status,
which also pins that the wrapper never retries on a non-zero exit.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from eawf.runtime.hooks.event import HookEventType
from eawf.surfaces.render.hooks import render_hook_sh
from eawf.workflow.skills.publication import BUNDLE_TARGET_EPOCH

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


def _run_wrapper(
    tmp_path: Path,
    event_type: HookEventType,
    runtime: str,
    *,
    eawf_on_path: bool,
    payload: str,
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
    log = tmp_path / "uv-argv.log"
    stdin_file = tmp_path / "uv-stdin.txt"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": os.pathsep.join(path_dirs),
        "HOME": str(home),
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
        check=False,
        timeout=30,
    )
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    argvs = [line.split(_ARG_SEP)[:-1] for line in lines]
    received = stdin_file.read_text(encoding="utf-8") if stdin_file.exists() else ""
    return result, argvs, received


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("event_type", list(HookEventType))
@pytest.mark.parametrize(
    ("eawf_on_path", "uv_prefix"),
    [(True, ["run", "--no-project"]), (False, ["run"])],
    ids=["eawf-on-path", "eawf-off-path"],
)
def test_wrapper_passes_no_project_only_when_eawf_is_on_path(
    tmp_path: Path,
    event_type: HookEventType,
    runtime: str,
    eawf_on_path: bool,
    uv_prefix: list[str],
) -> None:
    payload = '{"hook_event_name":"X"}'
    result, argvs, received = _run_wrapper(
        tmp_path, event_type, runtime, eawf_on_path=eawf_on_path, payload=payload
    )
    assert result.returncode == 0, result.stderr
    assert argvs == [
        [
            *uv_prefix,
            "eawf",
            "hook",
            "run",
            event_type.value,
            "--runtime",
            runtime,
            "--target-epoch",
            str(BUNDLE_TARGET_EPOCH),
        ]
    ]
    assert received == payload


@pytest.mark.parametrize("runtime", ["claude", "codex"])
@pytest.mark.parametrize("eawf_on_path", [True, False], ids=["eawf-on-path", "eawf-off-path"])
def test_pre_tool_deny_exits_2_without_retry(
    tmp_path: Path, runtime: str, eawf_on_path: bool
) -> None:
    result, argvs, _ = _run_wrapper(
        tmp_path,
        HookEventType.PRE_TOOL_USE,
        runtime,
        eawf_on_path=eawf_on_path,
        payload='{"tool_name":"Bash","tool_input":{"command":"rm -rf x"}}',
        uv_exit=2,
    )
    assert result.returncode == 2
    assert len(argvs) == 1


@pytest.mark.parametrize("uv_exit", [1, 2, 127])
def test_pre_tool_failure_on_judged_tool_never_retries(tmp_path: Path, uv_exit: int) -> None:
    """Any non-zero exit on a judged tool is refused once, never re-run without the flag."""
    result, argvs, _ = _run_wrapper(
        tmp_path,
        HookEventType.PRE_TOOL_USE,
        "claude",
        eawf_on_path=True,
        payload='{"tool_name":"Write"}',
        uv_exit=uv_exit,
    )
    assert result.returncode == 2
    assert argvs[0][:2] == ["run", "--no-project"]
    assert len(argvs) == 1


def test_pre_tool_success_forwards_the_hook_output(tmp_path: Path) -> None:
    result, argvs, _ = _run_wrapper(
        tmp_path,
        HookEventType.PRE_TOOL_USE,
        "claude",
        eawf_on_path=False,
        payload='{"tool_name":"Read"}',
        uv_stdout='{"decision":"allow"}',
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == '{"decision":"allow"}'
    assert len(argvs) == 1


def test_non_pre_tool_wrapper_propagates_the_exit_status(tmp_path: Path) -> None:
    result, argvs, _ = _run_wrapper(
        tmp_path,
        HookEventType.SESSION_END,
        "claude",
        eawf_on_path=False,
        payload="{}",
        uv_exit=9,
    )
    assert result.returncode == 9
    assert len(argvs) == 1


def test_empty_stdin_falls_back_to_synthesised_payload(tmp_path: Path) -> None:
    result, argvs, received = _run_wrapper(
        tmp_path, HookEventType.SESSION_START, "claude", eawf_on_path=True, payload=""
    )
    assert result.returncode == 0, result.stderr
    assert argvs[0][:2] == ["run", "--no-project"]
    assert '"claude_event_name":"session_start"' in received
