"""Emit-time sensitive-data scrubber for the logging pipeline.

:class:`SensitiveScrubber` is a :class:`logging.Filter` that rewrites
home-directory and server data-root paths, IP addresses, email addresses, and
API-key-shaped tokens out of every log record *before* the formatter
runs. It is wired as a filter on the CLI root handler and on both of
the daemon's logging branches (the foreground stderr stream and the
non-foreground ``eawfd.log`` file handler) so no sink ever serialises
raw secrets.

The email allowlist preserves only the canonical no-reply co-author
addresses; every other address is redacted.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

REDACTION = "<scrubbed>"

# Co-author no-reply addresses that are allowed to appear in logs
# verbatim (they carry no PII and are checked into every commit
# trailer already).
_DEFAULT_ALLOWED_EMAILS: frozenset[str] = frozenset(
    {
        "noreply@anthropic.com",
        "noreply@openai.com",
    }
)


def _email_pattern_index(patterns: tuple[re.Pattern[str], ...]) -> int:
    """Return the index of the email pattern within ``patterns``.

    Raises:
        ValueError: if no email pattern is present (guards against the
            tuple being reordered such that the allowlist can no longer
            be applied to the right pattern).
    """
    for index, pattern in enumerate(patterns):
        if "@" in pattern.pattern:
            return index
    raise ValueError("no email pattern in scrubber PATTERNS")


# A machine-path anchor counts only where it starts a path, so a letter or
# digit in any script, or a dot, right before it (``/usr/srv/x``, a URL's
# ``host/srv/x``, ``./srv``) leaves it alone while a separator such as grep's
# ``513-`` or an underscore still leads one. The last character of a JSON
# escape (``\n``, ``\f``, ``\u000a``) or of an ANSI CSI sequence (``ESC[0m``,
# raw or spelt ``\u001b[`` / ``\x1b[`` in encoded text) ends that code, not a
# word, so it leads a path too. ``re`` only takes fixed-width lookbehinds, so
# the CSI parameter run is spelt out once per length up to the longest
# truecolor SGR (``38;2;255;255;255``); the one-character guard in front keeps
# those lookbehinds from running after every letter of ordinary prose.
_CSI_INTRODUCERS = ("\x1b", r"\\u001b", r"\\x1b")
_CSI_MAX_PARAMS = 16
_CSI_END = (
    r"(?<=[0-9;\[][A-Za-z])(?:"
    + "|".join(
        rf"(?<={introducer}\[[0-9;]{{{count}}}[A-Za-z])"
        for introducer in _CSI_INTRODUCERS
        for count in range(_CSI_MAX_PARAMS + 1)
    )
    + ")"
)
_PATH_START = rf"(?:(?<![^\W_])(?<!\.)|(?<=\\[nrtfb])|(?<=\\u[0-9a-fA-F]{{4}})|{_CSI_END})"


class SensitiveScrubber(logging.Filter):
    """Strip path / email / API-key patterns from every log record at emit.

    Each compiled pattern in :data:`PATTERNS` is substituted with
    :data:`REDACTION` over the fully formatted message. Email addresses
    on the allowlist are restored after substitution so the canonical
    no-reply addresses survive.
    """

    PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"/Users/[^/\s]+"),  # macOS home  # eawf: allow-leak
        re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE),  # Windows home
        re.compile(r"/home/[^/\s]+"),  # Linux home  # eawf: allow-leak
        # A server keeps checkouts and scratch under its site data root, so a
        # path there names the machine as plainly as a home directory does.
        # It must start the path (see ``_PATH_START``).
        re.compile(rf"{_PATH_START}/srv/[^/\s]+"),  # server data root  # eawf: allow-leak
        # A login's runtime directory names its uid, and pytest's temp root
        # names the user, wherever the temp directory sits.
        re.compile(rf"{_PATH_START}/run/user/\d+"),  # runtime dir
        re.compile(rf"{_PATH_START}pytest-of-[^/\s]+"),  # pytest temp  # eawf: allow-leak
        # Bare ``~/`` home reference (shell-expanded path that still
        # leaks the project tree layout even without the username).
        re.compile(r"~/[^\s]+"),  # tilde home
        # API-key shapes: the Anthropic-specific prefix is listed before
        # the generic ``sk-`` shape so the longer match is attempted
        # first and the prefix is never left dangling.
        re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),  # Anthropic-shaped key
        re.compile(r"sk-[A-Za-z0-9_-]{20,}"),  # OpenAI-shaped key
        re.compile(r"ghp_[A-Za-z0-9]{36,}"),  # GitHub PAT
        # IPv6 before IPv4 so an IPv6 literal is matched whole rather
        # than its trailing dotted-quad tail being clipped by the IPv4
        # pattern. The form covers the fully expanded eight-group
        # address, the v4-embedded full form, and every compressed
        # ``::`` placement (leading ``::1``, trailing ``fe80::``,
        # interior ``a::b``, bare ``::``, and the v4-mapped
        # ``::ffff:1.2.3.4`` tail). The leading negative lookbehind on
        # hex / colon / dot anchors on the address shape rather than a
        # word boundary, since ``\b`` does not fire before a leading
        # ``::`` (``:`` is a non-word character). The left side of the
        # ``::`` ends on a hex group (no trailing colon) so the colon it
        # would otherwise consume stays part of the ``::`` marker, and
        # the right side prefers the greedy v4 tail so a v4-mapped
        # literal is never clipped to its first dotted octet. A single
        # colon (``1:2`` ratios, ``C:\`` drives, dotted version strings)
        # cannot satisfy the ``::`` requirement, so ordinary text is
        # left intact.
        re.compile(
            r"(?<![0-9A-Fa-f:.])(?:"
            r"(?:[0-9A-Fa-f]{1,4}:){6}(?:\d{1,3}\.){3}\d{1,3}"
            r"|(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
            r"|(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?::"
            r"(?:(?:[0-9A-Fa-f]{1,4}:)*(?:\d{1,3}\.){3}\d{1,3}"
            r"|[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?"
            r")"
        ),  # IPv6
        re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),  # IPv4
        re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),  # email
    )

    #: Format string for the per-match placeholder used to shield
    #: allowlisted email addresses from redaction. NUL bytes never occur
    #: in rendered log text and the body matches no entry in
    #: :data:`PATTERNS`, so the placeholder survives the substitution
    #: loop and is restored to its exact original position afterwards.
    _ALLOWLIST_PLACEHOLDER = "\x00EAWF-ALLOW-{index}\x00"

    def __init__(self, name: str = "", allowed_emails: frozenset[str] | None = None) -> None:
        """Build the scrubber.

        Args:
            name: passed through to :class:`logging.Filter`; selects
                which logger sub-tree the filter applies to (``""`` =
                all records).
            allowed_emails: explicit allowlist of email addresses to
                preserve. When ``None`` the allowlist is the default
                no-reply addresses.
        """
        super().__init__(name)
        if allowed_emails is None:
            allowed_emails = _DEFAULT_ALLOWED_EMAILS
        self._allowed_emails: frozenset[str] = frozenset(
            email.casefold() for email in allowed_emails
        )
        self._email_index = _email_pattern_index(self.PATTERNS)
        self._email_pattern = self.PATTERNS[self._email_index]

    def scrub(self, message: str) -> str:
        """Return ``message`` with every sensitive pattern redacted.

        Allowlisted email addresses are shielded *before* redaction by
        swapping each one for a unique positional placeholder, then
        restored verbatim after the substitution loop. Shielding first
        (rather than restoring the first ``<scrubbed>`` slot afterwards)
        keeps an allowlisted email in its own position even when an
        earlier token in the line — for example an absolute path — is
        itself redacted to ``<scrubbed>``.
        """
        # Shield each allowlisted email occurrence with a unique
        # positional placeholder. ``preserved[i]`` is the i-th
        # allowlisted email in left-to-right order; the spans are
        # rewritten right-to-left so earlier match offsets stay valid as
        # the string is mutated in place.
        matches = [
            match
            for match in self._email_pattern.finditer(message)
            if match.group(0).casefold() in self._allowed_emails
        ]
        preserved: list[str] = [match.group(0) for match in matches]
        shielded = message
        for index in range(len(matches) - 1, -1, -1):
            match = matches[index]
            placeholder = self._ALLOWLIST_PLACEHOLDER.format(index=index)
            shielded = shielded[: match.start()] + placeholder + shielded[match.end() :]

        scrubbed = shielded
        for pattern in self.PATTERNS:
            scrubbed = pattern.sub(REDACTION, scrubbed)

        for index, original in enumerate(preserved):
            placeholder = self._ALLOWLIST_PLACEHOLDER.format(index=index)
            scrubbed = scrubbed.replace(placeholder, original)
        return scrubbed

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact the record's rendered message in place; never drop it.

        The record's ``args`` are cleared after the message is
        rendered so downstream formatters do not re-interpolate raw
        values back into the scrubbed text.
        """
        record.msg = self.scrub(record.getMessage())
        record.args = ()
        return True


def configure_cli_logging() -> None:
    """Install a scrubbed stderr log sink for a CLI process.

    Attaches a :class:`SensitiveScrubber` to the root handler so any library
    log line the CLI emits (error details, resolved state paths) is redacted
    before it reaches the terminal -- the CLI is the operator-facing surface
    and would otherwise print raw machine paths and secret-shaped tokens.
    Skips installation when the root logger already has handlers so a caller
    (test harness, embedding process) that configured logging first is not
    clobbered. The level is WARNING because the INFO stream is daemon and
    library chatter an operator does not act on.

    It lives here rather than beside the Typer app so the console entry point
    can install the same sink for a statusline render without importing the
    app.
    """
    import sys

    root = logging.getLogger()  # noqa: EAWF003 (root-logger handler config, not library acquisition)
    if root.handlers:
        return
    handler = logging.StreamHandler(stream=sys.stderr)
    # Lead with the timestamp (matching the daemon file log) so arm / drive
    # latency is measurable from the inline console, not only from eawfd.log.
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(SensitiveScrubber())
    root.addHandler(handler)
    root.setLevel(logging.WARNING)
