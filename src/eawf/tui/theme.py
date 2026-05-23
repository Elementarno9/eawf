"""Per-theme palette definitions for the Eä TUI rebuild (tui).

The runtime ``/theme`` swap rebinds the semantic colour vars
(``$accent`` / ``$primary`` / ``$ok`` / ``$warn`` / ``$err`` / ``$muted``
and the ``$status-*`` lifecycle tints) at the App level. Those vars used
to live at global scope in ``theme.tcss``; a global definition cannot
change at runtime, so the swap would recolour nothing. Hosting the vars
inside each :class:`~textual.theme.Theme`'s ``variables`` map instead
lets :meth:`textual.app.App.get_css_variables` re-resolve every ``$var``
the structural CSS references when the active theme changes — the swap
becomes a pure var rebind, exactly as the structural CSS was written to
expect.

Every theme the App can switch to MUST carry the full semantic var set,
otherwise the structural CSS in ``theme.tcss`` references an undefined
var on that theme. The four operator-facing logical names map onto
registered Textual theme names through :data:`LOGICAL_THEMES`:

* ``dark`` → :data:`EA_DARK` — the Wong 2011 deuteranopia-safe palette,
  carrying the exact hex values that shipped at global scope in
  ``theme.tcss`` (so selecting ``dark`` is a no-op visual baseline; the
  migration introduces no regression).
* ``cb`` → :data:`EA_CB` — the IBM colour-blind-safe palette, visually
  distinct from Wong so a second swap is observable.
* ``light`` → :data:`EA_LIGHT` — a light-background variant that carries
  the same semantic var *names* tuned for a light surface, built on
  Textual's ``textual-light`` base colours.
* ``auto`` → terminal-background detect, resolving to ``dark`` or
  ``light``. The live OSC 11 probe (:func:`detect_auto_theme`) queries the
  terminal background and classifies it by luminance; it runs once at App
  construction (before Textual captures stdin) and the result is cached.
  The pure :func:`resolve_theme_name` validator keeps returning the dark
  baseline for ``auto`` so a persisted-value validation never triggers a
  terminal query.
"""

from __future__ import annotations

import logging
import os
import select
import sys
import time
from collections.abc import Callable
from typing import IO, Final

from textual.theme import Theme

logger = logging.getLogger(__name__)

#: OSC 11 request: ask the terminal to report its background colour. The
#: terminal replies with ``\x1b]11;rgb:RRRR/GGGG/BBBB`` terminated by BEL
#: (``\x07``) or ST (``\x1b\\``).
OSC11_QUERY: Final[str] = "\x1b]11;?\x07"

#: Bounded total wait for the OSC 11 reply. A terminal that does not answer
#: (no support, pipe, multiplexer that swallows the query) must never hang
#: the launch, so the read deadline is small and absolute.
_OSC11_TIMEOUT_S: Final[float] = 0.2

#: Relative-luminance threshold (sRGB, 0..255 channels) above which the
#: terminal background counts as light. ``127.5`` is the 50%-luminance
#: midpoint of the 0..255 range.
_LIGHT_LUMINANCE_THRESHOLD: Final[float] = 127.5

#: Wong 2011 deuteranopia-safe semantic vars — the exact hex values that
#: shipped at global scope in ``theme.tcss`` before the per-theme
#: migration. Hosted here so selecting ``dark`` reproduces the original
#: look byte-for-byte (no visual regression).
_WONG_VARIABLES: Final[dict[str, str]] = {
    "accent": "#56b6c2",
    "primary": "#56b6c2",
    "ok": "#009e73",
    "warn": "#e69f00",
    "err": "#d55e00",
    "muted": "#6c6c6c",
    "status-pending": "#6c6c6c",
    "status-claimed": "#56b6c2",
    "status-in-progress": "#e69f00",
    "status-closed": "#009e73",
    "status-failed": "#d55e00",
}

