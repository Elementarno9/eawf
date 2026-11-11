"""The data-loss guard is on by session context alone, as one decision table.

The guard is on for a call when the session is in a managed tree: the payload
``cwd`` or the host's project directory lies in one. Then every destructive
form the judge knows is denied. Otherwise it is off, whatever the command
names. The same rows run through the real rendered wrapper in
``tests/integration/runtime/test_data_loss_reach_wrapper.py``. The mutations
named in the payloads are judged and never executed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.runtime.hooks.context import resolve_hook_context
from eawf.runtime.hooks.data_loss_guard import guard_pre_tool_use
from eawf.runtime.hooks.event import HookEvent, HookEventType
from tests._linked_worktree import git

SESSION: Final = "0b6c1f2e-6a43-4d55-9c1e-2f7a9d1c3b4e"

#: The ON contexts outside git: a directory holding ``.ea/state.json``, one
#: under a ``.git`` git does not take, and one whose own ``.git`` file is broken.
_NOGIT_ON: Final = ("managed-nogit", "nogit-under-stray-git", "nogit-broken-gitfile")

#: The contexts in which the session is in a managed tree.
ON: Final = (
    "managed-root",
    "managed-subfolder",
    "managed-linked",
    "managed-symlink",
    "drifted",
    *_NOGIT_ON,
    # A repository made with --separate-git-dir: a worktree under its
    # .ea/worktrees, one beside it, and one whose main checkout no
    # registration names, found by walking up.
    "separate-worktree",
    "separate-linked",
    "separate-unnamed",
    # An unmanaged clone and a submodule inside the managed checkout.
    "nested-repo",
    "submodule",
    # Payloads whose cwd the wrapper cannot read for certain; its own
    # directory lies outside every managed tree.
    "cwd-late",
    "cwd-nested-key",
    "cwd-quote",
    "cwd-accent-escaped",
    "cwd-space-accent",
)

#: The contexts in which it is not, the wrapper standing down by itself.
_STANDING_DOWN: Final = ("unmanaged-git", "no-git", "unmanaged-nested", "plain-under-stray-git")

#: The contexts in which it is not, read unsure: eawf decides and prints nothing.
HANDED_OVER: Final = ("cwd-nested-key-unmanaged",)

#: The contexts in which it is not.
OFF: Final = (*_STANDING_DOWN, *HANDED_OVER)

#: The ON contexts whose cwd or project directory lies in a git repository
#: under ``<P>``, from which the judge finds that repository's worktree roots.
_GIT_ON: Final = tuple(name for name in ON if name not in _NOGIT_ON)

#: The contexts whose payload the wrapper reads in a directory of its own.
_ELSEWHERE: Final = frozenset(name for name in (*ON, *OFF) if name.startswith("cwd-"))

#: The contexts whose payload carries its text as raw UTF-8, not as escapes.
_RAW_UTF8: Final = frozenset({"cwd-space-accent"})


@dataclass(frozen=True)
class Layout:
    """The trees every row runs against.

    Attributes:
        home: The home directory, which holds ``parent``.
        parent: The directory holding the managed repository, ``<P>``.
        managed: The managed git repository, ``<M>``.
        worktree: A linked worktree under its ``.ea/worktrees``, ``<W>``.
        linked: A linked worktree of it outside the repository.
        link: A symlink to it.
        here: An unmanaged git repository beside it.
        plain: A directory outside git.
        ng: A project without git: a directory holding ``.ea/state.json``.
        more: The cwd of every context the layout adds beyond these.
    """

    home: Path
    parent: Path
    managed: Path
    worktree: Path
    linked: Path
    link: Path
    here: Path
    plain: Path
    ng: Path
    more: dict[str, Path]

    def context(self, name: str) -> tuple[Path, Path | None]:
        """Return the cwd and the project directory of a context."""
        fixed: dict[str, tuple[Path, Path | None]] = {
            "managed-root": (self.managed, None),
            "managed-subfolder": (self.managed / "src", None),
            "managed-linked": (self.linked, None),
            "managed-nogit": (self.ng / "sub", None),
            "managed-symlink": (self.link, None),
            "drifted": (self.here, self.managed),
            "unmanaged-git": (self.here, None),
            "no-git": (self.plain, None),
        }
        return fixed[name] if name in fixed else (self.more[name], None)

    def process_dir(self, name: str) -> Path:
        """Return the directory the wrapper runs in for a context."""
        return self.plain if name in _ELSEWHERE else self.context(name)[0]

    def payload(self, name: str, tool: str, tool_input: dict[str, Any]) -> str:
        """Return the payload a host sends for a call in a context, as JSON text."""
        cwd, _ = self.context(name)
        payload: dict[str, Any] = {"session_id": SESSION, "hook_event_name": "PreToolUse"}
        if name == "cwd-late":
            payload["transcript"] = "x" * 9000
        elif name == "cwd-nested-key":
            payload["meta"] = {"cwd": str(self.here)}
        elif name == "cwd-nested-key-unmanaged":
            payload["meta"] = {"cwd": str(self.managed)}
        payload["cwd"] = str(cwd)
        payload["tool_name"] = tool
        payload["tool_input"] = self.fill(tool_input)
        return json.dumps(payload, ensure_ascii=name not in _RAW_UTF8)

    def env(self) -> dict[str, str]:
        """Return the host environment every row runs in."""
        return {"HOME": str(self.home), "STATE": str(self.managed / ".ea" / "state.json")}

    def fill(self, value: Any) -> Any:
        """Return *value* with every placeholder replaced by its path."""
        if isinstance(value, dict):
            return {key: self.fill(item) for key, item in value.items()}
        if not isinstance(value, str):
            return value
        names = {"<M>": self.managed, "<P>": self.parent, "<W>": self.worktree}
        for placeholder, path in names.items():
            value = value.replace(placeholder, str(path))
        return value


def build_layout(root: Path) -> Layout:
    """Lay the trees out under *root*."""
    root = root.resolve()
    home = root / "home"
    parent = home / "src"
    managed = parent / "managed"
    (managed / "src").mkdir(parents=True)
    git(managed, "init", "-q", "-b", "main")
    git(managed, "commit", "-q", "--allow-empty", "-m", "root")
    (managed / ".ea").mkdir()
    (managed / ".ea" / "state.json").write_text("{}\n", encoding="utf-8")
    worktree = managed / ".ea" / "worktrees" / "w1"
    git(managed, "worktree", "add", "-q", str(worktree), "-b", "w1")
    linked = parent / "linked"
    git(managed, "worktree", "add", "-q", str(linked), "-b", "side")
    link = root / "link"
    link.symlink_to(managed)
    here = parent / "here"
    for directory in ("build", "src", "node_modules/pkg"):
        (here / directory).mkdir(parents=True)
    git(here, "init", "-q", "-b", "main")
    for name in ("README.md", "a.txt", "src/app.py"):
        (here / name).write_text("x\n", encoding="utf-8")
    plain = root / "plain"
    plain.mkdir()
    ng = root / "ng"
    (ng / "sub").mkdir(parents=True)
    (ng / ".ea").mkdir()
    (ng / ".ea" / "state.json").write_text("{}\n", encoding="utf-8")
    more = {
        **_lay_strays(root),
        **_lay_separate(root, parent),
        **_lay_nested(root, managed, here),
        "cwd-late": managed / "src",
        "cwd-nested-key": managed / "src",
        "cwd-nested-key-unmanaged": here,
    }
    for name in ('q"uote', "é", "a b ü"):
        (managed / name).mkdir()
    more["cwd-quote"] = managed / 'q"uote'
    more["cwd-accent-escaped"] = managed / "é"
    more["cwd-space-accent"] = managed / "a b ü"
    return Layout(home, parent, managed, worktree, linked, link, here, plain, ng, more)


def _state_holder(directory: Path) -> None:
    (directory / ".ea").mkdir(parents=True)
    (directory / ".ea" / "state.json").write_text("{}\n", encoding="utf-8")


def _lay_strays(root: Path) -> dict[str, Path]:
    """Lay out trees outside git beside a ``.git`` git does not take."""
    stray = root / "stray"
    (stray / ".git").mkdir(parents=True)
    _state_holder(stray / "ng")
    (stray / "ng" / "sub").mkdir()
    (stray / "plain").mkdir()
    broken = root / "broken"
    (broken / "sub").mkdir(parents=True)
    _state_holder(broken)
    (broken / ".git").write_text(f"gitdir: {root / 'nowhere'}\n", encoding="utf-8")
    return {
        "nogit-under-stray-git": stray / "ng" / "sub",
        "plain-under-stray-git": stray / "plain",
        "nogit-broken-gitfile": broken / "sub",
    }


def _lay_separate(root: Path, parent: Path) -> dict[str, Path]:
    """Lay out managed repositories whose git directories sit apart from them."""
    separate = parent / "separate"
    separate.mkdir()
    (root / "stores").mkdir()
    git(separate, "init", "-q", "-b", "main", f"--separate-git-dir={root / 'stores' / 'a.git'}")
    git(separate, "commit", "-q", "--allow-empty", "-m", "root")
    _state_holder(separate)
    worktree = separate / ".ea" / "worktrees" / "w"
    git(separate, "worktree", "add", "-q", str(worktree), "-b", "w")
    beside = parent / "separate-linked"
    git(separate, "worktree", "add", "-q", str(beside), "-b", "beside")
    # No registered tree names this one's main checkout: its worktree's own
    # registration is gone, so only the walk up finds the checkout.
    unnamed = parent / "separate-unnamed"
    unnamed.mkdir()
    store = root / "stores" / "b.git"
    git(unnamed, "init", "-q", "-b", "main", f"--separate-git-dir={store}")
    git(unnamed, "commit", "-q", "--allow-empty", "-m", "root")
    _state_holder(unnamed)
    inner = unnamed / ".ea" / "worktrees" / "w"
    git(unnamed, "worktree", "add", "-q", str(inner), "-b", "w")
    (store / "worktrees" / "w" / "gitdir").unlink()
    return {"separate-worktree": worktree, "separate-linked": beside, "separate-unnamed": inner}


def _lay_nested(root: Path, managed: Path, here: Path) -> dict[str, Path]:
    """Lay out repositories inside other repositories."""
    (managed / "inner").mkdir()
    git(managed / "inner", "init", "-q", "-b", "main")
    seed = root / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    git(seed, "commit", "-q", "--allow-empty", "-m", "root")
    git(managed, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(seed), "sub")
    (here / "inner").mkdir()
    git(here / "inner", "init", "-q", "-b", "main")
    return {
        "nested-repo": managed / "inner",
        "submodule": managed / "sub",
        "unmanaged-nested": here / "inner",
    }


@dataclass(frozen=True)
class Row:
    """One tool call and the contexts in which the guard denies it.

    Attributes:
        id: The row's name.
        tool: The host tool.
        tool_input: Its input, with placeholders.
        denied_in: The ON contexts in which the judge denies it; it runs in
            these and in every OFF context, where it is allowed.
        ordinary: Whether it is everyday work, which no context denies.
    """

    id: str
    tool: str
    tool_input: dict[str, Any]
    denied_in: tuple[str, ...] = ON
    ordinary: bool = False


def _bash(command: str) -> dict[str, Any]:
    return {"command": command}


#: The previous release's destructive forms, and the shell readings the judge
#: kept from this Milestone because the previous one missed them.
DESTRUCTIVE_ROWS: Final[tuple[Row, ...]] = (
    Row("rm-ea", "Bash", _bash("rm -rf .ea")),
    Row("rm-managed-ea", "Bash", _bash("rm -rf <M>/.ea")),
    Row("rm-managed-worktree", "Bash", _bash("rm -rf <W>")),
    # Outside git the judge finds no worktree root, so a parent's removal is
    # judged only from a session in the git repository.
    Row("rm-parent", "Bash", _bash("rm -rf <P>"), denied_in=_GIT_ON),
    Row("rm-home", "Bash", _bash("rm -rf ~"), denied_in=_GIT_ON),
    Row("find-delete", "Bash", _bash("find <M> -delete")),
    Row("find-exec-rm", "Bash", _bash("find <M> -exec rm -rf {} +")),
    Row("find-delete-ea", "Bash", _bash("find <M>/.ea -name '*.json' -delete")),
    Row("worktree-add-outside", "Bash", _bash("git worktree add ../x")),
    Row("enter-worktree", "EnterWorktree", {"name": "x"}),
    Row("agent-isolation", "Agent", {"prompt": "p", "isolation": "worktree"}),
    Row("task-isolation", "Task", {"prompt": "p", "isolation": "worktree"}),
    Row("write-relative-state", "Write", {"file_path": ".ea/state.json", "content": "{}"}),
    Row("edit-managed-state", "Edit", {"file_path": "<M>/.ea/state.json"}),
    Row("redirect-state", "Bash", _bash(": > .ea/state.json")),
    Row("inline-state", "Bash", _bash("python3 -c \"open('.ea/state.json', 'w')\"")),
    Row("clean-main", "Bash", _bash("git -C <M> clean -fdx")),
    Row("reset-main", "Bash", _bash("git -C <M> reset --hard")),
    Row("drifted-commit", "Bash", _bash("git commit -m x"), denied_in=("drifted",)),
    Row("line-continuation", "Bash", _bash("rm -rf .e\\\na")),
    Row("glob-under-ea", "Bash", _bash("rm -rf .ea/*")),
    Row("line-variable", "Bash", _bash("D=.ea; rm -rf $D")),
    Row("env-chdir", "Bash", _bash("env -C <M> rm -rf .ea")),
    Row("environment-variable", "Bash", _bash(': > "$STATE"')),
)

#: Everyday work; no context denies it, and an OFF context never starts eawf.
ORDINARY_ROWS: Final[tuple[Row, ...]] = tuple(
    Row(f"ordinary-{name}", tool, tool_input, denied_in=(), ordinary=True)
    for name, tool, tool_input in (
        ("ls", "Bash", _bash("ls")),
        ("status", "Bash", _bash("git status")),
        ("pytest", "Bash", _bash("pytest -q")),
        ("npm-test", "Bash", _bash("npm test")),
        ("rm-build", "Bash", _bash("rm -rf build")),
        ("rm-node-modules", "Bash", _bash("rm -rf node_modules")),
        ("mv", "Bash", _bash("mv a.txt b.txt")),
        ("find-pyc", "Bash", _bash("find . -name '*.pyc' -delete")),
        ("echo-home", "Bash", _bash("echo $HOME > out.txt")),
        ("long-echo", "Bash", _bash(f"echo {'x' * 60_000}")),
        ("cat", "Bash", _bash("cat README.md")),
        ("log", "Bash", _bash("git log --oneline -5")),
        ("grep", "Bash", _bash("grep -rn TODO src")),
        ("edit", "Edit", {"file_path": "src/app.py"}),
        ("write", "Write", {"file_path": "notes.md", "content": "x"}),
        ("read", "Read", {"file_path": "README.md"}),
    )
)

#: The ON contexts ordinary rows also run in, to show the guard stays quiet.
_ORDINARY_ON: Final = ("managed-root", "drifted")


def _cases() -> list[Any]:
    cases = []
    for row in DESTRUCTIVE_ROWS:
        for context in (*row.denied_in, *OFF):
            reason = "the session is not in a managed tree" if context in OFF else "denied"
            cases.append(pytest.param(context, row, id=f"{context}-{row.id}-{reason}"))
    for row in ORDINARY_ROWS:
        for context in (*OFF, *_ORDINARY_ON):
            cases.append(pytest.param(context, row, id=f"{context}-{row.id}"))
    return cases


#: Every (context, row) pair, with its pytest id.
CASES: Final = _cases()


def denied(context: str, row: Row) -> bool:
    """Return whether the guard denies *row* in *context*."""
    return context in row.denied_in


class _Down:
    """A daemon client factory whose daemon is down."""

    def __call__(self) -> Any:
        raise ConnectionRefusedError("daemon unreachable")


@pytest.fixture(scope="module")
def layout(tmp_path_factory: pytest.TempPathFactory) -> Layout:
    return build_layout(tmp_path_factory.mktemp("reach"))


def _event(payload: dict[str, Any]) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.PRE_TOOL_USE,
        scope_id="",
        command="",
        args={},
        runtime="claude",
        occurred_at=datetime.now(UTC),
        payloads={HookEventType.PRE_TOOL_USE.value: payload},
    )


@pytest.mark.parametrize(("context", "row"), CASES)
def test_reach_table(
    layout: Layout, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, context: str, row: Row
) -> None:
    cwd, project = layout.context(context)
    for name, value in layout.env().items():
        monkeypatch.setenv(name, value)
    if project is None:
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr("eawf.runtime.hooks.runner._default_daemon_client_factory", _Down())
    monkeypatch.chdir(layout.process_dir(context))
    event = _event(json.loads(layout.payload(context, row.tool, row.tool_input)))
    denial = guard_pre_tool_use(event, context=resolve_hook_context(cwd))
    assert (denial is not None) is denied(context, row), denial
    # A record never creates an .ea where none was.
    assert not (layout.here / ".ea").exists()
    assert not (layout.plain / ".ea").exists()


def test_reach_table_shape() -> None:
    assert set(ON).isdisjoint(OFF)
    assert len(set(ON)) == len(ON)
    assert len(set(OFF)) == len(OFF)
    assert all(set(row.denied_in) <= set(ON) for row in DESTRUCTIVE_ROWS)
    assert len(ORDINARY_ROWS) >= 15
