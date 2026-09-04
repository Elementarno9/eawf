"""Unit tests for :mod:`eawf.runtime.sandbox.argv_policy`.

Pin the L0 reject classes the gate-runner sandbox boundary enforces:

1. Type — :func:`validate_gate_argv` refuses anything but ``list[str]``.
2. Path-qualified head — ``argv[0]`` may not contain ``/`` or ``\\``.
3. Shell-deny floor — ``argv[0]`` may not be in
   :data:`SHELL_DENY_HEADS`.
4. Allowlist — ``argv[0]`` must appear in the caller's allowlist.
5. Shell metacharacters — none of :data:`SHELL_METACHARS` may appear
   anywhere in *argv*.
6. ``git`` sub-allowlist — read-only verbs only, denied verbs explicit.
7. Wrapper recursion — wrapper control tokens are scoped before
   recursion, so ``uv run X`` validates ``X`` and
   ``uv run npm run X`` validates ``X`` as the effective script.
"""

from __future__ import annotations

from typing import Any

import pytest

from eawf.runtime.sandbox.argv_policy import (
    GIT_ALLOWED_SUBVERBS,
    GIT_DENIED_SUBVERBS,
    SHELL_DENY_HEADS,
    SHELL_METACHARS,
    ArgvPolicyError,
    validate_gate_argv,
)

# ---- positive happy path -----------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["pre-commit", "run", "--all-files"],
        ["ruff", "check", "."],
        ["mypy", "."],
        ["pytest"],
    ],
)
def test_validate_gate_argv_accepts_allowlisted_bare_command(
    argv: list[str],
) -> None:
    """Allowlisted bare heads pass and the original argv is returned."""
    allowlist = ["pre-commit", "ruff", "mypy", "pytest"]
    assert validate_gate_argv(argv, allowlist=allowlist) is argv


def test_validate_gate_argv_accepts_uv_run_wrapper() -> None:
    """``uv run pre-commit ...`` passes with wrapper recursion."""
    argv = ["uv", "run", "pre-commit", "run", "--all-files"]
    allowlist = ["uv", "pre-commit"]
    assert validate_gate_argv(argv, allowlist=allowlist) is argv


def test_validate_gate_argv_accepts_uv_run_pytest() -> None:
    """The canonical ``uv run pytest`` default-gate shape passes."""
    argv = ["uv", "run", "pytest"]
    allowlist = ["uv", "pytest"]
    assert validate_gate_argv(argv, allowlist=allowlist) is argv


def test_validate_gate_argv_accepts_uv_run_ruff_with_dot() -> None:
    """``uv run ruff check .`` passes (dot is not a shell metachar)."""
    argv = ["uv", "run", "ruff", "check", "."]
    allowlist = ["uv", "ruff"]
    assert validate_gate_argv(argv, allowlist=allowlist) is argv


def test_validate_gate_argv_accepts_uv_run_mypy_with_dot() -> None:
    """``uv run mypy .`` passes."""
    argv = ["uv", "run", "mypy", "."]
    allowlist = ["uv", "mypy"]
    assert validate_gate_argv(argv, allowlist=allowlist) is argv


# ---- reject class 1: type ----------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        "pre-commit run --all-files",  # str, not list
        None,
        42,
        ("pre-commit", "run"),  # tuple, not list
        {"argv": "pre-commit"},
    ],
)
def test_validate_gate_argv_rejects_non_list(argv: Any) -> None:
    """A non-``list`` argv raises immediately."""
    with pytest.raises(ArgvPolicyError, match="argv must be list"):
        validate_gate_argv(argv, allowlist=["pre-commit"])


def test_validate_gate_argv_rejects_empty_list() -> None:
    """An empty argv carries no head to validate; reject."""
    with pytest.raises(ArgvPolicyError, match="non-empty"):
        validate_gate_argv([], allowlist=["pre-commit"])


@pytest.mark.parametrize(
    "argv",
    [
        ["pre-commit", 1],
        ["pre-commit", None],
        [b"pre-commit", "run"],
        ["pre-commit", ["nested"]],
    ],
)
def test_validate_gate_argv_rejects_non_str_element(argv: list[Any]) -> None:
    """Any non-``str`` element in argv rejects with index citation."""
    with pytest.raises(ArgvPolicyError, match="must be str"):
        validate_gate_argv(argv, allowlist=["pre-commit"])


# ---- reject class 2: path-qualified head -------------------------------------