#: IBM colour-blind-safe semantic vars — a palette visually distinct from
#: Wong (bluer accent, magenta error, gold in-progress) so a swap away
#: from ``dark`` is observable while staying colour-blind-safe.
_IBM_VARIABLES: Final[dict[str, str]] = {
    "accent": "#648fff",
    "primary": "#648fff",
    "ok": "#1a9988",
    "warn": "#ffb000",
    "err": "#dc267f",
    "muted": "#8a8a8a",
    "status-pending": "#8a8a8a",
    "status-claimed": "#648fff",
    "status-in-progress": "#ffb000",
    "status-closed": "#1a9988",
    "status-failed": "#dc267f",
}

#: Light-surface semantic vars — the same var *names* the structural CSS
#: references, retuned so the tints stay legible on a light background.
_LIGHT_VARIABLES: Final[dict[str, str]] = {
    "accent": "#007a87",
    "primary": "#007a87",
    "ok": "#007a52",
    "warn": "#a35b00",
    "err": "#a8331a",
    "muted": "#595959",
    "status-pending": "#595959",
    "status-claimed": "#007a87",
    "status-in-progress": "#a35b00",
    "status-closed": "#007a52",
    "status-failed": "#a8331a",
}


#: The Wong deuteranopia-safe dark theme — the default + the ``dark``
#: logical name. Its ``variables`` carry the exact pre-migration hex.
EA_DARK: Final[Theme] = Theme(
    name="ea-dark",
    primary="#56b6c2",
    accent="#56b6c2",
    success="#009e73",
    warning="#e69f00",
    error="#d55e00",
    dark=True,
    variables=dict(_WONG_VARIABLES),
)

#: The IBM colour-blind-safe dark theme — the ``cb`` logical name.
EA_CB: Final[Theme] = Theme(
    name="ea-cb",
    primary="#648fff",
    accent="#648fff",
    success="#1a9988",
    warning="#ffb000",
    error="#dc267f",
    dark=True,
    variables=dict(_IBM_VARIABLES),
)

#: The light-surface theme — the ``light`` logical name. Carries the
#: semantic var set retuned for a light background so the structural CSS
#: keeps resolving every ``$var`` it references.
EA_LIGHT: Final[Theme] = Theme(
    name="ea-light",
    primary="#007a87",
    accent="#007a87",
    success="#007a52",
    warning="#a35b00",
    error="#a8331a",
    dark=False,
    variables=dict(_LIGHT_VARIABLES),
)


#: Every custom theme the App registers. Order is registration order.
EA_THEMES: Final[tuple[Theme, ...]] = (EA_DARK, EA_CB, EA_LIGHT)

#: The default logical theme applied on startup when ``ui.theme`` is unset
#: — the Wong dark baseline, so a fresh launch matches the pre-migration
#: look.
DEFAULT_THEME: Final[str] = "dark"

#: Operator-facing logical names → registered Textual theme name. The
#: four logical names are the ``/theme`` argument grammar and the
#: ``ui.theme`` config choices; ``auto`` is resolved separately by
#: :func:`resolve_theme_name` since it depends on terminal background.
LOGICAL_THEMES: Final[dict[str, str]] = {
    "dark": EA_DARK.name,
    "cb": EA_CB.name,
    "light": EA_LIGHT.name,
}

#: The four logical names the operator may pass to ``/theme`` / persist in
#: ``ui.theme``. Kept as a tuple so the config registry choices and the
#: verb's accepted set share one source.
THEME_CHOICES: Final[tuple[str, ...]] = ("dark", "light", "cb", "auto")


