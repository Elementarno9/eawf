"""``eawf hook run <event>`` Typer command.

Surface contract:

- ``eawf hook run <event_type> [--runtime <claude|opencode|generic>]
  [--scope <ID>] [--command <str>]`` reads a JSON payload from stdin,
  builds a typed :class:`~eawf.runtime.hooks.event.HookEvent`, dispatches it
  through a fresh :class:`~eawf.runtime.hooks.runner.HookRunner`, and emits an
  :class:`~eawf.surfaces.render.envelope.OutputEnvelope` to stdout.
- ``eawf hook dispatch --event-type agent_end [--runtime ...] [--scope ...]
  [--command ...]`` is the interim verdict seeder: it translates one
  ``agent_end`` event (payload on stdin) into a single seeded verdict row via
  :func:`eawf.workflow.dispatch.seed.seed_interim_verdict` so the self-eval +
  jury surfaces read a primed cohort before the live verdict producer lands.
  Only ``agent_end`` is accepted; other event types exit ``3``.
- Exit ``0`` when no registered hook returns ``block=True``. ``session_end``
  registers the built-in runtime capture hook; other events without hooks keep
  the empty-result no-op path.
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

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

if TYPE_CHECKING:
    from eawf.kernel.state.models import State
    from eawf.platform.lint import LintConfig
    from eawf.runtime.hooks.event import HookEvent, HookEventType, HookRuntime
    from eawf.runtime.hooks.runner import HookResult
    from eawf.surfaces.render.envelope import EnvelopeStatus, OutputEnvelope

logger = logging.getLogger(__name__)


class AgentEndPayload(BaseModel):
    """Payload accepted by ``eawf hook run agent_end``."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    base_id: str
    body: dict[str, Any]
    artifact_ids: list[str] = Field(default_factory=list)
    blob_refs: list[str] = Field(default_factory=list)


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
    skill for the CLI hook surface yet (W05 may add ``/hook`` if an
    operator-facing skill is required). Status is ``ok`` when no hook
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