@pytest.mark.parametrize(
    "head",
    [
        "/usr/bin/pre-commit",
        "./pre-commit",
        "../pre-commit",
        "bin/pre-commit",
        "C:\\Python\\python.exe",
        "subdir\\tool",
    ],
)
def test_validate_gate_argv_rejects_path_qualified_head(head: str) -> None:
    """An ``argv[0]`` carrying a path separator is rejected."""
    with pytest.raises(ArgvPolicyError, match="bare command"):
        validate_gate_argv([head, "--version"], allowlist=[head, "pre-commit"])


# ---- reject class 3: shell-deny floor ----------------------------------------


@pytest.mark.parametrize("head", sorted(SHELL_DENY_HEADS))
def test_validate_gate_argv_rejects_shell_deny_head(head: str) -> None:
    """Every member of the shell-deny floor rejects regardless of allowlist."""
    # Allowlist explicitly includes the head so the deny floor is what catches it.
    with pytest.raises(ArgvPolicyError, match="shell-deny floor"):
        validate_gate_argv([head, "-c", "echo ok"], allowlist=[head])


# ---- reject class 4: not in allowlist ----------------------------------------


def test_validate_gate_argv_rejects_unknown_head() -> None:
    """A head not in the caller's allowlist rejects."""
    with pytest.raises(ArgvPolicyError, match="allowlist"):
        validate_gate_argv(["curl", "https://example.com"], allowlist=["pre-commit"])


def test_validate_gate_argv_rejects_with_empty_allowlist() -> None:
    """An empty allowlist rejects every head except shell-deny earlier."""
    with pytest.raises(ArgvPolicyError, match="allowlist"):
        validate_gate_argv(["pre-commit"], allowlist=[])


# ---- reject class 5: shell metacharacters ------------------------------------


@pytest.mark.parametrize("char", sorted(SHELL_METACHARS))
def test_validate_gate_argv_rejects_metachar_anywhere(char: str) -> None:
    """A metachar in any element rejects."""
    with pytest.raises(ArgvPolicyError, match="metacharacter"):
        validate_gate_argv(["pre-commit", f"arg{char}value"], allowlist=["pre-commit"])


def test_validate_gate_argv_rejects_metachar_in_head() -> None:
    """A metachar in argv[0] itself rejects."""
    with pytest.raises(ArgvPolicyError):
        validate_gate_argv(["pre-commit;rm"], allowlist=["pre-commit;rm", "pre-commit"])


def test_validate_gate_argv_rejects_glob_star() -> None:
    """``*`` rejects even when it looks like a benign glob arg."""
    with pytest.raises(ArgvPolicyError, match="metacharacter"):
        validate_gate_argv(["ls", "*.py"], allowlist=["ls"])


def test_validate_gate_argv_rejects_glob_question() -> None:
    """``?`` rejects as a metacharacter."""
    with pytest.raises(ArgvPolicyError, match="metacharacter"):
        validate_gate_argv(["ls", "?.py"], allowlist=["ls"])


# ---- git sub-allowlist -------------------------------------------------------


@pytest.mark.parametrize("subverb", sorted(GIT_ALLOWED_SUBVERBS))
def test_validate_gate_argv_accepts_git_read_only_subverb(subverb: str) -> None:
    """Each read-only git sub-verb passes (the canonical allow set)."""
    argv = ["git", subverb]
    assert validate_gate_argv(argv, allowlist=["git"]) is argv


@pytest.mark.parametrize("subverb", sorted(GIT_DENIED_SUBVERBS))
def test_validate_gate_argv_rejects_git_denied_subverb(subverb: str) -> None:
    """Each explicitly-denied git sub-verb rejects."""
    with pytest.raises(ArgvPolicyError, match="denied"):
        validate_gate_argv(["git", subverb], allowlist=["git"])


def test_validate_gate_argv_rejects_git_unknown_subverb() -> None:
    """A git sub-verb outside both the allow and deny sets still rejects."""
    with pytest.raises(ArgvPolicyError, match="read-only"):
        validate_gate_argv(["git", "fetch"], allowlist=["git"])


def test_validate_gate_argv_rejects_bare_git_without_subverb() -> None:
    """``git`` with no sub-verb rejects on the missing-subverb check."""
    with pytest.raises(ArgvPolicyError, match="sub-verb"):
        validate_gate_argv(["git"], allowlist=["git"])


