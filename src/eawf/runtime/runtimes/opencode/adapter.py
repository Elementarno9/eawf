"""OpenCode adapter — implements :class:`RuntimeAdapter`.

OpenCode CLI = ``opencode run``. There is no caller-side
``cache_control`` marker (the bundled ``@ai-sdk/anthropic`` provider
injects it internally + the live regression at
``anomalyco/opencode#17910`` strips the marker for the OAuth-Claude
auth path); :attr:`supports_cache_control` is ``False``.

OpenCode uses a two-store layout: a SQLite primary store + auxiliary
diff arrays. The :meth:`session_log_handle` returns an opaque URN; the
daemon resolves the SQLite path via its in-process map. "session-log
path nonexistent" is a documented v0.3 risk — :meth:`supports_continue`
therefore returns ``False`` until the documented path ships in v0.4.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from eawf.kernel.state.models import SessionAttempt, Wave
from eawf.runtime.runtimes.adapter import (
    ErrorClass,
    RuntimeAdapter,
    RuntimeSpawnError,
    SessionResumeFailedError,
    SpawnResult,
)
from eawf.runtime.runtimes.cache_control import inject_cache_control
from eawf.runtime.runtimes.selector import runtime_supports
from eawf.runtime.sandbox.cwd_guard import is_path_inside
from eawf.runtime.sandbox.jail import jail_command, jail_supported

if TYPE_CHECKING:
    from eawf.workflow.agents.specs.models import RoleContract

logger = logging.getLogger(__name__)

#: This adapter's canonical runtime id, reused by the local env-scrub /
#: jail helpers so the floor logging carries the lane.
_RUNTIME_ID: str = "opencode"

_RATE_LIMIT_RE = re.compile(rb"\b(?:429|rate[_ -]?limit)\b", re.IGNORECASE)
_AUTH_RE = re.compile(
    rb"\b(?:401|403|invalid_api_key|oauth_expired|unauthor[iz][sez]+ed)\b",
    re.IGNORECASE,
)
_SERVER_RE = re.compile(rb"\b5\d\d\b")
_TIMEOUT_RE = re.compile(rb"\b(?:timeout|deadline_exceeded)\b", re.IGNORECASE)
_API_RE = re.compile(rb"\b4\d\d\b")

# ---------------------------------------------------------------------------
# Floor helpers (env-scrub + OS jail), kept opencode-local
# ---------------------------------------------------------------------------
#
# The shared floor modules (``eawf.runtime.sandbox.env_scrub`` /
# ``eawf.runtime.sandbox.jail``) only register the claude-code + codex auth
# lanes today, so ``build_child_env("opencode")`` and
# ``jail_command(runtime="opencode")`` both raise ValueError for this lane.
# Until the opencode lane is registered there, this module keeps a local
# env-scrub allowlist for the opencode auth family and a local jail
# passthrough that fail-soft degrades to unjailed when the shared jail
# rejects the unknown lane -- the same fail-soft contract the claude helper
# already uses for a missing wrapper binary. The FS-jail still confines the
# child once the lane is registered upstream.

#: The PINNED PATH floor -- the parent ``PATH`` is deliberately never passed
#: through so a hostile entry injected into the operator's ``PATH`` cannot
#: reach the spawned child. Mirrors the shared env-scrub floor.
_PINNED_PATH: str = "/usr/bin:/bin:/usr/sbin:/sbin"

#: Locale default seeded when the base env carries no ``LANG``.
_DEFAULT_LANG: str = "C.UTF-8"

#: Exact-match floor keys copied verbatim from the base env when present.
#: ``PATH`` is pinned (never copied); ``LANG`` is defaulted when absent.
_FLOOR_EXACT_KEYS: frozenset[str] = frozenset({"HOME", "TERM"})

#: Floor locale carry-through prefix. ``LANG`` is seeded separately.
_FLOOR_PREFIXES: tuple[str, ...] = ("LC_",)

#: opencode-lane auth: prefix family kept when present. opencode reads its
#: own credential from its on-disk store under the data dir; the ``OPENCODE_*``
#: prefix carries the data-dir override + feature flags the child needs.
_OPENCODE_AUTH_PREFIXES: tuple[str, ...] = ("OPENCODE_",)

#: The OS-jail wrapper binary per platform. The spawn jails the child only
#: when the platform supports it AND the wrapper resolves on PATH.
_JAIL_WRAPPER_BINARY: dict[str, str] = {
    "darwin": "sandbox-exec",
    "linux": "bwrap",
}


def _build_child_env(
    *,
    base_env: Mapping[str, str] | None = None,
    extra_path_dir: str | None = None,
) -> dict[str, str]:
    """Build a scrubbed child environment for the opencode lane.

    Constructs the env an ``env -i``-equivalent allowlist would: start from
    empty, seed the shared floor (``HOME``, a pinned ``PATH``, ``LANG`` /
    ``LC_*``, ``TERM``), then add only the ``OPENCODE_*`` auth family. Every
    variable not on the allowlist -- ``AWS_*``, ``GH_*`` / ``GITHUB_*``,
    ``SSH_*``, ``KUBECONFIG``, the cross-lane ``ANTHROPIC_*`` / ``OPENAI_*``
    credentials, and any unknown variable -- is absent by construction.

    Kept opencode-local because the shared
    :func:`eawf.runtime.sandbox.env_scrub.build_child_env` does not yet
    register the opencode lane (it raises ValueError for it); this mirrors
    that module's allowlist floor with the opencode auth family.

    Args:
        base_env: The source environment to filter. Defaults to
            :data:`os.environ`; tests inject a fake mapping rather than
            mutating the real process environment.
        extra_path_dir: An additional directory PREPENDED to the pinned
            ``PATH`` floor. The spawn passes the resolved opencode binary's
            own directory here so the child can exec its CLI even when the
            binary lives outside the pinned floor (e.g. a Homebrew prefix);
            the parent ``PATH`` itself is still never passed through. ``None``
            keeps the floor pinned verbatim.

    Returns:
        A fresh ``dict`` of the scrubbed child environment. Always carries a
        pinned ``PATH`` and a ``LANG`` (defaulted to ``C.UTF-8`` when the
        base env has none), even from an empty *base_env*.
    """
    source = os.environ if base_env is None else base_env
    child: dict[str, str] = {}
    for key in _FLOOR_EXACT_KEYS:
        value = source.get(key)
        if value is not None:
            child[key] = value
    if extra_path_dir and extra_path_dir not in _PINNED_PATH.split(os.pathsep):
        child["PATH"] = extra_path_dir + os.pathsep + _PINNED_PATH
    else:
        child["PATH"] = _PINNED_PATH
    child["LANG"] = source.get("LANG", _DEFAULT_LANG)
    keep_prefixes = _FLOOR_PREFIXES + _OPENCODE_AUTH_PREFIXES
    for key, value in source.items():
        if key in child:
            continue
        if any(key.startswith(prefix) for prefix in keep_prefixes):
            child[key] = value
    logger.info(
        f"_build_child_env runtime={_RUNTIME_ID!r} kept={len(child)} "
        f"dropped={max(len(source) - len(child), 0)}"
    )
    return child


def _resolve_binary_dir(binary: str) -> str | None:
    """Return the directory of *binary* resolved on the parent PATH.

    Resolved with :func:`shutil.which` against the parent ``PATH`` (before
    the env scrub pins it) so the scrubbed child -- whose ``PATH`` floor is
    minimal -- can still exec a CLI installed outside that floor (e.g. a
    Homebrew prefix). The resolution reads only the binary's location, never
    a credential, so it does not weaken the env-scrub floor.

    Args:
        binary: The bare CLI binary name (``"opencode"``).

    Returns:
        The absolute directory holding *binary*, or ``None`` when it does not
        resolve on the parent PATH (the spawn then relies on the pinned floor
        and surfaces a clear ``FileNotFoundError`` if the binary is absent).
    """
    resolved = shutil.which(binary)
    if resolved is None:
        return None
    return os.path.dirname(os.path.realpath(resolved))


def _jail_wrapper_binary(platform: str) -> str | None:
    """Return the jail wrapper binary for *platform*, or ``None``.

    Args:
        platform: The platform string (``sys.platform``).

    Returns:
        ``"sandbox-exec"`` on macOS, ``"bwrap"`` on Linux, ``None`` on a
        platform with no FS-jail wrapper (e.g. Windows).
    """
    if platform.startswith("linux"):
        return _JAIL_WRAPPER_BINARY["linux"]
    return _JAIL_WRAPPER_BINARY.get(platform)


def _repo_root_for(path: Path) -> Path | None:
    """Return the git repo root containing *path*, or ``None``.

    Resolved via ``git rev-parse --show-toplevel`` so the jail confines the
    child to its actual repo root. A path outside any git working tree
    yields ``None`` (the spawn then runs unjailed with a warning rather than
    confining to a bogus root).
    """
    from eawf.runtime.worktree.git import repo_root

    try:
        return repo_root(path)
    except Exception as exc:
        logger.warning(f"_repo_root_for path={path!s} reason={exc!r}")
        return None


def _maybe_jail_argv(argv: list[str], *, runtime: str, cwd: str | None) -> list[str]:
    """Prefix *argv* with the OS jail when the host supports it.

    The jail is applied only when (a) the platform has an FS-jail wrapper
    (:func:`~eawf.runtime.sandbox.jail.jail_supported`) AND (b) that wrapper
    binary resolves on PATH AND (c) the cwd resolves inside a discoverable
    repo root. When the wrapper is absent (e.g. CI without bubblewrap) the
    child runs UNJAILED with a loud warning -- this keeps the spawn working
    on hosts without the tool and keeps CI green.

    The shared jail does not yet register the opencode auth lane, so
    :func:`~eawf.runtime.sandbox.jail.jail_command` raises ValueError for it;
    that is caught here and the spawn degrades to unjailed-with-warning (the
    same fail-soft contract as a missing wrapper binary) rather than crashing
    the spawn. Once the lane is registered upstream the jail engages with no
    change here.

    The daemon stays the sole session-setter: only the argv gains the prefix
    here; ``start_new_session`` / ``env`` / ``cwd`` on the spawn are
    untouched, so the wrapper inherits the daemon-set process group and the
    kill ladder reaps the whole tree unchanged.

    Args:
        argv: The child's own argv (``["opencode", "run", ...]``).
        runtime: The runtime adapter id selecting the own-cred carve-out.
        cwd: The spawn cwd. ``None`` defaults the jail confinement to the
            repo root containing the process cwd; when the process cwd is not
            inside a discoverable root the spawn runs unjailed with a warning.

    Returns:
        Either ``jail_command(argv, ...)`` (jailed) or *argv* unchanged
        (unjailed fallback).
    """
    if not jail_supported():
        logger.warning(
            f"spawn_session jail=unavailable platform={sys.platform!r} runtime={runtime!r}"
        )
        return argv

    wrapper = _jail_wrapper_binary(sys.platform)
    if wrapper is None or shutil.which(wrapper) is None:
        logger.warning(
            f"spawn_session jail=unavailable wrapper={wrapper!r} runtime={runtime!r} "
            "reason=binary-not-on-path"
        )
        return argv

    cwd_path = Path(cwd) if cwd is not None else Path.cwd()
    root = _repo_root_for(cwd_path)
    if root is None or not cwd_path.exists() or not is_path_inside(cwd_path, root=root):
        logger.warning(
            f"spawn_session jail=unavailable runtime={runtime!r} cwd={cwd_path!s} "
            "reason=cwd-outside-repo-root"
        )
        return argv

    try:
        jailed = jail_command(argv, runtime=runtime, cwd=cwd_path, root=root)
    except ValueError as exc:
        logger.warning(
            f"spawn_session jail=unavailable runtime={runtime!r} cwd={cwd_path!s} "
            f"reason=lane-not-registered detail={exc!r}"
        )
        return argv
    logger.info(f"spawn_session jail=on wrapper={wrapper!r} runtime={runtime!r} cwd={cwd_path!s}")
    return jailed


@dataclass(frozen=True, slots=True)
class _StreamFields:
    """The fields collected from an opencode event stream.

    Intermediate value object so the per-event accumulation loop lives in a
    small dedicated helper (:func:`_collect_stream_fields`) and the result
    assembly in :func:`_parse_opencode_result` stays flat.

    Attributes:
        session_id: First non-empty ``sessionID`` seen (``""`` when none).
        text: Concatenated ``text`` event fragments.
        input_tokens: Billed non-cached input tokens.
        output_tokens: Billed output tokens.
        cache_read: Prompt-cache read tokens.
        cache_write: Prompt-cache write tokens.
        cost_reported: Runtime self-reported cost, or ``None`` when absent.
    """

    session_id: str
    text: str
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_write: int
    cost_reported: Decimal | None


def _assert_no_error_event(events: list[dict[str, object]]) -> None:
    """Raise when the stream carries an ``error`` event.

    Args:
        events: The decoded event objects in stream order.

    Raises:
        RuntimeSpawnError: An ``error`` event is present (the message is
            lifted from ``error.data.message``, falling back to
            ``error.name``).
    """
    for event in events:
        if event.get("type") != "error":
            continue
        err = event.get("error")
        detail: object = err
        if isinstance(err, dict):
            data = err.get("data")
            if isinstance(data, dict) and data.get("message") is not None:
                detail = data.get("message")
            else:
                detail = err.get("name")
        raise RuntimeSpawnError(f"opencode reported an error event: {detail!r}")


def _accumulate_step_finish(part: dict[str, object]) -> tuple[int, int, int, int, Decimal | None]:
    """Extract ``(input, output, cache_read, cache_write, cost)`` from a step.

    Args:
        part: The ``part`` map of a ``step_finish`` event.

    Returns:
        The token counts (defaulting to 0 when undisclosed) and the
        self-reported cost (``None`` when absent).
    """
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_write = 0
    tokens = part.get("tokens")
    if isinstance(tokens, dict):
        input_tokens = int(tokens.get("input", 0) or 0)
        output_tokens = int(tokens.get("output", 0) or 0)
        cache = tokens.get("cache")
        if isinstance(cache, dict):
            cache_read = int(cache.get("read", 0) or 0)
            cache_write = int(cache.get("write", 0) or 0)
    cost_field = part.get("cost")
    cost = Decimal(str(cost_field)) if cost_field is not None else None
    return input_tokens, output_tokens, cache_read, cache_write, cost


def _collect_stream_fields(events: list[dict[str, object]]) -> _StreamFields:
    """Fold an opencode event stream into its collected fields.

    Walks the decoded events once: captures the first non-empty
    ``sessionID``, concatenates ``text`` fragments, and reads the terminal
    ``step_finish`` token map + cost via :func:`_accumulate_step_finish`.

    Args:
        events: The decoded event objects in stream order (error events
            already rejected by :func:`_assert_no_error_event`).

    Returns:
        The collected :class:`_StreamFields`.
    """
    session_id = ""
    text_parts: list[str] = []
    input_tokens = output_tokens = cache_read = cache_write = 0
    cost_reported: Decimal | None = None
    for event in events:
        sid = event.get("sessionID")
        if not session_id and isinstance(sid, str) and sid:
            session_id = sid
        part = event.get("part")
        if not isinstance(part, dict):
            continue
        event_type = event.get("type")
        if event_type == "text":
            fragment = part.get("text")
            if isinstance(fragment, str):
                text_parts.append(fragment)
        elif event_type == "step_finish":
            input_tokens, output_tokens, cache_read, cache_write, cost_reported = (
                _accumulate_step_finish(part)
            )
    return _StreamFields(
        session_id=session_id,
        text="".join(text_parts),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read=cache_read,
        cache_write=cache_write,
        cost_reported=cost_reported,
    )


def _line_json_objects(raw: str) -> list[dict[str, object]]:
    """Parse the opencode ``--format json`` NDJSON stream into objects.

    The opencode ``run --format json`` stream is newline-delimited JSON --
    one object per line (``step_start`` / ``text`` / ``step_finish`` /
    ``error``). The terminal additionally interleaves OSC title-set escape
    sequences onto the same stream, so each line is trimmed to its first
    ``{`` before decode and a line that does not decode to a JSON object is
    skipped rather than failing the whole parse.

    Args:
        raw: The decoded stdout text of the spawn.

    Returns:
        The decoded JSON objects in stream order (non-object / non-JSON
        lines dropped).
    """
    objects: list[dict[str, object]] = []
    for line in raw.splitlines():
        brace = line.find("{")
        if brace == -1:
            continue
        candidate = line[brace:].strip()
        try:
            decoded = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            objects.append(decoded)
    return objects


def _parse_opencode_result(
    *,
    runtime: str,
    model: str,
    stdout: bytes,
    stderr: bytes,
    exit_status: int,
    subprocess_pid: int,
    started_at: datetime,
    ended_at: datetime,
) -> SpawnResult:
    """Parse an ``opencode run --format json`` event stream into a result.

    The line-delimited event stream carries ``text`` events (whose
    ``part.text`` fragments concatenate into the final answer), a terminal
    ``step_finish`` event (whose ``part.tokens`` map carries the
    ``input`` / ``output`` / ``reasoning`` counts + a nested
    ``cache.{read,write}`` map and whose ``part.cost`` carries the
    self-reported cost), and a ``sessionID`` on every event. An ``error``
    event marks a runtime-reported failure. Kept standalone (no adapter
    ``self``) so the parse is unit-testable against a fixed stream without
    spawning a subprocess.

    The function is fail-fast: every malformed-output condition raises
    :class:`~eawf.runtime.runtimes.adapter.RuntimeSpawnError` rather than
    returning a partially-populated result, so a caller never silently meters
    a garbage spawn. Token counts default to 0 when the stream does not
    disclose them -- a later metering writer prices a 0 row honestly.

    Args:
        runtime: Adapter id stamped onto the result (``"opencode"``).
        model: Model alias/id the spawn was requested with (``provider/model``).
        stdout: Raw subprocess stdout bytes (the NDJSON event stream).
        stderr: Raw subprocess stderr bytes (surfaced in the error on a
            non-zero exit).
        exit_status: Subprocess exit code.
        subprocess_pid: PID of the spawned subprocess.
        started_at: When the subprocess started.
        ended_at: When the subprocess exited.

    Returns:
        The validated :class:`SpawnResult` for the completed call.

    Raises:
        RuntimeSpawnError: non-zero exit, empty stdout, no decodable JSON
            events, or an ``error`` event in the stream.
    """
    if exit_status != 0:
        snippet = stderr.decode(errors="replace").strip()[:200]
        raise RuntimeSpawnError(
            f"opencode spawn exited nonzero: status={exit_status} stderr={snippet!r}",
            exit_status=exit_status,
            stderr=stderr,
        )
    raw = stdout.decode(errors="replace").strip()
    if not raw:
        raise RuntimeSpawnError("opencode spawn produced empty stdout")
    events = _line_json_objects(raw)
    if not events:
        raise RuntimeSpawnError("opencode output carried no decodable json events")

    _assert_no_error_event(events)
    fields = _collect_stream_fields(events)

    return SpawnResult(
        session_id=fields.session_id or f"{runtime}-{subprocess_pid}",
        runtime=runtime,
        model=model,
        resolved_model=None,
        subprocess_pid=subprocess_pid,
        exit_status=exit_status,
        text=fields.text,
        input_tokens=fields.input_tokens,
        output_tokens=fields.output_tokens,
        cache_creation_input_tokens=fields.cache_write,
        cache_creation_5m_input_tokens=fields.cache_write,
        cache_creation_1h_input_tokens=0,
        cache_read_input_tokens=fields.cache_read,
        cost_usd_reported=fields.cost_reported,
        started_at=started_at,
        ended_at=ended_at,
    )


class OpenCodeAdapter:
    """OpenCode runtime adapter (``opencode run`` subprocess primary).

    :attr:`accepts_continue` ships ``False`` in v0.3 because the
    session-log path catalog is not yet fully verified; the daemon
    treats every dispatch as fresh under that branch and avoids the
    fallback churn that would otherwise fire on every retry. v0.4 flips
    :attr:`accepts_continue` to ``True`` once the documented path lands.
    """

    id: str = "opencode"
    cli_binary: str = "opencode"
    # ``accepts_continue`` + ``supports_cache_control`` derive from the
    # YAML-backed capability matrix via
    # :func:`eawf.runtime.runtimes.selector.runtime_supports` — no parallel
    # hard-coded table.
    accepts_continue: bool = runtime_supports("opencode", "session_resume")
    supports_cache_control: bool = runtime_supports("opencode", "cache_control")
    error_classes_emitted: tuple[ErrorClass, ...] = (
        "RUNTIME_RATE_LIMIT",
        "RUNTIME_SERVER_ERROR",
        "RUNTIME_TIMEOUT",
        "RUNTIME_API_ERROR",
        "RUNTIME_AUTH_ERROR",
    )

    async def open_session(
        self,
        wave: Wave,
        prompt: str,
        *,
        cache_prefix: str | None = None,
        model_hint: str | None = None,
        role_contract: RoleContract | None = None,
    ) -> SessionAttempt:
        """Construct a fresh-session :class:`SessionAttempt` row.

        ``cache_prefix`` is routed through
        :func:`~eawf.runtime.runtimes.cache_control.inject_cache_control` for
        boundary parity, but OpenCode is a **no-op path**: the bundled
        ``@ai-sdk/anthropic`` provider injects ``cache_control``
        internally and the OAuth-Claude path strips any caller-side
        marker (upstream ``#17910``), so the eawf adapter has no
        caller-side knob and returns the prefix unchanged.

        The optional *role_contract* keyword carries the typed
        :class:`~eawf.workflow.agents.specs.models.RoleContract`
        projection of the dispatched wave's role. When present the
        spawn seam reads its ``system_prompt`` / ``allowed_tools`` /
        ``model`` fields rather than a hardcoded executor preamble;
        today the adapter only debug-logs the attach so callers can
        observe the seam wire-up, and the live ``opencode run`` spawn
        in P26-SURFACES consumes the contract to materialise the
        per-session ``.opencode/agent/<role>.md`` linkage. ``None``
        (the default) keeps the spawn byte-equivalent to the pre-W13
        surface.
        """

        injected_prefix = inject_cache_control(
            runtime_id=self.id,
            cache_prefix=cache_prefix,
        )
        if injected_prefix is not None:
            logger.debug(f"open_session runtime={self.id!r} cache_control=no_op")
        if role_contract is not None:
            logger.debug(
                f"open_session runtime={self.id!r} role={role_contract.role!r} "
                f"allowed_tools={len(role_contract.allowed_tools)} "
                f"denied_tools={len(role_contract.denied_tools)} "
                f"model={role_contract.model!r}"
            )
        session_id = str(uuid.uuid4())
        attempts = sorted(wave.sessions)
        next_attempt = (max(attempts) + 1) if attempts else 1
        return SessionAttempt(
            attempt=next_attempt,
            runtime=self.id,
            session_id=session_id,
            session_log_handle=self.session_log_handle(session_id),
            started_at=datetime.now(UTC),
        )

    async def spawn_session(
        self,
        prompt: str,
        *,
        model: str,
        cwd: str | None = None,
        extra_args: Sequence[str] = (),
        denied_tools: Sequence[str] = (),
        timeout: float | None = None,
        on_spawn: Callable[[int], None] | None = None,
    ) -> SpawnResult:
        """Spawn a live ``opencode run`` subprocess and collect its result.

        Runs ``opencode run --format json -m <provider/model> <prompt>`` via
        :func:`asyncio.create_subprocess_exec`, captures stdout (the
        newline-delimited JSON event stream) + stderr, and parses the outcome
        into a typed
        :class:`~eawf.runtime.runtimes.adapter.SpawnResult` (concatenated
        answer text + the per-call token classes + pid + exit + the optional
        self-reported cost). The child is started in its own session /
        process group (``start_new_session=True``) so a later cancel can
        signal the whole group by pgid. The child environment is SCRUBBED via
        the opencode-local :func:`_build_child_env` -- it receives an
        allowlist floor (pinned ``PATH`` + ``HOME`` / locale / ``TERM``) plus
        the ``OPENCODE_*`` family, never the full parent env (which would
        carry ``AWS_*`` / ``GH_*`` / ``SSH_*`` / cross-lane credentials into
        the child). The argv is additionally prefixed with the OS filesystem
        jail (bubblewrap / seatbelt) when the host supports it and the
        opencode auth lane is registered with the shared jail; otherwise the
        child runs unjailed with a warning rather than failing.

        denied-tools gap (honest): opencode has no clean per-call ``opencode
        run`` deny flag -- tool restriction is via ``permission:`` agent
        frontmatter / agent gating, not a CLI flag (the ``run --help`` flag
        set offers only ``--dangerously-skip-permissions``, the inverse). So
        a non-empty *denied_tools* is NOT mapped to an argv flag here; the set
        is logged and the FS-jail floor still confines the child's writes to
        its worktree. *denied_tools* stays in the signature for Protocol
        conformance. When opencode ships a per-call deny flag a follow-up maps
        it; faking a flag now would silently drop the deny.

        Args:
            prompt: Rendered prompt passed to ``opencode run``.
            model: Model id for ``-m`` in ``provider/model`` form (e.g.
                ``anthropic/claude-...``). No hardcoded floor -- the caller
                resolves it (the routing decision feeds this).
            cwd: Working directory for the subprocess; ``None`` inherits the
                parent's.
            extra_args: Extra CLI args appended verbatim (the routing /
                structured-output escape hatch).
            denied_tools: Per-wave sandbox deny-list (tool names). opencode
                has no per-call deny flag, so a non-empty set is logged (not
                mapped to argv); the FS-jail floor still confines the child.
            timeout: Wall-clock ceiling in seconds; ``None`` waits
                indefinitely. On expiry the child is killed and a typed error
                is raised.
            on_spawn: Optional callback invoked with the child PID right after
                spawn (before output is awaited).

        Returns:
            The validated :class:`SpawnResult` for the completed call.

        Raises:
            RuntimeSpawnError: the spawn timed out, exited non-zero, or
                returned an unparseable / error result stream.
        """

        if denied_tools:
            # opencode has no per-call deny flag; log the requested set so the
            # gap is observable. The FS-jail floor still confines the child.
            logger.warning(
                f"spawn_session runtime={self.id!r} denied_tools_unmapped="
                f"{sorted(denied_tools)!r} reason=no-per-call-deny-flag"
            )
        argv = [
            self.cli_binary,
            "run",
            "--format",
            "json",
            "-m",
            model,
            *extra_args,
            prompt,
        ]
        # Prefix the OS filesystem jail (bubblewrap / seatbelt) when the host
        # supports it. The daemon stays the sole session-setter:
        # ``start_new_session=True`` lands on the jail wrapper, which is the
        # group leader, so ``cancel_process_group(os.getpgid(pid))`` reaps the
        # whole tree unchanged. When the wrapper binary is absent (or the
        # opencode lane is not yet registered upstream) the child runs unjailed
        # with a warning.
        argv = _maybe_jail_argv(argv, runtime=self.id, cwd=cwd)
        # Carry the resolved binary's own directory onto the scrubbed child
        # PATH so the pinned floor can still exec the opencode CLI when it
        # lives outside that floor (e.g. a Homebrew prefix). The parent PATH
        # itself is never passed through.
        binary_dir = _resolve_binary_dir(self.cli_binary)
        started_at = datetime.now(UTC)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            cwd=cwd,
            env=_build_child_env(extra_path_dir=binary_dir),
        )
        pid = proc.pid
        logger.info(f"spawn_session runtime={self.id!r} pid={pid} model={model!r}")
        if on_spawn is not None:
            on_spawn(pid)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            # 124 is the conventional timeout exit code parse_error maps to
            # RUNTIME_TIMEOUT, so a classifier routes the V5 switch ladder.
            raise RuntimeSpawnError(
                f"opencode spawn timed out: timeout={timeout}s pid={pid}",
                exit_status=124,
            ) from None
        ended_at = datetime.now(UTC)
        return _parse_opencode_result(
            runtime=self.id,
            model=model,
            stdout=stdout or b"",
            stderr=stderr or b"",
            exit_status=proc.returncode if proc.returncode is not None else -1,
            subprocess_pid=pid,
            started_at=started_at,
            ended_at=ended_at,
        )

    async def continue_session(
        self,
        session_id: str,
        prompt: str,
    ) -> SessionAttempt:
        """OpenCode v0.3: session resume not yet supported.

        Per :attr:`accepts_continue` = ``False`` the daemon does not
        normally invoke this path; if it does the adapter raises
        :class:`SessionResumeFailedError` so the caller falls back to
        :meth:`open_session`.

        Raises:
            SessionResumeFailedError: OpenCode adapter does not support
                session resume in v0.3 (path catalog pending).
        """

        raise SessionResumeFailedError(
            f"opencode adapter does not support continue_session in v0.3: {session_id!r}"
        )

    def session_log_handle(self, session_id: str) -> str:
        """Daemon-internal opaque handle for the SQLite session log.

        Resolves to ``data-dir/storage/session.db`` via the daemon's
        in-process map (§5.4). The URN format matches the other
        adapters so the daemon's resolver is adapter-agnostic.
        """

        return f"urn:eawf:v1:session-log:{self.id}:{session_id}"

    def parse_error(
        self,
        exit_status: int,
        stderr: bytes,
    ) -> ErrorClass:
        """Map ``(exit_status, stderr)`` to a canonical class per §5.5.

        OpenCode-via-OAuth-Claude inherits the upstream auth regex
        ladder; per-vendor auth-failure phrasing is multi-form so
        the auth check stays generic (HTTP code substring + token
        keywords).
        """

        if _AUTH_RE.search(stderr):
            return "RUNTIME_AUTH_ERROR"
        if _RATE_LIMIT_RE.search(stderr):
            return "RUNTIME_RATE_LIMIT"
        if _TIMEOUT_RE.search(stderr) or exit_status in (-15, 124):
            return "RUNTIME_TIMEOUT"
        if _SERVER_RE.search(stderr):
            return "RUNTIME_SERVER_ERROR"
        if _API_RE.search(stderr):
            return "RUNTIME_API_ERROR"
        return "RUNTIME_API_ERROR"

    def supports_continue(self) -> bool:
        """Whether the adapter supports session resume.

        Returns ``False`` in v0.3; flips to ``True`` in v0.4 once the
        session-log path catalog ships.
        """

        return self.accepts_continue


_ADAPTER_CHECK: RuntimeAdapter = OpenCodeAdapter()

__all__ = ["OpenCodeAdapter"]
