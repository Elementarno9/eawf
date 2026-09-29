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
- ``eawf`` and ``just`` scope — the project CLI and its task runner are
  admitted the way ``git`` is: ``eawf`` only in the read-only forms of
  :data:`EAWF_READ_ONLY_SUBVERBS` (a preview verb only with its preview
  flag, never with a flag in :data:`EAWF_MUTATING_FLAGS`), and ``just``
  only for a recipe in :data:`JUST_READ_ONLY_RECIPES`.
- Wrapper recursion — when ``argv[0]`` is in
  :data:`WRAPPER_HEADS` (``uv``, ``uvx``, ``npm``, ``pnpm``, ``yarn``,
  ``npx``, ``cargo``, ``python``, ``python3``, ``poetry``, ``pdm``,
  ``hatch``, ``tox``, ``nox``, ``pipx``), the validator resolves the
  wrapper's execution form and re-applies the same rules to the
  effective nested command. Wrapper control tokens such as ``uv run``
  and ``npm run`` are scoped to their wrapper; they are not treated as
  the nested executable head. Thus ``uv run bash -c ...`` rejects on
  ``bash`` while ``uv run npm run lint`` validates ``lint`` as the
  effective script command.

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

#: Wrapper heads that can run an effective nested command. The
#: wrapper-specific control tokens are resolved before recursion so
#: policy applies to the executable or package-script name rather than
#: to a literal wrapper token such as ``run``.
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

#: Wrappers whose nested command appears after a scoped sub-command.
WRAPPER_SCOPED_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "uv": frozenset({"run"}),
    "npm": frozenset({"exec", "run"}),
    "pnpm": frozenset({"dlx", "exec", "run"}),
    "yarn": frozenset({"dlx", "exec", "run"}),
    "poetry": frozenset({"run"}),
    "pdm": frozenset({"run"}),
    "hatch": frozenset({"run"}),
    "pipx": frozenset({"run"}),
}

#: Wrappers whose nested command starts immediately after the wrapper head.
WRAPPER_DIRECT_HEADS: frozenset[str] = frozenset({"uvx", "npx", "tox", "nox"})

#: Python module execution is a wrapper form only for ``python -m <module>``.
PYTHON_MODULE_WRAPPERS: frozenset[str] = frozenset({"python", "python3"})

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


#: ``eawf`` sub-verbs a gate may run. Admitting the project's own CLI is
#: interim: it exists so a criterion whose truth is this command line has
#: an admissible falsifier without a test that shells it, and it retires
#: once the gate contract names registered command families instead of
#: bare argv heads. Like ``git`` it is scoped to forms that observe and
#: report, never to a verb that writes state.
EAWF_READ_ONLY_SUBVERBS: frozenset[str] = frozenset(
    {"--version", "version", "status", "validate", "doctor", "why", "bench", "release"}
)

#: ``eawf`` verb groups that also carry mutating sub-verbs, mapped to the
#: sub-verbs of each a gate may run.
EAWF_SCOPED_SUBVERBS: dict[str, frozenset[str]] = {
    "bench": frozenset({"turn-cost"}),
    "release": frozenset({"show", "tag"}),
}

#: ``eawf`` commands that act unless the argv asks for their preview, mapped
#: to the flag that makes them a preview.
EAWF_PREVIEW_FLAGS: dict[tuple[str, str], str] = {("release", "tag"): "--dry-run"}

#: Flags that turn an otherwise read-only ``eawf`` form into a mutation.
EAWF_MUTATING_FLAGS: frozenset[str] = frozenset({"--fix", "--yes", "--push"})

#: ``just`` recipes a gate may run: the test recipes the repository's
#: justfile declares, each of which runs the suite and writes nothing back.
#: Retires with the ``eawf`` widening above, for the same reason.
JUST_READ_ONLY_RECIPES: frozenset[str] = frozenset({"test", "test-all", "test-tui"})


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


def _check_head_allowlisted(argv: list[str], *, allowlist: frozenset[str]) -> None:
    """Refuse if ``argv[0]`` is not in *allowlist*, quoting the argv it heads."""
    head = argv[0]
    if head not in allowlist:
        logger.warning(f"validate_gate_argv reject head={head!r} reason=not-in-allowlist")
        raise ArgvPolicyError(
            f"argv[0] {head!r} is not in the caller-supplied allowlist (argv {argv!r})"
        )


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


