"""CLI-side tests for the C05 ``--stream`` surface, ``completion``, and ``help``.

Covers the P26-I01-W06 deliverables:

* ``eawf.cli.streaming`` — NDJSON line-delimited shape on a mock event
  stream, the human ``[HH:MM:SS]`` shape, terminal-status → exit-code
  mapping, and the ``--md --stream`` / ``--quiet --verbose`` flag-combination
  rejections.
* ``eawf.cli.commands.completion`` — ``show`` renders the script to stdout,
  ``install`` writes to the canonical path, and a write failure falls back
  to stdout with a ``UserError`` recovery envelope.
* ``eawf.cli.commands.help`` — topic listing, topic rendering, unknown-topic
  rejection, and the six registered topics all resolving to ≤80-line files.
"""

from __future__ import annotations

import io
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.cli import exit_codes
from eawf.cli.app import app
from eawf.cli.commands import completion as completion_cmd
from eawf.cli.commands import help as help_cmd
from eawf.cli.commands.completion import Shell
from eawf.cli.errors import UserError
from eawf.cli.streaming import (
    end_frame,
    event_frame,
    reject_quiet_verbose_collision,
    reject_unstreamable_combination,
    render_human_line,
    render_ndjson_line,
    start_frame,
    stream_events,
)

pytestmark = pytest.mark.unit

runner = CliRunner()


# --- mock event stream ------------------------------------------------------

_MOCK_EVENTS: list[dict[str, object]] = [
    {"kind": "wave_claimed", "payload": {"wave": "P26-I01-W01"}},
    {"kind": "dispatch_log", "line": "runtime: claude-code"},
    {"kind": "wave_closed", "payload": {"status": "ok"}, "terminal": True, "status": "ok"},
]


# --- streaming: NDJSON shape ------------------------------------------------


def test_stream_events_ndjson_is_line_delimited() -> None:
    """``--json --stream`` emits one complete JSON object per line."""
    buf = io.StringIO()
    rc = stream_events(
        _MOCK_EVENTS,
        scope_id="urn:eawf:v1:state:QR/P26-I01-W01",
        json_output=True,
        out=buf,
    )

    raw = buf.getvalue()
    # Trailing newline on every line — the buffer ends with one.
    assert raw.endswith("\n")
    lines = raw.strip().split("\n")
    # start + 3 events + end.
    assert len(lines) == 5

    # Every line parses independently as a complete JSON object (NDJSON).
    frames = [orjson.loads(line) for line in lines]
    assert [f["type"] for f in frames] == ["start", "event", "event", "event", "end"]
    assert frames[0]["scope_id"] == "urn:eawf:v1:state:QR/P26-I01-W01"
    assert frames[1]["kind"] == "wave_claimed"
    assert frames[2]["line"] == "runtime: claude-code"
    assert frames[-1]["status"] == "ok"
    # ``ok`` terminal status → exit 0.
    assert rc == exit_codes.OK


def test_stream_events_ndjson_drops_none_fields() -> None:
    """NDJSON lines omit unset frame fields so each matches the § 5.8 shape."""
    line = render_ndjson_line(event_frame(kind="dispatch_log", line="x"))
    obj = orjson.loads(line)
    # An event frame carries no start/end-only keys.
    assert "scope_id" not in obj
    assert "status" not in obj
    assert "payload" not in obj
    assert obj["kind"] == "dispatch_log"


def test_stream_events_ndjson_terminal_status_failed_exit_5() -> None:
    """A ``failed`` terminal frame maps to exit 5 INTERNAL_ERROR."""
    buf = io.StringIO()
    rc = stream_events(
        [{"kind": "wave_failed", "terminal": True, "status": "failed"}],
        scope_id="urn:eawf:v1:state:QR/P26-I01-W01",
        json_output=True,
        out=buf,
    )
    assert rc == exit_codes.INTERNAL_ERROR
    assert orjson.loads(buf.getvalue().strip().split("\n")[-1])["status"] == "failed"


def test_stream_events_unknown_terminal_status_falls_back_to_failed() -> None:
    """An opaque terminal status surfaces as ``failed`` (non-zero), not silent ok."""
    buf = io.StringIO()
    rc = stream_events(
        [{"kind": "x", "terminal": True, "status": "weird"}],
        scope_id="urn:eawf:v1:state:QR/W01",
        json_output=True,
        out=buf,
    )
    assert rc == exit_codes.INTERNAL_ERROR


