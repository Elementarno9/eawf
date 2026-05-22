"""EAWF002 — log-key naming lint for library logger call sites.

Enforces the AGENTS naming-conventions rule that wave / iter / phase
identifiers appear in **log lines** under their bare key form
(``wave=<id>``, ``iter=<id>``, ``phase=<id>``) — never the
``_id``-suffixed form (``wave_id=<id>``). The trailing ``_id`` suffix is
reserved for typed-model field names and for structured envelopes
(``EventPayload``, ``state.json``) where the schema benefits from the
explicit suffix; a free-form ``logger.<level>(...)`` message is neither,
so the suffix is a drift to flag.

The rule walks a module AST, reduces each statically-inspectable logger
message to a skeleton (a bare ``str`` constant, or an f-string with its
interpolations collapsed to ``{}``), and flags every occurrence of a
banned ``<key>_id=`` token for the keys in :data:`_BANNED_KEYS`. A single
message may carry more than one offending key; each is reported
separately so the operator sees the full repair list.

This rule is the log-key sibling of EAWF001 (message shape): EAWF001
validates the overall ``<funcname> key=value`` grammar, EAWF002 narrows
in on the specific keys whose canonical spelling the conventions pin.
Only ``logger.<level>(...)`` call sites are inspected; dynamic messages
and non-logger calls are skipped, mirroring EAWF001's scope boundary.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass

RULE_CODE = "EAWF002"

# Logging levels whose first positional argument is the message.
_LOG_METHODS: frozenset[str] = frozenset(
    {"debug", "info", "warning", "error", "critical", "exception"}
)

# Receiver names treated as a logger (canonical ``logger`` + tolerated
# ``log`` alias and the ``self.logger`` attribute form).
_LOGGER_NAMES: frozenset[str] = frozenset({"logger", "log"})

# Cross-cutting identifiers whose bare key form is canonical in log
# lines. The ``_id``-suffixed spelling of any of these inside a log
# message is the drift EAWF002 flags.
_BANNED_KEYS: tuple[str, ...] = ("wave", "iter", "phase")

# Matches a banned ``<key>_id=`` token sitting on a word boundary, so a
# legitimate compound like ``request_id=`` (not in the banned set) is
# never matched and a substring like ``subwave_id=`` does not false-fire.
_BANNED_KEY_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(key) for key in _BANNED_KEYS) + r")_id="
)


@dataclass(frozen=True)
class LogKeyViolation:
    """One EAWF002 finding.

    Attributes:
        lineno: 1-based line of the offending ``logger.<level>(...)``
            call.
        col_offset: 0-based column of the call node.
        message: the static message skeleton that carried the bad key.
        key: the canonical bare key the operator should use instead
            (e.g. ``wave`` for a flagged ``wave_id=``).
    """

    lineno: int
    col_offset: int
    message: str
    key: str

    @property
    def code(self) -> str:
        """Return the rule code (``EAWF002``)."""
        return RULE_CODE

    def render(self) -> str:
        """Return a ``line:col: CODE reason`` style one-liner body."""
        reason = f"log key {self.key}_id= should be bare {self.key}="
        return f"{self.lineno}:{self.col_offset}: {RULE_CODE} {reason}: {self.message!r}"


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
    by ``{}``. Any other (dynamic) expression returns ``None`` and is
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


def banned_keys(message: str) -> list[str]:
    """Return the banned bare keys whose ``_id=`` form appears in ``message``.

    Args:
        message: A static log-message skeleton.

    Returns:
        The canonical bare keys (subset of :data:`_BANNED_KEYS`) found in
        ``_id=`` form, in source order, with duplicates preserved so a
        message that repeats ``wave_id=`` twice yields two entries.
    """
    return [match.group(1) for match in _BANNED_KEY_PATTERN.finditer(message)]


def check_source(source: str, *, filename: str = "<unknown>") -> list[LogKeyViolation]:
    """Return EAWF002 violations for ``source``.

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
    violations: list[LogKeyViolation] = []
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
        for key in banned_keys(message):
            violations.append(
                LogKeyViolation(
                    lineno=node.lineno,
                    col_offset=node.col_offset,
                    message=message,
                    key=key,
                )
            )
    violations.sort(key=lambda violation: (violation.lineno, violation.col_offset))
    return violations