def resolve_theme_name(logical: str) -> str | None:
    """Resolve an operator-facing logical name to a registered theme name.

    Pure validator: no I/O. ``auto`` resolves to the dark baseline so a
    persisted-config validation (``_persisted_theme`` calls this to decide
    whether a saved ``ui.theme`` value is recognised) never triggers a
    terminal query. The live terminal-background detection that refines
    ``auto`` into ``dark`` / ``light`` happens in :func:`detect_auto_theme`
    (cached at App construction). Any other unknown name returns ``None``
    so the caller can reject it without changing the theme.

    Args:
        logical: One of ``"dark"`` / ``"light"`` / ``"cb"`` / ``"auto"``.

    Returns:
        The registered Textual theme name, or ``None`` when *logical* is
        not a recognised logical name.
    """
    if logical == "auto":
        return LOGICAL_THEMES[DEFAULT_THEME]
    return LOGICAL_THEMES.get(logical)


def resolve_auto_theme(rgb: tuple[int, int, int] | None) -> str:
    """Classify a terminal background colour as the ``dark`` / ``light`` logical.

    Pure function — no I/O. Computes the sRGB relative luminance
    ``0.2126*r + 0.7152*g + 0.0722*b`` over 0..255 channels and returns
    ``"light"`` when it exceeds the 50%-luminance midpoint, else ``"dark"``.
    A missing colour (no reply / non-TTY / parse failure) classifies as
    ``"dark"`` — the safe baseline.

    Args:
        rgb: The 8-bit ``(r, g, b)`` background colour, or ``None`` when the
            background could not be determined.

    Returns:
        ``"light"`` for a light background, ``"dark"`` otherwise.
    """
    if rgb is None:
        return "dark"
    r, g, b = rgb
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "light" if luminance > _LIGHT_LUMINANCE_THRESHOLD else "dark"


def _scale_channel(hex_value: str) -> int:
    """Scale a 1-4 hex-digit OSC 11 channel to an 8-bit value.

    xterm reports each channel as 4 hex digits (``ffff``); some terminals
    use 2 (``ff``) or 1 (``f``). Normalise by interpreting the digits as a
    fraction of the channel's max and mapping onto 0..255.

    Args:
        hex_value: A 1-4 character hex string for one colour channel.

    Returns:
        The channel value scaled to 0..255.
    """
    width = len(hex_value)
    raw = int(hex_value, 16)
    maximum = (1 << (4 * width)) - 1
    return round(raw * 255 / maximum)


def _parse_osc11(reply: bytes) -> tuple[int, int, int] | None:
    """Parse an OSC 11 background-colour reply into 8-bit RGB.

    Matches the ``rgb:RRRR/GGGG/BBBB`` payload xterm returns (also accepting
    1-2 digit channels), tolerating either the BEL (``\\x07``) or ST
    (``\\x1b\\``) terminator and any surrounding bytes.

    Args:
        reply: The raw bytes read from the terminal (possibly partial or
            empty).

    Returns:
        The ``(r, g, b)`` 8-bit colour, or ``None`` when *reply* carries no
        recognisable ``rgb:`` payload.
    """
    import re

    text = reply.decode("ascii", errors="ignore")
    match = re.search(
        r"rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})",
        text,
    )
    if match is None:
        return None
    return (
        _scale_channel(match.group(1)),
        _scale_channel(match.group(2)),
        _scale_channel(match.group(3)),
    )


def _read_osc_reply(fd: int, timeout_s: float) -> bytes:
    """Read an OSC reply from *fd* until its terminator or a bounded deadline.

    Loops :func:`select.select` against an absolute monotonic deadline so the
    TOTAL wait is bounded regardless of how the terminal dribbles the reply
    out; stops early on the BEL (``\\x07``) or ST (``\\x1b\\``) terminator.
    Returns whatever was read — possibly empty (no reply) or partial (the
    deadline elapsed mid-reply); :func:`_parse_osc11` tolerates both.

    Args:
        fd: The terminal file descriptor (already in cbreak mode).
        timeout_s: The total wait budget in seconds.

    Returns:
        The bytes read from the terminal.
    """
    deadline = time.monotonic() + timeout_s
    buffer = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            break
        chunk = os.read(fd, 32)
        if not chunk:
            break
        buffer.extend(chunk)
        if chunk.endswith(b"\x07") or chunk.endswith(b"\x1b\\"):
            break
    return bytes(buffer)