def test_stream_events_disconnected_status_exit_4() -> None:
    """A daemon-disconnect terminal status maps to exit 4 DAEMON_UNREACHABLE."""
    buf = io.StringIO()
    rc = stream_events(
        [{"kind": "x", "terminal": True, "status": "disconnected"}],
        scope_id="urn:eawf:v1:state:QR/W01",
        json_output=True,
        out=buf,
    )
    assert rc == exit_codes.DAEMON_UNREACHABLE


def test_stream_events_skips_non_dict_items() -> None:
    """Non-dict items in the stream are skipped, not faulted."""
    buf = io.StringIO()
    rc = stream_events(
        ["not-a-dict", {"kind": "ok"}],  # type: ignore[list-item]
        scope_id="urn:eawf:v1:state:QR/W01",
        json_output=True,
        out=buf,
    )
    lines = buf.getvalue().strip().split("\n")
    # start + 1 event (the string skipped) + end.
    assert len(lines) == 3
    assert rc == exit_codes.OK


def test_stream_events_no_terminal_event_ends_ok() -> None:
    """Exhausting the stream with no terminal marker ends ``ok`` (exit 0)."""
    buf = io.StringIO()
    rc = stream_events(
        [{"kind": "a"}, {"kind": "b"}],
        scope_id="urn:eawf:v1:state:QR/W01",
        json_output=True,
        out=buf,
    )
    assert rc == exit_codes.OK
    assert orjson.loads(buf.getvalue().strip().split("\n")[-1])["status"] == "ok"


# --- streaming: human shape -------------------------------------------------


def test_stream_events_human_shape_blank_eof_marker() -> None:
    """``--stream`` alone emits bracketed lines terminated by a blank ``^$``."""
    buf = io.StringIO()
    rc = stream_events(
        _MOCK_EVENTS,
        scope_id="urn:eawf:v1:state:QR/P26-I01-W01",
        json_output=False,
        out=buf,
    )
    raw = buf.getvalue()
    # Human output terminates with a single blank marker line, not an `end`.
    assert raw.endswith("\n\n")
    assert "done:" not in raw  # no NDJSON-style end line in human mode
    first = raw.splitlines()[0]
    assert first.startswith("[") and "starting for" in first
    assert rc == exit_codes.OK


def test_render_human_line_truncates_log_without_verbose() -> None:
    """A long ``dispatch_log`` line truncates unless ``--verbose`` is set."""
    long_line = "x" * 200
    frame = event_frame(kind="dispatch_log", line=long_line)
    terse = render_human_line(frame, verbose=False)
    full = render_human_line(frame, verbose=True)
    assert "truncated; pass --verbose for full" in terse
    assert "truncated" not in full
    assert long_line in full


def test_render_human_line_start_and_end() -> None:
    """Start/end human lines carry the scope and status respectively."""
    start = render_human_line(start_frame(scope_id="urn:eawf:v1:state:QR/W01"))
    end = render_human_line(end_frame(status="ok"))
    assert "starting for urn:eawf:v1:state:QR/W01" in start
    assert "done: ok" in end


# --- streaming: flag-combination rejections ---------------------------------


def test_reject_quiet_verbose_collision_raises_user_error() -> None:
    """``--quiet --verbose`` is rejected as a USER_ERROR (exit 1)."""
    with pytest.raises(UserError) as excinfo:
        reject_quiet_verbose_collision(quiet=True, verbose=True)
    assert excinfo.value.exit_code == exit_codes.USER_ERROR
    assert "mutually exclusive" in str(excinfo.value)


def test_reject_quiet_verbose_collision_allows_either_alone() -> None:
    """Either flag alone (or neither) is accepted."""
    reject_quiet_verbose_collision(quiet=True, verbose=False)
    reject_quiet_verbose_collision(quiet=False, verbose=True)
    reject_quiet_verbose_collision(quiet=False, verbose=False)


def test_reject_unstreamable_combination_rejects_md_stream() -> None:
    """``--md --stream`` is rejected — markdown is not line-streamable."""
    with pytest.raises(UserError) as excinfo:
        reject_unstreamable_combination(md_output=True, stream=True)
    assert excinfo.value.exit_code == exit_codes.USER_ERROR
    assert "not streamable" in str(excinfo.value)


def test_reject_unstreamable_combination_allows_json_stream() -> None:
    """``--json --stream`` (md off) is accepted."""
    reject_unstreamable_combination(md_output=False, stream=True)
    reject_unstreamable_combination(md_output=True, stream=False)


