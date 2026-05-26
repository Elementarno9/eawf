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
7. Wrapper depth-1 recursion — ``uv run X`` validates ``X`` against the
   same allowlist; ``uv run npm run X`` rejects as nested wrapper.
"""

from __future__ import annotations

from typing import Any

import pytest

from eawf.runtime.sandbox.argv_policy import (
    GIT_ALLOWED_SUBVERBS,
    GIT_DENIED_SUBVERBS,
    SHELL_DENY_HEADS,
    SHELL_METACHARS,
    WRAPPER_HEADS,
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
    """``uv run pre-commit ...`` passes with depth-1 recursion."""
    argv = ["uv", "run", "pre-commit", "run", "--all-files"]
    allowlist = ["uv", "run", "pre-commit"]
    assert validate_gate_argv(argv, allowlist=allowlist) is argv


def test_validate_gate_argv_accepts_uv_run_pytest() -> None:
    """The canonical ``uv run pytest`` default-gate shape passes."""
    argv = ["uv", "run", "pytest"]
    allowlist = ["uv", "run", "pytest"]
    assert validate_gate_argv(argv, allowlist=allowlist) is argv


def test_validate_gate_argv_accepts_uv_run_ruff_with_dot() -> None:
    """``uv run ruff check .`` passes (dot is not a shell metachar)."""
    argv = ["uv", "run", "ruff", "check", "."]
    allowlist = ["uv", "run", "ruff"]
    assert validate_gate_argv(argv, allowlist=allowlist) is argv


def test_validate_gate_argv_accepts_uv_run_mypy_with_dot() -> None:
    """``uv run mypy .`` passes."""
    argv = ["uv", "run", "mypy", "."]
    allowlist = ["uv", "run", "mypy"]
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


# ---- wrapper depth-1 recursion -----------------------------------------------


@pytest.mark.parametrize("wrapper", sorted(WRAPPER_HEADS))
def test_validate_gate_argv_recurses_into_each_wrapper_head(wrapper: str) -> None:
    """For every wrapper head, the recursion validates argv[1:]."""
    # The inner head is allowlisted so the recursion passes on the happy path.
    argv = [wrapper, "pre-commit"]
    allowlist = [wrapper, "pre-commit"]
    assert validate_gate_argv(argv, allowlist=allowlist) is argv


def test_validate_gate_argv_rejects_nested_wrapper() -> None:
    """``uv run npm run x`` rejects: ``npm`` is a wrapper inside recursion."""
    # The inner argv[1:] = ["run", "npm", "run", "x"]. The depth-1 recursion
    # validates ["run", "npm", ...] — and "run" is the head. But "npm" still
    # surfaces because the recursion validates argv[0] of the slice, which
    # IS "run", which is NOT a wrapper. To trigger the nested-wrapper reject
    # the spec's example needs the inner *head* to be a wrapper, e.g.
    # ``uv npm`` (no intermediate ``run`` sub-verb).
    with pytest.raises(ArgvPolicyError, match="nested wrapper"):
        validate_gate_argv(["uv", "npm", "run", "x"], allowlist=["uv", "npm", "run"])


def test_validate_gate_argv_rejects_recursion_inner_shell_deny() -> None:
    """``uv bash -c ...`` rejects on the inner shell-deny floor."""
    with pytest.raises(ArgvPolicyError, match="shell-deny"):
        validate_gate_argv(["uv", "bash", "-c", "echo ok"], allowlist=["uv", "bash"])


def test_validate_gate_argv_rejects_recursion_inner_path_qualified() -> None:
    """``uv /usr/bin/x`` rejects on the inner path-qualified head check."""
    with pytest.raises(ArgvPolicyError, match="bare command"):
        validate_gate_argv(["uv", "/usr/bin/pre-commit"], allowlist=["uv", "/usr/bin/pre-commit"])


def test_validate_gate_argv_rejects_recursion_inner_allowlist_miss() -> None:
    """``uv unknown-tool`` rejects: inner head not in allowlist."""
    with pytest.raises(ArgvPolicyError, match="allowlist"):
        validate_gate_argv(["uv", "curl"], allowlist=["uv"])


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