def query_terminal_background(
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    timeout_s: float = _OSC11_TIMEOUT_S,
) -> tuple[int, int, int] | None:
    """Query the terminal background colour via OSC 11.

    Writes the OSC 11 request to *stdout* and reads the reply from *stdin*
    under a bounded :func:`select`-driven deadline, restoring the terminal
    mode afterward. MUST be called only while the process owns the TTY —
    before Textual captures stdin (e.g. App ``__init__``), never mid-run.

    Returns ``None`` (the safe fallback) without writing or hanging when:
    ``CI`` or ``EAWF_NO_OSC`` is set, either stream is not a TTY, the
    platform lacks ``termios`` / ``tty`` (non-POSIX), or the reply does not
    arrive / does not parse.

    Args:
        stdin: Input stream to read the reply from (defaults to
            :data:`sys.stdin`).
        stdout: Output stream to write the query to (defaults to
            :data:`sys.stdout`).
        timeout_s: Bounded total wait for the reply, in seconds.

    Returns:
        The 8-bit ``(r, g, b)`` terminal background colour, or ``None`` on
        any non-TTY / CI / unsupported / no-reply / parse-failure path.
    """
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    if os.environ.get("CI") or os.environ.get("EAWF_NO_OSC"):
        return None
    try:
        if not (stdin.isatty() and stdout.isatty()):
            return None
    except ValueError, OSError:
        return None
    try:
        import termios
        import tty
    except ImportError:
        return None
    try:
        fd = stdin.fileno()
        old = termios.tcgetattr(fd)
    except ValueError, OSError, termios.error:
        return None
    try:
        tty.setcbreak(fd)
        stdout.write(OSC11_QUERY)
        stdout.flush()
        reply = _read_osc_reply(fd, timeout_s)
    except ValueError, OSError, termios.error:
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    rgb = _parse_osc11(reply)
    logger.debug(f"query_terminal_background rgb={rgb!r}")
    return rgb


def detect_auto_theme() -> str:
    """Detect the ``auto`` logical theme from the live terminal background.

    Glue over :func:`query_terminal_background` (live OSC 11 probe) and
    :func:`resolve_auto_theme` (pure luminance classifier). MUST be called
    only at the safe point where the process still owns the TTY — App
    ``__init__``, before ``.run()`` captures stdin — and the result cached;
    runtime ``/theme auto`` reuses the cached value. Falls back to ``"dark"``
    on every non-TTY / no-reply / unsupported path.

    Returns:
        ``"light"`` or ``"dark"`` — the logical name ``auto`` resolves to.
    """
    return resolve_auto_theme(query_terminal_background())


#: Poll interval (seconds) for the live OS light/dark appearance watch. The
#: OSC 11 terminal-background probe (:func:`detect_auto_theme`) can only run
#: before ``.run()`` captures stdin, so once the App is running the system
#: theme is followed by polling the OS appearance off-thread on this cadence
#: — small enough to feel near-instant, large enough to stay cheap.
THEME_POLL_INTERVAL_S: Final[float] = 2.0


def classify_macos_appearance(stdout: str, returncode: int) -> str:
    """Classify the macOS system appearance from a ``defaults`` read result.

    ``defaults read -g AppleInterfaceStyle`` prints ``Dark`` in Dark mode and
    exits non-zero (the key is absent) in Light mode.

    Args:
        stdout: The command's stdout.
        returncode: The command's exit status.

    Returns:
        ``"dark"`` when the style reports Dark, ``"light"`` otherwise.
    """
    if returncode == 0 and "dark" in stdout.strip().lower():
        return "dark"
    return "light"