# --- completion: show -------------------------------------------------------


def test_completion_show_zsh_renders_script_to_stdout() -> None:
    """``completion show zsh`` prints the zsh completion script."""
    result = runner.invoke(app, ["completion", "show", "zsh"])
    assert result.exit_code == 0
    assert "#compdef eawf" in result.stdout
    assert "_EAWF_COMPLETE" in result.stdout


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_show_all_shells(shell: str) -> None:
    """All three supported shells render a non-empty script."""
    result = runner.invoke(app, ["completion", "show", shell])
    assert result.exit_code == 0
    assert result.stdout.strip()


def test_completion_show_rejects_unknown_shell() -> None:
    """An unsupported shell is rejected by the Typer enum (non-zero exit)."""
    result = runner.invoke(app, ["completion", "show", "powershell"])
    assert result.exit_code != 0


# --- completion: install ----------------------------------------------------


@pytest.mark.parametrize(
    ("shell", "rel"),
    [
        (Shell.BASH, "bash-completion/completions/eawf"),
        (Shell.ZSH, "zsh/site-functions/_eawf"),
        (Shell.FISH, "fish/completions/eawf.fish"),
    ],
)
def test_completion_install_writes_canonical_path(
    shell: Shell, rel: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``completion install`` writes the script under ``$XDG_DATA_HOME``."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    result = runner.invoke(app, ["completion", "install", shell.value])
    assert result.exit_code == 0
    target = tmp_path / rel
    assert target.is_file()
    assert target.read_text(encoding="utf-8").strip()
    assert str(target) in result.stdout


def test_completion_install_permission_failure_falls_back_to_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write failure prints the script to stdout + a USER_ERROR recovery hint."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("read-only fs")

    # Force the write to fail after the (successful) mkdir.
    monkeypatch.setattr(completion_cmd.Path, "write_text", _boom)

    result = runner.invoke(app, ["completion", "install", "zsh"])
    assert result.exit_code == exit_codes.USER_ERROR
    # The script still reached stdout for capture.
    assert "#compdef eawf" in result.stdout
    # The recovery hint names the explicit show > path command.
    assert "eawf completion show zsh" in result.stdout


# --- help -------------------------------------------------------------------


def test_help_no_arg_lists_all_topics() -> None:
    """``eawf help`` lists every registered topic."""
    result = runner.invoke(app, ["help"])
    assert result.exit_code == 0
    for topic in help_cmd.TOPICS:
        assert topic in result.stdout


def test_help_renders_known_topic_flat_off_tty() -> None:
    """``eawf help exit-codes`` prints the topic body (flat, off-TTY)."""
    result = runner.invoke(app, ["help", "exit-codes"])
    assert result.exit_code == 0
    assert "# Exit codes" in result.stdout
    assert "DAEMON_UNREACHABLE" in result.stdout


def test_help_unknown_topic_rejected_not_found() -> None:
    """An unknown topic exits 1 USER_ERROR with ``data.kind=NotFound`` + the list."""
    result = runner.invoke(app, ["help", "nonesuch"])
    assert result.exit_code == exit_codes.USER_ERROR
    assert "unknown help topic" in result.stdout
    assert "NotFound" in result.stdout


def test_help_json_emits_body_envelope() -> None:
    """``--json eawf help <topic>`` emits a JSON envelope carrying the body."""
    result = runner.invoke(app, ["--json", "help", "streaming"])
    assert result.exit_code == 0
    obj = orjson.loads(result.stdout)
    assert obj["topic"] == "streaming"
    assert "NDJSON" in obj["body"]


@pytest.mark.parametrize("topic", list(help_cmd.TOPICS))
def test_help_all_topics_resolve_and_fit_line_budget(topic: str) -> None:
    """Every registered topic resolves to an existing ≤80-line markdown file."""
    path = help_cmd._topic_path(topic)
    assert path is not None, f"topic {topic!r} has no source file"
    line_count = len(path.read_text(encoding="utf-8").splitlines())
    assert line_count <= 80, f"{topic}.md is {line_count} lines (budget 80)"


def test_help_topics_dir_resolves_in_repo() -> None:
    """The repo-root ``docs/help`` directory is discoverable from the package."""
    base = help_cmd._topics_dir()
    assert base is not None
    assert base.name == "help"
    assert (base / "exit-codes.md").is_file()
