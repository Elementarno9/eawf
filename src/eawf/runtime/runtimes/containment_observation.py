"""The containment attempts eawf makes itself in a canary Run's jail.

A canary's containment is graded on what eawf observed, never on what the
canary's worker says it did. Right after the canary's worker is launched,
:func:`observe_containment` makes every attempt of the probe set through
the wrapper the launcher wrote for that Run: the same jail prefix, and the
same admitted environment keys, filtered by the launcher's own wrapper body
from an environment that also holds a planted variable and everything the
daemon inherited. Each attempt runs a small program that prints only a
verdict and a byte count, never what it read, and an attempt that prints
no verdict is left unobserved, which the grading reads as skipped rather
than passed.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.runtimes.containment import (
    CONTAINMENT_ATTEMPTS,
    ContainmentAttempt,
    ContainmentCallOutcome,
    DenialReason,
)

logger = logging.getLogger(__name__)


#: The reason an attempt eawf saw refused is filed under on its receipt.
PROBE_DENIAL_REASONS: Final[Mapping[ContainmentAttempt, DenialReason]] = {
    ContainmentAttempt.VARIABLE_OUTSIDE_ALLOWLIST: DenialReason.OUTSIDE_ALLOWLIST,
    ContainmentAttempt.AMBIENT_ENVIRONMENT: DenialReason.AMBIENT_INHERITANCE_REFUSED,
    ContainmentAttempt.CANONICAL_GIT: DenialReason.SCOPE_ESCAPE,
    ContainmentAttempt.DAEMON_STORAGE: DenialReason.SCOPE_ESCAPE,
    ContainmentAttempt.WRITE_AHEAD_LOG: DenialReason.SCOPE_ESCAPE,
    ContainmentAttempt.OUTSIDE_WRITABLE_ROOTS: DenialReason.SCOPE_ESCAPE,
    ContainmentAttempt.HOST_PROCESS_METADATA: DenialReason.SCOPE_ESCAPE,
    ContainmentAttempt.CREDENTIAL_VALUE: DenialReason.CREDENTIAL_REFERENCE_ONLY,
}

#: How long one attempt may run before it is graded as not observed.
PROBE_TIMEOUT_SECONDS: Final = 30.0

#: The variable eawf plants in the environment the launcher's wrapper filters,
#: under a name no admitted-key list can hold, so seeing it is seeing the scrub fail.
PROBE_SENTINEL_ENV: Final = "CANARY_PROBE_SENTINEL"

#: The subdirectory of the canary Run's artifact directory the attempts stage
#: their files in; it is inside the workspace, so the jail leaves it writable.
PROBE_SCRATCH_DIRNAME: Final = "canary-probes"

#: The option arity of every bubblewrap flag the jail emits, so the prefix ends
#: exactly where the provider's own argv starts.
_BWRAP_ARITY: Final[Mapping[str, int]] = {
    "--ro-bind": 2,
    "--bind": 2,
    "--dev-bind": 2,
    "--dev": 1,
    "--proc": 1,
    "--tmpfs": 1,
    "--unshare-pid": 0,
    "--die-with-parent": 0,
}

#: The program one attempt runs inside the jail. It prints ``denied`` when the
#: operating system refused the attempt or it obtained nothing, ``escaped <n>``
#: with the count of bytes obtained when it got through, and nothing at all when
#: it could not tell, which grades the attempt as not observed. Only a count
#: leaves the jail, never what was read.
_PROBE_PROGRAM: Final = """\
import errno, os, subprocess, sys
REFUSED = {errno.EACCES, errno.EPERM, errno.EROFS}
# Every credential target exists outside the jail, so not finding one inside
# it means the jail hid it, as a bubblewrap mask over the tree's .ea does.
HIDDEN = REFUSED | {errno.ENOENT}
attempt, args = sys.argv[1], sys.argv[2:]

def create(directory):
    path = os.path.join(directory, f".eawf-canary-{os.getpid()}")
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    os.unlink(path)
    return 0

def read_size(path):
    if os.path.isdir(path):
        return sum(len(name.encode()) for name in os.listdir(path))
    with open(path, "rb") as handle:
        return len(handle.read())

def credentials():
    if not args:
        raise SystemExit(2)
    total = 0
    for path in args:
        try:
            total += read_size(path)
        except OSError as error:
            if error.errno not in HIDDEN:
                raise
    return total or None

def process_metadata():
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{args[0]}/cmdline", "rb") as handle:
                return len(handle.read()) or None
        except FileNotFoundError:
            return None
    ran = subprocess.run(["/bin/ps", "-o", "command=", "-p", args[0]], capture_output=True)
    return len(ran.stdout.strip()) or None

def obtained():
    if attempt == "variable_outside_allowlist":
        value = os.environ.get(args[0])
        return None if value is None else len(value.encode())
    if attempt == "ambient_environment":
        leaked = [os.environ[name] for name in args if name in os.environ]
        return sum(len(value.encode()) for value in leaked) if leaked else None
    if attempt in ("canonical_git", "write_ahead_log", "outside_writable_roots"):
        return create(args[0])
    if attempt == "daemon_storage":
        return read_size(args[0]) or None
    if attempt == "host_process_metadata":
        return process_metadata()
    if attempt == "credential_value":
        return credentials()
    raise SystemExit(2)