def _load_state(state_path: Path) -> State:
    """Load and validate state from *state_path*."""
    from eawf.kernel.validate.strict import validate_state

    path = state_path
    if not path.exists():
        raise cli_errors.UserError(f"state file not found: {path}", kind="NotFound")
    payload = orjson.loads(path.read_bytes())
    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        raise cli_errors.ValidationError(
            f"state schema invalid: {'; '.join(report.schema_errors[:3])}"
        )
    return report.state


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
    dispatch-only; the library implements). All CLI-level failures surface as
    the canonical error envelope and the function returns without raising.
    """
    from eawf.workflow.agent_report.store import (
        AgentReportRoleMismatchError,
        AgentReportScrubError,
    )
    from eawf.workflow.dispatch.seed import seed_interim_verdict

    try:
        agent_payload = AgentEndPayload.model_validate(payload)
        state_path = resolve_state_path(flags.workspace)
        state = _load_state(state_path)
        result = seed_interim_verdict(
            state=state,
            state_path=state_path,
            session_id=agent_payload.session_id,
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
# actual detection to a library surface (the scrubber patterns reused
# from ``eawf.observability.logging.scrub`` for leaks, the EAWF001 rule for log
# format, ``plugin doctor --strict`` for plugin drift). The conditional
# diff scan keeps each gate a no-op when nothing relevant changed.

# Suffixes the leak gates skip — binary-ish blobs are not text-scanned.
_BINARY_SUFFIXES: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".whl"}
)

# Inline marker that exempts a single line from the leak gates. Reuses
# the established ``detect-secrets`` allowlist phrasing so a line that is
# already acknowledged as a by-design fixture / pattern definition (a
# scrub-test sample path, the scrubber's own regex source) is exempt
# from both the path gate and the email gate without a brittle per-file
# path exclude list.
_LEAK_ALLOW_MARKER = "pragma: allowlist secret"


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


def _path_leak_patterns() -> tuple[re.Pattern[str], ...]:
    """Return the home-directory path patterns reused from the scrubber.

    The three home-directory anchors (macOS, Windows, Linux) are the
    leak shapes the path gate rejects; they are sourced from
    :data:`eawf.observability.logging.scrub.SensitiveScrubber.PATTERNS` so the gate
    and the emit-time scrubber never drift.
    """
    from eawf.observability.logging.scrub import SensitiveScrubber

    return tuple(
        p for p in SensitiveScrubber.PATTERNS if "Users" in p.pattern or "home" in p.pattern
    )


def _email_leak_pattern() -> re.Pattern[str]:
    """Return the email pattern reused from the scrubber."""
    from eawf.observability.logging.scrub import SensitiveScrubber

    return next(p for p in SensitiveScrubber.PATTERNS if "@" in p.pattern)


def _allowed_emails() -> frozenset[str]:
    """Return the canonical email allowlist (no-reply + pyproject authors).

    Reuses the scrubber's allowlist derivation so the gate accepts
    exactly the addresses the emit-time filter preserves: the no-reply
    co-author addresses plus the canonical ``pyproject.toml`` author
    rows.
    """
    from eawf.observability.logging.scrub import _DEFAULT_ALLOWED_EMAILS, _eawf_author_emails

    return frozenset(e.casefold() for e in (_DEFAULT_ALLOWED_EMAILS | _eawf_author_emails()))


# RFC 2606 / 6761 reserved domains used in fixtures + docs; never real PII.
_RESERVED_EMAIL_DOMAINS = frozenset({"example.com", "example.org", "example.net", "example.edu"})
_RESERVED_EMAIL_TLDS = (".example", ".invalid", ".localhost", ".test")


def _is_placeholder_or_nonemail(addr: str) -> bool:
    """Return ``True`` for reserved placeholders and ``@``-refs that are not emails.

    Guards the email-leak gate against two false-positive classes the
    scrubber's loose address-shaped pattern would otherwise flag:

    - RFC 2606 / 6761 reserved example domains (test fixtures, docs) such
      as ``test@example.com``; and
    - version / action pins like ``setup-uv@v8.1.0`` whose top-level
      label is not an alphabetic TLD.
    """
    _, _, domain = addr.partition("@")
    domain = domain.casefold()
    if not domain:
        return True
    if domain in _RESERVED_EMAIL_DOMAINS or domain.endswith(_RESERVED_EMAIL_TLDS):
        return True
    tld = domain.rsplit(".", 1)[-1]
    return not (tld.isalpha() and len(tld) >= 2)


def _is_placeholder_path(snippet: str) -> bool:
    """Return ``True`` for documented home-dir placeholders, not real path leaks.

    The path-leak gate's loose home-directory anchors (macOS, Windows,
    Linux, tilde) match the pedagogical "do NOT commit these" examples
    that the secrets-hygiene rule body and rendered ``AGENTS.md`` carry
    verbatim — ``/Users/<name>``, ``C:\\Users\\...``, ``~/Workspace/...``.
    Those are documentation, not leaks, so the gate skips them. This is
    the symmetric counterpart to :func:`_is_placeholder_or_nonemail`,
    which already shields the email gate from reserved-domain and
    version-pin false positives.

    A matched ``snippet`` is treated as a placeholder when it carries an
    angle bracket (``<`` or ``>``) — the convention for a name to be
    filled in (``/Users/<name>``) — or an ellipsis (``...``) — the
    convention for an elided tail (``C:\\Users\\...``). A concrete
    home-dir path with a real username after the anchor carries neither
    token and is still flagged as a leak.
    """
    return any(token in snippet for token in ("<", ">", "..."))


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


def _scan_path_leaks(paths: list[str], *, cwd: Path) -> list[LeakFinding]:
    """Return home-directory path-literal findings across ``paths``."""
    patterns = _path_leak_patterns()
    findings: list[LeakFinding] = []
    for rel in paths:
        lines = _read_text_lines(cwd / rel)
        if lines is None:
            continue
        for lineno, line in enumerate(lines, start=1):
            if _line_is_allowlisted(line):
                continue
            for pattern in patterns:
                for match in pattern.finditer(line):
                    if _is_placeholder_path(match.group(0)):
                        continue
                    findings.append(LeakFinding(path=rel, lineno=lineno, snippet=match.group(0)))
    return findings


def _scan_email_leaks(paths: list[str], *, cwd: Path) -> list[LeakFinding]:
    """Return non-allowlisted email findings across ``paths``."""
    pattern = _email_leak_pattern()
    allowed = _allowed_emails()
    findings: list[LeakFinding] = []
    for rel in paths:
        lines = _read_text_lines(cwd / rel)
        if lines is None:
            continue
        for lineno, line in enumerate(lines, start=1):
            if _line_is_allowlisted(line):
                continue
            for match in pattern.finditer(line):
                if match.group(0).casefold() in allowed:
                    continue
                if _is_placeholder_or_nonemail(match.group(0)):
                    continue
                findings.append(LeakFinding(path=rel, lineno=lineno, snippet=match.group(0)))
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
    return [p for p in candidates if not _is_state_bookkeeping_path(p) and p not in relocated]


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
    )
    if proc.returncode == 0:
        for line in proc.stdout.splitlines():
            match = _RENAME_STATUS_RE.match(line)
            if match is not None and match.group("new") == rel:
                return [match.group("old"), rel]
    return [rel]


def _staged_added_line_candidates(rel: str, *, cwd: Path) -> set[int]:
    """Return 1-based new-file lines added by the staged diff.

    ``--find-renames`` (``-M``) keeps a relocation from masquerading as a brand
    new file: without it ``git diff --cached`` reports every line of a moved
    artifact as freshly added, so a pure ``git mv`` would re-expose the entire
    pre-existing body to the diff-scoped chassis lints. With rename detection a
    pure move yields zero added-line candidates and a move-with-edit surfaces
    only the lines the edit actually touched.
    """
    pathspec = _staged_diff_pathspec(rel, cwd=cwd)
    proc = subprocess.run(
        ["git", "diff", "--cached", "--unified=0", "--find-renames", "--", *pathspec],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        return set()
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
    """Return ``True`` for daemon-managed bookkeeping files excluded from leak scans.

    Only ``.ea/state.json`` is exempt. Its content is machine-written, its line
    offsets churn on every mutation, and its free-text fields (backlog titles,
    outcomes, rule prose) legitimately carry home-directory path SHAPES as
    placeholders -- ``/Users/<name>`` in a rule that explains the leak lint is
    not a leak.

    The typed stores under ``.ea/store/`` are NOT exempt. They used to be, on the
    premise that a daemon-written file cannot carry user secrets; that premise
    held until a store began carrying the raw stdout of spawned agents, at which
    point the exemption made the one file that could leak the one file nobody
    scanned. The event store is now untracked entirely, and the typed stores that
    remain (audit / decision / evidence / role reports) are structured rows the
    daemon composes -- so scanning them costs nothing today and catches the next
    free-text field somebody adds to one.
    """
    return rel.replace("\\", "/") == ".ea/state.json"


def _is_generated_markdown_fixture_path(rel: str) -> bool:
    """Return ``True`` for generated markdown fixtures checked by golden tests."""
    return rel.replace("\\", "/").startswith("tests/golden/agents_md/")


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
) -> None:
    """Dispatch a hook event read from stdin and emit the result envelope."""
    from eawf.runtime.hooks.event import HookEventType
    from eawf.runtime.hooks.runner import HookRunner, register_runtime_capture_hooks

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
        # Skip stdin read on a TTY so interactive smoke runs don't block
        # waiting for EOF; piped/redirected stdin reads normally.
        stdin_text = "" if sys.stdin.isatty() else sys.stdin.read()
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
    repo_root = (flags.workspace or Path.cwd()).resolve()
    register_runtime_capture_hooks(runner, repo_root=repo_root)
    results = runner.run_event(event)

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
    files relevant to this gate per the conditional scan). A line
    carrying the ``pragma: allowlist secret`` marker is exempt (for
    by-design fixtures / pattern source). Exits 1 when a leak is found,
    0 on a clean scan.
    """
    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = _resolve_scan_paths(files, hook_name="path-leak-lint", base=base, cwd=cwd)
    findings = _scan_path_leaks(paths, cwd=cwd)
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
    ``pyproject.toml`` author rows. Exits 1 on a leak, 0 on a clean scan.
    """
    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = _resolve_scan_paths(files, hook_name="email-leak-lint", base=base, cwd=cwd)
    findings = _scan_email_leaks(paths, cwd=cwd)
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
    :func:`~eawf.surfaces.tui.widgets.sigils.status_sigil` to a non-empty glyph
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

    The EAWF023 placement gate scans the whole tracked artifact tree (not
    a staged delta) so a misplaced or date-stem-less artifact reds CI
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

    Walks the git-tracked ``.ea/artifacts/**/*.md`` set (or an explicit
    file list, for tests) and runs the EAWF023 placement rule: each
    artifact must live under its canonical kind sub-directory
    (``audits/``, ``research/``, ``incidents/``, ...) and its filename
    stem must lead with a ``YYYY-MM-DD-`` date prefix. A pre-convention
    legacy baseline is grandfathered so the clean tree passes. The scan
    covers the whole tracked tree (not a staged delta) so a misplaced
    artifact reds CI regardless of which files the commit touched. Exits
    1 on a violation, 0 when clean.
    """
    from eawf.platform.lint.eawf023_artifact_placement import check_artifact_paths

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = files if files else _git_tracked_artifacts(cwd=cwd)
    violations = check_artifact_paths(paths)
    rows = [f"  {v.path}: {v.render()}" for v in violations]
    _emit_static_lint_result(
        hook_name="eawf023-artifact-placement",
        rows=rows,
        scanned=len(paths),
        flags=flags,
        blocking=True,
    )


