"""Runtime regression tests for the ``eawf`` shell-completion handshake.

The P26-I01-W06 ``completion`` verb generates a completion script with
Typer's blessed ``get_completion_script``; the existing
:mod:`tests.cli.test_streaming` suite only exercises the *generation* path
(``completion show`` writes a script, ``completion install`` writes a file).
It never invokes the *runtime* handshake — the second process the shell spawns
at tab-completion time with ``_EAWF_COMPLETE=complete_<shell>`` set. That gap
let a Typer-0.25.1 / Click-8.2+ protocol mismatch ship: Typer's emitted zsh
script exported only ``_TYPER_COMPLETE_ARGS`` while Click's zsh handler reads
``COMP_WORDS`` / ``COMP_CWORD``, so tab-completion crashed with
``KeyError: 'COMP_WORDS'``.

These tests drive the real entry point (``eawf`` resolved on ``PATH`` via the
``uv``-installed console script) in a subprocess with the completion env vars
set — exactly what bash/zsh do at tab time — and assert the handshake (a) does
not crash / exit non-zero and (b) emits a non-empty candidate set for a known
prefix. The :func:`eawf.cli.commands.completion._patch_zsh_completion` fix is
covered head-on by :func:`test_zsh_script_exports_comp_words` plus the live
zsh-runtime test below.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from eawf.cli.commands.completion import _render_script

pytestmark = pytest.mark.unit

#: The console-script entry point ``uv`` installs into the active venv. When
#: the suite runs under ``uv run pytest`` the script is on ``PATH`` and its
#: ``argv[0]`` basename is ``eawf`` — which is what Typer keys the
#: ``_EAWF_COMPLETE`` env-var name off, so the completion path is only reached
#: when the program is actually invoked *as* ``eawf`` (not ``python -c``).
_EAWF_BIN = shutil.which("eawf")

#: Skip marker for environments where the console script is not installed
#: (e.g. a bare ``python -m pytest`` outside the managed venv). The
#: in-process script-content checks below still run there.
_requires_console_script = pytest.mark.skipif(
    _EAWF_BIN is None,
    reason="eawf console script not on PATH (run under `uv run pytest`)",
)


def _run_completion(
    shell: str, comp_words: str, comp_cword: int
) -> subprocess.CompletedProcess[str]:
    """Invoke the real ``eawf`` completion runtime in a subprocess.

    Mirrors the second process bash/zsh spawn at tab-completion time: the
    completion env vars are set and the entry point is run *as* ``eawf`` so
    Typer recognises the ``_EAWF_COMPLETE`` activation var.

    Args:
        shell: Completion flavour — ``bash`` or ``zsh`` (drives the
            ``_EAWF_COMPLETE=complete_<shell>`` instruction Click dispatches).
        comp_words: The space-joined command line, e.g. ``"eawf wa"``. Click's
            handler splits this back into ``COMP_WORDS``.
        comp_cword: 0-based index of the word under the cursor — the
            ``COMP_CWORD`` Click reads to slice args from the incomplete token.

    Returns:
        The completed subprocess (captured stdout/stderr, decoded text).
    """
    assert _EAWF_BIN is not None  # guarded by _requires_console_script
    env = {
        **os.environ,
        "_EAWF_COMPLETE": f"complete_{shell}",
        "COMP_WORDS": comp_words,
        "COMP_CWORD": str(comp_cword),
    }
    return subprocess.run(
        [_EAWF_BIN],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


# --- runtime handshake: the regression these tests exist to catch -----------


@_requires_console_script
@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_completion_runtime_does_not_crash(shell: str) -> None:
    """The completion handshake exits 0 — never ``KeyError: 'COMP_WORDS'``.

    This is the head-on regression for the Typer/Click protocol mismatch: the
    unpatched zsh script crashed here with a non-zero exit and a ``KeyError``
    traceback on stderr.
    """
    result = _run_completion(shell, comp_words="eawf wa", comp_cword=1)
    assert result.returncode == 0, (
        f"completion runtime exited {result.returncode} for {shell}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "KeyError" not in result.stderr
    assert "Traceback" not in result.stderr


@_requires_console_script
@pytest.mark.parametrize("shell", ["bash", "zsh"])
def test_completion_runtime_emits_candidate_for_prefix(shell: str) -> None:
    """A known prefix (``eawf wa``) offers the ``wave`` subcommand.

    Asserts the handshake produces real candidates, not just a clean exit — a
    silently-empty completion would also exit 0 but still be broken.
    """
    result = _run_completion(shell, comp_words="eawf wa", comp_cword=1)
    assert result.returncode == 0
    assert result.stdout.strip(), f"empty completion output for {shell}: {result.stderr!r}"
    # bash emits ``plain,wave``; zsh emits a ``plain\nwave\n<help>`` triple.
    # Both carry the bare ``wave`` token for the ``wa`` prefix.
    assert "wave" in result.stdout


@_requires_console_script
def test_completion_runtime_zsh_advances_to_subcommands() -> None:
    """``eawf wave <TAB>`` (zsh) advances past the group to its subcommands.

    Exercises a non-zero ``COMP_CWORD`` so the args-slicing path (``args =
    cwords[1:cword]``) is covered, not just the first-token case.
    """
    result = _run_completion("zsh", comp_words="eawf wave ", comp_cword=2)
    assert result.returncode == 0
    assert "KeyError" not in result.stderr
    # The ``wave`` group exposes lifecycle verbs; ``plan`` is one of them.
    assert "plan" in result.stdout


@_requires_console_script
def test_completion_runtime_bash_unaffected_by_zsh_patch() -> None:
    """The bash handshake still works — the fix only rewrites the zsh script.

    Guards against the zsh patch accidentally regressing bash (whose Typer
    template already exported ``COMP_WORDS`` / ``COMP_CWORD``).
    """
    result = _run_completion("bash", comp_words="eawf wa", comp_cword=1)
    assert result.returncode == 0
    assert "wave" in result.stdout


# --- generated-script content: the fix, asserted in-process -----------------


def test_zsh_script_exports_comp_words() -> None:
    """The rendered zsh script exports the vars Click's zsh handler reads.

    The Typer-0.25.1 template exports only ``_TYPER_COMPLETE_ARGS``; the
    :func:`_patch_zsh_completion` fix injects ``COMP_WORDS`` / ``COMP_CWORD``
    so the runtime handshake matches Click >=8.2.
    """
    from eawf.cli.commands.completion import Shell

    rendered = _render_script(Shell.ZSH)
    assert 'COMP_WORDS="${words[*]}"' in rendered
    assert "COMP_CWORD=$((CURRENT - 1))" in rendered
    # The completion-function still calls the program with the activation var.
    assert "_EAWF_COMPLETE=complete_zsh" in rendered


def test_bash_script_unchanged_still_exports_comp_words() -> None:
    """The bash script is returned verbatim and already exports ``COMP_WORDS``.

    Confirms the fix is scoped to zsh — bash's Typer template was never broken.
    """
    from eawf.cli.commands.completion import Shell

    rendered = _render_script(Shell.BASH)
    assert "COMP_WORDS" in rendered
    assert "COMP_CWORD" in rendered


@_requires_console_script
def test_installed_zsh_script_sources_in_live_zsh() -> None:
    """The patched zsh script's *own* completion function works in a real zsh.

    The strongest proof, and the one that fails on the unpatched script: source
    the generated script in an actual ``zsh`` (when one is on the box), then
    invoke the ``_eawf_completion`` function the script defines — letting the
    script build the completion env itself from ``words`` / ``CURRENT`` — and
    assert it offers ``wave`` with no ``KeyError``. The unpatched script builds
    only ``_TYPER_COMPLETE_ARGS`` here, so Click crashes with
    ``KeyError: 'COMP_WORDS'`` (leaked from the inner ``eval`` subshell).

    Both ``completion show`` and the ``eawf`` wrapper are pinned to this
    worktree's binary so a globally ``uv tool install``-ed ``eawf`` cannot mask
    the locally-built fix.
    """
    zsh = shutil.which("zsh")
    if zsh is None:
        pytest.skip("zsh not installed")
    assert _EAWF_BIN is not None
    # Drive the script's *own* ``_eawf_completion`` function so its env-building
    # (``COMP_WORDS`` / ``COMP_CWORD`` vs only ``_TYPER_COMPLETE_ARGS``) is what
    # is under test. The function does ``eval $(env ... eawf)``: the inner
    # ``$(...)`` runs eawf to produce candidates, which ``eval`` would then feed
    # to zsh's compsys. We override ``eval`` to *print* what it receives so we
    # capture the candidate list (patched) — or the Click crash traceback
    # (unpatched) — without needing a live completion context. ``eawf`` and
    # ``compdef`` are stubbed so the worktree binary is used and the trailing
    # ``compdef`` registration is a no-op.
    program = (
        "emulate -L zsh\n"
        f'eawf() {{ {_EAWF_BIN!r} "$@"; }}\n'
        "compdef() { : }\n"
        f"source <({_EAWF_BIN!r} completion show zsh)\n"
        "words=(eawf wa)\n"
        "CURRENT=2\n"
        'eval() { print -r -- "$@"; }\n'
        "_eawf_completion\n"
    )
    result = subprocess.run(
        [zsh, "-c", program],
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = result.stdout + result.stderr
    assert "KeyError" not in combined, f"live zsh completion crashed: {combined!r}"
    assert "Traceback" not in combined
    assert "wave" in result.stdout, f"no candidate from live zsh: {combined!r}"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
