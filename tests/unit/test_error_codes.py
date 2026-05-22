"""Tests for :mod:`eawf.cli.error_codes` — cause-level ErrorCode enum.

:class:`ErrorCode` is a closed cause-level vocabulary layered over the five
exit buckets (:mod:`eawf.cli.exit_codes`). Each member folds onto exactly
one bucket via :func:`ErrorCode.exit_code` and has exactly one anchor in
``docs/reference/error-codes.md``.

The :class:`~eawf.cli.errors.ErrorEnvelope` gains an optional
``error_code`` field; when set the text branch renders the C10 error-UX
order: cause -> next_step -> ``See <code>``. When unset the envelope renders
exactly as before so the change stays non-breaking.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from eawf.cli import errors, exit_codes
from eawf.cli.error_codes import ErrorCode
from eawf.cli.flags import GlobalFlags

runner = CliRunner()

_DOC = Path(__file__).resolve().parents[2] / "docs" / "reference" / "error-codes.md"

_VALID_BUCKETS = {
    exit_codes.USER_ERROR,
    exit_codes.VALIDATION_ERROR,
    exit_codes.STATE_CONFLICT,
    exit_codes.DAEMON_UNREACHABLE,
    exit_codes.INTERNAL_ERROR,
}


# --- Enum shape ------------------------------------------------------------


def test_error_code_member_count_matches_spec() -> None:
    """The C10 cause-level vocabulary is 33 closed members."""
    assert len(ErrorCode) == 33


def test_error_code_is_strenum_value_identity() -> None:
    """Each member's value is its own name (PEP 663 StrEnum)."""
    for member in ErrorCode:
        assert member.value == member.name
        assert member == member.name


def test_error_code_unknown_fallback_present() -> None:
    """The closed enum carries an explicit UNKNOWN fallback."""
    assert ErrorCode.UNKNOWN.value == "UNKNOWN"


# --- Exit-bucket mapping ---------------------------------------------------


def test_every_error_code_maps_to_a_valid_bucket() -> None:
    """The cause -> bucket mapping is total over the enum."""
    for member in ErrorCode:
        assert member.exit_code in _VALID_BUCKETS, member.name


def test_error_code_exit_code_never_ok() -> None:
    """No cause-level code folds onto exit 0 — failures stay non-zero."""
    for member in ErrorCode:
        assert member.exit_code != exit_codes.OK, member.name


@pytest.mark.parametrize(
    ("member", "bucket"),
    [
        (ErrorCode.WAVE_DEPS_NOT_SATISFIED, exit_codes.USER_ERROR),
        (ErrorCode.STATE_VALIDATION_FAILED, exit_codes.VALIDATION_ERROR),
        (ErrorCode.CHERRY_PICK_CONFLICT, exit_codes.STATE_CONFLICT),
        (ErrorCode.DAEMON_SOCKET_UNREACHABLE, exit_codes.DAEMON_UNREACHABLE),
        (ErrorCode.BACKUP_WRITE_FAILED, exit_codes.INTERNAL_ERROR),
        (ErrorCode.UNKNOWN, exit_codes.INTERNAL_ERROR),
    ],
)
def test_representative_bucket_assignments(member: ErrorCode, bucket: int) -> None:
    """Spot-check that representative members fold onto their bucket."""
    assert member.exit_code == bucket


# --- Anchor coverage -------------------------------------------------------


def _doc_anchors(text: str) -> set[str]:
    """Return the set of anchor tokens declared in *text*.

    Recognises both ``## <CODE>`` / ``### <CODE>`` headings (GitHub derives
    a slug anchor from each heading) and explicit ``<a id="<CODE>">`` HTML
    anchors, so the coverage check is robust to either anchor style.
    """
    heading = set(re.findall(r"^#{2,4}\s+([A-Z][A-Z0-9_]+)\s*$", text, flags=re.MULTILINE))
    explicit = set(re.findall(r'<a\s+id="([A-Z][A-Z0-9_]+)"', text))
    return heading | explicit


def test_doc_reference_page_exists() -> None:
    assert _DOC.is_file(), f"missing error-codes reference: {_DOC.name!r}"


def test_every_error_code_has_a_doc_anchor() -> None:
    """Anchor coverage — every ErrorCode member is documented exactly once."""
    text = _DOC.read_text(encoding="utf-8")
    anchors = _doc_anchors(text)
    missing = sorted(m.value for m in ErrorCode if m.value not in anchors)
    assert not missing, f"ErrorCode members without a doc anchor: {missing}"


