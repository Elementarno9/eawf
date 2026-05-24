"""``eawf hook run <event>`` Typer command.

Surface contract:

- ``eawf hook run <event_type> [--runtime <claude|opencode|generic>]
  [--scope <ID>] [--command <str>]`` reads a JSON payload from stdin,
  builds a typed :class:`~eawf.runtime.hooks.event.HookEvent`, dispatches it
  through a fresh :class:`~eawf.runtime.hooks.runner.HookRunner`, and emits an
  :class:`~eawf.surfaces.render.envelope.OutputEnvelope` to stdout.
- Exit ``0`` when no registered hook returns ``block=True``.
- Exit ``9`` (``HOOK_BLOCKED``) when at least one hook reports a block.
- Exit ``3`` (``INVALID_INPUT``) when the stdin payload is not valid JSON
  or is not a mapping.

The runner mounted by this command starts empty: registration is the
runtime adapter's job (W05 wires up the Claude-installed hook
callables; the v1 surface here is the CLI dispatch primitive). When no
hook is registered the result list is empty and the exit code is
``0``.
"""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

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


# --- Diff-scoped lint gates (hooks 16-19 per C09 §5.3) ---------------------
# Each gate is a thin CLI dispatcher: it resolves the files to inspect
# (explicit args, else the conditional diff scan) and delegates the
# actual detection to a library surface (the scrubber patterns reused
# from ``eawf.logging.scrub`` for leaks, the EAWF001 rule for log
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
    :data:`eawf.logging.scrub.SensitiveScrubber.PATTERNS` so the gate
    and the emit-time scrubber never drift.
    """
    from eawf.logging.scrub import SensitiveScrubber

    return tuple(
        p for p in SensitiveScrubber.PATTERNS if "Users" in p.pattern or "home" in p.pattern
    )


def _email_leak_pattern() -> re.Pattern[str]:
    """Return the email pattern reused from the scrubber."""
    from eawf.logging.scrub import SensitiveScrubber

    return next(p for p in SensitiveScrubber.PATTERNS if "@" in p.pattern)


def _allowed_emails() -> frozenset[str]:
    """Return the canonical email allowlist (no-reply + pyproject authors).

    Reuses the scrubber's allowlist derivation so the gate accepts
    exactly the addresses the emit-time filter preserves: the no-reply
    co-author addresses plus the canonical ``pyproject.toml`` author
    rows.
    """
    from eawf.logging.scrub import _DEFAULT_ALLOWED_EMAILS, _eawf_author_emails

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
    :func:`eawf.lint._conditional.relevant_for_hook` (``staged=True``),
    which yields only the staged files relevant to ``hook_name``. The
    staged scope makes a real ``git commit`` scan only its delta and
    ``pre-commit run --all-files`` (nothing staged) a clean no-op
    rather than re-scanning the whole tree. The early-exit signal is an
    empty list.
    """
    from eawf.lint._conditional import relevant_for_hook

    if files:
        return files
    candidates = relevant_for_hook(hook_name, base, cwd=cwd, staged=True)
    return [p for p in candidates if not _is_state_bookkeeping_path(p)]


def _is_state_bookkeeping_path(rel: str) -> bool:
    """Return ``True`` for daemon-managed bookkeeping files excluded from leak scans.

    ``.ea/state.json`` + ``.ea/store/*.jsonl`` are the daemon's canonical
    bookkeeping surface (AGENTS rule 4): their content is machine-written,
    their line offsets churn on every mutation, and free-text fields
    (backlog titles, outcomes) may legitimately carry home-directory
    path-shaped placeholders. ``detect-secrets`` excludes the
    same set for the same reasons, so the leak gates mirror it rather than
    block every state-bookkeeping commit.
    """
    norm = rel.replace("\\", "/")
    if norm == ".ea/state.json":
        return True
    return norm.startswith(".ea/store/") and norm.endswith(".jsonl")


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
    from eawf.runtime.hooks.runner import HookRunner
    from eawf.workflow.agent_report.store import (
        AgentReportRoleMismatchError,
        AgentReportScrubError,
        append_agent_report,
        parse_agent_report_body,
    )

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
        try:
            agent_payload = AgentEndPayload.model_validate(payload)
            state_path = resolve_state_path(flags.workspace)
            state = _load_state(state_path)
            body = parse_agent_report_body(agent_payload.body)
            result = append_agent_report(
                state=state,
                state_path=state_path,
                session_id=agent_payload.session_id,
                base_id=agent_payload.base_id,
                body=body,
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
        return

    runner = HookRunner()
    # The v1 CLI surface dispatches with no registered hooks — runtime
    # adapters (W05) will register hooks via a sidecar config. The
    # empty-bucket path is the documented success case (no-op exit 0).
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
        raise typer.Exit(exit_codes.HOOK_BLOCKED)


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
    from eawf.lint.eawf001 import check_source

    flags: GlobalFlags = ctx.obj
    cwd = (flags.workspace or Path.cwd()).resolve()
    paths = _resolve_scan_paths(files, hook_name="log-format-lint", base=base, cwd=cwd)
    rows: list[str] = []
    violation_count = 0
    for rel in paths:
        if not rel.endswith(".py"):
            continue
        target = cwd / rel
        try:
            source = target.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        try:
            violations = check_source(source, filename=rel)
        except SyntaxError:
            continue
        for violation in violations:
            violation_count += 1
            rows.append(f"  {rel}:{violation.render()}")
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
    from eawf.lint._conditional import relevant_for_hook
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
