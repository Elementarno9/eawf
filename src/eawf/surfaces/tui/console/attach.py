"""The console attach path: which pre-session state a launch lands in, from what it read.

``eawf ui`` resolves a tree before any projection exists, and every way that can end
short of a session is one of the entry layer's states (:data:`ENTRY_STATES`). This module
decides which one from real conditions -- the workspace registry, the tree's epoch
evidence, its cutover journal and the state schema it was written at -- and fills the
chrome's static state with the facts it read and the commands that change them.

Nothing here writes. The resolver reads the registry and a few small files, and every
command it names is handed to the operator rather than run, so no frame of the layer can
mutate state it cannot reach. Resolution never guesses: a tree named outright by a flag
or ``EA_STATE`` wins, otherwise only an exact registered root matches and membership is
read from the registry, never inferred.
"""

from __future__ import annotations

import json
import logging
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

from eawf._version import __version__
from eawf.kernel.migration.epoch2.canary import GENERATIONS_DIRNAME, MARKER_FILENAME
from eawf.kernel.migration.epoch2.errors import MigrationJournalBrokenError
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.migration.epoch2.journal import CutoverJournalRow, CutoverStage, read_journal
from eawf.kernel.projection.compute import (
    CANONICAL_SEQUENCE_FIELD,
    RouteProjection,
    build_route_projection,
)
from eawf.kernel.state.epoch2.authority import AuthorityGap, RootAuthority
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.registry import (
    WORKSPACE_AMBIGUOUS,
    Registry,
    RegistryReadError,
    WorkspaceResolutionError,
    project_codes_at_root,
    read_registry,
    resolve_workspace,
)
from eawf.surfaces.tui.console.chrome import ConsoleChrome, EntryState
from eawf.surfaces.tui.console.format import clock_time, day, group, span
from eawf.surfaces.tui.console.registry import ENTRY_STATES, EntryStateSpec
from eawf.surfaces.tui.console.tokens import TRUTH

if TYPE_CHECKING:
    from eawf.surfaces.tui.console.navigation import Ctx

logger = logging.getLogger(__name__)

#: The cells an ordinary pane row gives its step or fact name before the value.
_NAME_CELLS = 26

#: The unavailable token, for a fact nothing read.
_UNAVAILABLE = TRUTH["unknown"].unicode

#: The marker schema this console reads; a marker naming another was written by a
#: console this one predates.
_MARKER_SCHEMA = "1"

#: The apply's own stages, in the order it runs them: what "stage N of M" counts.
_APPLY_STAGES: Final[tuple[CutoverStage, ...]] = (
    CutoverStage.FENCE_CLEARED,
    CutoverStage.WORKSPACE_RESOLVED,
    CutoverStage.AUTHORITY_LOCKED,
    CutoverStage.QUIESCENCE_PROVED,
    CutoverStage.RECENSUS_MATCHED,
    CutoverStage.MAINTENANCE_ENTERED,
    CutoverStage.SNAPSHOT_TAKEN,
    CutoverStage.GENERATION_BUILT,
    CutoverStage.READ_SMOKE_PASSED,
    CutoverStage.GENERATION_SELECTED,
    CutoverStage.MARKER_WRITTEN,
    CutoverStage.MAINTENANCE_EXITED,
)

#: The stages a finished rollback leaves last: the tree is back at its previous
#: generation, so the migration is owed again rather than interrupted.
_ROLLED_BACK: Final[frozenset[CutoverStage]] = frozenset(
    {CutoverStage.ROLLBACK_DISCARDED, CutoverStage.SURFACES_RESTORED}
)

_SPEC: Final[dict[str, EntryStateSpec]] = {spec.id: spec for spec in ENTRY_STATES}


@dataclass(frozen=True, slots=True, kw_only=True)
class EntryCommand:
    """One command a pre-session state hands over, run by the operator and never here.

    Attributes:
        argv: The words after ``eawf``: the subcommand path, then its options and
            values. A value in angle brackets is one the operator fills in.
        purpose: What running it changes, in one short clause.
    """

    argv: tuple[str, ...]
    purpose: str

    @property
    def line(self) -> str:
        """Return the command as a shell line, quoting any value the shell would split."""
        return _shell(self.argv)

    def shown(self, here: Path) -> str:
        """Return the line as the frame prints it: a path at or under ``here`` made relative.

        The operator runs the command from the shell the console was launched in, so the
        folder they stand in reads as ``.`` rather than as a root too long for the frame.
        Enter still copies :attr:`line`, whole.

        Args:
            here: The directory the console was launched from.
        """
        return _shell(tuple(_relative(word, here) for word in self.argv))


