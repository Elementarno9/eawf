"""EAWF001 — log-format lint for library logger call sites.

Enforces the canonical structured-log format from the AGENTS naming
conventions: ``<funcname> key=value key=value`` — a leading function-
name token, then space-separated event-qualifier slugs and/or
``key=value`` pairs, with **no leading colon** after the function name.
The rule walks a module AST and flags every ``logger.<level>(...)``
call whose message string does not match.

The grammar is deliberately permissive about the body to stay
faithful to the existing corpus, which mixes bare status slugs with
key=value pairs (e.g. ``handle_connection skip peer-cred
unsupported-platform`` and ``run idle-timeout-trip idle_for={...}``)
and tolerates a trailing ``; <prose>`` clause (``... raw={raw!r};
using default``). What the rule actually rejects is the structural
deviations the conventions call out:

* a colon directly after the function-name token (``_run: ...``);
* a free-prose colon before the structured body (``oops: {detail}``);
* whitespace around ``=`` in a pair (``phase = P27``);
* a leading token that is not a valid identifier (so the line does
  not start with a function name at all).

Only statically-inspectable messages are checked: a bare string
constant or an f-string (:class:`ast.JoinedStr`). The f-string's
interpolated values are reduced to a ``{}`` placeholder so the static
skeleton can be validated without evaluating the expressions. Calls
whose first argument is built dynamically (a name, a concatenation,
``str.join(...)``) are skipped — they cannot be validated statically
and are out of this rule's scope.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

RULE_CODE = "EAWF001"

# Logging levels whose first positional argument is the message.
_LOG_METHODS: frozenset[str] = frozenset(
    {"debug", "info", "warning", "error", "critical", "exception"}
)

# Receiver names treated as a logger. ``logger`` is the project-wide
# canonical (``logger = logging.getLogger(__name__)``); ``log`` and
# ``self.logger`` are tolerated aliases.
_LOGGER_NAMES: frozenset[str] = frozenset({"logger", "log"})

# Canonical message skeleton, faithful to the existing corpus:
#   <funcname> ( <slug> | <key>=<value> )*  ( ; <prose> )?
# where the funcname is an identifier NOT directly followed by a colon,
# a slug is a hyphen-joined status word, and a pair is a tight
# ``key=value`` with no spaces around ``=`` (the value is any run of
# non-space characters, e.g. a ``{}`` f-string placeholder). A free
# colon anywhere in the structured body fails both body alternatives,
# which is how prose like ``oops: {detail}`` gets flagged.
# A status slug is a hyphen/underscore-joined word and may carry a
# state-transition arrow (``applied->fsynced``), both of which appear in
# the corpus as bare qualifiers between the funcname and the pairs.
_FUNCNAME = r"[A-Za-z_][A-Za-z0-9_]*"
_SLUG = r"[A-Za-z][A-Za-z0-9_-]*(?:->[A-Za-z][A-Za-z0-9_-]*)?"
_PAIR = r"[A-Za-z_][A-Za-z0-9_]*=\S+"
_BODY_TOKEN = rf"(?:{_PAIR}|{_SLUG})"
_TRAILER = r"(?:; .*)?"
_MESSAGE_PATTERN = re.compile(rf"^{_FUNCNAME}(?!:)(?: {_BODY_TOKEN})*{_TRAILER}$")


@dataclass(frozen=True)
class LogFormatViolation:
    """One EAWF001 finding.

    Attributes:
        lineno: 1-based line of the offending ``logger.<level>(...)``
            call.
        col_offset: 0-based column of the call node.
        message: the static message skeleton that failed validation.
        reason: short human-readable cause.
    """

    lineno: int
    col_offset: int
    message: str
    reason: str

    @property
    def code(self) -> str:
        """Return the rule code (``EAWF001``)."""
        return RULE_CODE

    def render(self) -> str:
        """Return a ``path:line:col: CODE reason`` style one-liner body."""
        return f"{self.lineno}:{self.col_offset}: {RULE_CODE} {self.reason}: {self.message!r}"


def _is_logger_call(node: ast.Call) -> str | None:
    """Return the log level name if ``node`` is a logger call, else ``None``.

    Recognises ``logger.<level>(...)`` and ``self.logger.<level>(...)``
    (and the ``log`` alias) where ``<level>`` is in
    :data:`_LOG_METHODS`.
    """
    func = node.func
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr not in _LOG_METHODS:
        return None
    receiver = func.value
    if isinstance(receiver, ast.Name) and receiver.id in _LOGGER_NAMES:
        return func.attr
    if (
        isinstance(receiver, ast.Attribute)
        and receiver.attr in _LOGGER_NAMES
        and isinstance(receiver.value, ast.Name)
    ):
        return func.attr
    return None


def _static_message(node: ast.expr) -> str | None:
    """Reduce a message expression to a static skeleton, or ``None``.

    A plain ``str`` constant returns its value. An f-string returns the
    concatenation of its literal parts with each interpolation replaced
    by ``{}``. Any other expression (dynamic) returns ``None`` and is
    skipped by the caller.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append("{}")
            else:  # pragma: no cover - JoinedStr only holds these two
                return None
        return "".join(parts)
    return None


def check_message(message: str) -> bool:
    """Return ``True`` if ``message`` matches the canonical log format.

    The canonical format is ``<funcname> key=value ...`` with no
    leading colon after the function-name token.
    """
    return bool(_MESSAGE_PATTERN.match(message))


def check_source(source: str, *, filename: str = "<unknown>") -> list[LogFormatViolation]:
    """Return EAWF001 violations for ``source``.

    Args:
        source: Python source text to inspect.
        filename: name used for the parse (surfaced in ``SyntaxError``).

    Returns:
        Violations in source order. Dynamic messages and non-logger
        calls produce no findings.

    Raises:
        SyntaxError: if ``source`` is not parseable Python.
    """
    tree = ast.parse(source, filename=filename)
    violations: list[LogFormatViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _is_logger_call(node) is None:
            continue
        if not node.args:
            continue
        message = _static_message(node.args[0])
        if message is None:
            continue
        if check_message(message):
            continue
        violations.append(
            LogFormatViolation(
                lineno=node.lineno,
                col_offset=node.col_offset,
                message=message,
                reason="log message does not match '<funcname> key=value' format",
            )
        )
    violations.sort(key=lambda violation: (violation.lineno, violation.col_offset))
    return violations