def classify_linux_appearance(stdout: str) -> str | None:
    """Classify the GNOME ``color-scheme`` gsettings value.

    Maps ``'prefer-dark'`` → dark and ``'default'`` / ``'prefer-light'`` →
    light (the value arrives single-quoted). An unrecognised value yields
    ``None`` so the caller leaves the theme untouched.

    Args:
        stdout: The ``gsettings get`` stdout.

    Returns:
        ``"dark"`` / ``"light"``, or ``None`` when unrecognised.
    """
    text = stdout.strip().strip("'\"").lower()
    if "dark" in text:
        return "dark"
    if text in ("default", "prefer-light", "light"):
        return "light"
    return None


def classify_windows_appearance(stdout: str, returncode: int) -> str | None:
    """Classify the Windows ``AppsUseLightTheme`` registry DWORD.

    ``reg query`` prints ``... REG_DWORD    0x1`` in Light mode and ``0x0``
    in Dark mode.

    Args:
        stdout: The ``reg query`` stdout.
        returncode: The command's exit status.

    Returns:
        ``"dark"`` / ``"light"``, or ``None`` when the read failed / did not
        parse.
    """
    if returncode != 0:
        return None
    text = stdout.lower()
    if "0x0" in text:
        return "dark"
    if "0x1" in text:
        return "light"
    return None


def _run_appearance_probe(cmd: list[str]) -> tuple[str, int] | None:
    """Run *cmd* with a short timeout; return ``(stdout, returncode)`` or ``None``.

    Returns ``None`` on any failure (missing binary, timeout, OS error) so
    the caller treats an unprobeable platform as "appearance unknown" rather
    than crashing the poll.

    Args:
        cmd: The argv to run.

    Returns:
        ``(stdout, returncode)``, or ``None`` on failure.
    """
    import subprocess

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1.0)
    except OSError, subprocess.SubprocessError:
        return None
    return proc.stdout, proc.returncode


def detect_os_appearance(
    *,
    runner: Callable[[list[str]], tuple[str, int] | None] | None = None,
) -> str | None:
    """Best-effort live read of the OS light/dark appearance — no TTY access.

    Unlike the OSC 11 terminal-background probe, this shells out to the
    platform's appearance setting (macOS ``defaults``, GNOME ``gsettings``,
    Windows ``reg``), so it is safe to call repeatedly while Textual owns
    stdin. It powers the live ``/theme auto`` follow: when the operator flips
    the system theme, the next poll observes the change.

    Returns ``None`` (appearance unknown — caller leaves the theme alone) when
    ``CI`` is set, the platform is unsupported, or the probe fails.

    Args:
        runner: Test seam — runs an argv and returns ``(stdout, returncode)``
            or ``None``. Defaults to :func:`_run_appearance_probe`.

    Returns:
        ``"light"`` / ``"dark"``, or ``None`` when undetermined.
    """
    if os.environ.get("CI"):
        return None
    run = runner if runner is not None else _run_appearance_probe
    platform = sys.platform
    if platform == "darwin":
        result = run(["defaults", "read", "-g", "AppleInterfaceStyle"])
        return None if result is None else classify_macos_appearance(result[0], result[1])
    if platform.startswith("linux"):
        result = run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"])
        return None if result is None else classify_linux_appearance(result[0])
    if platform.startswith("win"):
        result = run(
            [
                "reg",
                "query",
                r"HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                "/v",
                "AppsUseLightTheme",
            ]
        )
        return None if result is None else classify_windows_appearance(result[0], result[1])
    return None


__all__ = [
    "DEFAULT_THEME",
    "EA_CB",
    "EA_DARK",
    "EA_LIGHT",
    "EA_THEMES",
    "LOGICAL_THEMES",
    "OSC11_QUERY",
    "THEME_CHOICES",
    "THEME_POLL_INTERVAL_S",
    "classify_linux_appearance",
    "classify_macos_appearance",
    "classify_windows_appearance",
    "detect_auto_theme",
    "detect_os_appearance",
    "query_terminal_background",
    "resolve_auto_theme",
    "resolve_theme_name",
]