def _shell(argv: Sequence[str]) -> str:
    """Return ``eawf`` and ``argv`` as a shell line, placeholders left unquoted."""
    words = [w if w.startswith("<") and w.endswith(">") else shlex.quote(w) for w in argv]
    return " ".join(("eawf", *words))


def _relative(word: str, here: Path) -> str:
    """Return ``word`` relative to ``here`` when it is an absolute path at or under it."""
    path = Path(word)
    if not path.is_absolute() or not path.is_relative_to(here):
        return word
    return str(path.relative_to(here))


@dataclass(frozen=True, slots=True, kw_only=True)
class AttachStep:
    """One step of the resolution, with what it found.

    Attributes:
        name: The step, such as ``registered root``.
        result: What the step found, in words.
    """

    name: str
    result: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AttachRequest:
    """What one launch resolves from.

    Attributes:
        repo_root: The repository root holding the tree's ``.ea`` directory.
        authority: The tree's resolved authority epoch.
        state_path: The tree's ``state.json``.
        named_by: What named the tree outright (``--workspace`` or ``EA_STATE``), or
            ``None`` for a bare launch, which only an exact registered root answers.
        workspace_key: A workspace key named outright (``EAWF_WORKSPACE_KEY``), or
            ``None``.
        registry_path: The machine workspace registry.
    """

    repo_root: Path
    authority: RootAuthority
    state_path: Path
    named_by: str | None
    workspace_key: str | None
    registry_path: Path


@dataclass(frozen=True, slots=True, kw_only=True)
class AttachResult:
    """Where a launch lands.

    Attributes:
        entry: The pre-session state the console must open on, filled from what the
            resolver read; ``None`` when the tree attaches.
        trace: The resolution steps and what each found, the resolving frame's rows.
    """

    entry: EntryState | None
    trace: tuple[AttachStep, ...]


def _base(chrome: ConsoleChrome, state_id: str) -> EntryState:
    """Return the chrome's static state ``state_id`` with the registry's process label.

    The header's process value is the registry's label, upper-cased, so a state's header
    can never carry a connection value where no connection exists.

    Raises:
        ValueError: the chrome carries no entry state ``state_id``.
    """
    for state in chrome.entry:
        if state.id == state_id:
            return state.model_copy(update={"state": _SPEC[state_id].label.upper()})
    raise ValueError(f"chrome carries no entry state {state_id!r}")


def _fact(name: str, value: str) -> str:
    """Return one pane row: the fact's name in its column, then its value."""
    return f"{name:<{_NAME_CELLS}}{value}"


def _command_lines(commands: Sequence[EntryCommand]) -> tuple[str, ...]:
    """Return each command's purpose, then the command as printed on a line of its own.

    Each command gets its own line so a long root never shares the width with prose.
    """
    here = Path.cwd()
    lines: list[str] = []
    for command in commands:
        lines.extend((command.purpose, f"  {command.shown(here)}"))
    return tuple(lines)


def resolving_state(
    chrome: ConsoleChrome, trace: Sequence[AttachStep], *, elapsed: float
) -> EntryState:
    """Return the resolving frame: each resolution step and what it found.

    Args:
        chrome: The packaged chrome.
        trace: The resolution steps, in the order they ran.
        elapsed: Seconds the resolution took on the attach clock. It is derived rather
            than measured by the daemon, so it carries the approximation mark.

    Returns:
        The resolving state.

    Raises:
        ValueError: ``trace`` is empty or ``elapsed`` is negative.
    """
    if not trace:
        raise ValueError("a resolving frame needs at least one resolution step")
    if elapsed < 0:
        raise ValueError(f"elapsed cannot be negative, got {elapsed}")
    panes = tuple(
        ("STEP" if i == 0 else "", _fact(step.name, step.result)) for i, step in enumerate(trace)
    )
    return _base(chrome, "resolving").model_copy(
        update={
            "panes": panes,
            "tail": (
                _fact("elapsed", f"~{elapsed:.1f}s"),
                "",
                "Nothing has been read and nothing has been changed.",
            ),
            "disclosure": (
                (120, "READS", "the registry only · no projection, no events, no state"),
                (
                    160,
                    "SAFETY",
                    "an explicit --workspace or EA_STATE wins outright; otherwise only an"
                    " exact registered root matches. Membership is read, never inferred,"
                    " so this screen cannot progress by guessing.",
                ),
            ),
        }
    )


