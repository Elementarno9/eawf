"""Tests for the EAWF001 log-format lint rule."""

from __future__ import annotations

import textwrap

import pytest

from eawf.lint.eawf001 import (
    RULE_CODE,
    LogFormatViolation,
    check_message,
    check_source,
)

# --- the core success criterion ------------------------------------------


def test_flags_non_conforming_logger_call() -> None:
    source = textwrap.dedent(
        """
        import logging
        logger = logging.getLogger(__name__)
        logger.info("totally freeform: with a prose colon and no funcname")
        """
    )
    violations = check_source(source)
    assert len(violations) == 1
    assert violations[0].code == RULE_CODE
    assert violations[0].lineno == 4


def test_passes_conforming_logger_call() -> None:
    source = textwrap.dedent(
        """
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"create_worktree wave={wave_id} branch={name!r}")
        """
    )
    assert check_source(source) == []


# --- check_message: format boundaries ------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "create_worktree wave=W03 branch=feat",
        "phase_activate phase=P27 base=main",
        "_resolve_idle_timeout raw={} default={}",  # f-string skeleton
        "run start",  # funcname + bare status slug (corpus shape)
        "run idle-timeout-trip idle_for={}",  # hyphenated slug + pair
        "handle_connection skip peer-cred unsupported-platform",  # multi-slug
        "_resolve_idle_timeout unparseable raw={}; using default",  # ; trailer
        "validate_state path={} field={} reason={}",
        "run exit",
    ],
)
def test_check_message_accepts_canonical(message: str) -> None:
    assert check_message(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "_run: invoking args cwd={}",  # leading colon after funcname
        "oops: something went wrong {}",  # prose colon right after funcname
        "something went wrong: detail here",  # prose colon mid-body
        "=value missing leading funcname",  # starts with non-identifier token
        "phase_activate phase = P27",  # spaces around '=' break the pair shape
        "",  # empty message
    ],
)
def test_check_message_rejects_non_canonical(message: str) -> None:
    assert check_message(message) is False


# --- f-string handling ----------------------------------------------------


def test_fstring_with_conversion_and_format_spec_is_conforming() -> None:
    source = 'logger.error(f"validate_state path={p!r} field={f} reason={r!r}")\n'
    assert check_source(source) == []


def test_fstring_non_conforming_is_flagged() -> None:
    source = 'logger.warning(f"something went wrong: {detail}")\n'
    violations = check_source(source)
    assert len(violations) == 1
    assert "<funcname> key=value" in violations[0].reason


# --- logger-call recognition ---------------------------------------------


@pytest.mark.parametrize("level", ["debug", "info", "warning", "error", "critical", "exception"])
def test_all_log_levels_are_checked(level: str) -> None:
    source = f'logger.{level}("oops: freeform prose with a colon")\n'
    assert len(check_source(source)) == 1


def test_self_logger_attribute_call_is_checked() -> None:
    source = 'self.logger.info("oops: freeform prose with a colon")\n'
    assert len(check_source(source)) == 1


def test_log_alias_is_checked() -> None:
    source = 'log.info("oops: freeform prose with a colon")\n'
    assert len(check_source(source)) == 1


def test_non_logger_call_is_ignored() -> None:
    source = 'print("oops: freeform prose with a colon")\n'
    assert check_source(source) == []


def test_non_logger_dot_log_attribute_is_not_flagged() -> None:
    """A bare ``<obj>.log.<level>`` member is not a logger and must not be flagged."""
    source = 'self.log.info("oops: freeform prose with a colon")\n'
    assert check_source(source) == []


def test_non_logger_named_dot_log_attribute_is_not_flagged() -> None:
    """An arbitrarily-named ``.log`` attribute receiver is likewise skipped."""
    source = 'audit.log.warning("oops: freeform prose with a colon")\n'
    assert check_source(source) == []


def test_self_logger_attribute_still_flagged_after_narrowing() -> None:
    """Narrowing the ``.log`` attribute must not stop flagging ``self.logger``."""
    source = 'self.logger.info("oops: freeform prose with a colon")\n'
    assert len(check_source(source)) == 1


def test_non_logging_method_on_logger_is_ignored() -> None:
    source = 'logger.setLevel("DEBUG: anything goes")\n'
    assert check_source(source) == []


# --- dynamic / unsupported messages are skipped --------------------------


def test_dynamic_name_message_is_skipped() -> None:
    source = textwrap.dedent(
        """
        msg = "oops: freeform prose"
        logger.info(msg)
        """
    )
    assert check_source(source) == []


def test_concatenated_message_is_skipped() -> None:
    source = 'logger.info("oops: " + variable)\n'
    assert check_source(source) == []


def test_logger_call_with_no_args_is_skipped() -> None:
    source = "logger.info()\n"
    assert check_source(source) == []


# --- ordering + error path -----------------------------------------------


def test_violations_returned_in_source_order() -> None:
    source = textwrap.dedent(
        """
        logger.info("first: bad message")
        logger.info("good_fn key=value")
        logger.error("second: bad message")
        """
    )
    violations = check_source(source)
    assert [v.lineno for v in violations] == [2, 4]


def test_check_source_raises_on_syntax_error() -> None:
    with pytest.raises(SyntaxError):
        check_source("logger.info('unterminated\n")


def test_violation_render_includes_code_and_location() -> None:
    violation = LogFormatViolation(
        lineno=7,
        col_offset=4,
        message="bad message",
        reason="log message does not match '<funcname> key=value' format",
    )
    rendered = violation.render()
    assert rendered.startswith("7:4: EAWF001")
    assert "bad message" in rendered