def test_doc_anchors_have_no_orphans() -> None:
    """Every code-shaped anchor maps back to a live ErrorCode member."""
    text = _DOC.read_text(encoding="utf-8")
    members = {m.value for m in ErrorCode}
    orphans = sorted(a for a in _doc_anchors(text) if a not in members)
    assert not orphans, f"doc anchors with no matching ErrorCode: {orphans}"


# --- ErrorEnvelope render order + non-breaking optionality ------------------


def test_envelope_error_code_defaults_to_none() -> None:
    """``error_code`` is optional so legacy error sites keep working."""
    env = errors.ErrorEnvelope(
        error="UserError",
        message="m",
        exit_code=exit_codes.USER_ERROR,
        exit_name="USER_ERROR",
    )
    assert env.error_code is None


def test_render_text_omits_see_line_when_no_code() -> None:
    """Without an error_code the text branch renders exactly as before."""
    env = errors.ErrorEnvelope(
        error="UserError",
        message="scope/.ea missing",
        exit_code=exit_codes.USER_ERROR,
        exit_name="USER_ERROR",
        suggested_next_step="check the scope id",
    )
    rendered = errors._render_text(env)
    assert "See " not in rendered
    assert rendered.splitlines() == [
        "error: scope/.ea missing",
        "hint: check the scope id",
        "exit_code: 1 (USER_ERROR)",
    ]


def test_render_text_cause_next_step_see_order() -> None:
    """With a code the lead order is cause -> next_step -> See <code>."""
    env = errors.ErrorEnvelope(
        error="UserError",
        message="wave 'P27-I01-W21' has unsatisfied deps",
        exit_code=exit_codes.USER_ERROR,
        exit_name="USER_ERROR",
        suggested_next_step="close the blocking wave(s) first",
        error_code=ErrorCode.WAVE_DEPS_NOT_SATISFIED.value,
    )
    lines = errors._render_text(env).splitlines()
    assert lines[0].startswith("error: ")  # cause
    assert lines[1].startswith("hint: ")  # next_step
    assert lines[2] == "See WAVE_DEPS_NOT_SATISFIED"  # see
    # cause precedes next_step precedes see.
    assert lines.index("See WAVE_DEPS_NOT_SATISFIED") > 1


def test_build_envelope_threads_error_code() -> None:
    """build_envelope stores the ErrorCode string value, not the member."""
    err = errors.UserError("bad deps")
    env = errors.build_envelope(err, error_code=ErrorCode.WAVE_DEPS_NOT_SATISFIED)
    assert env.error_code == "WAVE_DEPS_NOT_SATISFIED"
    assert isinstance(env.error_code, str)


def test_build_envelope_error_code_optional() -> None:
    """Omitting error_code leaves the field None (non-breaking default)."""
    env = errors.build_envelope(errors.UserError("oops"))
    assert env.error_code is None


def _make_emitting_app(
    err: errors.CliError,
    flags: GlobalFlags,
    error_code: ErrorCode | None,
) -> typer.Typer:
    app = typer.Typer(no_args_is_help=False)

    @app.command()
    def go() -> None:
        errors.emit_error(err, flags=flags, error_code=error_code)

    return app


def test_emit_error_text_includes_see_line() -> None:
    """End-to-end: emit_error renders the See <code> line in text mode."""
    flags = GlobalFlags(json_output=False)
    app = _make_emitting_app(
        errors.UserError("worktree has uncommitted changes"),
        flags,
        ErrorCode.WORKTREE_DIRTY,
    )
    result = runner.invoke(app, [])
    assert result.exit_code == exit_codes.USER_ERROR
    assert "See WORKTREE_DIRTY" in result.stdout


def test_emit_error_json_includes_error_code() -> None:
    """JSON branch surfaces error_code for machine consumers."""
    import json

    flags = GlobalFlags(json_output=True)
    app = _make_emitting_app(
        errors.StateConflict("cherry-pick hit a conflict"),
        flags,
        ErrorCode.CHERRY_PICK_CONFLICT,
    )
    result = runner.invoke(app, [])
    body = json.loads(result.stdout)
    assert body["error_code"] == "CHERRY_PICK_CONFLICT"
    # The cause-level code does not change the stable bucket exit code.
    assert body["exit_code"] == exit_codes.STATE_CONFLICT


def test_emit_error_json_error_code_none_when_unset() -> None:
    """Legacy emit_error (no error_code) keeps error_code null in JSON."""
    import json

    flags = GlobalFlags(json_output=True)
    app = _make_emitting_app(errors.UserError("plain"), flags, None)
    result = runner.invoke(app, [])
    body = json.loads(result.stdout)
    assert body["error_code"] is None