def _date(stamp: datetime | None) -> str:
    """Return a registry stamp as a date in words, or the unavailable token."""
    return day(stamp) if stamp is not None else _UNAVAILABLE


def ambiguous_state(
    chrome: ConsoleChrome, registry: Registry, candidates: Sequence[str], root: Path
) -> EntryState:
    """Return the ambiguous frame: every workspace claiming ``root``, and the two exits.

    Args:
        chrome: The packaged chrome.
        registry: The registry the candidates were resolved from.
        candidates: The competing workspace keys, sorted.
        root: The repository root they share.

    Returns:
        The ambiguous state; Enter copies the session-local select command for the row
        under the cursor, and Escape cancels.

    Raises:
        ValueError: fewer than two candidates, which is not an ambiguity.
        KeyError: a candidate is not a registered workspace.
    """
    if len(candidates) < 2:
        raise ValueError(f"an ambiguity needs two candidates or more, got {len(candidates)}")
    rows: list[tuple[str, ...]] = []
    commands: list[str] = []
    for key in candidates:
        record = registry.workspaces[key]
        home = registry.repos.get(record.home_project_code)
        last = home.last_seen if home is not None else None
        rows.append(
            (record.title or key, record.home_project_code, _date(record.updated_at), _date(last))
        )
        commands.append(EntryCommand(argv=("workspace", "select", key), purpose="").line)
    return _base(chrome, "ambiguous").model_copy(
        update={
            "rows": tuple(rows),
            "keys": (
                ("↑↓", "candidate"),
                ("Enter", "copy the select command"),
                ("Esc", "cancel · not attached"),
            ),
            "commands": tuple(commands),
            "tail": (
                "Your pick is remembered for this session only:",
                "  eawf workspace select <KEY> exports it for this shell.",
                "Pass --workspace to name the tree outright instead.",
            ),
            "disclosure": (
                (160, "ROOT", str(root)),
                (
                    160,
                    "DIAGNOSTICS",
                    f"{len(candidates)} registrations share this root. That is legal, not"
                    " an error - the console asks because choosing the most recent would"
                    " be a guess.",
                ),
            ),
        }
    )


def failed_state(
    chrome: ConsoleChrome, reason: tuple[str, ...], commands: Sequence[EntryCommand]
) -> EntryState:
    """Return the failed frame: the rule resolution applied and the commands that help.

    Args:
        chrome: The packaged chrome.
        reason: The rule that was not met, in friendly words, one line per row; the raw
            error code belongs to diagnostics, never here.
        commands: The commands that change the situation, the one Enter copies first.

    Returns:
        The failed state.

    Raises:
        ValueError: ``reason`` or ``commands`` is empty: a terminal state must say why
            and hand over at least one command.
    """
    if not reason or not commands:
        raise ValueError("a failed frame needs a reason and at least one command")
    return _base(chrome, "failed").model_copy(
        update={
            "panes": tuple(("REASON" if i == 0 else "", line) for i, line in enumerate(reason)),
            "tail": (*_command_lines(commands), "", "exit 4 · nothing was changed"),
            "commands": tuple(command.line for command in commands),
        }
    )