def _git_tracked_unit_tests(*, cwd: Path) -> list[str]:
    """Return git-tracked ``tests/unit/**/*.py`` paths under ``cwd``.

    The EAWF024 test-tier gate scans the whole tracked unit-test tree
    (not a staged delta) so a mislabeled unit test reds CI regardless of
    which files the commit touched. A failed git invocation yields an
    empty list (fail-open) -- the authoritative backstop is the same gate
    on the next clean run.
    """
    proc = subprocess.run(
        ["git", "ls-files", "tests/unit/**/*.py", "tests/unit/*.py"],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


@hook_app.command(name="eawf024-test-tier-contract")
def eawf024_test_tier_contract(
    ctx: typer.Context,
    files: _FilesArg = None,
) -> None:
    """Reject non-unit imports in the ``tests/unit/`` tier.

    Walks the git-tracked ``tests/unit/**/*.py`` set (or an explicit file
    list, for tests) and runs the EAWF024 rule: a unit-tier test must not
    import ``subprocess``, ``textual``, or ``CliRunner`` (each marks a
    slower integration/TUI test mislabeled into the unit tier). An
    offending import may carry a line-level ``# noqa: EAWF024`` waiver for
    a deliberate fixture. The scan covers the whole tracked unit tree (not
    a staged delta) so a mislabeled test reds CI regardless of which files
    the commit touched. Exits 1 on a violation, 0 when clean.
    """
    from eawf.platform.lint.eawf024_test_tier_contract import check_source, is_unit_tier_path

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = files if files else _git_tracked_unit_tests(cwd=cwd)
    unit_paths = [rel for rel in paths if is_unit_tier_path(rel)]
    rows, scanned = _scan_python_rows(
        unit_paths, cwd=cwd, check=lambda src, rel: check_source(src, filename=rel)
    )
    _emit_static_lint_result(
        hook_name="eawf024-test-tier-contract",
        rows=rows,
        scanned=scanned,
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
    base: _BaseOpt = "origin/main",
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


class _ValeRunError(RuntimeError):
    """Vale ran but emitted its error-object JSON (e.g. an unsynced StylesPath).

    ``vale --output=JSON`` returns a ``{path: [alert]}`` map on a normal run,
    but on a runtime error (most commonly ``E100`` — a ``BasedOnStyles``
    package missing from the ``StylesPath`` because ``vale sync`` has not been
    run) it returns a single error object ``{"Code": "E100", "Text": ...}``.
    The wrapper treats that as a fail-open skip, not a finding, because a
    machine without the synced package is the same advisory-skip case as a
    machine without the binary.
    """


def _vale_findings_to_rows(payload: dict[str, Any], *, rel_for: dict[str, str]) -> list[str]:
    """Render parsed ``vale --output=JSON`` alerts into ``  rel:line:col`` rows.

    ``vale --output=JSON`` maps each linted path to a list of alert objects
    (``Line``, ``Span``, ``Severity``, ``Check``, ``Message``). This flattens
    them into the one-liner rows the static-lint envelope renders, remapping
    the temp-file path Vale reports back to the caller-facing label via
    ``rel_for`` (so a commit-body bridge shows ``<commit body>`` not a
    ``/tmp/...`` path). Non-list values (the Vale error-object shape) are
    skipped defensively, though :func:`_run_vale_json` already raises
    :class:`_ValeRunError` on that shape before this is reached.
    """
    rows: list[str] = []
    for vale_path, alerts in payload.items():
        if not isinstance(alerts, list):
            continue
        label = rel_for.get(vale_path, vale_path)
        for alert in alerts:
            line = alert.get("Line", 0)
            span = alert.get("Span") or [0]
            col = span[0] if span else 0
            sev = alert.get("Severity", "warning")
            check = alert.get("Check", "Vale")
            message = alert.get("Message", "")
            rows.append(f"  {label}:{line}:{col}: {sev} {check} {message}")
    return rows


def _run_vale_json(targets: list[Path], *, cwd: Path) -> dict[str, Any]:
    """Subprocess ``vale --output=JSON`` over *targets* and parse the payload.

    Returns the decoded ``{path: [alert, ...]}`` mapping. Vale exits non-zero
    when it finds alerts at or above ``MinAlertLevel`` — that is the normal
    "found something" path, not a failure — so the return code is ignored and
    only the JSON body is parsed. An empty / unparseable body yields ``{}``.

    Raises:
        _ValeRunError: when Vale emits its error-object shape (a top-level
            object carrying a ``"Code"`` key instead of the ``{path: [alert]}``
            map), e.g. ``E100`` for an unsynced ``StylesPath``. The caller
            fails open on this signal.
    """
    proc = subprocess.run(
        ["vale", "--output=JSON", *[str(t) for t in targets]],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # Vale writes the alert map to stdout on a normal run, but writes its
    # error-object JSON (E100 etc.) to stderr with a non-zero exit. Check
    # stderr first so an unsynced StylesPath fails open instead of looking
    # like a clean (empty) result.
    if proc.stderr.strip():
        _raise_if_vale_error(proc.stderr)
    if not proc.stdout.strip():
        return {}
    try:
        decoded: Any = orjson.loads(proc.stdout)
    except orjson.JSONDecodeError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    _raise_if_vale_error_obj(decoded)
    return cast(dict[str, Any], decoded)


def _raise_if_vale_error(stderr: str) -> None:
    """Raise :class:`_ValeRunError` when *stderr* parses to a Vale error object.

    Vale emits its runtime-error JSON (``E100`` for an unsynced ``StylesPath``,
    a config parse error, …) on stderr. A stderr body that is not JSON, or is
    JSON but not the error-object shape, is ignored (best-effort diagnostics).
    """
    try:
        decoded: Any = orjson.loads(stderr)
    except orjson.JSONDecodeError:
        return
    if isinstance(decoded, dict):
        _raise_if_vale_error_obj(decoded)


def _raise_if_vale_error_obj(decoded: dict[str, Any]) -> None:
    """Raise :class:`_ValeRunError` when *decoded* is the Vale error-object shape.

    The error object carries a top-level ``"Code"`` key (``E100`` …) whose
    sibling values are scalars, not the per-path alert lists of a normal run.
    """
    if "Code" in decoded and not any(isinstance(v, list) for v in decoded.values()):
        detail = str(decoded.get("Text") or decoded.get("Code") or "vale runtime error")
        raise _ValeRunError(detail.splitlines()[0])


def _emit_vale_skip(*, reason: str, flags: GlobalFlags) -> None:
    """Emit the advisory fail-open envelope when Vale cannot run, exit 0."""
    payload: dict[str, object] = {
        "hook": "vale-prose",
        "skipped": True,
        "clean": True,
        "reason": reason,
    }
    emit_json_or_text(payload, f"vale-prose: skipped ({reason})", flags=flags)


@hook_app.command(name="vale-prose")
def vale_prose(
    ctx: typer.Context,
    files: _FilesArg = None,
    base: _BaseOpt = "origin/main",
    text_surface: Annotated[
        str,
        typer.Option(
            "--text-surface",
            help="Lint a literal prose string (commit/PR body, state.json "
            "description) instead of files: the text is written to a temp "
            "markdown file, linted, then discarded. Mutually exclusive with "
            "file args.",
        ),
    ] = "",
    surface_label: Annotated[
        str,
        typer.Option(
            "--surface-label",
            help="Human label for the --text-surface bridge (e.g. 'commit "
            "body'); shown in findings instead of the temp path.",
        ),
    ] = "<text surface>",
) -> None:
    """Run the Vale prose linter over Markdown and emit the findings.

    Subprocesses ``vale --output=JSON <file>`` and emits the parsed alerts
    through the same static-lint envelope as the EAWF lints. Two input modes:

    - **files** — the given ``.md`` files (or, when none are given, the staged
      ``.md`` delta per the conditional scan).
    - **--text-surface** — a literal prose string (a commit body, PR body, or
      ``state.json`` description); Vale lints files on disk, so the text is
      written to a temporary markdown file, linted, then deleted. The findings
      report ``--surface-label`` instead of the temp path.

    When the ``vale`` binary is absent the gate **fails open**: it emits an
    advisory skip note and exits 0 (the prose lint is advisory, never a
    hard block on a machine without Vale installed). On a clean lint it also
    exits 0; findings are emitted non-blocking (advisory-first per the
    doc-clarity rollout).
    """
    import shutil
    import tempfile

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()

    if shutil.which("vale") is None:
        _emit_vale_skip(reason="vale binary not found on PATH", flags=flags)
        return

    if text_surface:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir) / "surface.md"
            tmp.write_text(text_surface, encoding="utf-8")
            try:
                payload = _run_vale_json([tmp], cwd=cwd)
            except _ValeRunError as exc:
                _emit_vale_skip(reason=str(exc), flags=flags)
                return
            rows = _vale_findings_to_rows(payload, rel_for={str(tmp): surface_label})
        _emit_static_lint_result(
            hook_name="vale-prose", rows=rows, scanned=1, flags=flags, blocking=False
        )
        return

    paths = _resolve_scan_paths(files, hook_name="vale-prose", base=base, cwd=cwd)
    targets = [cwd / rel for rel in paths if rel.endswith(".md") and (cwd / rel).exists()]
    if not targets:
        emit_json_or_text(
            {"hook": "vale-prose", "scanned": 0, "clean": True},
            "vale-prose: clean (0 file(s) scanned)",
            flags=flags,
        )
        return
    try:
        payload = _run_vale_json(targets, cwd=cwd)
    except _ValeRunError as exc:
        _emit_vale_skip(reason=str(exc), flags=flags)
        return
    rel_for = {str(cwd / rel): rel for rel in paths}
    rows = _vale_findings_to_rows(payload, rel_for=rel_for)
    _emit_static_lint_result(
        hook_name="vale-prose", rows=rows, scanned=len(targets), flags=flags, blocking=False
    )


def _collect_vale_rows(
    targets: list[Path], *, cwd: Path, rel_for: dict[str, str]
) -> tuple[str, ...]:
    """Run the Vale subprocess over *targets* and return rendered rows, fail-open.

    The Vale leg of the ``validate-prose`` chokepoint. Vale is a subprocess that
    fails open: when the binary is absent, or it emits its error-object JSON (an
    unsynced ``StylesPath``), this returns an empty tuple so the deterministic
    EAWF013/014/017 legs still run. The returned rows are the same
    ``  label:line:col: sev Check Message`` shape :func:`_vale_findings_to_rows`
    emits, ready to fold into :func:`~eawf.platform.lint.validate_prose.validate_prose`.
    """
    import shutil

    if shutil.which("vale") is None or not targets:
        return ()
    try:
        payload = _run_vale_json(targets, cwd=cwd)
    except _ValeRunError:
        return ()
    return tuple(_vale_findings_to_rows(payload, rel_for=rel_for))


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
    EAWF017 inline-reference tabulation in-process) and folds in the Vale
    subprocess leg, over the given ``.md`` files — or, when none are given, the
    staged ``.md`` delta per the conditional scan.

    Two modes implement the stability contract:

    - **fail-open** (default) — a local pre-commit / in-skill run. Findings are
      emitted as advisory and the gate exits 0, never blocking the operator.
    - **fail-closed** (``--strict``) — the CI gate. Any finding from the
      composed deterministic lints exits 1, blocking the PR.

    The Vale leg always fails open (an absent binary or an unsynced
    ``StylesPath`` yields no Vale rows), so ``--strict`` still rejects a
    known-bad artifact on a machine without Vale — the deterministic
    EAWF013/014/017 legs run regardless.
    """
    from eawf.platform.lint.validate_prose import validate_prose

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = _resolve_scan_paths(files, hook_name="validate-prose", base=base, cwd=cwd)
    md_paths = [rel for rel in paths if rel.endswith(".md") and (cwd / rel).exists()]
    targets = [cwd / rel for rel in md_paths]
    rel_for = {str(cwd / rel): rel for rel in md_paths}
    vale_rows = _collect_vale_rows(targets, cwd=cwd, rel_for=rel_for)

    rows: list[str] = []
    for rel in md_paths:
        try:
            source = (cwd / rel).read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        # The Vale rows already carry their file label; pass them only to the
        # report whose surface they belong to so a finding is attributed once.
        per_file_vale = tuple(row for row in vale_rows if row.lstrip().startswith(f"{rel}:"))
        report = validate_prose(source, strict=strict, vale_rows=per_file_vale)
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
    if not report.clean:
        body = (
            f"plugin-doctor-drift: drift detected "
            f"(drifted={len(report.drifted)} missing={len(report.missing)}) — run eawf plugin sync"
        )
        emit_json_or_text(payload, body, flags=flags)
        raise typer.Exit(exit_codes.USER_ERROR)
    emit_json_or_text(payload, "plugin-doctor-drift: clean", flags=flags)


__all__ = [
    "hook_app",
]
