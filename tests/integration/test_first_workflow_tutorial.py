"""REL-021: the first-workflow tutorial replays end to end on a fresh epoch-2 tree.

``docs/tutorial/first-workflow.md`` teaches the native Task flow as a run of
fenced blocks. Every ``bash`` block is executable, and every block that
follows an ``<!-- eawf:file PATH -->`` marker is a file the reader saves.
The replay walks the page top to bottom in a fresh Git repository: it writes
each marked file, runs each ``eawf`` line through the real Typer app with the
daemon's registered verbs answering in process, runs each ``git`` line for
real, and requires every command to exit 0. A verb the CLI does not carry
exits non-zero, so the page cannot teach one; the verbs its prose names in
inline code are resolved against the command tree as well.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import click
import pytest
import typer
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import require_native_authority
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon import methods
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain as domain_cmd
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import method_context
from tests.integration.runtime.daemon.test_delivery_landed_loop import InProcessClient

pytestmark = pytest.mark.integration

runner = CliRunner()

_TUTORIAL: Final = Path(__file__).resolve().parents[2] / "docs" / "tutorial" / "first-workflow.md"
_FILE_MARKER: Final = re.compile(r"^<!-- eawf:file (?P<path>\S+) -->$")
_REV_PARSE: Final = re.compile(r"\$\(git rev-parse (?P<rev>[^)\s]+)\)")
_INLINE_VERB: Final = re.compile(r"`eawf ([a-z][a-z-]*(?: [a-z][a-z-]*)?)`")


@dataclass(frozen=True)
class Step:
    """One thing the reader does: save a file or run a command.

    Attributes:
        path: The repository-relative file to write, for a file step.
        content: The file's content, for a file step.
        line: The command line as the reader types it, for a command step.
    """

    path: str | None = None
    content: str | None = None
    line: str = ""


def tutorial_steps(markdown: str) -> list[Step]:
    """Return the file and command steps of *markdown* in document order.

    Args:
        markdown: The tutorial source.

    Returns:
        One step per marked file block and per command line of a ``bash``
        block.

    Raises:
        ValueError: A marker is not followed by a fenced block, a fence is
            not closed, or a ``bash`` line runs neither ``eawf`` nor ``git``.
    """
    lines = markdown.splitlines()
    steps: list[Step] = []
    pending_path: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        marker = _FILE_MARKER.match(line)
        if marker:
            pending_path = marker["path"]
        elif line.startswith("```"):
            closing = next(
                (at for at in range(index + 1, len(lines)) if lines[at].startswith("```")), None
            )
            if closing is None:
                raise ValueError(f"the fence opened on line {index + 1} is not closed")
            body = lines[index + 1 : closing]
            if pending_path is not None:
                steps.append(Step(path=pending_path, content="\n".join(body) + "\n"))
                pending_path = None
            elif line == "```bash":
                steps.extend(_command_steps(body))
            index = closing
        elif pending_path is not None and line.strip():
            raise ValueError(f"the file marker for {pending_path} is not followed by a fence")
        index += 1
    return steps


def _command_steps(body: list[str]) -> list[Step]:
    """Return the command steps of one ``bash`` block body."""
    steps: list[Step] = []
    for raw in body:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.split(maxsplit=1)[0] not in {"eawf", "git"}:
            raise ValueError(f"tutorial line runs neither eawf nor git: {stripped!r}")
        steps.append(Step(line=stripped))
    return steps


def _git(repo: Path, *args: str) -> str:
    """Run one git command in *repo* and return its stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _resolve(repo: Path, line: str) -> list[str]:
    """Return the argv of *line*, each ``$(git rev-parse REV)`` replaced by its sha."""
    return shlex.split(_REV_PARSE.sub(lambda match: _git(repo, "rev-parse", match["rev"]), line))


def _resolves(argv: list[str]) -> bool:
    """Return whether *argv* names a command the ``eawf`` tree carries."""
    command: click.Command = typer.main.get_command(app)
    context = click.Context(command)
    for word in argv:
        if not isinstance(command, click.Group):
            return False
        found = command.get_command(context, word)
        if found is None:
            return False
        command = found
    return True


def _stored_task(repo: Path, key: str) -> dict[str, Any]:
    """Return one Task from whichever tier of the selected generation holds it."""
    authority = require_native_authority(repo / ".ea")
    assert authority.epoch == 2
    assert authority.target is not None and authority.generation_id is not None
    path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    row = read_document(path).get(Epoch2Collection.TASK.value, {}).get(key)
    if isinstance(row, dict):
        return row
    records = effective_records(read_ledger_records(ledger_path(path, Epoch2Collection.TASK)))
    payload = next(record.payload for record in records if record.record_key == key)
    assert isinstance(payload, dict)
    return payload