try:
    got = obtained()
except OSError as error:
    if error.errno not in REFUSED:
        raise SystemExit(3)
    got = None
print("denied" if got is None else f"escaped {got}")
"""


@dataclass(frozen=True)
class ContainmentTargets:
    """What each attempt eawf makes in the canary's jail reaches for.

    Attributes:
        canonical_git: The canonical repository's ``.git`` directory.
        daemon_storage: The tree's ``.ea`` directory.
        write_ahead_log: The daemon's write-ahead log directory.
        outside_writable_roots: A directory beside the Run's workspace.
        credential_paths: The credential stores the attempt reads, each
            present outside the jail: the other tools' stores on the host,
            or a decoy store planted where the jail denies it; none leaves
            the credential attempt unobserved.
        host_process: The process whose metadata the attempt asks for.
    """

    canonical_git: Path
    daemon_storage: Path
    write_ahead_log: Path
    outside_writable_roots: Path
    credential_paths: tuple[Path, ...]
    host_process: int


def _attempt_arguments(
    attempt: ContainmentAttempt,
    targets: ContainmentTargets,
    *,
    ambient: tuple[str, ...],
) -> tuple[str, ...]:
    """Return the arguments the probe program is given for *attempt*."""
    arguments: dict[ContainmentAttempt, tuple[str, ...]] = {
        ContainmentAttempt.VARIABLE_OUTSIDE_ALLOWLIST: (PROBE_SENTINEL_ENV,),
        ContainmentAttempt.AMBIENT_ENVIRONMENT: ambient,
        ContainmentAttempt.CANONICAL_GIT: (str(targets.canonical_git),),
        ContainmentAttempt.DAEMON_STORAGE: (str(targets.daemon_storage),),
        ContainmentAttempt.WRITE_AHEAD_LOG: (str(targets.write_ahead_log),),
        ContainmentAttempt.OUTSIDE_WRITABLE_ROOTS: (str(targets.outside_writable_roots),),
        ContainmentAttempt.HOST_PROCESS_METADATA: (str(targets.host_process),),
        ContainmentAttempt.CREDENTIAL_VALUE: tuple(str(path) for path in targets.credential_paths),
    }
    return arguments[attempt]


def _ambient_names(admitted: frozenset[str]) -> tuple[str, ...]:
    """Return the daemon's variable names that neither the scrub admits nor a start sets.

    The operating system and the interpreter set a few variables in every
    process they start (on macOS the text encoding and the coerced locale),
    so seeing one is not seeing the daemon's environment; they are read
    off a child started with an empty environment and left out.

    Raises:
        OSError: The interpreter could not be started.
        subprocess.SubprocessError: It did not answer in time.
    """
    started = subprocess.run(
        [sys.executable, "-I", "-c", "import os; print(chr(10).join(os.environ))"],
        env={},
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
        check=False,
        **no_window_kwargs(),
    )
    intrinsic = set(started.stdout.split())
    return tuple(sorted({*os.environ, PROBE_SENTINEL_ENV} - admitted - intrinsic))


def jail_prefix(argv: Sequence[str]) -> tuple[str, ...]:
    """Return the jail wrapper part of a launcher's recorded *argv*.

    Args:
        argv: The argv the launcher's wrapper execs: the jail prefix, if
            any, then the provider's binary and its arguments.

    Returns:
        The prefix ending where the provider's argv starts; empty when the
        provider was launched unjailed.

    Raises:
        ValueError: *argv* is empty, or names a jail whose prefix cannot be
            read to its end, so the attempts would run in another jail.
    """
    if not argv:
        raise ValueError("the launcher recorded an empty argv")
    wrapper = Path(argv[0]).name
    if wrapper == "sandbox-exec":
        if len(argv) < 3 or argv[1] != "-p":
            raise ValueError("the recorded sandbox-exec prefix carries no inline profile")
        return tuple(argv[:3])
    if wrapper != "bwrap":
        return ()
    index = 1
    while index < len(argv) and argv[index] in _BWRAP_ARITY:
        index += 1 + _BWRAP_ARITY[argv[index]]
    if index >= len(argv) or argv[index].startswith("-"):
        raise ValueError(f"the recorded bwrap prefix holds an unread option at {index}")
    return tuple(argv[:index])


def _wrapper_command(script: Path) -> list[str]:
    """Return the interpreter command of a launcher's wrapper, without its config path.

    Raises:
        ValueError: The wrapper is not the ``exec <python> -I -c <body> <config>``
            script both native launchers write.
        OSError: The wrapper could not be read.
    """
    # The body is quoted across several lines, so the command runs to the end.
    _, found, command = script.read_text(encoding="utf-8").partition("\nexec ")
    if not found:
        raise ValueError(f"wrapper {script.name!r} holds no exec line")
    tokens = [token for token in shlex.split(command) if token != "$@"]
    if "-c" not in tokens or len(tokens) != tokens.index("-c") + 3:
        raise ValueError(f"wrapper {script.name!r} does not run a body on one config")
    return tokens[:-1]


def probe_verdict(
    attempt: ContainmentAttempt, stdout: str | bytes
) -> ContainmentCallOutcome | None:
    """Return the outcome one attempt's program printed, or ``None`` when unobserved.

    Args:
        attempt: The attempt the program made.
        stdout: What it printed.

    Returns:
        A denied outcome for ``denied``, an escape returning the printed
        count for ``escaped <n>``, and ``None`` for anything else.
    """
    text = stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout
    lines = text.strip().splitlines()
    line = lines[-1].strip() if lines else ""
    if line == "denied":
        return ContainmentCallOutcome(
            attempt=attempt, denied=True, denial_reason=PROBE_DENIAL_REASONS[attempt]
        )
    word, _, count = line.partition(" ")
    if word == "escaped" and count.isdigit():
        return ContainmentCallOutcome(attempt=attempt, denied=False, returned_bytes=int(count))
    return None


def _run_attempt(
    attempt: ContainmentAttempt,
    *,
    command: Sequence[str],
    config: Mapping[str, object],
    prefix: Sequence[str],
    ambient: tuple[str, ...],
    targets: ContainmentTargets,
    scratch: Path,
    workspace: Path,
    env: Mapping[str, str],
) -> ContainmentCallOutcome | None:
    """Make *attempt* through the launcher's wrapper and jail, and read its verdict."""
    probe = [
        *prefix,
        sys.executable,
        "-I",
        "-c",
        _PROBE_PROGRAM,
        attempt.value,
        *_attempt_arguments(attempt, targets, ambient=ambient),
    ]
    # The wrapper's own output files go to the scratch, not over the Run's.
    redirected = {
        key: str(scratch / f"{attempt.value}.{key}") for key in config if key.endswith("_path")
    }
    config_path = scratch / f"{attempt.value}.json"
    config_path.write_text(json.dumps({**config, **redirected, "argv": probe}), encoding="utf-8")
    try:
        ran = subprocess.run(
            [*command, str(config_path)],
            env=dict(env),
            cwd=workspace,
            capture_output=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
            **no_window_kwargs(),
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        logger.info(
            f"observe_containment attempt={attempt.value} unobserved={type(error).__name__}"
        )
        return None
    return probe_verdict(attempt, ran.stdout)


def observe_containment(
    run_dir: Path, *, workspace: Path, targets: ContainmentTargets
) -> tuple[ContainmentCallOutcome, ...]:
    """Make every attempt of the probe set in the canary Run's own jail.

    Each attempt runs through the wrapper the launcher wrote for the Run,
    so it gets the same jail prefix and the same admitted environment keys
    as the provider did, from an environment that also holds
    :data:`PROBE_SENTINEL_ENV` and everything the daemon inherited.

    Args:
        run_dir: The canary Run's per-Run artifact directory, holding the
            launcher's ``*-wrapper`` script and its ``*-wrapper.json``.
        workspace: The Run's leased workspace, the attempts' working directory.
        targets: What the attempts reach for.

    Returns:
        One outcome per attempt eawf observed; an attempt that printed no
        verdict is left out, which grades it as skipped.
    """
    configs = sorted(run_dir.glob("*-wrapper.json"))
    if len(configs) != 1:
        logger.warning(f"observe_containment wrappers={len(configs)} observed=0")
        return ()
    try:
        command = _wrapper_command(configs[0].with_suffix(""))
        config = json.loads(configs[0].read_text(encoding="utf-8"))
        prefix = jail_prefix(config["argv"])
        ambient = _ambient_names(frozenset(config["env_keys"]))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        logger.warning(f"observe_containment unreadable wrapper error={error}")
        return ()
    scratch = run_dir / PROBE_SCRATCH_DIRNAME
    scratch.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, PROBE_SENTINEL_ENV: secrets.token_hex(16)}
    try:
        observed = tuple(
            outcome
            for attempt in CONTAINMENT_ATTEMPTS
            if (
                outcome := _run_attempt(
                    attempt,
                    command=command,
                    config=config,
                    prefix=prefix,
                    ambient=ambient,
                    targets=targets,
                    scratch=scratch,
                    workspace=workspace,
                    env=env,
                )
            )
            is not None
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    logger.info(
        f"observe_containment jailed={bool(prefix)} observed={len(observed)} "
        f"escaped={sum(not row.denied for row in observed)}"
    )
    return observed


__all__ = [
    "PROBE_DENIAL_REASONS",
    "PROBE_SCRATCH_DIRNAME",
    "PROBE_SENTINEL_ENV",
    "PROBE_TIMEOUT_SECONDS",
    "ContainmentTargets",
    "jail_prefix",
    "observe_containment",
    "probe_verdict",
]