def migration_state(chrome: ConsoleChrome, target_root: Path) -> EntryState:
    """Return the migration-required frame: the four paths in their safety order.

    The order is the safety property: the dry run reports only, the backup precedes the
    apply, the apply needs the dry run's digest, and rollback exists once applied. None
    of them runs here.

    Args:
        chrome: The packaged chrome.
        target_root: The declared tree, the one ``--target-root`` names.

    Returns:
        The migration-required state.
    """
    target = str(target_root)
    plan = ("migrate", "epoch2", "--plan", "--snapshot-root", "<SNAPSHOT>")
    plan_keys = (
        "--allowlist",
        "<ALLOWLIST>",
        "--workspace-key",
        "<KEY>",
        "--project-key",
        "<KEY>",
        "--repository-key",
        "<KEY>",
    )
    commands = (
        EntryCommand(argv=(*plan, *plan_keys), purpose="report only · changes nothing"),
        EntryCommand(argv=("backup", "create"), purpose="written before anything is applied"),
        EntryCommand(
            argv=(
                "migrate",
                "epoch2",
                "--apply",
                "--plan-digest",
                "<DIGEST>",
                "--target-root",
                target,
                "--snapshot-root",
                "<SNAPSHOT>",
                *plan_keys,
            ),
            purpose="requires the dry run to have passed",
        ),
        EntryCommand(
            argv=("migrate", "epoch2", "--rollback", "--target-root", target),
            purpose="restores the backup · available after apply",
        ),
    )
    names = ("dry run", "backup", "apply", "rollback")
    return _base(chrome, "migration").model_copy(
        update={
            "panes": (
                ("MIGRATION", "never auto-applies"),
                ("", _fact("state generation", "1  →  2")),
                ("", _fact("entities to carry", f"{_UNAVAILABLE} counted by the dry run")),
            ),
            "paths": tuple(zip(names, (c.purpose for c in commands), strict=True)),
            "commands": tuple(command.line for command in commands),
            "tail": ("Every normal command is refused until this finishes.", "exit 4"),
        }
    )


def _clock(row: CutoverJournalRow) -> str:
    """Return a journal row's instant as a wall-clock time."""
    return clock_time(row.recorded_at)


def interrupted_state(
    chrome: ConsoleChrome,
    target_root: Path,
    journal: Sequence[CutoverJournalRow],
    *,
    why: str,
) -> EntryState:
    """Return the interrupted frame: which generation rules now, and the two exits.

    Before a generation is selected the previous one still rules, and the exits are to
    resume from the journal or discard the partial generation. After selection the
    marker is still unwritten, so readers still read the previous generation, and the
    exit is to reconcile the selected one from the same journal.

    Args:
        chrome: The packaged chrome.
        target_root: The declared tree.
        journal: The cutover journal's rows; empty when none could be read.
        why: Why the apply is judged stopped, in words.

    Returns:
        The interrupted state.
    """
    target = str(target_root)
    selected = any(row.stage is CutoverStage.GENERATION_SELECTED for row in journal)
    applied = [row for row in journal if row.stage in _APPLY_STAGES]
    if applied:
        last = applied[-1]
        at = _APPLY_STAGES.index(last.stage) + 1
        stopped = f"stage {at} of {len(_APPLY_STAGES)} · {_clock(last)}"
        reached = last.stage.value.replace("_", " ")
    else:
        stopped = f"{_UNAVAILABLE} no journal row could be read"
        reached = _UNAVAILABLE
    recover = EntryCommand(
        argv=("migrate", "epoch2", "--recover", "--target-root", target), purpose=""
    )
    rollback = EntryCommand(
        argv=("migrate", "epoch2", "--rollback", "--target-root", target), purpose=""
    )
    if selected:
        authoritative = "generation 1 · the selected generation is not read yet"
        paths = (
            ("reconcile", "finish the selected generation from the journal"),
            ("rollback", "restore the surfaces the restore point pinned"),
        )
        rule = "Generation 1 stays authoritative until the marker is written."
    else:
        authoritative = "generation 1 · unchanged"
        paths = (
            ("resume", "continue from the journal"),
            ("discard", "remove the partial generation 2"),
        )
        rule = "Generation 1 stays authoritative until a selection is made."
    return _base(chrome, "interrupted").model_copy(
        update={
            "panes": (
                ("JOURNAL", _fact("stopped at", stopped)),
                ("", _fact("last stage done", reached)),
                ("", _fact("reason", why)),
                ("", _fact("authoritative now", authoritative)),
            ),
            "paths": paths,
            "commands": (recover.line, rollback.line),
            "tail": ("Both generations are never written at once.", rule),
        }
    )


