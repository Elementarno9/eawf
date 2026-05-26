r"""L0 argv-policy validator for the gate-runner sandbox boundary.

The ship gauntlet, the audit-DSL ``command_exit_zero`` check, and any
future helper that shells out via :func:`subprocess.run` route their
argv through :func:`validate_gate_argv` first. The validator rejects
five reject classes before any process is spawned:

1. **Type** — argv must be ``list[str]``; a single ``str`` (typical
   shell-injection vector) or a list with non-``str`` elements fails.
2. **Path-qualified head** — ``argv[0]`` may not contain ``/`` or ``\``;
   the binary must be looked up via ``PATH`` so the policy table cannot
   be sidestepped with an absolute / explicit-relative path.
3. **Shell-deny floor** — ``argv[0]`` may not be any of
   :data:`SHELL_DENY_HEADS` (``sh``, ``bash``, ``env``, ``xargs``,
   ``sudo``, ``ssh``, ``eval``, ``fish``, ``zsh``, ``csh``, ``ksh``,
   ``dash``) regardless of the caller-supplied allowlist.
4. **Allowlist** — ``argv[0]`` must appear in the caller's
   ``allowlist`` argument.
5. **Shell metacharacters** — none of
   :data:`SHELL_METACHARS` (``|``, ``;``, ``&``, ``$``, `` ` ``,
   ``>``, ``<``, ``(``, ``)``, ``*``, ``?``) may appear anywhere in
   *argv*; even a benign-looking ``ls *`` is rejected so the wrapper
   cannot rely on shell-expansion semantics it never receives.

Two special cases extend the floor:

- ``git`` sub-allowlist — when ``argv[0] == "git"``, ``argv[1]`` must
  be in :data:`GIT_ALLOWED_SUBVERBS` (read-only verbs only); any
  member of :data:`GIT_DENIED_SUBVERBS` or an unknown sub-verb is
  rejected even if ``git`` itself is allowlisted.
- Depth-1 wrapper recursion — when ``argv[0]`` is in
  :data:`WRAPPER_HEADS` (``uv``, ``uvx``, ``npm``, ``pnpm``, ``yarn``,
  ``npx``, ``cargo``, ``python``, ``python3``, ``poetry``, ``pdm``,
  ``hatch``, ``tox``, ``nox``, ``pipx``), the validator re-applies
  the same rules to ``argv[1:]`` exactly once. Inside that recursion
  any further wrapper head is rejected as a "nested wrapper" — so
  ``uv run npm run x`` fails while ``uv run pre-commit run
  --all-files`` passes (assuming ``uv``, ``run``, ``pre-commit`` are
  all in the caller's allowlist).

Reject decisions emit a structured log line at WARNING; never raw-log
the offending argv (rule 16) when the caller cannot vouch for its
contents — the validator does log ``argv!r`` so triage works, but
callers that pass untrusted user input should pre-redact before
invocation.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


#: Shells / privilege-escalating heads that are never allowed to run as
#: a gate command. The floor applies regardless of the caller's
#: allowlist — a shell handed an argv vector still composes its own
#: command interpretation, which is exactly what the gate-runner sandbox
#: refuses to expose to. ``env`` and ``xargs`` are included because both
#: re-exec arbitrary argv pulled from their own arguments / stdin and
#: would defeat the rest of the rules.
SHELL_DENY_HEADS: frozenset[str] = frozenset(
    {
        "sh",
        "bash",
        "env",
        "xargs",
        "sudo",
        "ssh",
        "eval",
        "fish",
        "zsh",
        "csh",
        "ksh",
        "dash",
    }
)

#: Characters that carry shell metasemantics. The gate-runner spawns the
#: process directly (``shell=False``), so these would survive only as
#: literal characters in argv — but rejecting them means callers cannot
#: accidentally lean on shell expansion and inherit the surprise of a
#: future ``shell=True`` regression.
SHELL_METACHARS: frozenset[str] = frozenset({"|", ";", "&", "$", "`", ">", "<", "(", ")", "*", "?"})

#: Wrapper heads whose ``argv[1:]`` runs the real command. Validated
#: with depth-1 recursion so the inner command (``uv run pre-commit
#: ...``) is checked against the same rules without the outer wrapper
#: providing a sandbox bypass. Nested wrappers (``uv run npm ...``)
#: reject inside the recursion because they would otherwise hide the
#: inner-most head from policy.
WRAPPER_HEADS: frozenset[str] = frozenset(
    {
        "uv",
        "uvx",
        "npm",
        "pnpm",
        "yarn",
        "npx",
        "cargo",
        "python",
        "python3",
        "poetry",
        "pdm",
        "hatch",
        "tox",
        "nox",
        "pipx",
    }
)

#: Git sub-verbs that read-only inspect the repository state. The ship
#: gauntlet, the audit-DSL runner, and the worktree helpers all need to
#: read git state; none of them needs to mutate it through the gate
#: runner. (Mutating verbs land via the dedicated ``git`` helper module,
#: which has its own typed interface.)
GIT_ALLOWED_SUBVERBS: frozenset[str] = frozenset(
    {
        "diff",
        "log",
        "status",
        "rev-parse",
        "show",
        "ls-files",
        "cat-file",
        "for-each-ref",
        "describe",
        "blame",
        "grep",
    }
)

#: Git sub-verbs that mutate state or escalate execution. These are
#: rejected explicitly so a typo against the allowlist doesn't silently
#: pass; the explicit deny set is also the canonical citation for code
#: reviewers who need to know what the gate runner forbids.
GIT_DENIED_SUBVERBS: frozenset[str] = frozenset(
    {
        "config",
        "commit",
        "push",
        "-c",
        "--exec-path",
        "hooks",
    }
)


class ArgvPolicyError(ValueError):
    """Raised when :func:`validate_gate_argv` rejects an argv vector.

    Subclasses :class:`ValueError` so callers that catch the broader
    type (e.g. CLI front-ends mapping value errors to ``InvalidInput``)
    catch the policy violation transparently. The message names the
    rejected head / element and the reject class.
    """


def _check_head_path_qualified(head: str) -> None:
    """Refuse if *head* contains ``/`` or ``\\`` — must be a bare command."""
    if "/" in head or "\\" in head:
        logger.warning(f"validate_gate_argv reject head={head!r} reason=path-qualified")
        raise ArgvPolicyError(f"argv[0] must be a bare command (no path separators): {head!r}")


def _check_head_shell_deny(head: str) -> None:
    """Refuse if *head* is in :data:`SHELL_DENY_HEADS`."""
    if head in SHELL_DENY_HEADS:
        logger.warning(f"validate_gate_argv reject head={head!r} reason=shell-deny")
        raise ArgvPolicyError(f"argv[0] is in the shell-deny floor: {head!r}")


def _check_head_allowlisted(head: str, *, allowlist: frozenset[str]) -> None:
    """Refuse if *head* is not in *allowlist*."""
    if head not in allowlist:
        logger.warning(f"validate_gate_argv reject head={head!r} reason=not-in-allowlist")
        raise ArgvPolicyError(f"argv[0] {head!r} is not in the caller-supplied allowlist")


def _check_metachars(argv: list[str]) -> None:
    """Refuse if any element of *argv* carries a shell metacharacter."""
    for element in argv:
        for char in element:
            if char in SHELL_METACHARS:
                logger.warning(
                    f"validate_gate_argv reject element={element!r} "
                    f"reason=shell-metachar char={char!r}"
                )
                raise ArgvPolicyError(
                    f"argv element {element!r} contains shell metacharacter {char!r}"
                )


def _check_git_subverb(argv: list[str]) -> None:
    """Apply the git sub-allowlist when ``argv[0] == "git"``."""
    if len(argv) < 2:
        logger.warning(f"validate_gate_argv reject argv={argv!r} reason=git-missing-subverb")
        raise ArgvPolicyError("git invocation must include a sub-verb")
    subverb = argv[1]
    if subverb in GIT_DENIED_SUBVERBS:
        logger.warning(f"validate_gate_argv reject subverb={subverb!r} reason=git-denied-subverb")
        raise ArgvPolicyError(f"git sub-verb {subverb!r} is in the denied set")
    if subverb not in GIT_ALLOWED_SUBVERBS:
        logger.warning(f"validate_gate_argv reject subverb={subverb!r} reason=git-unknown-subverb")
        raise ArgvPolicyError(f"git sub-verb {subverb!r} is not in the read-only allow set")


def _check_list_of_str(argv: object) -> list[str]:
    """Narrow *argv* to ``list[str]`` or raise."""
    if not isinstance(argv, list):
        logger.warning(f"validate_gate_argv reject argv_type={type(argv).__name__}")
        raise ArgvPolicyError(f"argv must be list[str]; got {type(argv).__name__}")
    if not argv:
        logger.warning("validate_gate_argv reject argv=empty")
        raise ArgvPolicyError("argv must be a non-empty list[str]")
    for index, element in enumerate(argv):
        if not isinstance(element, str):
            logger.warning(
                f"validate_gate_argv reject index={index} element_type={type(element).__name__}"
            )
            raise ArgvPolicyError(f"argv[{index}] must be str; got {type(element).__name__}")
    return argv


def _validate_one_level(argv: list[str], *, allowlist: frozenset[str], depth: int) -> None:
    """Apply the single-level rules to *argv* at recursion *depth*.

    Internal helper extracted so :func:`validate_gate_argv` can recurse
    exactly once for wrapper heads without duplicating the per-level
    rule sequence.
    """
    head = argv[0]
    _check_head_path_qualified(head)
    _check_head_shell_deny(head)
    if depth >= 1 and head in WRAPPER_HEADS:
        logger.warning(
            f"validate_gate_argv reject head={head!r} reason=nested-wrapper depth={depth}"
        )
        raise ArgvPolicyError(f"nested wrapper {head!r} inside wrapper recursion (depth-1 limit)")
    _check_head_allowlisted(head, allowlist=allowlist)
    _check_metachars(argv)
    if head == "git":
        _check_git_subverb(argv)


def validate_gate_argv(argv: list[str], *, allowlist: list[str]) -> list[str]:
    """Validate *argv* against the L0 gate-runner argv policy.

    Returns *argv* unchanged on pass so the validator can be used as a
    pass-through filter at the gate-runner boundary
    (``subprocess.run(validate_gate_argv(argv, allowlist=...))``). On
    reject, raises :class:`ArgvPolicyError` with a message naming
    the offending element + reject class — see the module docstring
    for the full reject-class enumeration.

    Args:
        argv: The argv vector that would be handed to
            :func:`subprocess.run`. Must be a non-empty ``list[str]``.
        allowlist: Caller-supplied list of permitted heads. Includes
            both outer wrappers (e.g. ``"uv"``) and post-recursion
            inner commands (e.g. ``"pre-commit"``, ``"ruff"``,
            ``"mypy"``, ``"pytest"``) so the depth-1 recursion can
            apply the same allowlist to the inner argv.

    Returns:
        The same *argv* on pass.

    Raises:
        ArgvPolicyError: When any reject class matches; the
            message names the offending element and the reject class
            so triage works without re-running validation.
    """
    checked = _check_list_of_str(argv)
    allowlist_set = frozenset(allowlist)
    _validate_one_level(checked, allowlist=allowlist_set, depth=0)
    head = checked[0]
    if head in WRAPPER_HEADS and len(checked) > 1:
        _validate_one_level(checked[1:], allowlist=allowlist_set, depth=1)
    return checked


__all__ = [
    "GIT_ALLOWED_SUBVERBS",
    "GIT_DENIED_SUBVERBS",
    "SHELL_DENY_HEADS",
    "SHELL_METACHARS",
    "WRAPPER_HEADS",
    "ArgvPolicyError",
    "validate_gate_argv",
]
