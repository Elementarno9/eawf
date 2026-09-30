"""The fail-closed per-call guard over the four data-loss patterns.

A host harness runs its own tools, so a call that would destroy state Eawf owns has
to be refused before it runs, in the harness's pre-tool hook. Per-call guards cost
measured latency on every call, so exactly four patterns are judged here, each an
irreversible mutation of owned state:

- :attr:`DataLossPattern.FOREIGN_WORKTREE_MUTATION` -- a managed worktree removed,
  moved or reset by any route other than Eawf's own: ``git worktree remove|move``,
  ``rm``/``mv`` of the worktree directory or a directory holding it, or a
  work-discarding git command aimed into the worktree from outside it.
- :attr:`DataLossPattern.OUT_OF_ROOT_WORKTREE` -- a worktree created outside the
  managed worktree roots: ``git worktree add`` elsewhere, or a host-native worktree
  (the host's worktree tool, or a subagent spawned with worktree isolation).
- :attr:`DataLossPattern.DRIFTED_COMMIT` -- a ``git commit`` or ``git push`` that
  names no directory while the session's working directory sits in another work tree
  than the one the session was started in, so the commit lands where nobody meant.
- :attr:`DataLossPattern.CANONICAL_STORE_EDIT` -- a direct write to a canonical state
  or store file, which only the daemon may change.

Everything else is advisory or post-hoc. A rule Eawf enforces at its own mutator is
not judged again here: an ``eawf`` command line is never judged, because the verb it
runs refuses for itself.

The judge is pure and needs no daemon, so an unreachable daemon cannot open it. It
fails closed: a call to a tool that can mutate files whose input cannot be parsed or
judged is denied. A tool that cannot mutate files (a read, a search) is never judged,
so no guard fault can stop one.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class DataLossPattern(StrEnum):
    """The closed set of patterns a per-call guard denies."""

    FOREIGN_WORKTREE_MUTATION = "foreign_worktree_mutation"
    OUT_OF_ROOT_WORKTREE = "out_of_root_worktree"
    DRIFTED_COMMIT = "drifted_commit"
    CANONICAL_STORE_EDIT = "canonical_store_edit"


#: The rule a denial names when the call could not be judged at all.
UNJUDGED_RULE: Final = "unjudged"


class DataLossDenial(BaseModel):
    """Why one host tool call is refused.

    Attributes:
        pattern: The data-loss pattern the call matches; ``None`` when the guard
            could not judge a call that might mutate owned state, which it refuses.
        reason: What the call would have done and how to do it instead, in words
            the host shows its agent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    pattern: DataLossPattern | None
    reason: str

    @property
    def rule(self) -> str:
        """Return the rule that refused: the pattern's name, or :data:`UNJUDGED_RULE`."""
        return UNJUDGED_RULE if self.pattern is None else self.pattern.value


#: Managed worktree roots, relative to the main checkout.
MANAGED_WORKTREE_ROOTS: Final[tuple[tuple[str, ...], ...]] = (
    (".ea", "worktrees"),
    (".ea", "local", "epoch2", "workspaces"),
)

#: The revision of the data-loss policy a recorded denial belongs to.
DATA_LOSS_POLICY_REVISION: Final = 1

#: What each rule held when it refused: the roots or files it protects.
RULE_VALUES: Final[dict[str, str]] = {
    DataLossPattern.FOREIGN_WORKTREE_MUTATION.value: ".ea/worktrees .ea/local/epoch2/workspaces",
    DataLossPattern.OUT_OF_ROOT_WORKTREE.value: ".ea/worktrees .ea/local/epoch2/workspaces",
    DataLossPattern.DRIFTED_COMMIT.value: "the work tree the session started in",
    DataLossPattern.CANONICAL_STORE_EDIT.value: (
        ".ea/state.json .ea/store .ea/ledger .ea/telemetry.db .ea/local/epoch2"
    ),
    UNJUDGED_RULE: "fail closed",
}

#: The host tools that run a shell command line.
SHELL_TOOLS: Final = frozenset({"Bash", "shell", "exec_command", "local_shell"})

#: The host tools that write one file, and the input key naming it.
FILE_WRITE_TOOLS: Final[dict[str, str]] = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

#: The host tool that applies a patch, naming each file it touches in the patch text.
PATCH_TOOL: Final = "apply_patch"

#: The host tool that creates a host-native worktree.
HOST_WORKTREE_TOOL: Final = "EnterWorktree"