# ---- wrapper recursion -------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["uv", "run", "pre-commit"],
        ["uvx", "pre-commit"],
        ["npm", "run", "pre-commit"],
        ["npm", "exec", "pre-commit"],
        ["pnpm", "run", "pre-commit"],
        ["pnpm", "exec", "pre-commit"],
        ["pnpm", "dlx", "pre-commit"],
        ["yarn", "run", "pre-commit"],
        ["yarn", "exec", "pre-commit"],
        ["yarn", "dlx", "pre-commit"],
        ["npx", "pre-commit"],
        ["python", "-m", "pre-commit"],
        ["python3", "-m", "pre-commit"],
        ["poetry", "run", "pre-commit"],
        ["pdm", "run", "pre-commit"],
        ["hatch", "run", "pre-commit"],
        ["tox", "pre-commit"],
        ["nox", "pre-commit"],
        ["pipx", "run", "pre-commit"],
    ],
)
def test_validate_gate_argv_recurses_into_wrapper_execution_forms(argv: list[str]) -> None:
    """For every wrapper head, recursion validates the scoped command."""
    allowlist = [argv[0], "pre-commit"]
    assert validate_gate_argv(argv, allowlist=allowlist) is argv


def test_validate_gate_argv_accepts_uv_run_npm_run_script() -> None:
    """``uv run npm run lint`` validates ``lint`` rather than ``run``."""
    argv = ["uv", "run", "npm", "run", "lint"]
    assert validate_gate_argv(argv, allowlist=["uv", "npm", "lint"]) is argv


@pytest.mark.parametrize(
    "argv",
    [
        ["uv", "run", "npm", "run", "lint"],
        ["uv", "run", "pnpm", "run", "lint"],
        ["uv", "run", "yarn", "run", "lint"],
        ["uv", "run", "npx", "lint"],
        ["uv", "run", "python", "-m", "pytest"],
    ],
)
def test_validate_gate_argv_accepts_nested_wrapper_variants(argv: list[str]) -> None:
    """Nested wrappers recurse to the final effective command."""
    allowlist = ["uv", "npm", "pnpm", "yarn", "npx", "python", "lint", "pytest"]
    assert validate_gate_argv(argv, allowlist=allowlist) is argv


def test_validate_gate_argv_rejects_uv_run_npm_run_script_allowlist_miss() -> None:
    """``run`` allowlisting cannot hide a disallowed npm script command."""
    with pytest.raises(ArgvPolicyError, match="allowlist"):
        validate_gate_argv(["uv", "run", "npm", "run", "lint"], allowlist=["uv", "npm", "run"])


def test_validate_gate_argv_rejects_uv_run_bash_c() -> None:
    """``uv run bash -c ...`` rejects on the effective shell head."""
    with pytest.raises(ArgvPolicyError, match="shell-deny"):
        validate_gate_argv(["uv", "run", "bash", "-c", "echo ok"], allowlist=["uv", "bash"])


def test_validate_gate_argv_rejects_unsupported_uv_wrapper_subcommand() -> None:
    """``uv npm ...`` is not scoped as an execution form."""
    with pytest.raises(ArgvPolicyError, match="does not expose"):
        validate_gate_argv(["uv", "npm", "run", "lint"], allowlist=["uv", "npm", "lint"])


def test_validate_gate_argv_rejects_recursion_inner_shell_deny() -> None:
    """``uv run bash -c ...`` rejects on the inner shell-deny floor."""
    with pytest.raises(ArgvPolicyError, match="shell-deny"):
        validate_gate_argv(["uv", "run", "bash", "-c", "echo ok"], allowlist=["uv", "bash"])


def test_validate_gate_argv_rejects_recursion_inner_path_qualified() -> None:
    """``uv run /usr/bin/x`` rejects on the inner path-qualified head check."""
    with pytest.raises(ArgvPolicyError, match="bare command"):
        validate_gate_argv(
            ["uv", "run", "/usr/bin/pre-commit"],
            allowlist=["uv", "/usr/bin/pre-commit"],
        )


def test_validate_gate_argv_rejects_recursion_inner_allowlist_miss() -> None:
    """``uv run unknown-tool`` rejects: inner head not in allowlist."""
    with pytest.raises(ArgvPolicyError, match="allowlist"):
        validate_gate_argv(["uv", "run", "curl"], allowlist=["uv"])


def test_validate_gate_argv_rejects_recursion_inner_metachar() -> None:
    """A metachar in the wrapped argv elements rejects at the outer check."""
    with pytest.raises(ArgvPolicyError, match="metacharacter"):
        validate_gate_argv(
            ["uv", "run", "pre-commit;rm"],
            allowlist=["uv", "run", "pre-commit;rm"],
        )


def test_validate_gate_argv_wrapper_with_no_inner_argv_passes() -> None:
    """A wrapper with no inner argv (just ``uv``) passes the recursion guard.

    The recursion only fires when ``len(argv) > 1`` so a bare wrapper
    invocation passes the depth-1 check trivially (the outer allowlist
    still requires the wrapper to be allowlisted).
    """
    argv = ["uv"]
    assert validate_gate_argv(argv, allowlist=["uv"]) is argv
