"""Emit-time sensitive-data scrubber for the logging pipeline.

:class:`SensitiveScrubber` is a :class:`logging.Filter` that rewrites
home-directory paths, IP addresses, email addresses, and
API-key-shaped tokens out of every log record *before* the formatter
runs. It is wired as a filter on the CLI root handler and on both of
the daemon's logging branches (the foreground stderr stream and the
non-foreground ``eawfd.log`` file handler) so no sink ever serialises
raw secrets.

The email allowlist preserves the canonical ``pyproject.toml``
``[project].authors`` address (the project's own author block, which
is allowed to appear verbatim per the secrets-hygiene policy) plus the
canonical no-reply co-author addresses. The author email is read from
``pyproject.toml`` at construction time rather than hard-coded, so the
literal address never lands in committed source.
"""

from __future__ import annotations

import logging
import re
import tomllib
from importlib.resources import files
from pathlib import Path

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

# Walk at most this many parents looking for eawf's own pyproject.toml.
_MAX_PYPROJECT_WALK_LEVELS = 6


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


def _eawf_author_emails() -> frozenset[str]:
    """Return the canonical author emails from eawf's ``pyproject.toml``.

    Anchors on the :mod:`eawf` package directory and walks up at most
    :data:`_MAX_PYPROJECT_WALK_LEVELS` levels, accepting only a
    ``pyproject.toml`` whose ``[project].name`` is ``"eawf"`` so a host
    project's metadata is never picked up for a wheel install. Returns
    an empty set (no extra allowlist entries) when the file cannot be
    located or parsed — the default no-reply allowlist still applies.
    """
    package_root = Path(str(files("eawf"))).resolve()
    candidates = [package_root, *package_root.parents][: _MAX_PYPROJECT_WALK_LEVELS + 1]
    for candidate in candidates:
        pyproject_path = candidate / "pyproject.toml"
        if not pyproject_path.is_file():
            continue
        try:
            with pyproject_path.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            logger.debug(f"_eawf_author_emails skipping path={pyproject_path} reason={exc!r}")
            continue
        project = data.get("project", {})
        if project.get("name") != "eawf":
            return frozenset()
        emails: set[str] = set()
        for author in project.get("authors", []) or []:
            if isinstance(author, dict):
                email = author.get("email")
                if isinstance(email, str) and email:
                    emails.add(email)
        return frozenset(emails)
    return frozenset()


class SensitiveScrubber(logging.Filter):
    """Strip path / email / API-key patterns from every log record at emit.

    Each compiled pattern in :data:`PATTERNS` is substituted with
    :data:`REDACTION` over the fully formatted message. Email addresses
    on the allowlist are restored after substitution so the canonical
    author and no-reply addresses survive.
    """

    PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"/Users/[^/\s]+"),  # macOS home  # pragma: allowlist secret
        re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE),  # Windows home
        re.compile(r"/home/[^/\s]+"),  # Linux home  # pragma: allowlist secret
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
        # pattern. Word boundaries keep dotted version strings intact.
        re.compile(r"\b(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}\b"),  # IPv6
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
                preserve. When ``None`` the allowlist is the union of
                the default no-reply addresses and the author emails
                read from eawf's ``pyproject.toml``.
        """
        super().__init__(name)
        if allowed_emails is None:
            allowed_emails = _DEFAULT_ALLOWED_EMAILS | _eawf_author_emails()
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
