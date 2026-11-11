"""``eawf hook run <event>`` Typer command.

Surface contract:

- ``eawf hook run <event_type> [--runtime <claude|opencode|generic>]
  [--scope <ID>] [--command <str>]`` reads a JSON payload from stdin,
  builds a typed :class:`~eawf.runtime.hooks.event.HookEvent`, dispatches it
  through a fresh :class:`~eawf.runtime.hooks.runner.HookRunner`, and emits an
  :class:`~eawf.surfaces.render.envelope.OutputEnvelope` to stdout.
- ``eawf hook dispatch --event-type agent_end [--runtime ...] [--scope ...]
  [--command ...]`` is the interim verdict seeder: it translates one
  ``agent_end`` event (payload on stdin, naming a native Run) into a single
  seeded verdict row via
  :func:`eawf.workflow.dispatch.seed.seed_interim_verdict` so the self-eval +
  jury surfaces read a primed cohort before the live verdict producer lands.
  Only ``agent_end`` is accepted; other event types exit ``3``.
- ``eawf hook agent-output`` is the streaming sibling of that seeder: an
  orchestrator that spawned the agent itself forwards one output chunk
  (payload on stdin, naming a native Run) and it lands as the same
  ``agent.output.chunk`` event the in-daemon spawn path writes, so the agent
  watch surface renders a Run the daemon never spawned instead of an empty
  pane.
- Both resolve the reporting role and scope from the named Run in the
  selected generation; neither reads the v1 ``state.json``.
- Exit ``0`` when no registered hook returns ``block=True``. ``session_end``
  registers the built-in runtime capture hook; other events without hooks keep
  the empty-result no-op path.
- ``session_start`` also runs the rule-projection staleness check, which
  reports once per session when a rendered projection's stamp does not name
  the installed rule graph. Under ``--runtime claude`` the command prints
  that report as the host's session-start context document instead of the
  envelope, and prints nothing when there is nothing to report, because
  whatever a session-start hook prints lands in the session's context.
- Exit ``9`` (``HOOK_BLOCKED``) when at least one hook reports a block.
- Exit ``3`` (``INVALID_INPUT``) when the stdin payload is not valid JSON
  or is not a mapping.

The runner mounted by this command starts with built-in Eä hooks only. Runtime
adapters may register additional hooks later; when no hook matches, the result
list is empty and the exit code is ``0``.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Protocol, cast

import orjson
import typer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

if TYPE_CHECKING:
    from eawf.kernel.state.epoch2.urns import RunUrn
    from eawf.observability.logging.state_leak import StateLeakKind
    from eawf.platform.lint import LintConfig
    from eawf.runtime.hooks.context import HookContext
    from eawf.runtime.hooks.event import HookEvent, HookEventType, HookRuntime
    from eawf.runtime.hooks.health import HookFailure
    from eawf.runtime.hooks.runner import HookResult, HookRunner
    from eawf.surfaces.render.envelope import EnvelopeStatus, OutputEnvelope

logger = logging.getLogger(__name__)


class AgentEndPayload(BaseModel):
    """Payload accepted by ``eawf hook run agent_end``.

    Attributes:
        run_ref: The native Run the report speaks for; its sealed capsule
            grants the role the body must claim.
        base_id: Report ``base_id`` the attempts are grouped under.
        body: The role-report body.
        artifact_ids: Artifact ids attached to the row.
        blob_refs: Blob refs attached to the row.
    """

    model_config = ConfigDict(extra="forbid")

    run_ref: RunUrn
    base_id: str
    body: dict[str, Any]
    artifact_ids: list[str] = Field(default_factory=list)
    blob_refs: list[str] = Field(default_factory=list)


class AgentOutputPayload(BaseModel):
    """Payload accepted by ``eawf hook agent-output``.

    Carries only the chunk itself. The scope the chunk routes to is resolved
    from the named Run, so a forwarder cannot mis-address its own stream.

    Attributes:
        run_ref: The native Run the chunk belongs to.
        seq: Per-Run monotonic chunk index (0-based).
        text: The forwarded output text for this chunk.
    """

    model_config = ConfigDict(extra="forbid")

    run_ref: RunUrn
    seq: int = Field(ge=0)
    text: str


def _run_ref_model[M: type[BaseModel]](model: M) -> M:
    """Return *model* with its ``RunUrn`` field resolved.

    The payload models name ``RunUrn`` only for type checkers, so importing
    this module does not load the state package every hook process skips;
    the verbs that read a Run resolve it here, once per process.

    Args:
        model: A payload model whose ``run_ref`` field is a ``RunUrn``.

    Returns:
        The same model class, rebuilt when it was not yet complete.
    """
    from eawf.kernel.state.epoch2.urns import RunUrn

    model.model_rebuild(_types_namespace={"RunUrn": RunUrn})
    return model


hook_app = typer.Typer(
    name="hook",
    help="Dispatch hook events through the Eä hook runner.",
    no_args_is_help=True,
)


def _parse_event_type(raw: str) -> HookEventType:
    """Resolve *raw* (CLI argument string) into a :class:`HookEventType`.

    Accepts the canonical lowercase value ("pre_commit", "post_commit",
    …) — matches the StrEnum value verbatim. Unknown values raise
    :class:`~eawf.surfaces.cli.errors.UserError` (``kind="InvalidInput"``) so the
    handler can surface exit code 3.
    """
    from eawf.runtime.hooks.event import HookEventType

    try:
        return HookEventType(raw)
    except ValueError as exc:
        valid = sorted(t.value for t in HookEventType)
        raise cli_errors.UserError(
            f"unknown event type {raw!r}; expected one of {valid}", kind="InvalidInput"
        ) from exc


def _parse_payload(stdin_text: str) -> dict[str, Any]:
    """Decode and shape-check the stdin JSON payload.

    Returns a mapping. Empty input is treated as ``{}`` so callers may
    invoke ``eawf hook run pre_commit`` with no piped payload during
    smoke checks.

    Raises:
        UserError: ``stdin_text`` is non-empty but not valid JSON,
            or decodes to something other than a JSON object
            (``kind="InvalidInput"``).
    """
    if not stdin_text.strip():
        return {}
    try:
        decoded: Any = orjson.loads(stdin_text)
    except orjson.JSONDecodeError as exc:
        raise cli_errors.UserError(f"stdin is not valid JSON: {exc}", kind="InvalidInput") from exc
    if not isinstance(decoded, dict):
        raise cli_errors.UserError(
            f"stdin payload must be a JSON object; got {type(decoded).__name__}",
            kind="InvalidInput",
        )
    return cast(dict[str, Any], decoded)


def _build_event(
    *,
    event_type: HookEventType,
    payload: dict[str, Any],
    scope: str,
    command: str,
    runtime: HookRuntime,
    occurred_at: datetime,
) -> HookEvent:
    """Build the typed :class:`HookEvent` from CLI args + decoded stdin.

    The decoded stdin mapping is folded into ``payloads[<event_type>]``
    so downstream hooks see the original shape under a stable key.
    """
    from eawf.runtime.hooks.event import HookEvent

    return HookEvent(
        event_type=event_type,
        scope_id=scope,
        command=command,
        args={},
        runtime=runtime,
        occurred_at=occurred_at,
        payloads={event_type.value: dict(payload)} if payload else {},
    )


def _envelope_for(
    *,
    event: HookEvent,
    results: list[HookResult],
    started_at: datetime,
    finished_at: datetime,
) -> OutputEnvelope:
    """Assemble the output envelope for a finished dispatch.

    The envelope is the same shape every Eä CLI command emits; we use
    ``/audit`` as the carrier ``skill`` because there is no dedicated
    skill for the CLI hook surface (a ``/hook`` skill would only be
    worth adding if operators need to invoke hooks by hand). Status is ``ok`` when no hook
    blocked, ``blocked`` otherwise.
    """
    from eawf.surfaces.render.envelope import (
        EnvelopeFooter,
        EnvelopeHeader,
        EnvelopeWarning,
        OutputEnvelope,
    )

    blocked = any(r.block for r in results)
    status: EnvelopeStatus = "blocked" if blocked else "ok"
    body: dict[str, object] = {
        "event_type": event.event_type.value,
        "scope_id": event.scope_id,
        "runtime": event.runtime,
        "occurred_at": event.occurred_at.isoformat(),
        "results": [r.model_dump(mode="json") for r in results],
        "blocked": blocked,
    }
    warnings = [
        EnvelopeWarning(code="hook_raised", detail=f"hook {r.name!r}: {r.output}")
        for r in results
        if r.raised
    ]
    repair_commands = (
        [f"review hook output for {r.name!r}" for r in results if r.block] if blocked else None
    )
    header = EnvelopeHeader(
        skill="/audit",
        scope_id=event.scope_id or "urn:eawf:v1:state:hook-run",
        session="urn:eawf:v1:store:hook/sessions/SES-cli",
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        instrument_probe={},
    )
    footer = EnvelopeFooter(
        persisted_artifacts=[],
        persisted_store_records=[],
        state_mutations=[],
        evidence_refs=[],
        next_valid_actions=[],
        warnings=warnings,
        repair_commands=repair_commands,
    )
    return OutputEnvelope(header=header, body=body, footer=footer)


def _envelope_for_agent_report(
    *,
    event: HookEvent,
    report_id: str,
    store_kind: str,
    attempt: int,
    store_urn: str,
    started_at: datetime,
    finished_at: datetime,
) -> OutputEnvelope:
    """Assemble the output envelope for an agent_end report append."""
    from eawf.surfaces.render.envelope import EnvelopeFooter, EnvelopeHeader, OutputEnvelope

    body: dict[str, object] = {
        "event_type": event.event_type.value,
        "scope_id": event.scope_id,
        "runtime": event.runtime,
        "report_id": report_id,
        "store_kind": store_kind,
        "attempt": attempt,
        "persisted_store_record": store_urn,
        "blocked": False,
        "results": [],
    }
    header = EnvelopeHeader(
        skill="/audit",
        scope_id=event.scope_id or "urn:eawf:v1:state:hook-run",
        session="urn:eawf:v1:store:hook/sessions/SES-cli",
        started_at=started_at,
        finished_at=finished_at,
        status="ok",
        instrument_probe={},
    )
    footer = EnvelopeFooter(
        persisted_artifacts=[],
        persisted_store_records=[store_urn],
        state_mutations=[],
        evidence_refs=[store_urn],
        next_valid_actions=[],
        warnings=[],
        repair_commands=None,
    )
    return OutputEnvelope(header=header, body=body, footer=footer)


def _envelope_for_agent_output(
    *,
    run_key: str,
    scope_id: str,
    seq: int,
    event_id: str | None,
    store_urn: str | None,
    started_at: datetime,
    finished_at: datetime,
) -> OutputEnvelope:
    """Assemble the output envelope for a forwarded output-chunk ingest.

    Args:
        run_key: The native Run the chunk was forwarded for.
        scope_id: The scope the chunk was routed to.
        seq: The chunk index the forwarder supplied.
        event_id: Appended envelope id, or ``None`` when the forwarded text
            held nothing renderable.
        store_urn: Store URN of the appended row, or ``None`` for that no-op.
        started_at: When the command started.
        finished_at: When the append finished.

    Returns:
        The canonical output envelope, ``status="ok"``.
    """
    from eawf.surfaces.render.envelope import EnvelopeFooter, EnvelopeHeader, OutputEnvelope

    body: dict[str, object] = {
        "event_type": "agent_output",
        "scope_id": scope_id,
        "run_key": run_key,
        "seq": seq,
        "event_id": event_id,
        "persisted": event_id is not None,
        "persisted_store_record": store_urn,
        "blocked": False,
        "results": [],
    }
    header = EnvelopeHeader(
        skill="/audit",
        scope_id=scope_id or "urn:eawf:v1:state:hook-run",
        session="urn:eawf:v1:store:hook/sessions/SES-cli",
        started_at=started_at,
        finished_at=finished_at,
        status="ok",
        instrument_probe={},
    )
    footer = EnvelopeFooter(
        persisted_artifacts=[],
        persisted_store_records=[store_urn] if store_urn is not None else [],
        state_mutations=[],
        evidence_refs=[store_urn] if store_urn is not None else [],
        next_valid_actions=[],
        warnings=[],
        repair_commands=None,
    )
    return OutputEnvelope(header=header, body=body, footer=footer)


def _emit_envelope(env: OutputEnvelope) -> None:
    """Write the JSON envelope to stdout, newline-terminated."""
    raw = orjson.dumps(
        env.model_dump(mode="json"),
        option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
    )
    typer.echo(raw.decode("utf-8"))


def _runtime_default() -> HookRuntime:
    """Return the v1 default runtime label.

    Pulled into a helper so tests can monkeypatch the default cleanly.
    """
    return "generic"


def _seed_agent_end_verdict(
    *,
    event: HookEvent,
    payload: dict[str, Any],
    flags: GlobalFlags,
    started_at: datetime,
) -> None:
    """Seed one interim verdict row from an ``agent_end`` event and emit the envelope.

    Shared by ``eawf hook run agent_end`` and ``eawf hook dispatch
    --event-type agent_end``: both translate the same ``agent_end`` payload
    into a seeded verdict row through the library seeder
    :func:`eawf.workflow.dispatch.seed.seed_interim_verdict` (CLI stays
    dispatch-only; the library implements), filed as the native Run the
    payload names. All CLI-level failures surface as the canonical error
    envelope and the function returns without raising.
    """
    from eawf.runtime.daemon.dispatch_runner import native_run_author
    from eawf.surfaces.cli.scope import resolve_state_path
    from eawf.workflow.agent_report.store import (
        AgentReportRoleMismatchError,
        AgentReportScrubError,
    )
    from eawf.workflow.dispatch.seed import seed_interim_verdict

    try:
        agent_payload = _run_ref_model(AgentEndPayload).model_validate(payload)
        state_path = resolve_state_path(flags.workspace)
        result = seed_interim_verdict(
            author=native_run_author(state_path.parent, agent_payload.run_ref),
            state_path=state_path,
            base_id=agent_payload.base_id,
            body=agent_payload.body,
            runtime=event.runtime,
            generated_at=started_at,
            artifact_ids=agent_payload.artifact_ids,
            blob_refs=agent_payload.blob_refs,
        )
    except ValidationError as err:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"agent_end payload rejected: {err.errors()[0]['msg']}", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    except KeyError as err:
        cli_errors.emit_error(cli_errors.UserError(str(err), kind="NotFound"), flags=flags)
        return
    except (AgentReportRoleMismatchError, AgentReportScrubError) as err:
        cli_errors.emit_error(cli_errors.ValidationError(str(err)), flags=flags)
        return
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    finished_at = datetime.now(UTC)
    envelope = _envelope_for_agent_report(
        event=event,
        report_id=result.envelope.id,
        store_kind=result.store_kind,
        attempt=result.attempt,
        store_urn=result.urn,
        started_at=started_at,
        finished_at=finished_at,
    )
    _emit_envelope(envelope)


# --- Diff-scoped lint gates (hooks 16-19 per C09 §5.3) ---------------------
# Each gate is a thin CLI dispatcher: it resolves the files to inspect
# (explicit args, else the conditional diff scan) and delegates the
# actual detection to a library surface (the shared pattern set in
# ``eawf.observability.logging.state_leak`` for leaks, the EAWF001 rule for log
# format, ``plugin doctor --strict`` for plugin drift). The conditional
# diff scan keeps each gate a no-op when nothing relevant changed.

# Suffixes the leak gates skip — binary-ish blobs are not text-scanned.
_BINARY_SUFFIXES: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".whl"}
)

# Inline marker that exempts a single line from both the path gate and the
# email gate, for a by-design fixture or pattern definition (a scrub-test
# sample path, the scrubber's own regex source), without a brittle per-file
# path exclude list. It is distinct from the ``detect-secrets`` allowlist
# pragma so that a leak exemption never also silences the secret scan, and
# a secret exemption never also silences the leak gates.
_LEAK_ALLOW_MARKER = "eawf: allow-leak"


# Files whose lines are JSON text, so every string in them is escaped.
_JSON_SUFFIXES = (".json", ".jsonl")

# A JSON string escape the leak scan must read as the text it stands for.
_JSON_ESCAPE = re.compile(r"\\\\|\\[ntr]")
_GENERATION_LEDGER = re.compile(r"\.ea/generations/[^/]+/(?:ledger|history)/[^/]+\.jsonl")


def _unescape_json(match: re.Match[str]) -> str:
    """Return a backslash for an escaped backslash and a space for an escaped break."""
    return "\\" if match.group(0) == "\\\\" else " "


def _line_is_allowlisted(line: str) -> bool:
    """Return ``True`` when ``line`` carries the inline leak-allow marker."""
    return _LEAK_ALLOW_MARKER in line


@dataclass(frozen=True)
class LeakFinding:
    """One path/email leak hit inside a scanned file.

    Attributes:
        path: Repo-relative file path the hit was found in.
        lineno: 1-based line number of the hit.
        snippet: The matched substring (already a sensitive literal —
            surfaced verbatim so the operator can locate and scrub it).
    """

    path: str
    lineno: int
    snippet: str

    def render(self) -> str:
        """Return a ``path:line: snippet`` one-liner."""
        return f"{self.path}:{self.lineno}: {self.snippet}"


def _read_text_lines(path: Path) -> list[str] | None:
    """Return ``path``'s text lines, or ``None`` for unreadable/binary files.

    Files with a binary-ish suffix or that fail UTF-8 decode are
    skipped (return ``None``) so the leak scan never chokes on a blob.
    """
    if path.suffix.lower() in _BINARY_SUFFIXES:
        return None
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError, UnicodeDecodeError:
        return None


def _scan_path_leaks(paths: list[str], *, cwd: Path, diff_scoped: bool) -> list[LeakFinding]:
    """Return home-directory path-literal findings across ``paths``.

    On the state file the gate also reports credential-shaped tokens:
    detect-secrets is excluded from that file, so no other gate would.
    """
    from eawf.observability.logging.state_leak import StateLeakKind

    return _scan_leaks(
        paths,
        cwd=cwd,
        diff_scoped=diff_scoped,
        kinds=frozenset({StateLeakKind.HOME_PATH}),
        state_kinds=frozenset({StateLeakKind.HOME_PATH, StateLeakKind.TOKEN}),
    )


def _scan_email_leaks(paths: list[str], *, cwd: Path, diff_scoped: bool) -> list[LeakFinding]:
    """Return non-allowlisted email findings across ``paths``."""
    from eawf.observability.logging.state_leak import StateLeakKind

    kinds = frozenset({StateLeakKind.EMAIL})
    return _scan_leaks(paths, cwd=cwd, diff_scoped=diff_scoped, kinds=kinds, state_kinds=kinds)


def _scan_leaks(
    paths: list[str],
    *,
    cwd: Path,
    diff_scoped: bool,
    kinds: frozenset[StateLeakKind],
    state_kinds: frozenset[StateLeakKind],
) -> list[LeakFinding]:
    """Return the shared-pattern-set hits of the wanted kinds across ``paths``.

    Args:
        paths: Repo-relative (or absolute) files to scan.
        cwd: Repository root the paths resolve against.
        diff_scoped: ``True`` when ``paths`` came from the staged scan rather
            than from explicit arguments.
        kinds: Hit kinds reported for an ordinary file.
        state_kinds: Hit kinds reported for the state file.

    Returns:
        One finding per hit, in file, line and pattern order. On a
        diff-scoped scan the state file and each generation ledger contribute
        only the lines the staged diff adds: their older rows predate this
        scan, so a whole-file pass would re-flag text the commit never
        touched. When the staged diff cannot be read they are scanned in full.
    """
    from eawf.observability.logging.state_leak import default_allowed_emails, scan_state_leaks

    allowed = default_allowed_emails()
    findings: list[LeakFinding] = []
    for rel in paths:
        lines = _read_text_lines(cwd / rel)
        if lines is None:
            continue
        is_state = _is_state_bookkeeping_path(rel)
        wanted = state_kinds if is_state else kinds
        line_scoped = is_state or _is_generation_ledger_path(rel)
        added = _staged_added_line_candidates(rel, cwd=cwd) if line_scoped and diff_scoped else None
        for lineno, line in enumerate(lines, start=1):
            if added is not None and lineno not in added:
                continue
            if _line_is_allowlisted(line):
                continue
            # JSON escapes a backslash as two, which would hide a Windows home
            # path from its single-backslash anchor, and spells a line break as
            # backslash-n, which would glue the next word onto an "@" after it.
            text = _JSON_ESCAPE.sub(_unescape_json, line) if rel.endswith(_JSON_SUFFIXES) else line
            findings.extend(
                LeakFinding(path=rel, lineno=lineno, snippet=hit.snippet)
                for hit in scan_state_leaks(text, allowed_emails=allowed)
                if hit.kind in wanted
            )
    return findings


def _resolve_scan_paths(
    files: list[str] | None,
    *,
    hook_name: str,
    base: str,
    cwd: Path,
) -> list[str]:
    """Resolve the repo-relative paths a pre-commit gate should inspect.

    Explicit ``files`` (passed by a test) win; when none are supplied
    the gate falls back to the conditional **staged** scan via
    :func:`eawf.platform.lint._conditional.relevant_for_hook` (``staged=True``),
    which yields only the staged files relevant to ``hook_name``. The
    staged scope makes a real ``git commit`` scan only its delta and
    ``pre-commit run --all-files`` (nothing staged) a clean no-op
    rather than re-scanning the whole tree. The early-exit signal is an
    empty list.
    """
    from eawf.platform.lint._conditional import relevant_for_hook

    if files:
        return files
    candidates = relevant_for_hook(hook_name, base, cwd=cwd, staged=True)
    relocated = _pure_relocation_destinations(cwd=cwd)
    return [p for p in candidates if p not in relocated]


_PURE_RENAME_STATUS_RE = re.compile(r"^R100\t(?P<old>.+)\t(?P<new>.+)$")


def _pure_relocation_destinations(*, cwd: Path) -> frozenset[str]:
    """Return staged destinations of content-identical relocations (``R100``).

    A pure ``git mv`` carries a file's already-committed body to a new path
    without changing a byte, so re-linting that body would re-flag pre-existing
    findings the relocation neither introduced nor can fix in scope. ``git``'s
    ``R100`` similarity score marks exactly these moves; a move that also edits
    the body scores below 100 and stays in scope so its edited lines are linted.
    """
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-status", "--find-renames"],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        **no_window_kwargs(),
    )
    if proc.returncode != 0:
        return frozenset()
    return frozenset(
        match.group("new")
        for line in proc.stdout.splitlines()
        if (match := _PURE_RENAME_STATUS_RE.match(line)) is not None
    )


_DIFF_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
_RENAME_STATUS_RE = re.compile(r"^R\d*\t(?P<old>.+)\t(?P<new>.+)$")


def _staged_diff_pathspec(rel: str, *, cwd: Path) -> list[str]:
    """Return the pathspec to diff ``rel`` with so rename detection can fire.

    ``git`` only resolves a rename when both endpoints are inside the diff
    pathspec. A relocated artifact's old path is *not* ``rel``, so scoping the
    diff to ``rel`` alone hides the source and ``-M`` reports the whole file as
    added. When ``rel`` is the destination of a staged rename, this widens the
    pathspec to include the source so ``-M`` pairs them; otherwise ``rel``
    alone (an in-place edit or fresh add) suffices.
    """
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-status", "--find-renames"],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        **no_window_kwargs(),
    )
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            match = _RENAME_STATUS_RE.match(line)
            if match is not None and match.group("new") == rel:
                return [match.group("old"), rel]
    return [rel]


def _staged_added_line_candidates(rel: str, *, cwd: Path) -> set[int] | None:
    """Return 1-based new-file lines added by the staged diff.

    ``--find-renames`` (``-M``) keeps a relocation from masquerading as a brand
    new file: without it ``git diff --cached`` reports every line of a moved
    artifact as freshly added, so a pure ``git mv`` would re-expose the entire
    pre-existing body to the diff-scoped chassis lints. With rename detection a
    pure move yields zero added-line candidates and a move-with-edit surfaces
    only the lines the edit actually touched.

    Args:
        rel: The repository-relative path of the staged file.
        cwd: Repository root the diff runs in.

    Returns:
        The added line numbers, an empty set when the diff adds none, or
        ``None`` when ``git diff`` fails. ``None`` means the added lines are
        unknown, so a caller scans the whole file: reading an empty set there
        would skip every line and let a failed diff pass the gate.
    """
    pathspec = _staged_diff_pathspec(rel, cwd=cwd)
    proc = subprocess.run(
        ["git", "diff", "--cached", "--unified=0", "--find-renames", "--", *pathspec],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        **no_window_kwargs(),
    )
    if proc.returncode != 0:
        return None
    candidates: set[int] = set()
    new_lineno: int | None = None
    for line in proc.stdout.splitlines():
        hunk = _DIFF_HUNK_RE.match(line)
        if hunk is not None:
            new_lineno = int(hunk.group("start"))
            continue
        if new_lineno is None or line.startswith(("diff --git", "index ", "--- ", "+++ ")):
            continue
        if line.startswith("+"):
            candidates.add(new_lineno)
            new_lineno += 1
            continue
        if line.startswith("-") or line.startswith("\\"):
            continue
        new_lineno += 1
    return candidates


def _is_state_bookkeeping_path(rel: str) -> bool:
    """Return ``True`` for the state file, whose leak scan is limited to added lines.

    Only ``.ea/state.json`` gets the narrower scan. Its line offsets churn on
    every mutation and its older rows predate the scan, so the leak gates read
    only the lines a commit adds to it. It is not exempt: its free-text fields
    (backlog titles, outcomes, rule prose) take agent and operator text, and an
    exemption once let a worktree path reach a public commit through exactly
    those fields. Its documented placeholders (``/Users/<name>`` in a rule that
    explains the leak lint) pass through the shared placeholder skip instead.

    The typed stores under ``.ea/store/`` are scanned in full. They were exempt
    once, on the premise that a daemon-written file cannot carry user secrets;
    that premise held until a store began carrying the raw stdout of spawned
    agents, at which point the exemption made the one file that could leak the
    one file nobody scanned.
    """
    return rel.replace("\\", "/") == ".ea/state.json"


def _is_generation_ledger_path(rel: str) -> bool:
    """Return ``True`` for an append-only ledger or history file of a generation.

    A committed line of these files can never change, so a diff-scoped scan
    reads only the lines a commit adds to them: a whole-file pass would let a
    row committed before a leak pattern widened fail every later commit that
    stages the file. They keep the full leak kinds, and the typed stores under
    ``.ea/store/`` stay scanned in full.

    Args:
        rel: The repository-relative path of a scanned file.

    Returns:
        Whether *rel* is a ``ledger/`` or ``history/`` JSONL file of a generation.
    """
    return _GENERATION_LEDGER.fullmatch(rel.replace("\\", "/")) is not None


def _is_generated_markdown_fixture_path(rel: str) -> bool:
    """Return ``True`` for generated markdown fixtures checked by golden tests.

    Every ``.md`` under ``tests/golden/`` is byte-pinned renderer output
    (agents.md variants, agent reports, plugin-install trees, subagent
    specs, plan-view fixtures, wave narrative bodies) compared verbatim
    by its golden test — never prose an operator authored by hand. Both
    the strict prose gate and the branch-delta test that mirrors it skip
    this whole subtree rather than the narrower ``agents_md/`` prefix.
    """
    return rel.replace("\\", "/").startswith("tests/golden/")


def _emit_leak_result(
    *,
    hook_name: str,
    findings: list[LeakFinding],
    scanned: int,
    flags: GlobalFlags,
) -> None:
    """Emit the leak-gate result and exit 1 when findings are present."""
    payload: dict[str, object] = {
        "hook": hook_name,
        "scanned": scanned,
        "clean": not findings,
        "findings": [{"path": f.path, "lineno": f.lineno, "snippet": f.snippet} for f in findings],
    }
    if findings:
        body_lines = [f"{hook_name}: {len(findings)} leak(s) across {scanned} file(s)"]
        body_lines.extend(f"  {f.render()}" for f in findings)
        emit_json_or_text(payload, "\n".join(body_lines), flags=flags)
        raise typer.Exit(exit_codes.USER_ERROR)
    emit_json_or_text(payload, f"{hook_name}: clean ({scanned} file(s) scanned)", flags=flags)


_STALENESS_HOOK_NAME = "rules.projection_staleness"


def _register_projection_staleness(runner: HookRunner, *, repo_root: Path) -> None:
    """Register the session-start rule-projection staleness check on *runner*.

    Args:
        runner: The runner dispatching this event.
        repo_root: The repository whose projections are checked.
    """
    from eawf.runtime.hooks.event import HookEventType
    from eawf.runtime.hooks.runner import HookResult

    def _hook(event: HookEvent) -> HookResult:
        from eawf.platform.rules.staleness import report_projection_staleness_once

        payload = event.payloads.get(HookEventType.SESSION_START.value, {})
        raw_session = payload.get("session_id")
        session_id = raw_session if isinstance(raw_session, str) and raw_session else None
        report = report_projection_staleness_once(repo_root, session_id=session_id)
        return HookResult(name=_STALENESS_HOOK_NAME, output=report or "")

    runner.register(HookEventType.SESSION_START, _hook, name=_STALENESS_HOOK_NAME)


_FAILURE_WARNING_HOOK_NAME = "hooks.failure_warning"


def _register_failure_warning(runner: HookRunner, *, ea_dir: Path) -> None:
    """Register the session-start warning about hook failures recorded since the last one.

    Registered after the other session-start hooks have been, it reads the
    record before this hook's own failures are appended, so those are
    reported by the next session start.

    Args:
        runner: The runner dispatching this event.
        ea_dir: The main checkout's ``.ea``, where the record is kept.
    """
    from eawf.runtime.hooks.event import HookEventType
    from eawf.runtime.hooks.health import take_failure_warning
    from eawf.runtime.hooks.runner import HookResult

    def _hook(_event: HookEvent) -> HookResult:
        warning = take_failure_warning(ea_dir)
        return HookResult(name=_FAILURE_WARNING_HOOK_NAME, output=warning or "")

    runner.register(HookEventType.SESSION_START, _hook, name=_FAILURE_WARNING_HOOK_NAME)


def _register_daemon_start(runner: HookRunner, *, context: HookContext, named: bool) -> None:
    """Register the background daemon start of a session in a managed repository.

    A launch that fails raises inside the handler, so the runner records it
    as one health row and the hook still answers as if nothing happened.

    Args:
        runner: The runner dispatching this event.
        context: Where the hook fired; nothing is registered outside a
            managed repository.
        named: Whether ``-w`` named the repository, as the host wrapper always
            does after its own probe. A bare ``eawf hook run`` typed in some
            checkout starts nothing, so a daemon is only ever started for the
            repository a host session runs in.
    """
    if context.kind != "managed" or not named:
        return
    from eawf.runtime.hooks.daemon_start import DAEMON_START_HOOK, request_daemon_start
    from eawf.runtime.hooks.event import HookEventType
    from eawf.runtime.hooks.runner import HookResult

    def _hook(_event: HookEvent) -> HookResult:
        request_daemon_start(context.main)
        return HookResult(name=DAEMON_START_HOOK, output="")

    runner.register(HookEventType.SESSION_START, _hook, name=DAEMON_START_HOOK)


#: Runtimes whose session-start hook prints a host context document instead
#: of the result envelope: Claude and Codex read ``additionalContext`` from
#: it, and the OpenCode bridge lifts the same field out of it.
_SESSION_CONTEXT_RUNTIMES: frozenset[str] = frozenset({"claude", "codex", "opencode"})

#: The tool events whose hooks observe a host call and print nothing on stdout.
_SILENT_TOOL_EVENTS: frozenset[str] = frozenset(
    {"pre_tool_use", "post_tool_use", "post_tool_use_failure"}
)

#: The lifecycle events Claude reads stdout from only for fields eawf never
#: sets, so their hooks print nothing there either.
_SILENT_LIFECYCLE_EVENTS: frozenset[str] = frozenset(
    {"subagent_start", "subagent_stop", "pre_compact", "session_end"}
)


def _emit_session_context(results: list[HookResult], *, runtime: str) -> None:
    """Print the staleness reports and the Run's anchors as a session-start context document.

    Args:
        results: The session-start hook results; the projection staleness,
            plugin version and hook failure checks' non-empty output and the
            anchors a compaction boundary restated are printed, and nothing at
            all when there are none, so a current repository with no Run on
            the session adds no context.
        runtime: The runtime label. Claude also gets the hook failure warning
            as ``systemMessage``, the field it reserves for the operator;
            the context document carries it for the model on every runtime.
    """
    from eawf.runtime.hooks.host_lane import PLUGIN_VERSION_HOOK, host_context_restatement

    reported = {_STALENESS_HOOK_NAME, PLUGIN_VERSION_HOOK, _FAILURE_WARNING_HOOK_NAME}
    reports = [r.output for r in results if r.name in reported and r.output]
    restatement = host_context_restatement(results)
    if restatement is not None:
        reports.append(restatement)
    if not reports:
        return
    document: dict[str, Any] = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(reports),
        }
    }
    warning = next(
        (r.output for r in results if r.name == _FAILURE_WARNING_HOOK_NAME and r.output), None
    )
    if warning is not None and runtime == "claude":
        document["systemMessage"] = warning
    typer.echo(orjson.dumps(document).decode("utf-8"))


def _emit_permission_decision(results: list[HookResult]) -> None:
    """Print a principal's decision as the host's permission-hook answer.

    Claude reads ``hookSpecificOutput.decision.behavior`` (``allow`` or
    ``deny``) from a ``PermissionRequest`` hook's stdout. Nothing is printed
    when no decision arrived in time, which leaves the call to the host's own
    prompt rather than denying it.

    Args:
        results: The permission-request hook results.
    """
    from eawf.runtime.hooks.host_calls import HOST_PERMISSION_BEHAVIOR, host_permission_decision

    decision = host_permission_decision(results)
    if decision is None:
        return
    verdict: dict[str, str] = {"behavior": HOST_PERMISSION_BEHAVIOR[decision]}
    if decision == "denied":
        verdict["message"] = "A principal denied this call in Eä."
    document = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": verdict}}
    typer.echo(orjson.dumps(document).decode("utf-8"))


def _emit_skill_refusal(event: HookEvent, reason: str) -> None:
    """Print the host's refusal of a skill invocation, so the skill never starts.

    Claude reads ``decision: block`` from a prompt-submit hook and drops the
    prompt before the model sees it; a skill-tool call is refused through the
    pre-tool hook's deny.

    Args:
        event: The USER_PROMPT_SUBMIT or PRE_TOOL_USE event invoking the skill.
        reason: Why the invocation is refused, shown to whoever invoked it.
    """
    from eawf.runtime.hooks.event import HookEventType

    document: dict[str, Any] = (
        {"decision": "block", "reason": reason}
        if event.event_type == HookEventType.USER_PROMPT_SUBMIT
        else {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )
    typer.echo(orjson.dumps(document).decode("utf-8"))


def _emitted_skill_refusal(event: HookEvent, results: list[HookResult]) -> bool:
    """Print the refusal a blocked skill invocation owes the host, if one was blocked.

    Args:
        event: The dispatched event.
        results: What its hooks returned.

    Returns:
        Whether a refusal was printed.
    """
    from eawf.runtime.hooks.host_lane import HOST_SKILL_HOOK

    refusal = next((r for r in results if r.name == HOST_SKILL_HOOK and r.block), None)
    if refusal is None or event.runtime != "claude":
        return False
    _emit_skill_refusal(event, refusal.output)
    return True


def _payload_cwd(stdin_text: str) -> Path | None:
    """Return the ``cwd`` the host reported in its payload, if it reported one.

    Args:
        stdin_text: The hook payload.

    Returns:
        The ``cwd`` field as a path; ``None`` when the payload is malformed
        or carries no non-empty string there, since the dispatch that follows
        names a malformed payload itself.
    """
    try:
        cwd = _parse_payload(stdin_text).get("cwd")
    except cli_errors.CliError:
        return None
    return Path(cwd) if isinstance(cwd, str) and cwd else None


def _hook_context(stdin_text: str, flags: GlobalFlags) -> HookContext:
    """Return the repository context the hook fired in, resolved once per hook.

    ``-w`` comes first: the wrapper passes it only after its own probe found
    the session's directory managed, and an operator names it on purpose.
    Then the host's reported ``cwd``, then the process's own directory; each
    answers for its main checkout, and outside git for the first directory
    upward holding ``.ea/``. The guard judges a call against the payload
    ``cwd`` itself, never against this context's tree.

    Args:
        stdin_text: The hook payload.
        flags: The global flags.

    Returns:
        The resolved context.
    """
    from eawf.runtime.hooks.context import resolve_hook_context

    cwd = flags.workspace or _payload_cwd(stdin_text) or Path.cwd()
    return resolve_hook_context(cwd.resolve())


def _stood_down(context: HookContext, event_type: str, *, named: bool) -> bool:
    """Stand the hook down outside a repository eawf manages.

    Such a repository has nothing to protect or record, so a hook the host
    fired there does nothing and says nothing. A ``-w`` directory was named on
    purpose, by the wrapper after its own probe or by an operator, so it is
    not second-guessed. A tree in the eawf 0.8 layout says so once, at session
    start.

    Args:
        context: Where the hook fired.
        event_type: The event argument as given.
        named: Whether ``-w`` named the repository.

    Returns:
        Whether the hook stood down and must stop here.
    """
    from eawf.kernel.migration.epoch2.canary import V08_LAYOUT_MESSAGE, uses_v08_layout
    from eawf.runtime.hooks.event import HookEventType
    from eawf.workflow.skills.publication import is_managed

    if not named and not is_managed(context.main):
        return True
    if uses_v08_layout(context.main / ".ea"):
        if event_type == HookEventType.SESSION_START.value:
            typer.echo(V08_LAYOUT_MESSAGE)
        return True
    return False


def _emitted_host_answer(
    event: HookEvent, results: list[HookResult], failed: frozenset[str]
) -> bool:
    """Print the host-shaped answer an event owes instead of the envelope, if it owes one.

    Args:
        event: The dispatched event.
        results: What its hooks returned.
        failed: The hooks whose failure was already named on stderr.

    Returns:
        Whether an answer was printed, so the envelope must not be.
    """
    from eawf.runtime.hooks.event import HookEventType

    if (
        event.event_type == HookEventType.SESSION_START
        and event.runtime in _SESSION_CONTEXT_RUNTIMES
    ):
        _emit_session_context(results, runtime=event.runtime)
        return True
    if event.event_type == HookEventType.PERMISSION_REQUEST and event.runtime == "claude":
        _emit_permission_decision(results)
        return True
    if _emitted_skill_refusal(event, results):
        return True
    if event.event_type == HookEventType.USER_PROMPT_SUBMIT and event.runtime == "claude":
        # Claude adds a prompt hook's stdout to the prompt; an admitted one adds nothing.
        return True
    silent = _SILENT_TOOL_EVENTS | _SILENT_LIFECYCLE_EVENTS
    if event.event_type.value in silent and event.runtime == "claude":
        # Claude Code reads a tool hook's stdout as a decision about the call; an
        # observer has none to give, so what it did goes to stderr alone. A hook
        # that skipped the call did nothing, and on every tool call of a session
        # with no Run its line would only be noise in the host's log. A hook
        # that failed already has its one failure line there.
        for result in results:
            if result.name in failed or result.output.startswith(f"{result.name} skipped:"):
                continue
            typer.echo(result.output, err=True)
        return True
    return False


def _guarded_pre_tool_use(
    event_type: str, runtime: str, stdin_text: str, context: HookContext, started_at: datetime
) -> bool:
    """Run the data-loss guard on a pre-tool call; print the host's deny when it refuses.

    The guard runs ahead of every other hook and of the managed-repository stand-down,
    because it reads no state; it judges only a call that reaches a repository eawf
    manages. A payload it cannot read passes with nothing printed. Codex subscribes its
    pre-tool hook for the guard alone, so on Codex an allowed call ends here too, with
    nothing printed.

    Args:
        event_type: The event argument as given.
        runtime: The runtime label, lowercased.
        stdin_text: The hook payload.
        context: The hook's repository context.
        started_at: When the hook started.

    Returns:
        Whether the command has answered the host and must stop here.
    """
    from eawf.runtime.hooks.event import HookEventType

    # Every other event leaves before the guard and its sandbox rules load.
    if event_type != HookEventType.PRE_TOOL_USE.value:
        return False

    from eawf.runtime.hooks.data_loss_guard import (
        DATA_LOSS_GUARD_HOOK,
        DENY_EMISSION_RUNTIMES,
        deny_document,
        guard_pre_tool_use,
    )
    from eawf.runtime.sandbox.data_loss import DataLossDenial

    if runtime not in DENY_EMISSION_RUNTIMES:
        return False
    denial: DataLossDenial | None
    try:
        event = _build_event(
            event_type=HookEventType.PRE_TOOL_USE,
            payload=_parse_payload(stdin_text),
            scope="",
            command="",
            runtime=cast("HookRuntime", runtime),
            occurred_at=started_at,
        )
    except cli_errors.CliError, ValidationError:
        # A payload the hook cannot read is a host fault, not a call to judge: it passes.
        return True
    else:
        denial = guard_pre_tool_use(event, context=context)
    if denial is None:
        return runtime == "codex"
    typer.echo(orjson.dumps(deny_document(denial)).decode("utf-8"))
    typer.echo(f"{DATA_LOSS_GUARD_HOOK} deny {denial.rule}: {denial.reason}", err=True)
    return True


def _unexpected_failures(failures: list[HookFailure], event: HookEvent) -> list[HookFailure]:
    """Drop the failures a session start expects: a daemon not up yet.

    Session start launches the daemon in the background, so a daemon call it
    makes before that daemon answers finds none running. Counting that would
    warn about every fresh session at the next one.

    Args:
        failures: What the runner caught, oldest first.
        event: The dispatched event.

    Returns:
        *failures* without a session start's daemon-not-running calls.
    """
    from eawf.runtime.hooks.event import HookEventType
    from eawf.runtime.hooks.health import DAEMON_CALL_FAILED

    if event.event_type != HookEventType.SESSION_START:
        return failures
    return [
        failure
        for failure in failures
        if not (failure.code == DAEMON_CALL_FAILED and failure.error == "DaemonNotRunningError")
    ]


def _report_failures(failures: list[HookFailure], *, ea_dir: Path, payload: dict[str, Any]) -> None:
    """Record each handler failure in the hook health record and name it on stderr.

    The hook still exits as its results say: a failure is reported, never
    turned into a non-zero exit the host would treat as a broken hook.

    Args:
        failures: What the runner caught, oldest first.
        ea_dir: The main checkout's ``.ea``, where the record is kept.
        payload: The host payload, whose ``session_id`` each row records.
    """
    if not failures:
        return
    from eawf.runtime.hooks.health import failure_line, record_failures

    session = payload.get("session_id")
    try:
        record_failures(
            ea_dir,
            failures,
            session_id=session if isinstance(session, str) and session else None,
        )
    except OSError as exc:
        typer.echo(f"eawf hook: health record not written ({type(exc).__name__})", err=True)
    for failure in failures:
        typer.echo(failure_line(failure), err=True)


@hook_app.command(name="run")
def run(
    ctx: typer.Context,
    event_type: Annotated[
        str,
        typer.Argument(
            help="HookEventType value (e.g., pre_commit, post_commit, "
            "session_start). See docs/hook-events.md.",
        ),
    ],
    runtime: Annotated[
        str,
        typer.Option(
            "--runtime",
            help="Runtime label recorded on the event (claude/opencode/generic).",
            case_sensitive=False,
        ),
    ] = _runtime_default(),
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Eä scope ID (wave/iter/phase) the event was raised inside.",
        ),
    ] = "",
    command: Annotated[
        str,
        typer.Option(
            "--command",
            help="Originating Eä CLI command string for the event record.",
        ),
    ] = "",
    target_epoch: Annotated[
        int | None,
        typer.Option(
            "--target-epoch",
            hidden=True,
            help="Accepted and ignored, so an older wrapper that still passes it keeps working.",
        ),
    ] = None,
) -> None:
    """Dispatch a hook event read from stdin and emit the result envelope."""
    from eawf.runtime.hooks.event import HookEventType
    from eawf.runtime.hooks.runner import HookRunner, register_runtime_capture_hooks

    del target_epoch
    flags: GlobalFlags = ctx.obj
    started_at = datetime.now(UTC)
    # Skip stdin read on a TTY so interactive smoke runs don't block
    # waiting for EOF; piped/redirected stdin reads normally.
    stdin_text = "" if sys.stdin.isatty() else sys.stdin.read()
    context = _hook_context(stdin_text, flags)
    if _guarded_pre_tool_use(event_type, runtime.lower(), stdin_text, context, started_at):
        return
    if _stood_down(context, event_type, named=flags.workspace is not None):
        return

    if runtime.lower() not in {"claude", "codex", "opencode", "generic"}:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"--runtime must be one of claude/codex/opencode/generic; got {runtime!r}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return

    try:
        resolved_event_type = _parse_event_type(event_type)
        payload = _parse_payload(stdin_text)
        event = _build_event(
            event_type=resolved_event_type,
            payload=payload,
            scope=scope,
            command=command,
            runtime=cast("HookRuntime", runtime.lower()),
            occurred_at=started_at,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    except ValidationError as err:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"event payload rejected: {err.errors()[0]['msg']}", kind="InvalidInput"
            ),
            flags=flags,
        )
        return

    if event.event_type == HookEventType.AGENT_END:
        _seed_agent_end_verdict(event=event, payload=payload, flags=flags, started_at=started_at)
        return

    runner = HookRunner()
    repo_root = flags.workspace.resolve() if flags.workspace else context.main
    register_runtime_capture_hooks(runner, repo_root=repo_root)
    _register_projection_staleness(runner, repo_root=repo_root)
    _register_failure_warning(runner, ea_dir=context.main / ".ea")
    _register_daemon_start(runner, context=context, named=flags.workspace is not None)
    results = runner.run_event(event)
    failures = _unexpected_failures(runner.failures, event)
    _report_failures(failures, ea_dir=context.main / ".ea", payload=payload)

    failed = frozenset(failure.hook for failure in failures)
    if _emitted_host_answer(event, results, failed):
        return

    finished_at = datetime.now(UTC)
    envelope = _envelope_for(
        event=event,
        results=results,
        started_at=started_at,
        finished_at=finished_at,
    )
    _emit_envelope(envelope)
    if any(r.block for r in results):
        raise typer.Exit(exit_codes.STATE_CONFLICT)


@hook_app.command(name="dispatch")
def dispatch(
    ctx: typer.Context,
    event_type: Annotated[
        str,
        typer.Option(
            "--event-type",
            help="Hook event to dispatch. Only 'agent_end' is supported on this "
            "surface (the interim verdict seeder).",
        ),
    ],
    runtime: Annotated[
        str,
        typer.Option(
            "--runtime",
            help="Runtime label recorded on the seeded report (claude/codex/opencode/generic).",
            case_sensitive=False,
        ),
    ] = _runtime_default(),
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Eä scope ID (wave/iter/phase) the event was raised inside.",
        ),
    ] = "",
    command: Annotated[
        str,
        typer.Option(
            "--command",
            help="Originating Eä CLI command string for the event record.",
        ),
    ] = "",
) -> None:
    """Seed an interim verdict cohort from an ``agent_end`` event read from stdin.

    The interim / manual verdict producer: it translates one ``agent_end``
    hook event (payload on stdin) into a single seeded verdict row, appended
    through the canonical agent-report writer so the self-eval + jury surfaces
    read a primed cohort before the live per-wave verdict producer lands. The
    seeded row reuses the session named in the payload as authority and is
    indistinguishable from a live-produced row. Append targets the per-role
    store JSONL only — no ``state.json`` mutation.

    Only ``--event-type agent_end`` is accepted; any other event type is
    rejected with exit 1 (``InvalidInput``) because this surface is the
    verdict seeder, not the general hook dispatcher (use ``eawf hook run``).
    """
    from eawf.runtime.hooks.event import HookEventType

    flags: GlobalFlags = ctx.obj
    started_at = datetime.now(UTC)

    if runtime.lower() not in {"claude", "codex", "opencode", "generic"}:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"--runtime must be one of claude/codex/opencode/generic; got {runtime!r}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return

    try:
        resolved_event_type = _parse_event_type(event_type)
        stdin_text = "" if sys.stdin.isatty() else sys.stdin.read()
        payload = _parse_payload(stdin_text)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    if resolved_event_type != HookEventType.AGENT_END:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"hook dispatch seeds verdicts from agent_end only; got "
                f"{resolved_event_type.value!r} (use eawf hook run for other events)",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return

    event = _build_event(
        event_type=resolved_event_type,
        payload=payload,
        scope=scope,
        command=command,
        runtime=cast("HookRuntime", runtime.lower()),
        occurred_at=started_at,
    )
    _seed_agent_end_verdict(event=event, payload=payload, flags=flags, started_at=started_at)


@hook_app.command(name="agent-output")
def agent_output(ctx: typer.Context) -> None:
    """Ingest one output chunk forwarded for an externally dispatched Run.

    The streaming sibling of ``eawf hook dispatch --event-type agent_end``.
    The watch surface reads persisted ``agent.output.chunk`` rows, whose only
    other producer is the daemon piping a runtime it spawned itself; a session
    an external orchestrator spawned therefore shows a correct row with no
    stream behind it. That orchestrator forwards each output batch here as JSON
    on stdin (``{"run_ref": ..., "seq": ..., "text": ...}``) and it lands as
    the same chunk row the spawn path writes, so the row and its stream agree.

    Routing is resolved from the named Run (its scope), never from the
    forwarder, so a forwarded chunk cannot land in a scope the watch reader
    never reads.

    Exits ``1`` when the stdin envelope is malformed (``InvalidInput``) or the
    Run cannot be resolved (``NotFound``); nothing is persisted in either
    case. Appends to the event store only; ``state.json`` is never read.
    """
    from eawf.kernel.state.enums import StoreKind
    from eawf.kernel.state.urn import build as build_urn
    from eawf.kernel.store.paths import store_path
    from eawf.runtime.daemon.dispatch_runner import persist_forwarded_output_chunk
    from eawf.surfaces.cli.scope import resolve_state_path

    flags: GlobalFlags = ctx.obj
    started_at = datetime.now(UTC)

    try:
        stdin_text = "" if sys.stdin.isatty() else sys.stdin.read()
        chunk = _run_ref_model(AgentOutputPayload).model_validate(_parse_payload(stdin_text))
        state_path = resolve_state_path(flags.workspace)
        result = persist_forwarded_output_chunk(
            store_path(state_path, StoreKind.EVENT),
            tree_root=state_path.parent,
            run_ref=chunk.run_ref,
            seq=chunk.seq,
            text=chunk.text,
        )
    except ValidationError as err:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"agent output payload rejected: {err.errors()[0]['msg']}", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    except KeyError as err:
        cli_errors.emit_error(cli_errors.UserError(str(err), kind="NotFound"), flags=flags)
        return
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    store_urn = (
        build_urn("store", owner=result.scope_id, id=f"event/{result.envelope_id}")
        if result.envelope_id is not None
        else None
    )
    _emit_envelope(
        _envelope_for_agent_output(
            run_key=result.run_key,
            scope_id=result.scope_id,
            seq=chunk.seq,
            event_id=result.envelope_id,
            store_urn=store_urn,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
    )


_FilesArg = Annotated[
    list[str] | None,
    typer.Argument(
        help="Files to scan. When omitted, the conditional diff scan "
        "(git diff <base>...HEAD) selects only the relevant changed files.",
    ),
]
_BaseOpt = Annotated[
    str,
    typer.Option(
        "--base",
        help="Diff base ref for the conditional scan (default origin/main).",
    ),
]


@hook_app.command(name="path-leak-lint")
def path_leak_lint(
    ctx: typer.Context,
    files: _FilesArg = None,
    base: _BaseOpt = "origin/main",
) -> None:
    """Reject home-directory path literals (macOS, Windows, and Linux home roots).

    Scans the given files (or, when none are given, only the staged
    files relevant to this gate per the conditional scan). A staged
    ``.ea/state.json`` is scanned only on the lines the commit adds,
    for credential-shaped tokens as well as home paths. A line
    carrying the ``eawf: allow-leak`` marker is exempt (for
    by-design fixtures / pattern source). Exits 1 when a leak is found,
    0 on a clean scan.
    """
    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = _resolve_scan_paths(files, hook_name="path-leak-lint", base=base, cwd=cwd)
    findings = _scan_path_leaks(paths, cwd=cwd, diff_scoped=not files)
    _emit_leak_result(
        hook_name="path-leak-lint", findings=findings, scanned=len(paths), flags=flags
    )


@hook_app.command(name="email-leak-lint")
def email_leak_lint(
    ctx: typer.Context,
    files: _FilesArg = None,
    base: _BaseOpt = "origin/main",
) -> None:
    """Reject email addresses outside the canonical author/no-reply allowlist.

    Scans the given files (or, when none are given, only the changed
    files relevant to this gate per the conditional diff scan). The
    allowlist is the no-reply co-author addresses plus the
    ``pyproject.toml`` author rows. A staged ``.ea/state.json`` is
    scanned only on the lines the commit adds. Exits 1 on a leak, 0 on
    a clean scan.
    """
    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = _resolve_scan_paths(files, hook_name="email-leak-lint", base=base, cwd=cwd)
    findings = _scan_email_leaks(paths, cwd=cwd, diff_scoped=not files)
    _emit_leak_result(
        hook_name="email-leak-lint", findings=findings, scanned=len(paths), flags=flags
    )


@hook_app.command(name="log-format-lint")
def log_format_lint(
    ctx: typer.Context,
    files: _FilesArg = None,
    base: _BaseOpt = "origin/main",
) -> None:
    """Run the EAWF001 log-format rule over changed library modules.

    Scans the given Python files (or, when none are given, only the
    changed ``src/eawf/**/*.py`` modules per the conditional diff
    scan). Each ``logger.<level>(...)`` call site must match the
    canonical ``<funcname> key=value`` shape. Exits 1 when a violation
    is found, 0 when clean. Files that fail to parse are skipped (an
    authoring bug surfaced by ruff elsewhere).
    """
    from eawf.platform.lint.eawf001 import check_source

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = _resolve_scan_paths(files, hook_name="log-format-lint", base=base, cwd=cwd)
    rows, _scanned = _scan_python_rows(
        paths, cwd=cwd, check=lambda src, rel: check_source(src, filename=rel)
    )
    violation_count = len(rows)
    payload: dict[str, object] = {
        "hook": "log-format-lint",
        "scanned": len(paths),
        "clean": violation_count == 0,
        "violations": violation_count,
    }
    if violation_count:
        body = "\n".join(
            [f"log-format-lint: {violation_count} violation(s) across {len(paths)} file(s)", *rows]
        )
        emit_json_or_text(payload, body, flags=flags)
        raise typer.Exit(exit_codes.USER_ERROR)
    emit_json_or_text(
        payload, f"log-format-lint: clean ({len(paths)} file(s) scanned)", flags=flags
    )


def _emit_static_lint_result(
    *,
    hook_name: str,
    rows: list[str],
    scanned: int,
    flags: GlobalFlags,
    blocking: bool,
) -> None:
    """Emit a static lint result, optionally failing for blocking rules."""
    payload: dict[str, object] = {
        "hook": hook_name,
        "scanned": scanned,
        "clean": not rows,
        "violations": len(rows),
        "blocking": blocking,
    }
    if rows:
        noun = "violation" if blocking else "warning"
        body = "\n".join([f"{hook_name}: {len(rows)} {noun}(s) across {scanned} file(s)", *rows])
        emit_json_or_text(payload, body, flags=flags)
        if blocking:
            raise typer.Exit(exit_codes.USER_ERROR)
        return
    emit_json_or_text(payload, f"{hook_name}: clean ({scanned} file(s) scanned)", flags=flags)


class _Renderable(Protocol):
    """Minimal protocol every lint-violation dataclass already satisfies."""

    def render(self) -> str:  # pragma: no cover - structural protocol
        ...


def _scan_python_rows(
    paths: list[str],
    *,
    cwd: Path,
    check: Callable[[str, str], Iterable[_Renderable]],
    exclude: frozenset[str] = frozenset(),
) -> tuple[list[str], int]:
    """Run an AST-source ``check`` over the ``.py`` files in ``paths``.

    Shared loop for the source-lint dispatchers (EAWF002 / EAWF003 /
    EAWF010 / EAWF011 / EAWF012): each only differs in the per-file
    ``check`` callable (already closed over its threshold) and the
    optional grandfather ``exclude`` set.

    Non-``.py`` paths, excluded paths, and unreadable / non-UTF-8 files
    are skipped silently; a file whose ``check`` raises
    :class:`SyntaxError` is skipped too (the parse failure is an
    authoring bug surfaced by ruff elsewhere, not this gate's concern).

    Args:
        paths: Repo-relative candidate paths (already diff/arg-resolved).
        cwd: Repository working directory the paths are relative to.
        check: ``(source, rel) -> violations`` callable; each violation
            exposes ``render()``.
        exclude: Repo-relative paths (forward-slash normalized) exempt
            from the scan.

    Returns:
        ``(rows, scanned)`` — the rendered ``  rel:body`` row strings and
        the count of files actually scanned.
    """
    rows: list[str] = []
    scanned = 0
    for rel in paths:
        if not rel.endswith(".py"):
            continue
        if rel.replace("\\", "/") in exclude:
            continue
        try:
            source = (cwd / rel).read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        scanned += 1
        try:
            violations = check(source, rel)
        except SyntaxError:
            continue
        rows.extend(f"  {rel}:{violation.render()}" for violation in violations)
    return rows, scanned


@hook_app.command(name="eawf012-design-provenance")
def eawf012_design_provenance(
    ctx: typer.Context,
    files: _FilesArg = None,
    base: _BaseOpt = "origin/main",
) -> None:
    """Reject design/audit/agent provenance breadcrumbs in source comments."""
    from eawf.platform.lint.eawf012_design_provenance import check_source

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = _resolve_scan_paths(files, hook_name="eawf012-design-provenance", base=base, cwd=cwd)
    rows, scanned = _scan_python_rows(paths, cwd=cwd, check=lambda src, _rel: check_source(src))
    _emit_static_lint_result(
        hook_name="eawf012-design-provenance",
        rows=rows,
        scanned=scanned,
        flags=flags,
        blocking=True,
    )


@hook_app.command(name="eawf013-bracket-position")
def eawf013_bracket_position(
    ctx: typer.Context,
    files: _FilesArg = None,
    base: _BaseOpt = "origin/main",
) -> None:
    """Reject detached or post-punctuation numeric citation brackets."""
    from eawf.platform.lint.eawf013_bracket_position import check_source

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = _resolve_scan_paths(files, hook_name="eawf013-bracket-position", base=base, cwd=cwd)
    rows: list[str] = []
    scanned = 0
    for rel in paths:
        if not rel.endswith(".md"):
            continue
        target = cwd / rel
        try:
            source = target.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        scanned += 1
        rows.extend(f"  {rel}:{violation.render()}" for violation in check_source(source))
    _emit_static_lint_result(
        hook_name="eawf013-bracket-position",
        rows=rows,
        scanned=scanned,
        flags=flags,
        blocking=True,
    )


@hook_app.command(name="eawf014-no-manual-wrap")
def eawf014_no_manual_wrap(
    ctx: typer.Context,
    files: _FilesArg = None,
    base: _BaseOpt = "origin/main",
) -> None:
    """Reject manually wrapped rendered Markdown paragraphs."""
    from eawf.platform.lint.eawf014_no_manual_wrap import check_source

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = _resolve_scan_paths(files, hook_name="eawf014-no-manual-wrap", base=base, cwd=cwd)
    rows: list[str] = []
    scanned = 0
    diff_scoped = not files
    for rel in paths:
        if not rel.endswith(".md"):
            continue
        if _is_generated_markdown_fixture_path(rel):
            continue
        target = cwd / rel
        try:
            source = target.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        scanned += 1
        candidate_lines = _staged_added_line_candidates(rel, cwd=cwd) if diff_scoped else None
        rows.extend(
            f"  {rel}:{violation.render()}"
            for violation in check_source(source, candidate_lines=candidate_lines)
        )
    _emit_static_lint_result(
        hook_name="eawf014-no-manual-wrap",
        rows=rows,
        scanned=scanned,
        flags=flags,
        blocking=True,
    )


@hook_app.command(name="eawf015-ears-advisory")
def eawf015_ears_advisory(
    ctx: typer.Context,
    files: _FilesArg = None,
    base: _BaseOpt = "origin/main",
) -> None:
    """Warn on requirement-like prose outside EARS shape without blocking."""
    from eawf.platform.lint.eawf015_ears_advisory import check_source

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = _resolve_scan_paths(files, hook_name="eawf015-ears-advisory", base=base, cwd=cwd)
    rows: list[str] = []
    scanned = 0
    for rel in paths:
        if not rel.endswith(".md"):
            continue
        target = cwd / rel
        try:
            source = target.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        scanned += 1
        rows.extend(f"  {rel}:{violation.render()}" for violation in check_source(source))
    _emit_static_lint_result(
        hook_name="eawf015-ears-advisory",
        rows=rows,
        scanned=scanned,
        flags=flags,
        blocking=False,
    )


@hook_app.command(name="eawf018-structure-smell")
def eawf018_structure_smell(
    ctx: typer.Context,
    files: _FilesArg = None,
    base: _BaseOpt = "origin/main",
) -> None:
    """Warn on block-bloat structure smells in Markdown and docstrings without blocking.

    Runs the EAWF018 advisory over ``.md`` files (over-long prose block,
    run-on bullet list, over-long single bullet) and ``.py`` files
    (over-long docstring leading paragraph, lines joined). Thresholds come
    from the pyproject ``[tool.eawf.lint.eawf018]`` sub-table (clamped so a
    local override can only tighten the calibrated defaults). Advisory
    only: a finding emits a warning and exits 0, never failing the commit.
    A ``.py`` file that fails to parse is skipped (an authoring bug
    surfaced by ruff elsewhere).
    """
    from eawf.platform.lint import Eawf018Config
    from eawf.platform.lint.eawf018_structure_smell import check_docstrings, check_markdown

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    config = _resolve_lint_config(cwd)
    # No pyproject -> the dataclass defaults (the calibrated baselines).
    caps = config.eawf018 if config is not None else Eawf018Config()
    paths = _resolve_scan_paths(files, hook_name="eawf018-structure-smell", base=base, cwd=cwd)
    rows: list[str] = []
    scanned = 0
    for rel in paths:
        is_md = rel.endswith(".md")
        is_py = rel.endswith(".py")
        if not (is_md or is_py):
            continue
        try:
            source = (cwd / rel).read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        if is_md:
            scanned += 1
            findings = check_markdown(
                source,
                max_prose_chars=caps.max_prose_chars,
                max_bullet_run=caps.max_bullet_run,
                max_bullet_chars=caps.max_bullet_chars,
            )
        else:
            try:
                findings = check_docstrings(source, max_para_chars=caps.max_docstring_para_chars)
            except SyntaxError:
                continue
            scanned += 1
        rows.extend(f"  {rel}:{finding.render()}" for finding in findings)
    _emit_static_lint_result(
        hook_name="eawf018-structure-smell",
        rows=rows,
        scanned=scanned,
        flags=flags,
        blocking=False,
    )


@hook_app.command(name="sigil-totality")
def sigil_totality(ctx: typer.Context) -> None:
    """Assert every TUI-render status value resolves to a real ratified glyph.

    Sweeps every status enum the reskin renders (plus the lifecycle FSM
    terminals) and proves each member resolves through the single resolver
    :func:`~eawf.surfaces.tui.chassis.sigils.status_sigil` to a non-empty glyph
    that is neither the ``?`` fallthrough nor the bare ``.value`` word. The
    check is pure -- it sweeps a fixed enum surface, builds no widget, and
    mutates no state, so it takes no file arguments. It BLOCKS: a value that
    does not resolve to a real glyph emits the offending members and exits
    non-zero, so a dropped resolver row is caught at commit time rather than
    shipping a word where a glyph belongs.
    """
    from eawf.platform.lint.sigil_totality import check_sigil_totality

    flags: GlobalFlags = ctx.obj
    result = check_sigil_totality()
    payload: dict[str, object] = {
        "hook": "sigil-totality",
        "checked": result.checked,
        "clean": result.passed,
        "violations": len(result.misses),
        "blocking": True,
    }
    if result.passed:
        emit_json_or_text(payload, result.message, flags=flags)
        return
    body = "\n".join([result.message, *(f"  {miss}" for miss in result.misses)])
    emit_json_or_text(payload, body, flags=flags)
    raise typer.Exit(exit_codes.USER_ERROR)


def _git_tracked_artifacts(*, cwd: Path) -> list[str]:
    """Return git-tracked ``.ea/artifacts/**/*.md`` paths under ``cwd``.

    The EAWF023 placement gate narrows these to the committed artifacts and
    scans that whole set (not a staged delta) so a misplaced or
    date-stem-less artifact reds CI
    irrespective of which files the commit touched. A failed git
    invocation yields an empty list (fail-open) — the authoritative
    backstop is the same gate on the next clean run.
    """
    proc = subprocess.run(
        ["git", "ls-files", ".ea/artifacts/**/*.md", ".ea/artifacts/*.md"],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        **no_window_kwargs(),
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


@hook_app.command(name="eawf023-artifact-placement")
def eawf023_artifact_placement(
    ctx: typer.Context,
    files: _FilesArg = None,
) -> None:
    """Reject misplaced or date-stem-less artifacts under ``.ea/artifacts/``.

    Walks the committed ``.ea/artifacts/**/*.md`` set -- the git-tracked
    paths the commit policy keeps, which are the promoted artifacts --
    (or an explicit file list, for tests) and runs the EAWF023 placement
    rule: each artifact must live under its canonical kind sub-directory
    (``audits/``, ``research/``, ``incidents/``, ...) and its filename
    stem must lead with a ``YYYY-MM-DD-`` date prefix. The scan covers
    the whole committed set (not a staged delta) so a misplaced artifact
    reds CI regardless of which files the commit touched. Exits 1 on a
    violation, 0 when clean.
    """
    from eawf.platform.lint.eawf023_artifact_placement import (
        check_artifact_paths,
        committed_artifact_paths,
    )

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = files if files else committed_artifact_paths(_git_tracked_artifacts(cwd=cwd))
    violations = check_artifact_paths(paths)
    rows = [f"  {v.path}: {v.render()}" for v in violations]
    _emit_static_lint_result(
        hook_name="eawf023-artifact-placement",
        rows=rows,
        scanned=len(paths),
        flags=flags,
        blocking=True,
    )


@hook_app.command(name="eawf026-settings-categories")
def eawf026_settings_categories(ctx: typer.Context) -> None:
    """Reject a configuration catalog section the settings rail cannot reach.

    Runs the EAWF026 rule over the shipped leaf catalog and the settings
    category table: every catalog section must be filed under exactly one
    of the five orientation categories. Exits 1 naming each unfiled,
    doubly filed or phantom section and each unknown category, 0 when
    the assignment is total.
    """
    from eawf.platform.lint import eawf026_settings_categories as eawf026

    sections = eawf026.catalog_sections()
    defects = eawf026.category_assignment_defects(sections)
    _emit_static_lint_result(
        hook_name="eawf026-settings-categories",
        rows=[f"  {eawf026.RULE_CODE} {defect}" for defect in defects],
        scanned=len(sections),
        flags=ctx.obj,
        blocking=True,
    )


@hook_app.command(name="eawf027-citation-scope")
def eawf027_citation_scope(
    ctx: typer.Context,
    pr_body: Annotated[
        Path | None,
        typer.Option(
            "--pr-body",
            exists=True,
            dir_okay=False,
            help="A pull-request body to scan beside the tree.",
        ),
    ] = None,
) -> None:
    """Reject committed text quoting a reflection row it may not.

    Runs the EAWF027 citation-scope rule over every tracked artifact,
    rendered document and rule projection, the local proposal packet
    where it exists, and the pull-request body when one is named. Exits
    1 naming the file, line and mark of each non-quotable or unmarked
    row and each ``scrubbed_extract`` title, 0 when none is quoted.
    """
    from eawf.platform.lint import eawf027_citation_scope as eawf027

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = [*eawf027.committed_scope(cwd), *eawf027.packet_scope(cwd)]
    findings = eawf027.scan_paths(cwd, paths)
    if pr_body is not None:
        body = pr_body.read_text(encoding="utf-8")
        findings += eawf027.scan_text("pull-request body", body)
    _emit_static_lint_result(
        hook_name="eawf027-citation-scope",
        rows=[f"  {finding.render()}" for finding in findings],
        scanned=len(paths) + (pr_body is not None),
        flags=flags,
        blocking=True,
    )


@hook_app.command(name="eawf028-brief-immutable")
def eawf028_brief_immutable(ctx: typer.Context) -> None:
    """Reject a staged wording edit to a committed research brief.

    Runs the EAWF028 brief-immutability rule over every staged modified
    or renamed brief, compared with its ``HEAD`` version. Exits 1 naming
    each brief whose wording changed, the first changed words and the
    superseding-brief remediation, 0 when every edit repairs format only.
    """
    from eawf.platform.lint import eawf028_brief_immutable as eawf028

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    edits = eawf028.staged_edits(cwd)
    findings = [
        finding
        for old, new in edits
        if (finding := eawf028.check_brief(new, *eawf028.staged_texts(cwd, old, new))) is not None
    ]
    _emit_static_lint_result(
        hook_name="eawf028-brief-immutable",
        rows=[f"  {finding.render()}" for finding in findings],
        scanned=len(edits),
        flags=flags,
        blocking=True,
    )


def _git_tracked_tests(*, cwd: Path) -> list[str]:
    """Return git-tracked ``tests/**/*.py`` paths under ``cwd``.

    The EAWF024 test-tier gate scans the whole tracked test tree (not a
    staged delta) so a mislabeled or mis-tiered test reds CI regardless
    of which files the commit touched. A failed git invocation yields an
    empty list (fail-open) -- the authoritative backstop is the same gate
    on the next clean run.
    """
    proc = subprocess.run(
        ["git", "ls-files", "tests/**/*.py", "tests/*.py"],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        **no_window_kwargs(),
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


@hook_app.command(name="eawf024-test-tier-contract")
def eawf024_test_tier_contract(
    ctx: typer.Context,
    files: _FilesArg = None,
) -> None:
    """Reject non-unit imports in ``tests/unit/`` and mis-tiered kind markers.

    Walks the git-tracked ``tests/**/*.py`` set (or an explicit file list,
    for tests) and runs both EAWF024 rules: a unit-tier test must not
    import ``subprocess``, ``textual``, or ``CliRunner`` (each marks a
    slower integration/TUI test mislabeled into the unit tier), and a file
    under a ``tests/<kind>/`` directory must not declare a kind marker
    whose gate tier differs from its directory's. An offending line may
    carry a ``# noqa: EAWF024`` waiver for a deliberate fixture. The scan
    covers the whole tracked tree (not a staged delta) so a violation reds
    CI regardless of which files the commit touched. Exits 1 on a
    violation, 0 when clean.
    """
    from eawf.platform.lint.eawf024_test_tier_contract import (
        check_source,
        check_tier_ladder,
        is_unit_tier_path,
    )
    from eawf.platform.lint.kind_taxonomy import kind_for_test_path

    def check(src: str, rel: str) -> list[_Renderable]:
        # The import rule binds the unit tier only; the ladder binds every kind directory.
        imports = check_source(src, filename=rel) if is_unit_tier_path(rel) else []
        return [*imports, *check_tier_ladder(src, path=rel)]

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = files if files else _git_tracked_tests(cwd=cwd)
    tiered_paths = [
        rel for rel in paths if is_unit_tier_path(rel) or kind_for_test_path(rel) is not None
    ]
    rows, scanned = _scan_python_rows(tiered_paths, cwd=cwd, check=check)
    _emit_static_lint_result(
        hook_name="eawf024-test-tier-contract",
        rows=rows,
        scanned=scanned,
        flags=flags,
        blocking=True,
    )


def _staged_added_tests(*, cwd: Path) -> list[str]:
    """Return the ``tests/`` paths this commit ADDS, per the staged diff.

    EAWF025 is diff-scoped by design: the pre-taxonomy suite does not
    mirror the source layout, so a whole-tree scan would red every
    commit. Only newly added paths are held to the placement contract; a
    modified or renamed file keeps whatever address it already had. A
    failed git invocation yields an empty list (fail-open) -- the
    authoritative backstop is the same gate on the next clean run.
    """
    proc = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=A", "--", "tests/"],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        **no_window_kwargs(),
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


@hook_app.command(name="eawf025-test-placement")
def eawf025_test_placement(
    ctx: typer.Context,
    files: _FilesArg = None,
) -> None:
    """Reject a newly added test filed outside its taxonomy address.

    Reads the staged ADDED ``tests/`` set (or an explicit file list, for
    tests) and runs the EAWF025 rule: a new test lives under
    ``tests/<kind>/<source-package-path>/`` where the kind is a declared
    ``TestKind`` and the mirror chain names a real package under
    ``src/eawf/``, and no subject is filed under two kinds at once. The
    pre-taxonomy subsystem directories are exempt. Exits 1 on a
    violation, 0 when clean.
    """
    from eawf.platform.lint import eawf025_test_placement as eawf025

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = files if files else _staged_added_tests(cwd=cwd)
    violations = eawf025.check_test_paths(paths, source_packages=eawf025.source_package_paths(cwd))
    rows = [f"  {violation.path}: {violation.render()}" for violation in violations]
    _emit_static_lint_result(
        hook_name="eawf025-test-placement",
        rows=rows,
        scanned=len(paths),
        flags=flags,
        blocking=True,
    )


def _staged_added_lines(rel: str, *, cwd: Path) -> list[tuple[int, str]]:
    """Return ``(1-based new lineno, text)`` for lines the staged diff added.

    Companion of :func:`_staged_added_line_candidates`, but returns the line
    *text* alongside the number so a content lint (EAWF016 title-clarity) can
    inspect only the freshly-authored ``state.json`` lines. The leading ``+``
    of each added diff line is stripped. ``--find-renames`` (``-M``) keeps a
    relocated file from reporting its whole body as added (mirrors
    :func:`_staged_added_line_candidates`).
    """
    pathspec = _staged_diff_pathspec(rel, cwd=cwd)
    proc = subprocess.run(
        ["git", "diff", "--cached", "--unified=0", "--find-renames", "--", *pathspec],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
        **no_window_kwargs(),
    )
    if proc.returncode != 0:
        return []
    added: list[tuple[int, str]] = []
    new_lineno: int | None = None
    for line in proc.stdout.splitlines():
        hunk = _DIFF_HUNK_RE.match(line)
        if hunk is not None:
            new_lineno = int(hunk.group("start"))
            continue
        if new_lineno is None or line.startswith(("diff --git", "index ", "--- ", "+++ ")):
            continue
        if line.startswith("+"):
            added.append((new_lineno, line[1:]))
            new_lineno += 1
            continue
        if line.startswith("-") or line.startswith("\\"):
            continue
        new_lineno += 1
    return added


@hook_app.command(name="eawf016-title-clarity")
def eawf016_title_clarity(
    ctx: typer.Context,
    files: _FilesArg = None,
) -> None:
    """Reject unclear entity titles added to the staged ``state.json`` delta.

    The diff-scoped backstop behind the mutation-boundary gate (the lifecycle
    ``plan_wave`` / open / plan transitions). It parses only the **added**
    ``"title": "..."`` lines of the staged ``.ea/state.json`` diff and runs
    the EAWF016 title-clarity rules (over-cap, trailing period,
    conventional-commit prefix, ``+``-join cluster soup, bare-id-only) on each
    new title. Because only added lines are scanned, the hundreds of unchanged
    legacy titles are grandfathered and never re-flagged. ``pre-commit
    run --all-files`` (nothing staged) is a clean no-op. Exits 1 on a
    violation, 0 when clean.
    """
    from eawf.platform.lint.eawf016_title_clarity import check_state_title_lines

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    # The title-clarity surface is state.json only; an explicit file list (a
    # test) wins, else scan just the staged state.json delta.
    targets = files if files else [".ea/state.json"]
    rows: list[str] = []
    scanned = 0
    for rel in targets:
        norm = rel.replace("\\", "/")
        if not norm.endswith("state.json"):
            continue
        if not (cwd / rel).exists():
            continue
        scanned += 1
        added = _staged_added_lines(rel, cwd=cwd)
        rows.extend(f"  {rel}:{violation.render()}" for violation in check_state_title_lines(added))
    _emit_static_lint_result(
        hook_name="eawf016-title-clarity",
        rows=rows,
        scanned=scanned,
        flags=flags,
        blocking=True,
    )


@hook_app.command(name="eawf017-inline-refs")
def eawf017_inline_refs(
    ctx: typer.Context,
    files: _FilesArg = None,
    base: _BaseOpt = "origin/main",
) -> None:
    """Reject inline bare URLs and inline ``path:line`` reference soup.

    Scans the given Markdown files (or, when none are given, only the staged
    ``.md`` files relevant to this gate per the conditional scan). Each bare
    inline URL is a finding, and more than two inline ``path:line`` references
    in one prose block is a finding — both must move into a numbered
    ``## References`` table. Fenced code, the ``## References`` section,
    reference rows, and inline-code spans are exempt. Composes with EAWF013
    (which positions the ``[N]`` markers). Exits 1 on a violation, 0 when
    clean.
    """
    from eawf.platform.lint.eawf017_inline_reference import check_source

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = _resolve_scan_paths(files, hook_name="eawf017-inline-refs", base=base, cwd=cwd)
    rows: list[str] = []
    scanned = 0
    for rel in paths:
        if not rel.endswith(".md"):
            continue
        try:
            source = (cwd / rel).read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        scanned += 1
        rows.extend(f"  {rel}:{violation.render()}" for violation in check_source(source))
    _emit_static_lint_result(
        hook_name="eawf017-inline-refs",
        rows=rows,
        scanned=scanned,
        flags=flags,
        blocking=True,
    )


@hook_app.command(name="validate-prose")
def validate_prose_gate(
    ctx: typer.Context,
    files: _FilesArg = None,
    base: _BaseOpt = "origin/main",
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Fail-closed CI mode: exit non-zero on any finding. Omit for "
            "the fail-open local/in-skill mode (advisory, exit 0).",
        ),
    ] = False,
) -> None:
    """Compose every Layer-2 prose check over changed Markdown — the chokepoint.

    The single enforcement entry point for the doc-clarity Layer-2 stack. It
    runs :func:`~eawf.platform.lint.validate_prose.validate_prose` (which
    composes EAWF013 citation-bracket position, EAWF014 no-manual-wrap, and
    EAWF017 inline-reference tabulation in-process) over the given ``.md``
    files — or, when none are given, the staged ``.md`` delta per the
    conditional scan.

    Two modes implement the stability contract:

    - **fail-open** (default) — a local pre-commit / in-skill run. Findings are
      emitted as advisory and the gate exits 0, never blocking the operator.
    - **fail-closed** (``--strict``) — any finding from the composed
      deterministic lints exits 1.
    """
    from eawf.platform.lint.validate_prose import validate_prose

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = _resolve_scan_paths(files, hook_name="validate-prose", base=base, cwd=cwd)
    md_paths = [
        rel
        for rel in paths
        if rel.endswith(".md")
        and (cwd / rel).exists()
        and not _is_generated_markdown_fixture_path(rel)
    ]
    rows: list[str] = []
    for rel in md_paths:
        try:
            source = (cwd / rel).read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        report = validate_prose(source, strict=strict)
        rows.extend(f"  {rel}:{finding.render()}" for finding in report.findings)
    _emit_static_lint_result(
        hook_name="validate-prose",
        rows=rows,
        scanned=len(md_paths),
        flags=flags,
        blocking=strict,
    )


@hook_app.command(name="eawf002-log-key")
def eawf002_log_key(
    ctx: typer.Context,
    files: _FilesArg = None,
    base: _BaseOpt = "origin/main",
) -> None:
    """Reject ``_id``-suffixed wave/iter/phase keys in library log messages.

    Scans the given Python files (or, when none are given, only the
    staged ``src/eawf/**/*.py`` modules per the conditional scan). A
    ``logger.<level>(...)`` message must spell cross-cutting identifiers
    with their bare key (``wave=``, ``iter=``, ``phase=``), never the
    ``_id``-suffixed form. Exits 1 on a violation, 0 when clean. Files
    that fail to parse are skipped (an authoring bug surfaced elsewhere).
    """
    from eawf.platform.lint.eawf002 import check_source

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = _resolve_scan_paths(files, hook_name="eawf002-log-key", base=base, cwd=cwd)
    rows, scanned = _scan_python_rows(
        paths, cwd=cwd, check=lambda src, rel: check_source(src, filename=rel)
    )
    _emit_static_lint_result(
        hook_name="eawf002-log-key",
        rows=rows,
        scanned=scanned,
        flags=flags,
        blocking=True,
    )


@hook_app.command(name="eawf003-logger-acquire")
def eawf003_logger_acquire(
    ctx: typer.Context,
    files: _FilesArg = None,
    base: _BaseOpt = "origin/main",
) -> None:
    """Reject library ``getLogger`` calls that do not pass ``__name__``.

    Scans the given Python files (or, when none are given, only the
    staged ``src/eawf/**/*.py`` modules per the conditional scan). A
    library logger must be acquired via ``logging.getLogger(__name__)``;
    a hard-coded name, the root logger (no argument), or any other
    argument is flagged. Exits 1 on a violation, 0 when clean. Files that
    fail to parse are skipped (an authoring bug surfaced elsewhere).
    """
    from eawf.platform.lint.eawf003 import check_source

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = _resolve_scan_paths(files, hook_name="eawf003-logger-acquire", base=base, cwd=cwd)
    rows, scanned = _scan_python_rows(
        paths, cwd=cwd, check=lambda src, rel: check_source(src, filename=rel)
    )
    _emit_static_lint_result(
        hook_name="eawf003-logger-acquire",
        rows=rows,
        scanned=scanned,
        flags=flags,
        blocking=True,
    )


@hook_app.command(name="eawf019-math-facets")
def eawf019_math_facets(
    ctx: typer.Context,
    files: _FilesArg = None,
    base: _BaseOpt = "origin/main",
) -> None:
    """Reject math-explainer claims missing a facet, an unresolved citation, or a dead gate.

    Scans the given JSON files (or, when none are given, the staged files
    relevant to this gate per the conditional scan), validating each as a typed
    :class:`~eawf.kernel.spec.math.MathExplainer` and running the EAWF019
    checks: facet-presence (runnable example), citation-resolution (against the
    canonical EviBound resolver), collected-gate (the kappa silent-skip
    regression), and formula well-formedness. A JSON file that is not a valid
    ``MathExplainer`` is skipped silently (it is not a math-explainer doc).
    Exits 1 on a violation, 0 when clean.
    """
    from pydantic import ValidationError as _ValidationError

    from eawf.kernel.spec.math import MathExplainer
    from eawf.platform.lint.eawf019_math_facets import check_explainer

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = _resolve_scan_paths(files, hook_name="eawf019-math-facets", base=base, cwd=cwd)
    rows: list[str] = []
    scanned = 0
    for rel in paths:
        if not rel.endswith(".json"):
            continue
        try:
            source = (cwd / rel).read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        try:
            explainer = MathExplainer.model_validate_json(source)
        except _ValidationError:
            # Not a math-explainer doc (or a malformed one rejected at
            # ingestion) — outside this gate's surface.
            continue
        scanned += 1
        rows.extend(
            f"  {rel}:{violation.render()}"
            for violation in check_explainer(explainer, project_root=cwd)
        )
    _emit_static_lint_result(
        hook_name="eawf019-math-facets",
        rows=rows,
        scanned=scanned,
        flags=flags,
        blocking=True,
    )


def _resolve_lint_config(cwd: Path) -> LintConfig | None:
    """Return the resolved ``[tool.eawf.lint]`` config under ``cwd``, or ``None``.

    Reads ``cwd / pyproject.toml`` via :func:`load_lint_config`. Returns
    ``None`` when no pyproject is present (e.g. a hermetic test that
    invokes the gate with an explicit threshold override and an explicit
    file list), so the dispatcher falls back to the rule's own default
    cap rather than raising.
    """
    from eawf.platform.lint import load_lint_config

    pyproject = cwd / "pyproject.toml"
    if not pyproject.exists():
        return None
    return load_lint_config(pyproject)


_MaxLocOpt = Annotated[
    int,
    typer.Option(
        "--max-loc",
        help="Per-module line budget override. -1 (default) reads the "
        "pyproject [tool.eawf.lint.eawf010] max-loc key.",
    ),
]
_MaxComplexityOpt = Annotated[
    int,
    typer.Option(
        "--max-complexity",
        help="Per-function cognitive-complexity budget override. -1 "
        "(default) reads the pyproject [tool.eawf.lint.eawf011] key.",
    ),
]


@hook_app.command(name="eawf010-module-length")
def eawf010_module_length(
    ctx: typer.Context,
    files: _FilesArg = None,
    base: _BaseOpt = "origin/main",
    max_loc: _MaxLocOpt = -1,
) -> None:
    """Reject Python modules over the physical line-count budget.

    Scans the given Python files (or, when none are given, only the
    staged ``src/eawf/**/*.py`` modules per the conditional scan). The
    budget and a per-path grandfather ``exclude`` list come from the
    pyproject ``[tool.eawf.lint.eawf010]`` table; ``--max-loc`` overrides
    the budget for hermetic tests. An over-budget module is clean only
    when it is on the exclude list or carries a
    ``# noqa: EAWF010 <rationale>`` waiver. Exits 1 on a violation, 0
    when clean.
    """
    from eawf.platform.lint.eawf010 import DEFAULT_MAX_LOC, check_source

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    config = _resolve_lint_config(cwd)
    if max_loc >= 0:
        budget = max_loc
        exclude: frozenset[str] = frozenset()
    elif config is not None:
        budget = config.eawf010.max_loc
        exclude = config.eawf010.exclude
    else:
        budget = DEFAULT_MAX_LOC
        exclude = frozenset()
    paths = _resolve_scan_paths(files, hook_name="eawf010-module-length", base=base, cwd=cwd)
    rows, scanned = _scan_python_rows(
        paths,
        cwd=cwd,
        check=lambda src, _rel: check_source(src, max_loc=budget),
        exclude=frozenset(e.replace("\\", "/") for e in exclude),
    )
    _emit_static_lint_result(
        hook_name="eawf010-module-length",
        rows=rows,
        scanned=scanned,
        flags=flags,
        blocking=True,
    )


@hook_app.command(name="eawf011-cognitive-complexity")
def eawf011_cognitive_complexity(
    ctx: typer.Context,
    files: _FilesArg = None,
    base: _BaseOpt = "origin/main",
    max_complexity: _MaxComplexityOpt = -1,
) -> None:
    """Reject functions over the cognitive-complexity budget.

    Scans the given Python files (or, when none are given, only the
    staged ``src/eawf/**/*.py`` modules per the conditional scan). The
    budget and a per-path grandfather ``exclude`` list come from the
    pyproject ``[tool.eawf.lint.eawf011]`` table; ``--max-complexity``
    overrides the budget for hermetic tests. Exits 1 on a violation, 0
    when clean. Files that fail to parse are skipped (an authoring bug
    surfaced elsewhere).
    """
    from eawf.platform.lint.eawf011 import DEFAULT_MAX_COMPLEXITY, check_source

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    config = _resolve_lint_config(cwd)
    if max_complexity >= 0:
        budget = max_complexity
        exclude: frozenset[str] = frozenset()
    elif config is not None:
        budget = config.eawf011.max_complexity
        exclude = config.eawf011.exclude
    else:
        budget = DEFAULT_MAX_COMPLEXITY
        exclude = frozenset()
    paths = _resolve_scan_paths(files, hook_name="eawf011-cognitive-complexity", base=base, cwd=cwd)
    rows, scanned = _scan_python_rows(
        paths,
        cwd=cwd,
        check=lambda src, rel: check_source(src, filename=rel, max_complexity=budget),
        exclude=frozenset(e.replace("\\", "/") for e in exclude),
    )
    _emit_static_lint_result(
        hook_name="eawf011-cognitive-complexity",
        rows=rows,
        scanned=scanned,
        flags=flags,
        blocking=True,
    )


_LedgerRangeOpt = Annotated[
    str,
    typer.Option(
        "--range",
        help="<base>..<tip> to check, or <base> alone to check the working tree.",
    ),
]


@hook_app.command(name="ledger-history")
def ledger_history(
    ctx: typer.Context,
    range_spec: _LedgerRangeOpt,
) -> None:
    """Check the committed ledgers stay append-only and bind only reachable commits.

    Over the range, every changed ledger file must extend its bytes at the
    merge base, and every commit SHA an effective ledger row names must be an
    ancestor of the base or the tip unless the row declares it in
    ``unreachable_shas``. Leading lines a seal moved into the ledger's
    snapshot are listed under ``sealed`` and are not findings. Exits 1 on a
    finding, 0 when clean; exits 2 on a malformed range or unknown rev.
    """
    from eawf.platform.lint.ledger_history import LedgerHistoryError, scan_ledger_history

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    try:
        report = scan_ledger_history(cwd, range_spec)
    except LedgerHistoryError as exc:
        typer.echo(f"ledger-history: {exc}", err=True)
        raise typer.Exit(exit_codes.VALIDATION_ERROR) from exc
    payload: dict[str, object] = {
        "hook": "ledger-history",
        **report.model_dump(mode="json"),
        "clean": report.clean,
    }
    if report.clean:
        emit_json_or_text(
            payload,
            f"ledger-history: clean ({report.ledgers_scanned} ledger file(s) scanned)",
            flags=flags,
        )
        return
    lines = [
        f"ledger-history: {len(report.append_only)} append-only, "
        f"{len(report.unreachable)} unreachable, {len(report.malformed)} malformed "
        f"over {report.range}",
        *(finding.render() for finding in report.append_only),
        *(finding.render() for finding in report.unreachable),
        *(finding.render() for finding in report.malformed),
    ]
    emit_json_or_text(payload, "\n".join(lines), flags=flags)
    raise typer.Exit(exit_codes.USER_ERROR)


def _marketplace_mode() -> bool:
    """True when the eawf plugin is installed for the user via the marketplace.

    Resolved through the plugin command module so the detector stays one
    patchable seam for the gate, the doctor arm and this hook.
    """
    from eawf.surfaces.cli.commands import plugin as _plugin

    return _plugin.detect_marketplace_install() is not None


@hook_app.command(name="plugin-doctor-drift")
def plugin_doctor_drift(
    ctx: typer.Context,
    base: _BaseOpt = "origin/main",
) -> None:
    """Fail when ``plugin doctor --strict`` reports drift in the plugin tree.

    Conditional-skip per C09 §5.3: when the diff between ``base`` and
    ``HEAD`` touches no AGENTS.md / skill / runtime / build path the
    gate exits 0 immediately (the drift surface cannot have moved).
    Otherwise it runs the Claude checksum sweep and exits 1 on drift.
    """
    from eawf.platform.lint._conditional import relevant_for_hook
    from eawf.runtime.runtimes.claude.plugin_doctor import doctor_plugin_strict

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    relevant = relevant_for_hook("plugin-doctor-drift", base, cwd=cwd)
    if not relevant:
        emit_json_or_text(
            {"hook": "plugin-doctor-drift", "skipped": True, "clean": True},
            "plugin-doctor-drift: skipped (no plugin-surface change)",
            flags=flags,
        )
        return
    report = doctor_plugin_strict(cwd)
    payload: dict[str, object] = {
        "hook": "plugin-doctor-drift",
        "skipped": False,
        "clean": report.clean,
        "drifted": [entry.region_id for entry in report.drifted],
        "missing": [entry.region_id for entry in report.missing],
    }
    # An entirely absent local render, while the plugin is installed from the
    # marketplace, is the intended configuration. Reporting it as drift
    # blocked every push and pointed at `plugin sync`, which the conflict gate
    # refuses precisely because running it would recreate the duplicate.
    if not report.drifted and report.missing and _marketplace_mode():
        payload["mode"] = "marketplace"
        emit_json_or_text(
            payload,
            "plugin-doctor-drift: marketplace mode (no project-local render expected)",
            flags=flags,
        )
        return
    if not report.clean:
        body = (
            f"plugin-doctor-drift: drift detected "
            f"(drifted={len(report.drifted)} missing={len(report.missing)}) — run eawf plugin sync"
        )
        emit_json_or_text(payload, body, flags=flags)
        raise typer.Exit(exit_codes.USER_ERROR)
    emit_json_or_text(payload, "plugin-doctor-drift: clean", flags=flags)


health_app = typer.Typer(
    name="health",
    help="Read and acknowledge the hook health record.",
    no_args_is_help=True,
)
hook_app.add_typer(health_app, name="health")


@health_app.command(name="ack")
def health_ack(ctx: typer.Context) -> None:
    """Acknowledge the recorded hook failures so the status line stops showing them.

    Writes the acknowledgement beside the record in the main checkout and
    prints how many failures of the last 24 hours it covered. Exits 1
    outside a managed repository.
    """
    from eawf.runtime.hooks.health import acknowledge
    from eawf.runtime.runtimes.claude.statusline import drop_cached_lines
    from eawf.runtime.vcs.branch_name import main_checkout
    from eawf.surfaces.cli.scope import resolve_state_path

    flags: GlobalFlags = ctx.obj
    try:
        state_dir = resolve_state_path(flags.workspace).parent
        count = acknowledge(main_checkout(state_dir.parent) / state_dir.name, now=datetime.now(UTC))
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotManaged"), flags=flags)
    drop_cached_lines()
    emit_json_or_text({"acknowledged": count}, f"acknowledged {count} hook failures", flags=flags)


__all__ = [
    "health_app",
    "hook_app",
]
