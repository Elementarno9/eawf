"""Registered command families a gate may run.

A gate names the command family its argv belongs to instead of leaning on
a bare argv head being present in an allowlist. Each family declares the
executable it runs, the argv shape a gate may hand it and that the shape
only observes and reports. The project's own CLI is admissible here as a
family scoped to its read-only forms, not as an unscoped head.

This module is pure data: :mod:`eawf.runtime.sandbox.argv_policy` applies
a family's shape wherever its head is admitted and resolves a gate argv to
the family it runs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

#: The id a registered command family is known by, in gates and in
#: provider tool policy alike.
CommandFamilyId = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_-]{0,63}$")]

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

#: Wrapper heads a gate argv may lead with ahead of the family it runs;
#: the argv policy unwraps them to reach the family's own head.
GATE_WRAPPER_HEADS: frozenset[str] = frozenset({"uv", "uvx"})


class CommandFamily(BaseModel):
    """One registered command family and the argv shape a gate may run.

    Attributes:
        family_id: The id a gate names in ``command_family_ref``.
        head: The executable the family runs, looked up on ``PATH``.
        read_only: Always true: a family is registrable only when every
            form its shape admits observes and reports, never writes state.
        verbs: Admitted first arguments after the head; ``None`` admits
            any first argument not in :attr:`mutating_verbs` (a test
            runner taking paths).
        mutating_verbs: First arguments that make the head write state,
            refused whatever :attr:`verbs` admits.
        scoped_verbs: Verbs that also carry mutating sub-verbs, mapped to
            the sub-verbs of each a gate may run.
        preview_flags: Commands (``"verb"`` or ``"verb sub-verb"``) that act
            unless asked for their preview, mapped to the preview flag.
        mutating_flags: Flags that turn an admitted form into a mutation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    family_id: CommandFamilyId
    head: str = Field(min_length=1)
    read_only: Literal[True]
    verbs: frozenset[str] | None = None
    mutating_verbs: frozenset[str] = frozenset()
    scoped_verbs: Mapping[str, frozenset[str]] = Field(default_factory=dict)
    preview_flags: Mapping[str, str] = Field(default_factory=dict)
    mutating_flags: frozenset[str] = frozenset()

    def shape_violation(self, argv: list[str]) -> str | None:
        """Return why *argv* falls outside this family's shape, or ``None``.

        Args:
            argv: The effective command, starting at :attr:`head`.

        Returns:
            A one-line reason naming the offending verb or flag, or
            ``None`` when *argv* is one of the family's admitted forms.
        """
        verb = argv[1] if len(argv) > 1 else ""
        if verb in self.mutating_verbs:
            return f"{self.head} sub-verb {verb!r} writes state"
        if self.verbs is not None and verb not in self.verbs:
            return f"{self.head} sub-verb {verb!r} is not in the read-only allow set"
        command = verb
        scoped = self.scoped_verbs.get(verb)
        if scoped is not None:
            sub_verb = argv[2] if len(argv) > 2 else ""
            if sub_verb not in scoped:
                return f"{self.head} {verb} {sub_verb!r} is not in the read-only allow set"
            command = f"{verb} {sub_verb}"
        mutating = sorted(self.mutating_flags.intersection(argv))
        if mutating:
            return f"{self.head} argv carries mutating flag(s) {mutating}"
        preview = self.preview_flags.get(command)
        if preview is not None and preview not in argv:
            return f"{self.head} {command} acts unless run as {preview}"
        return None


_FAMILIES: Final[tuple[CommandFamily, ...]] = (
    CommandFamily(family_id="pytest", head="pytest", read_only=True),
    CommandFamily(family_id="mypy", head="mypy", read_only=True),
    CommandFamily(
        family_id="ruff",
        head="ruff",
        read_only=True,
        preview_flags={"format": "--check"},
        mutating_flags=frozenset({"--fix", "--unsafe-fixes"}),
    ),
    # ``pre-commit run`` (also its bare form) is the gauntlet a gate reads
    # the exit status of; the verbs that rewrite hooks or config are not.
    CommandFamily(
        family_id="pre-commit",
        head="pre-commit",
        read_only=True,
        mutating_verbs=frozenset(
            {
                "autoupdate",
                "clean",
                "gc",
                "init-templatedir",
                "install",
                "install-hooks",
                "migrate-config",
                "uninstall",
            }
        ),
    ),
    CommandFamily(family_id="git", head="git", read_only=True, verbs=GIT_ALLOWED_SUBVERBS),
    CommandFamily(
        family_id="eawf",
        head="eawf",
        read_only=True,
        verbs=frozenset(
            {"--version", "version", "status", "validate", "doctor", "why", "bench", "release"}
        ),
        scoped_verbs={
            "bench": frozenset({"turn-cost"}),
            "release": frozenset({"show", "tag"}),
        },
        preview_flags={"release tag": "--dry-run"},
        mutating_flags=frozenset({"--fix", "--yes", "--push"}),
    ),
    # The test recipes the repository's justfile declares, each of which
    # runs the suite and writes nothing back.
    CommandFamily(
        family_id="just",
        head="just",
        read_only=True,
        verbs=frozenset({"test", "test-all", "test-tui"}),
    ),
)

#: Every command family a gate may name, keyed by ``family_id``.
COMMAND_FAMILIES: Final[Mapping[str, CommandFamily]] = {
    family.family_id: family for family in _FAMILIES
}

#: Registered families keyed by the executable head they run.
FAMILIES_BY_HEAD: Final[Mapping[str, CommandFamily]] = {family.head: family for family in _FAMILIES}

#: Every head a gate argv may carry at any wrapper depth.
REGISTERED_GATE_HEADS: Final[frozenset[str]] = GATE_WRAPPER_HEADS | frozenset(FAMILIES_BY_HEAD)


__all__ = [
    "COMMAND_FAMILIES",
    "FAMILIES_BY_HEAD",
    "GATE_WRAPPER_HEADS",
    "GIT_ALLOWED_SUBVERBS",
    "REGISTERED_GATE_HEADS",
    "CommandFamily",
    "CommandFamilyId",
]