@pytest.fixture
def in_process_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Answer the CLI's daemon requests from the registered verbs, in process.

    ``EAWF_DAEMONLESS`` stays set, so the registry verbs take their
    in-process arm; ``HOME`` moves under the test so the registry they
    write is not the operator's.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", lambda *_a, **_k: 0)
    monkeypatch.setattr(domain_cmd, "DaemonClient", InProcessClient)
    InProcessClient.context = method_context(tmp_path / "runtime")
    methods.ensure_all_methods_registered()
    yield
    InProcessClient.context = None


def test_rel_021_first_workflow_tutorial_replays_on_a_fresh_epoch2_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_process_daemon: None
) -> None:
    """Every step of the tutorial runs in order and completes the Task."""
    steps = tutorial_steps(_TUTORIAL.read_text(encoding="utf-8"))
    assert any(step.line == "eawf ui" for step in steps)
    repo = tmp_path / "demo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "demo@example.com")
    _git(repo, "config", "user.name", "demo")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "chore: start")
    monkeypatch.chdir(repo)

    for step in steps:
        if step.path is not None:
            target = repo / step.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(step.content or "", encoding="utf-8")
            continue
        argv = _resolve(repo, step.line)
        if argv[0] == "git":
            _git(repo, *argv[1:])
            continue
        result = runner.invoke(app, argv[1:])
        assert result.exit_code == 0, (
            f"`{shlex.join(argv)}` exited {result.exit_code}\n{result.output}"
        )

    task = _stored_task(repo, "DEMO-0001")
    assert task["status"] == "COMPLETED"
    assert task["integrated_binding"]["head_sha"] == _git(repo, "rev-parse", "HEAD")


def test_rel_021_every_verb_the_tutorial_names_exists() -> None:
    """The verbs the prose names in inline code resolve in the command tree."""
    named = set(_INLINE_VERB.findall(_TUTORIAL.read_text(encoding="utf-8")))
    assert named, "the tutorial names no verb in inline code"
    missing = sorted(verb for verb in named if not _resolves(verb.split()))
    assert missing == []


def test_rel_021_verb_resolution_rejects_a_retired_verb() -> None:
    """A verb the tree does not carry is caught, so the check above can fail."""
    assert _resolves(["task", "prove"])
    assert not _resolves(["wave", "claim-next"])
    assert not _resolves(["task", "prove", "extra"])


def test_rel_021_steps_parse_files_and_commands_in_order() -> None:
    """A marked block is a file, a bash line is a command, prose is skipped."""
    source = (
        "Intro.\n\n<!-- eawf:file a.json -->\n\n```json\n{}\n```\n\n"
        "```bash\n# comment\neawf status\ngit log\n```\n\n```text\nignored\n```\n"
    )
    assert tutorial_steps(source) == [
        Step(path="a.json", content="{}\n"),
        Step(line="eawf status"),
        Step(line="git log"),
    ]


def test_rel_021_steps_reject_a_stray_shell_line() -> None:
    """A bash line that is neither eawf nor git cannot hide in the replay."""
    with pytest.raises(ValueError, match="neither eawf nor git"):
        tutorial_steps("```bash\ncd /tmp\n```\n")


def test_rel_021_steps_reject_an_unclosed_fence() -> None:
    """An unterminated block is refused rather than truncating the replay."""
    with pytest.raises(ValueError, match="not closed"):
        tutorial_steps("```bash\neawf status\n")


def test_rel_021_steps_reject_a_marker_without_a_fence() -> None:
    """A file marker followed by prose is refused rather than dropped."""
    with pytest.raises(ValueError, match="not followed by a fence"):
        tutorial_steps("<!-- eawf:file a.json -->\n\njust prose\n")


def test_rel_021_rev_parse_substitution_resolves_to_a_sha(tmp_path: Path) -> None:
    """``$(git rev-parse REV)`` becomes the full sha the reader's shell would print."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "-c", "user.email=d@example.com", "-c", "user.name=d",
         "commit", "-q", "--allow-empty", "-m", "start")  # fmt: skip
    head = _git(tmp_path, "rev-parse", "HEAD")
    assert _resolve(tmp_path, "eawf --head $(git rev-parse HEAD)") == [
        "eawf",
        "--head",
        head,
    ]