def schema_state(
    chrome: ConsoleChrome, *, written: str, reads: str, target_root: Path
) -> EntryState:
    """Return the schema-unsupported frame: the refusal, and the read-only export.

    Args:
        chrome: The packaged chrome.
        written: The schema the workspace was written at, as that console named it.
        reads: The newest schema this console reads.
        target_root: The declared tree.

    Returns:
        The schema-unsupported state; the export is offered for inspection elsewhere,
        never as a way to attach.
    """
    export = EntryCommand(
        argv=("migrate", "epoch2", "--export", "--snapshot-root", str(target_root)),
        purpose="write a read-only export of the last known state",
    )
    return _base(chrome, "schema").model_copy(
        update={
            "panes": (
                ("SCHEMA", _fact("workspace writes", written)),
                ("", _fact("written by", f"a console newer than eawf {__version__}")),
                ("", _fact("this console reads", f"up to {reads} · eawf {__version__}")),
                ("", _fact("compatible", "no")),
            ),
            "tail": (
                "Reading is refused. The export is for inspection elsewhere:",
                *_command_lines((export,)),
                "",
                "Upgrading the console is the only way to attach.",
                "exit 4",
            ),
            "commands": (export.line,),
        }
    )


#: The entry state a first run with no registered workspace lands in.
ONBOARDING: Final = "onboarding"

#: What stays unavailable when a first-run step is skipped, by step. Skipping records
#: nothing: the step is still owed, and the frame says what its absence withholds.
ONBOARDING_SKIPS: tuple[str, ...] = (
    "no workspace · nothing can attach until one is registered",
    "no authorised provider · every run verb stays refused until one is",
    "the default sandbox policy stays in force",
    "no task attached · attach one later to watch it run",
)


def skip_step(ctx: Ctx, steps: int) -> bool:
    """Skip the first-run step under the cursor, saying what stays unavailable without it.

    Returns:
        ``True``: the key is always claimed.
    """
    s = ctx.s
    at = min(max(s.path_sel, 0), len(ONBOARDING_SKIPS) - 1)
    ctx.log("s", f"skipped step {at + 1} · {ONBOARDING_SKIPS[at]}")
    s.path_sel = min(at + 1, max(steps - 1, 0))
    return True


def onboarding_state(chrome: ConsoleChrome, root: Path, *, registered: bool) -> EntryState:
    """Return the first-run frame: what a session needs before one can exist.

    Args:
        chrome: The packaged chrome.
        root: The repository root the launch stands in.
        registered: Whether ``root`` is a registered repository already, which decides
            whether the first step registers it or makes it a workspace.

    Returns:
        The onboarding state; Enter copies the step's command where a declared one
        exists, and nothing here applies a migration or starts an agent.
    """
    first = (
        EntryCommand(argv=("workspace", "add", "<KEY>", "--home", "<CODE>"), purpose="")
        if registered
        else EntryCommand(argv=("repo", "register", str(root)), purpose="")
    )
    return _base(chrome, ONBOARDING).model_copy(
        update={
            "rows": (
                ("1 workspace", "none registered", "every entity hangs off one"),
                ("2 provider", "not checked here", "no agent runs without it"),
                ("3 sandbox", "not checked here", "what an agent may touch"),
                ("4 first task", "optional", "you may attach and watch"),
            ),
            "keys": (("↑↓", "step"), ("Enter", "do it"), ("s", "skip"), ("Esc", "exit")),
            "commands": (first.line, "", "", ""),
            "tail": (
                "No workspace is registered on this machine.",
                f"  {first.shown(Path.cwd())}",
                "",
                "No migration is applied and no agent is started here.",
            ),
        }
    )


def _read_registry(path: Path) -> Registry | None:
    """Return the registry at ``path``, or ``None`` when the machine has none yet.

    Raises:
        RegistryReadError: the file exists but does not parse.
    """
    if not path.is_file():
        return None
    return read_registry(path)