#: The host tools that spawn a subagent, which may ask for a host-native worktree.
SUBAGENT_TOOLS: Final = frozenset({"Agent", "Task"})

#: Every tool the guard judges; any other tool cannot mutate owned state.
JUDGED_TOOLS: Final = frozenset(
    {*SHELL_TOOLS, *FILE_WRITE_TOOLS, PATCH_TOOL, HOST_WORKTREE_TOOL, *SUBAGENT_TOOLS}
)

_GLOB_CHARS: Final = frozenset("*?[")
_REDIRECT_OUT: Final = frozenset({">", ">>", ">|", "&>", "&>>", "<>"})
_REDIRECT_IN: Final = frozenset({"<", "<<", "<<<", "<<-", "<&", ">&"})
_PUNCTUATION: Final = "();<>|&\n"
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)

#: Command prefixes that run the rest of the line as a command.
_WRAPPERS: Final = frozenset(
    {"sudo", "command", "builtin", "exec", "nohup", "time", "nice", "rtk", "stdbuf", "caffeinate"}
)
_SHELLS: Final = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_REMOVERS: Final = frozenset({"rm", "rmdir", "unlink", "shred", "trash", "srm"})
_COPIERS: Final = frozenset({"cp", "install", "rsync", "ln", "ditto"})
_IN_PLACE_EDITORS: Final = frozenset({"sed", "gsed", "perl"})
_INTERPRETERS: Final = frozenset({"python", "python3", "node", "ruby", "perl", "uv"})
_CANONICAL_FRAGMENTS: Final = (".ea/state.json", ".ea/store", ".ea/ledger", "telemetry.db")
_WRITE_WORDS = re.compile(
    r"write|dump|unlink|rename|replace|remove|rmtree|truncate|['\"][wa]b?\+?['\"]"
)
_WORKTREE_ADD_VALUED: Final = frozenset({"-b", "-B", "--reason"})
_GIT_VALUED: Final = frozenset({"-c", "--namespace", "--exec-path", "--config-env"})


@dataclass
class _Shell:
    """Where one command line is running, as the line itself moves it."""

    cwd: Path
    session_cwd: Path
    anchor: Path
    moved: bool = False


def judge_tool_call(
    tool_name: str, tool_input: object, *, cwd: Path, anchor: Path
) -> DataLossDenial | None:
    """Return the denial one host tool call earns, or ``None`` when it may run.

    Args:
        tool_name: The host's name for the tool.
        tool_input: The tool's input as the host reported it.
        cwd: The session's current working directory.
        anchor: The directory the session was started in.

    Returns:
        The denial, or ``None``. A judged tool whose input cannot be read or
        parsed is denied with no pattern.
    """
    if tool_name not in JUDGED_TOOLS:
        return None
    try:
        return _judge(tool_name, tool_input, cwd=cwd, anchor=anchor)
    except Exception as exc:
        logger.warning(f"judge_tool_call tool={tool_name} status=unjudged error={exc!r}")
        return DataLossDenial(
            pattern=None,
            reason=f"the data-loss guard could not judge this {tool_name} call ({exc}); "
            "rewrite it in a plainer form",
        )


def _judge(tool_name: str, tool_input: object, *, cwd: Path, anchor: Path) -> DataLossDenial | None:
    """Judge a call to a tool in :data:`JUDGED_TOOLS`; raise when its input is unreadable."""
    if tool_name == HOST_WORKTREE_TOOL:
        return _deny(
            DataLossPattern.OUT_OF_ROOT_WORKTREE,
            "host-native worktrees are refused; Eawf owns worktrees under .ea/worktrees",
        )
    if not isinstance(tool_input, dict):
        raise ValueError("the tool input is not a mapping")
    if tool_name in SUBAGENT_TOOLS:
        if tool_input.get("isolation") == "worktree":
            return _deny(
                DataLossPattern.OUT_OF_ROOT_WORKTREE,
                "a subagent with worktree isolation creates a host-native worktree outside "
                "Eawf's managed root; create the worktree under .ea/worktrees instead "
                "(git worktree add .ea/worktrees/<name> -b <branch>) and give the subagent "
                "its absolute path, without isolation",
            )
        return None
    if tool_name in FILE_WRITE_TOOLS:
        raw = tool_input.get(FILE_WRITE_TOOLS[tool_name])
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"{FILE_WRITE_TOOLS[tool_name]} is missing")
        return _canonical_denial(_resolve(cwd, raw), verb=f"{tool_name} of")
    if tool_name == PATCH_TOOL:
        return _judge_patch(_patch_text(tool_input), cwd=cwd)
    command = _command_text(tool_input)
    return _judge_line(command, _Shell(cwd=cwd, session_cwd=cwd, anchor=anchor), depth=0)