def _check_eawf_form(argv: list[str]) -> None:
    """Apply the read-only scope when ``argv[0] == "eawf"``."""
    subverb = argv[1] if len(argv) > 1 else None
    if subverb not in EAWF_READ_ONLY_SUBVERBS:
        logger.warning(f"validate_gate_argv reject subverb={subverb!r} reason=eawf-not-read-only")
        raise ArgvPolicyError(
            f"eawf sub-verb {subverb!r} is not in the read-only allow set (argv {argv!r})"
        )
    scoped = EAWF_SCOPED_SUBVERBS.get(subverb)
    command = subverb if scoped is None else (argv[2] if len(argv) > 2 else None)
    if scoped is not None and command not in scoped:
        logger.warning(
            f"validate_gate_argv reject command={subverb!r}/{command!r} reason=eawf-not-read-only"
        )
        raise ArgvPolicyError(
            f"eawf {subverb} {command!r} is not in the read-only allow set (argv {argv!r})"
        )
    mutating = sorted(EAWF_MUTATING_FLAGS.intersection(argv))
    if mutating:
        logger.warning(f"validate_gate_argv reject flags={mutating!r} reason=eawf-mutating-flag")
        raise ArgvPolicyError(f"eawf argv carries mutating flag(s) {mutating} (argv {argv!r})")
    preview = EAWF_PREVIEW_FLAGS.get((subverb, command or ""))
    if preview is not None and preview not in argv:
        logger.warning(f"validate_gate_argv reject command={subverb!r} reason=eawf-not-preview")
        raise ArgvPolicyError(
            f"eawf {subverb} {command} acts unless run as {preview} (argv {argv!r})"
        )


def _check_just_recipe(argv: list[str]) -> None:
    """Apply the declared-recipe scope when ``argv[0] == "just"``."""
    recipe = argv[1] if len(argv) > 1 else None
    if recipe not in JUST_READ_ONLY_RECIPES:
        logger.warning(f"validate_gate_argv reject recipe={recipe!r} reason=just-undeclared")
        raise ArgvPolicyError(
            f"just recipe {recipe!r} is not in the read-only allow set "
            f"{sorted(JUST_READ_ONLY_RECIPES)} (argv {argv!r})"
        )


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


def _validate_one_level(argv: list[str], *, allowlist: frozenset[str]) -> None:
    """Apply the single-level rules to *argv*.

    Internal helper extracted so :func:`validate_gate_argv` can recurse
    for wrapper heads without duplicating the per-level rule sequence.
    """
    head = argv[0]
    _check_head_path_qualified(head)
    _check_head_shell_deny(head)
    _check_head_allowlisted(argv, allowlist=allowlist)
    _check_metachars(argv)
    if head == "git":
        _check_git_subverb(argv)
    elif head == "eawf":
        _check_eawf_form(argv)
    elif head == "just":
        _check_just_recipe(argv)


def _effective_wrapper_argv(argv: list[str]) -> list[str] | None:
    """Return the effective nested command for a wrapper invocation.

    ``None`` means the wrapper invocation does not expose an inner
    command that this policy can validate. An empty list means the
    wrapper form expects an inner command but none was supplied.
    """
    head = argv[0]
    if head in WRAPPER_SCOPED_SUBCOMMANDS:
        if len(argv) == 1:
            return None
        subcommand = argv[1]
        if subcommand not in WRAPPER_SCOPED_SUBCOMMANDS[head]:
            logger.warning(
                f"validate_gate_argv reject head={head!r} "
                f"subcommand={subcommand!r} reason=unsupported-wrapper-subcommand"
            )
            raise ArgvPolicyError(
                f"wrapper {head!r} subcommand {subcommand!r} does not expose a scoped command"
            )
        return argv[2:]
    if head in WRAPPER_DIRECT_HEADS:
        if len(argv) == 1:
            return None
        return argv[1:]
    if head in PYTHON_MODULE_WRAPPERS:
        if len(argv) == 1:
            return None
        if len(argv) >= 3 and argv[1] == "-m":
            return argv[2:]
        logger.warning(
            f"validate_gate_argv reject head={head!r} reason=unsupported-python-wrapper-form"
        )
        raise ArgvPolicyError(f"python wrapper {head!r} must use -m to expose a scoped command")
    if len(argv) == 1:
        return None
    return argv[1:]


def _validate_effective_wrappers(argv: list[str], *, allowlist: frozenset[str]) -> None:
    """Recurse through wrapper layers and validate the effective command."""
    current = argv
    while current[0] in WRAPPER_HEADS:
        effective = _effective_wrapper_argv(current)
        if effective is None:
            return
        if not effective:
            logger.warning(
                f"validate_gate_argv reject head={current[0]!r} reason=wrapper-missing-command"
            )
            raise ArgvPolicyError(f"wrapper {current[0]!r} invocation must include a command")
        _validate_one_level(effective, allowlist=allowlist)
        current = effective


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
    _validate_one_level(checked, allowlist=allowlist_set)
    _validate_effective_wrappers(checked, allowlist=allowlist_set)
    return checked


__all__ = [
    "EAWF_MUTATING_FLAGS",
    "EAWF_PREVIEW_FLAGS",
    "EAWF_READ_ONLY_SUBVERBS",
    "EAWF_SCOPED_SUBVERBS",
    "GIT_ALLOWED_SUBVERBS",
    "GIT_DENIED_SUBVERBS",
    "JUST_READ_ONLY_RECIPES",
    "SHELL_DENY_HEADS",
    "SHELL_METACHARS",
    "WRAPPER_HEADS",
    "ArgvPolicyError",
    "validate_gate_argv",
]