def _newer_marker_schema(authority: RootAuthority) -> str | None:
    """Return the schema an unreadable marker names, when a newer console wrote it.

    A marker that parses as an object but names another schema, or a later epoch, was
    written by a console this one predates; anything else is a damaged marker, which is
    an interrupted migration rather than an unsupported one.
    """
    path = authority.root / GENERATIONS_DIRNAME / MARKER_FILENAME
    try:
        payload = json.loads(path.read_text("utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    schema = payload.get("schema_version")
    epoch = payload.get("epoch")
    if isinstance(schema, str) and schema != _MARKER_SCHEMA:
        return f"marker schema v{schema}"
    if isinstance(epoch, int) and epoch > 2:
        return f"epoch {epoch}"
    return None


def _version_key(version: str) -> tuple[int, ...] | None:
    """Return a dotted numeric version as a sortable tuple, or ``None`` when it is not one."""
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return None


def _newer_state_schema(state_path: Path) -> tuple[str, str] | None:
    """Return the state schema written and the newest read, when a newer console wrote it.

    Only the document's ``schema_version`` is read; a missing or unparseable document is
    not this state's to report.
    """
    from eawf.kernel.migrations import model_supported_max_version

    try:
        payload = json.loads(state_path.read_text("utf-8"))
    except OSError, ValueError:
        return None
    written = payload.get("schema_version") if isinstance(payload, dict) else None
    if not isinstance(written, str):
        return None
    supported = model_supported_max_version()
    have, want = _version_key(written), _version_key(supported)
    if have is None or want is None or have <= want:
        return None
    return (f"state schema v{written}", f"state schema v{supported}")


def _journal(authority: RootAuthority) -> tuple[tuple[CutoverJournalRow, ...], bool]:
    """Return the tree's cutover journal and whether it could be read."""
    try:
        return read_journal(authority.root / GENERATIONS_DIRNAME / "journal.jsonl"), True
    except MigrationJournalBrokenError, OSError:
        return (), False


def _migration(chrome: ConsoleChrome, authority: RootAuthority) -> EntryState | None:
    """Return the migration state a declared tree without epoch-2 authority is in."""
    if authority.gap is AuthorityGap.MARKER_UNREADABLE:
        newer = _newer_marker_schema(authority)
        if newer is not None:
            return schema_state(
                chrome,
                written=newer,
                reads=f"marker schema v{_MARKER_SCHEMA}",
                target_root=authority.root,
            )
        journal, _ok = _journal(authority)
        return interrupted_state(
            chrome, authority.root, journal, why="the epoch marker does not parse"
        )
    if authority.gap is AuthorityGap.MARKER_ABSENT:
        journal, readable = _journal(authority)
        if not readable:
            return interrupted_state(
                chrome, authority.root, (), why="the cutover journal does not parse"
            )
        if journal and journal[-1].stage not in _ROLLED_BACK:
            return interrupted_state(
                chrome, authority.root, journal, why="the process ended before the marker"
            )
        return migration_state(chrome, authority.root)
    return None


def _register_commands(registry: Registry, root: Path) -> tuple[EntryCommand, ...]:
    """Return the commands that make ``root`` resolvable, the most direct first."""
    listing = EntryCommand(
        argv=("workspace", "list"), purpose="show the workspaces already registered"
    )
    codes = sorted(project_codes_at_root(registry, root))
    if not codes:
        register = EntryCommand(
            argv=("repo", "register", str(root)), purpose="register this folder as a root"
        )
        return (register, listing)
    add = EntryCommand(
        argv=("workspace", "add", "<KEY>", "--home", codes[0]),
        purpose=f"make project {codes[0]} a workspace",
    )
    return (add, listing)


def _resolve_workspace(
    chrome: ConsoleChrome, request: AttachRequest
) -> tuple[EntryState | None, tuple[AttachStep, ...]]:
    """Resolve the launch's workspace through the registry, or name why it cannot.

    Returns:
        The entry state resolution ended in, ``None`` once one workspace answered, and
        the steps it ran.
    """
    root = request.repo_root
    try:
        registry = _read_registry(request.registry_path)
    except RegistryReadError:
        reason = (
            "The workspace registry on this machine does not parse, so no",
            "root can be matched against it. Nothing was assumed instead.",
        )
        doctor = EntryCommand(argv=("doctor",), purpose="diagnose the registry")
        return failed_state(chrome, reason, (doctor,)), ()
    if registry is None or not registry.workspaces:
        registered = registry is not None and bool(project_codes_at_root(registry, root))
        return onboarding_state(chrome, root, registered=registered), ()
    try:
        resolution = resolve_workspace(
            registry,
            env_key=request.workspace_key,
            repo_root=None if request.workspace_key else root,
        )
    except WorkspaceResolutionError as error:
        if error.code == WORKSPACE_AMBIGUOUS:
            return ambiguous_state(chrome, registry, error.candidates, root), ()
        if request.workspace_key:
            reason = (
                f"The workspace named by EAWF_WORKSPACE_KEY, {request.workspace_key}, is",
                "not registered. A named workspace is never swapped for another.",
            )
            listing = EntryCommand(
                argv=("workspace", "list"), purpose="show the workspaces already registered"
            )
            return failed_state(chrome, reason, (listing,)), ()
        reason = (
            "The console only attaches to an exact registered root. Parent",
            "folders are never scanned, and membership is never assumed.",
        )
        return failed_state(chrome, reason, _register_commands(registry, root)), ()
    codes = sorted(project_codes_at_root(registry, root))
    matched = f"matched · {codes[0]}" if codes else "named by EAWF_WORKSPACE_KEY"
    steps = (
        AttachStep(name="registered root", result=matched),
        AttachStep(name="workspace", result=f"resolved · {resolution.key}"),
        AttachStep(
            name="project membership",
            result=f"member · {', '.join(sorted(resolution.record.member_project_codes))}",
        ),
    )
    return None, steps


def _epoch1(chrome: ConsoleChrome, request: AttachRequest) -> EntryState:
    """Return the state an undeclared tree lands in: migration owed, or no tree at all."""
    if request.state_path.is_file():
        return migration_state(chrome, request.authority.root)
    reason = (
        "No eawf tree is here: this folder holds no state file,",
        "so there is nothing to attach to or migrate.",
    )
    init = EntryCommand(argv=("init",), purpose="create a tree in this folder")
    return failed_state(chrome, reason, (init,))


def resolve_attach(chrome: ConsoleChrome, request: AttachRequest) -> AttachResult:
    """Return where a launch lands, from the conditions it actually reads.

    A plain epoch-1 tree, one that never declared the epoch-2 canary, is not resolved
    through the registry: the console cannot attach to it, so it lands in the
    migration-required state (or the schema state, for a state schema newer than this
    console), and a folder holding no state at all lands in the failed state naming
    ``eawf init``. A declared tree resolves its workspace (unless a flag named the tree
    outright), then its migration evidence, then its state schema.

    Args:
        chrome: The packaged chrome the states are filled from.
        request: What the launch resolves from.

    Returns:
        The entry state the console must open on, or none with the resolution trace.
    """
    declared = request.authority.gap is not AuthorityGap.UNDECLARED
    steps: tuple[AttachStep, ...]
    if request.named_by is not None:
        steps = (
            AttachStep(name="registered root", result=f"named outright · {request.named_by}"),
            AttachStep(name="workspace", result="not consulted · the flag wins"),
        )
    elif declared:
        entry, steps = _resolve_workspace(chrome, request)
        if entry is not None:
            return AttachResult(entry=entry, trace=steps)
    else:
        steps = (AttachStep(name="registered root", result="not consulted · epoch-1 tree"),)
    if declared:
        migration = _migration(chrome, request.authority)
        if migration is not None:
            return AttachResult(entry=migration, trace=steps)
    newer = _newer_state_schema(request.state_path)
    if newer is not None:
        written, reads = newer
        state = schema_state(
            chrome, written=written, reads=reads, target_root=request.state_path.parent
        )
        return AttachResult(entry=state, trace=steps)
    if not declared:
        return AttachResult(entry=_epoch1(chrome, request), trace=steps)
    daemon = AttachStep(name="daemon", result="contacted · waiting on the first projection")
    return AttachResult(entry=None, trace=(*steps, daemon))


#: The entry state a launch lands in when the daemon answers nothing.
OFFLINE: Final = "offline"

#: The route the offline frame's rows are read for: scope home, one row per Track.
_OFFLINE_ROUTE: Final = "scope.home"

#: What the offline frame says when there is no snapshot to show.
NO_SNAPSHOT: Final = "∅ no snapshot held · this console has read nothing from the tree yet"


@dataclass(frozen=True, slots=True, kw_only=True)
class OfflineSnapshot:
    """The last state the tree committed, read from disk without the daemon.

    Attributes:
        projection: The scope-home rows built from the committed document.
        committed_at: When that document was last written, which is what the rows are
            as of.
    """

    projection: RouteProjection
    committed_at: datetime


def offline_snapshot(
    authority: RootAuthority, *, scope_id: str, now: datetime
) -> OfflineSnapshot | None:
    """Return the tree's last committed scope home, read from its document; ``None`` if none.

    Nothing is written and no daemon is asked: the selected generation's document is
    the last state any commit left behind, so it is what an offline console can show.

    Args:
        authority: The tree's resolved authority.
        scope_id: The scope the rows are stated for.
        now: When the snapshot is read, which stamps the rows' projection.

    Returns:
        The snapshot, or ``None`` when the tree names no generation or its document
        cannot be read as one.
    """
    target, generation = authority.target, authority.generation_id
    if target is None or generation is None:
        return None
    path = target.generation_path(generation) / GENERATION_DOCUMENT
    try:
        document = read_document(path)
        cursor = document.get(CANONICAL_SEQUENCE_FIELD, 0)
        projection = build_route_projection(
            route=_OFFLINE_ROUTE,
            document=document,
            cursor=cursor if isinstance(cursor, int) else 0,
            scope_id=scope_id,
            generated_at=now,
        )
        committed_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except (OSError, ValueError) as error:
        logger.info(f"offline_snapshot unreadable path={path.name} cause={error!s}")
        return None
    return OfflineSnapshot(projection=projection, committed_at=committed_at)


def offline_state(chrome: ConsoleChrome, snapshot: OfflineSnapshot | None) -> EntryState:
    """Return the offline frame: each outcome as the last snapshot held it, or its absence.

    Args:
        chrome: The packaged chrome.
        snapshot: The last committed scope home; ``None`` when none could be read.

    Returns:
        The offline state, titled with the revision the snapshot holds and its age, one
        row per Track with the Runs filed under it and the time the snapshot is as of.
        What needs you is not read without the daemon, so that cell is the unknown token
        rather than a count.
    """
    base = _base(chrome, OFFLINE)
    if snapshot is None:
        return base.model_copy(update={"rows": (), "tail": (NO_SNAPSHOT,)})
    at = clock_time(snapshot.committed_at)[:5]
    header = snapshot.projection.header
    age = span(max(0, int((header.generated_at - snapshot.committed_at).total_seconds())))
    # the cursor is opaque by type; the offline read builds it from the document's sequence
    cursor = header.source_cursor
    revision = group(int(cursor)) if cursor.isdigit() else cursor
    title = f"Scope home · attached to revision {revision} · {age} old"
    tracks = [row for row in snapshot.projection.rows if row.collection is Epoch2Collection.TRACK]
    rows = tuple(
        (
            row.title or row.key,
            row.facts.get("runs", "0"),
            _UNAVAILABLE,
            at,
        )
        for row in tracks
    )
    # what still works is the keybar's to say, so the body names only what is absent
    tail = (
        "Everything here is a snapshot. Nothing is arriving, and no count",
        f"can be called complete for the time since {at}.",
        "Controls are gone until the daemon answers again.",
    )
    return base.model_copy(
        update={"title": title, "rows": rows or None, "tail": tail if rows else (NO_SNAPSHOT,)}
    )


def with_entry_state(chrome: ConsoleChrome, state: EntryState) -> ConsoleChrome:
    """Return ``chrome`` with its entry state of the same id replaced by ``state``.

    Raises:
        ValueError: ``chrome`` carries no entry state with ``state``'s id.
    """
    entry = list(chrome.entry)
    for index, held in enumerate(entry):
        if held.id == state.id:
            entry[index] = state
            return chrome.model_copy(update={"entry": tuple(entry)})
    raise ValueError(f"chrome carries no entry state {state.id!r}")


__all__ = [
    "NO_SNAPSHOT",
    "OFFLINE",
    "ONBOARDING",
    "ONBOARDING_SKIPS",
    "AttachRequest",
    "AttachResult",
    "AttachStep",
    "EntryCommand",
    "OfflineSnapshot",
    "ambiguous_state",
    "failed_state",
    "interrupted_state",
    "migration_state",
    "offline_snapshot",
    "offline_state",
    "onboarding_state",
    "resolve_attach",
    "resolving_state",
    "schema_state",
    "skip_step",
    "with_entry_state",
]