def _deny(pattern: DataLossPattern, reason: str) -> DataLossDenial:
    return DataLossDenial(pattern=pattern, reason=reason)


def _command_text(tool_input: dict[str, object]) -> str:
    """Return the command line a shell tool runs."""
    command = tool_input.get("command", tool_input.get("cmd"))
    if isinstance(command, list) and all(isinstance(word, str) for word in command):
        return shlex.join(command)
    if isinstance(command, str):
        return command
    raise ValueError("the shell command is missing")


def _patch_text(tool_input: dict[str, object]) -> str:
    """Return the patch text an apply-patch call carries."""
    for key in ("input", "patch", "command"):
        value = tool_input.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list) and all(isinstance(word, str) for word in value):
            return "\n".join(value)
    raise ValueError("the patch text is missing")


def _judge_patch(patch: str, *, cwd: Path) -> DataLossDenial | None:
    for match in _PATCH_FILE.finditer(patch):
        target = (match.group(1) or match.group(2)).strip()
        denial = _canonical_denial(_resolve(cwd, target), verb="a patch to")
        if denial is not None:
            return denial
    return None


# --- paths -----------------------------------------------------------------


def _resolve(base: Path, raw: str) -> Path:
    """Resolve *raw* against *base* the way a shell would, without touching the disk."""
    expanded = os.path.expanduser(raw)
    path = Path(expanded)
    return Path(os.path.normpath(path if path.is_absolute() else base / path))


def _work_tree(path: Path) -> Path | None:
    """Return the top of the git work tree holding *path*, if any."""
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _main_checkout(tree: Path) -> Path:
    """Return the main checkout of the repository *tree* is a work tree of."""
    marker = tree / ".git"
    if not marker.is_file():
        return tree
    text = marker.read_text(encoding="utf-8").strip()
    if not text.startswith("gitdir:"):
        return tree
    gitdir = _resolve(tree, text.removeprefix("gitdir:").strip())
    if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
        return gitdir.parent.parent.parent
    return tree


def _managed_roots(*paths: Path) -> set[Path]:
    """Return the managed worktree roots of every repository holding one of *paths*."""
    roots: set[Path] = set()
    for path in paths:
        tree = _work_tree(path)
        if tree is None:
            continue
        main = _main_checkout(tree)
        roots.update(main.joinpath(*parts) for parts in MANAGED_WORKTREE_ROOTS)
    return roots


def _managed_worktree(path: Path, roots: set[Path]) -> Path | None:
    """Return the managed worktree *path* sits in, if it sits in one."""
    for root in roots:
        if path != root and path.is_relative_to(root):
            return root / path.relative_to(root).parts[0]
    return None


def _holds_managed_worktree(path: Path, roots: set[Path]) -> bool:
    """Return whether removing or moving *path* takes a whole managed worktree with it."""
    if any(root.is_relative_to(path) for root in roots):
        return True
    worktree = _managed_worktree(path, roots)
    return worktree is not None and worktree == path


def _canonical(path: Path) -> bool:
    """Return whether *path* is, or holds, a canonical state or store file."""
    parts = path.parts
    if ".ea" not in parts:
        return False
    rest = parts[len(parts) - 1 - parts[::-1].index(".ea") + 1 :]
    if not rest:
        return True
    head = rest[0]
    if head in {"state.json", "telemetry.db", "store", "ledger"}:
        return True
    return rest[:2] == ("local", "epoch2") and (len(rest) < 3 or rest[2] != "workspaces")


def _glob_base(path: Path) -> Path:
    """Return the fixed directory a glob pattern expands under; *path* when it is no glob."""
    parts = path.parts
    for index, part in enumerate(parts):
        if _GLOB_CHARS & set(part):
            return Path(*parts[:index])
    return path


def _canonical_denial(path: Path, *, verb: str) -> DataLossDenial | None:
    if not _canonical(_glob_base(path)):
        return None
    return _deny(
        DataLossPattern.CANONICAL_STORE_EDIT,
        f"{verb} a canonical state or store file is refused; "
        "change project state through the eawf verbs, which go through the daemon",
    )


def _removal_denial(path: Path, shell: _Shell, *, verb: str) -> DataLossDenial | None:
    """Judge removing or moving *path*: a canonical file, or a whole managed worktree."""
    denial = _canonical_denial(path, verb=verb)
    if denial is not None:
        return denial
    base = _glob_base(path)
    if not _holds_managed_worktree(base, _managed_roots(shell.cwd, shell.anchor, base)):
        return None
    return _deny(
        DataLossPattern.FOREIGN_WORKTREE_MUTATION,
        f"{verb} a managed worktree outside Eawf is refused; "
        "remove it with eawf worktree cleanup once its work has landed",
    )


# --- command lines -----------------------------------------------------------


def _strip_heredocs(command: str) -> str:
    """Drop heredoc bodies, which are data rather than commands."""
    kept: list[str] = []
    pending: list[str] = []
    for line in command.split("\n"):
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        kept.append(line)
        pending.extend(match.group(2) for match in _HEREDOC.finditer(line))
    return "\n".join(kept)


def _segments(command: str) -> Iterator[tuple[list[str], list[tuple[str, str]]]]:
    """Yield each simple command's words and its redirections.

    Raises:
        ValueError: The line does not tokenize (an unclosed quote, say).
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    words: list[str] = []
    redirects: list[tuple[str, str]] = []
    pending: str | None = None
    for token in lexer:
        if pending is not None:
            redirects.append((pending, token))
            pending = None
            continue
        if token and all(char in _PUNCTUATION for char in token):
            if token in _REDIRECT_OUT or token in _REDIRECT_IN:
                pending = token
                continue
            if words or redirects:
                yield words, redirects
            words, redirects = [], []
            continue
        words.append(token)
    if pending is not None:
        raise ValueError(f"redirection {pending!r} names no target")
    if words or redirects:
        yield words, redirects


def _judge_line(command: str, shell: _Shell, *, depth: int) -> DataLossDenial | None:
    """Judge every simple command of a command line, in order."""
    if depth > 8:
        raise ValueError("the command nests too deeply to judge")
    for match in _SUBSTITUTION.finditer(command):
        inner = match.group(1) if match.group(1) is not None else match.group(2)
        denial = _judge_line(inner, shell, depth=depth + 1)
        if denial is not None:
            return denial
    for words, redirects in _segments(_strip_heredocs(command)):
        for operator, target in redirects:
            if operator in _REDIRECT_OUT and not target.isdigit() and target != "-":
                denial = _canonical_denial(_resolve(shell.cwd, target), verb="a redirect to")
                if denial is not None:
                    return denial
        denial = _judge_words(words, shell, depth=depth)
        if denial is not None:
            return denial
    return None


def _unwrap(words: list[str]) -> list[str]:
    """Drop assignments and command prefixes that run the rest of the line."""
    index = 0
    while index < len(words):
        word = words[index]
        name = os.path.basename(word)
        if _ASSIGNMENT.match(word):
            index += 1
        elif name in _WRAPPERS:
            index += 1
            while index < len(words) and words[index].startswith("-"):
                index += 1
        elif name == "env":
            index += 1
            while index < len(words) and (
                words[index].startswith("-") or _ASSIGNMENT.match(words[index])
            ):
                index += 1
        elif name == "timeout":
            index += 1
            while index < len(words) and words[index].startswith("-"):
                index += 1
            index += 1
        else:
            break
    return words[index:]


def _judge_words(words: list[str], shell: _Shell, *, depth: int) -> DataLossDenial | None:
    """Judge one simple command."""
    words = _unwrap(words)
    if not words:
        return None
    name = os.path.basename(words[0])
    args = words[1:]
    if name in {"cd", "pushd"}:
        operands = [arg for arg in args if not arg.startswith("-") or arg == "-"]
        target = operands[0] if operands and operands[0] != "-" else str(Path.home())
        shell.cwd = _resolve(shell.cwd, target)
        shell.moved = True
        return None
    if name in _SHELLS:
        script = _shell_script(args)
        return None if script is None else _judge_line(script, shell, depth=depth + 1)
    if name == "eval":
        return _judge_line(" ".join(args), shell, depth=depth + 1)
    if name == "eawf":
        return None
    if name == "git":
        return _judge_git(args, shell)
    return _judge_file_command(name, args, shell)


def _shell_script(args: list[str]) -> str | None:
    """Return the script a ``sh -c`` style call runs, if it runs one."""
    for index, arg in enumerate(args):
        if arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]:
            return args[index + 1] if index + 1 < len(args) else None
        if not arg.startswith("-"):
            return None
    return None


def _operands(args: list[str]) -> list[str]:
    """Return a command's non-option arguments; everything after ``--`` is one."""
    if "--" in args:
        split = args.index("--")
        return [arg for arg in args[:split] if not arg.startswith("-")] + args[split + 1 :]
    return [arg for arg in args if not arg.startswith("-")]


def _judge_file_command(name: str, args: list[str], shell: _Shell) -> DataLossDenial | None:
    """Judge a command that removes, moves or writes files."""
    removed: list[str] = []
    if name in _REMOVERS or name == "mv":
        removed = _operands(args)
    elif name == "find" and any(arg in {"-delete", "-exec", "-execdir", "-ok"} for arg in args):
        removed = _find_starts(args)
    if name in _INTERPRETERS or name.startswith("python3."):
        denial = _judge_inline_code(args)
        if denial is not None:
            return denial
    verb = "moving" if name == "mv" else "removing"
    for operand in removed:
        denial = _removal_denial(_resolve(shell.cwd, operand), shell, verb=verb)
        if denial is not None:
            return denial
    for target in _written_files(name, args):
        denial = _canonical_denial(_resolve(shell.cwd, target), verb=f"{name} writing")
        if denial is not None:
            return denial
    return None


def _written_files(name: str, args: list[str]) -> list[str]:
    """Return the files a writing command names as its targets."""
    if name in _COPIERS:
        return [args[args.index("-t") + 1]] if "-t" in args[:-1] else _operands(args)[-1:]
    if name in {"tee", "truncate", "touch"}:
        return _operands(args)
    if name == "dd":
        return [arg.removeprefix("of=") for arg in args if arg.startswith("of=")]
    if name in _IN_PLACE_EDITORS and any(_in_place_flag(arg) for arg in args):
        return _operands(args)
    if name in {"sqlite3", "sqlite"} and "-readonly" not in args:
        return _operands(args)[:1]
    return []


def _in_place_flag(arg: str) -> bool:
    """Return whether *arg* asks sed or perl to edit its files in place."""
    if arg.startswith("--"):
        return arg == "--in-place" or arg.startswith("--in-place=")
    return arg.startswith("-") and "i" in arg[1:]


def _find_starts(args: list[str]) -> list[str]:
    starts: list[str] = []
    for arg in args:
        if arg.startswith(("-", "(", "!")):
            break
        starts.append(arg)
    return starts or ["."]


def _judge_inline_code(args: list[str]) -> DataLossDenial | None:
    """Judge inline interpreter code that names a canonical file and writes."""
    for arg in args:
        if any(fragment in arg for fragment in _CANONICAL_FRAGMENTS) and _WRITE_WORDS.search(arg):
            return _deny(
                DataLossPattern.CANONICAL_STORE_EDIT,
                "inline code writing a canonical state or store file is refused; "
                "change project state through the eawf verbs, which go through the daemon",
            )
    return None


# --- git -------------------------------------------------------------------


def _git_globals(args: list[str], shell: _Shell) -> tuple[Path, bool, int]:
    """Return where git runs, whether a directory was named, and the subcommand's index."""
    where = shell.cwd
    explicit = shell.moved
    index = 0
    while index < len(args) and args[index].startswith("-"):
        option, value = args[index], args[index + 1] if index + 1 < len(args) else None
        if "=" in option and option.startswith(("--git-dir=", "--work-tree=")):
            option, value = option.split("=", 1)
            index -= 1
        if option in {"-C", "--git-dir", "--work-tree"} and value is not None:
            explicit = True
            if option != "--git-dir":
                where = _resolve(where, value)
            index += 2
            continue
        index += 2 if option in _GIT_VALUED else 1
    return where, explicit, index


def _judge_git(args: list[str], shell: _Shell) -> DataLossDenial | None:
    """Judge one git invocation."""
    where, explicit, index = _git_globals(args, shell)
    if index >= len(args):
        return None
    subcommand, rest = args[index], args[index + 1 :]
    if subcommand == "worktree":
        return _judge_git_worktree(rest, where, shell)
    if subcommand in {"commit", "push"}:
        return _judge_drift(subcommand, where=where, explicit=explicit, shell=shell)
    if subcommand in {"checkout", "restore"}:
        denial = _judge_restore_canonical(subcommand, rest, where)
        if denial is not None:
            return denial
    if _discards_work(subcommand, rest):
        return _judge_foreign_reset(subcommand, where, shell)
    return None


def _worktree_operands(args: list[str]) -> list[str]:
    """Return a ``git worktree`` action's operands, skipping the options that take a value."""
    operands: list[str] = []
    skip = False
    for arg in args:
        if skip or arg in _WORKTREE_ADD_VALUED:
            skip = not skip
            continue
        if not arg.startswith("-"):
            operands.append(arg)
    return operands


def _judge_git_worktree(args: list[str], where: Path, shell: _Shell) -> DataLossDenial | None:
    if not args:
        return None
    action = args[0]
    operands = _worktree_operands(args[1:])
    if not operands:
        return None
    target = _resolve(where, operands[0])
    roots = _managed_roots(where, shell.anchor)
    if action == "add":
        if any(target != root and target.is_relative_to(root) for root in roots):
            return None
        return _deny(
            DataLossPattern.OUT_OF_ROOT_WORKTREE,
            "creating a worktree outside the managed roots is refused; "
            "create it under .ea/worktrees",
        )
    if action in {"remove", "move"}:
        if _managed_worktree(target, roots) is None and not _holds_managed_worktree(target, roots):
            return None
        verb = "removing" if action == "remove" else "moving"
        return _deny(
            DataLossPattern.FOREIGN_WORKTREE_MUTATION,
            f"{verb} a managed worktree with git is refused; "
            "remove it with eawf worktree cleanup once its work has landed",
        )
    return None


def _judge_drift(
    subcommand: str, *, where: Path, explicit: bool, shell: _Shell
) -> DataLossDenial | None:
    if explicit:
        return None
    here = _work_tree(where)
    home = _work_tree(shell.anchor)
    if here is None or home is None or here == home:
        return None
    return _deny(
        DataLossPattern.DRIFTED_COMMIT,
        f"git {subcommand} from a working directory that drifted out of the session's "
        f"work tree is refused; name the tree explicitly with git -C <path> {subcommand}",
    )


def _judge_restore_canonical(
    subcommand: str, args: list[str], where: Path
) -> DataLossDenial | None:
    """Judge a checkout or restore that rewrites a canonical file from git."""
    if subcommand == "checkout" and "--" not in args:
        return None
    paths = args[args.index("--") + 1 :] if "--" in args else _operands(args)
    for raw in paths:
        denial = _canonical_denial(_resolve(where, raw), verb=f"git {subcommand} of")
        if denial is not None:
            return denial
    return None


def _discards_work(subcommand: str, args: list[str]) -> bool:
    """Return whether a git command throws uncommitted work away."""
    if subcommand == "reset":
        return "--hard" in args
    if subcommand == "clean":
        return any(
            arg == "--force" or (arg.startswith("-") and not arg.startswith("--") and "f" in arg)
            for arg in args
        )
    if subcommand == "checkout":
        return "--" in args or "." in args or "-f" in args or "--force" in args
    if subcommand == "restore":
        staged_only = any(arg in {"--staged", "-S"} for arg in args) and not any(
            arg in {"--worktree", "-W"} for arg in args
        )
        return not staged_only
    if subcommand == "stash":
        return bool(args) and args[0] in {"drop", "clear"}
    if subcommand == "switch":
        return any(arg in {"-f", "--force", "--discard-changes"} for arg in args)
    return False


def _judge_foreign_reset(subcommand: str, where: Path, shell: _Shell) -> DataLossDenial | None:
    worktree = _managed_worktree(where, _managed_roots(where, shell.anchor))
    if worktree is None or shell.session_cwd.is_relative_to(worktree):
        return None
    return _deny(
        DataLossPattern.FOREIGN_WORKTREE_MUTATION,
        f"git {subcommand} discarding work in a managed worktree from outside it is refused; "
        "run it from inside that worktree",
    )


__all__ = [
    "DATA_LOSS_POLICY_REVISION",
    "JUDGED_TOOLS",
    "MANAGED_WORKTREE_ROOTS",
    "RULE_VALUES",
    "UNJUDGED_RULE",
    "DataLossDenial",
    "DataLossPattern",
    "judge_tool_call",
]
