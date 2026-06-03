"""Unit tests for the ``eawf hook vale-prose`` + ``eawf hook eawf017-inline-refs`` gates.

Pins:

- ``vale-prose`` fails open (advisory skip, exit 0) when the ``vale`` binary
  is absent (monkeypatched ``shutil.which`` so the test is deterministic
  regardless of the host's Vale install).
- ``vale-prose`` parses real ``vale --output=JSON`` into the static-lint
  envelope (skipped when the ``vale`` binary is genuinely absent).
- The ``--text-surface`` temp-file bridge round-trips a commit-body string
  (parses without error; the surface label, not the temp path, is reported).
- ``eawf017-inline-refs`` exits 1 on a bare inline URL and on 3+ inline
  ``path:line`` refs, and 0 when refs live in a ``## References`` table.
- Both subcommands register on the ``eawf hook`` surface.
"""

from __future__ import annotations

import json
import shutil

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()

_VALE_ABSENT = shutil.which("vale") is None


# --- vale-prose: fail-open when the binary is absent ----------------------


def test_vale_prose_fails_open_when_binary_absent(tmp_path, monkeypatch) -> None:
    # Simulate a host without Vale: the gate must skip advisory + exit 0.
    monkeypatch.setattr("shutil.which", lambda _name: None)
    doc = tmp_path / "note.md"
    doc.write_text("Some prose with https://example.org/x inline.\n", encoding="utf-8")
    result = runner.invoke(app, ["hook", "vale-prose", str(doc)])
    assert result.exit_code == 0, result.stdout
    assert "skip" in result.stdout.lower()


def test_vale_prose_fail_open_json_marks_skipped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    doc = tmp_path / "note.md"
    doc.write_text("Prose.\n", encoding="utf-8")
    result = runner.invoke(app, ["--json", "hook", "vale-prose", str(doc)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["skipped"] is True
    assert payload["clean"] is True


def test_vale_prose_no_markdown_targets_is_clean_noop(tmp_path, monkeypatch) -> None:
    # A non-.md explicit arg yields zero targets -> clean exit 0 (binary
    # present so the which() short-circuit is not the cause).
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/vale")
    txt = tmp_path / "note.txt"
    txt.write_text("not markdown\n", encoding="utf-8")
    result = runner.invoke(app, ["hook", "vale-prose", str(txt)])
    assert result.exit_code == 0, result.stdout
    assert "clean" in result.stdout.lower()


_E100_JSON = '{"Code": "E100", "Text": "E100 [loadStyles] missing package", "Span": 0, "Line": 0}'


def test_run_vale_json_treats_stderr_error_object_as_fail_open() -> None:
    # Vale writes E100 (unsynced StylesPath) to STDERR with a non-zero exit;
    # _run_vale_json must raise _ValeRunError so the command fails open rather
    # than reading the empty stdout as a clean result.
    from eawf.surfaces.cli.commands import hook as hook_mod

    class _Proc:
        stdout = ""
        stderr = _E100_JSON

    monkey = pytest.MonkeyPatch()
    monkey.setattr(hook_mod.subprocess, "run", lambda *a, **k: _Proc())
    try:
        with pytest.raises(hook_mod._ValeRunError):
            hook_mod._run_vale_json([], cwd=hook_mod.Path("."))
    finally:
        monkey.undo()


def test_run_vale_json_treats_stdout_error_object_as_fail_open() -> None:
    # Defensive: some Vale paths emit the error object on stdout instead.
    from eawf.surfaces.cli.commands import hook as hook_mod

    class _Proc:
        stdout = _E100_JSON
        stderr = ""

    monkey = pytest.MonkeyPatch()
    monkey.setattr(hook_mod.subprocess, "run", lambda *a, **k: _Proc())
    try:
        with pytest.raises(hook_mod._ValeRunError):
            hook_mod._run_vale_json([], cwd=hook_mod.Path("."))
    finally:
        monkey.undo()


def test_run_vale_json_parses_normal_alert_map() -> None:
    # The happy path: a {path: [alert]} map parses through cleanly.
    import json as _json

    from eawf.surfaces.cli.commands import hook as hook_mod

    alert = {
        "Line": 3,
        "Span": [5, 9],
        "Severity": "warning",
        "Check": "Google.Weasel",
        "Message": "Weasel word",
    }

    class _Proc:
        stdout = _json.dumps({"/tmp/x.md": [alert]})
        stderr = ""

    monkey = pytest.MonkeyPatch()
    monkey.setattr(hook_mod.subprocess, "run", lambda *a, **k: _Proc())
    try:
        payload = hook_mod._run_vale_json([], cwd=hook_mod.Path("."))
        rows = hook_mod._vale_findings_to_rows(payload, rel_for={"/tmp/x.md": "note.md"})
    finally:
        monkey.undo()
    assert len(rows) == 1
    assert "note.md:3:5" in rows[0]
    assert "Google.Weasel" in rows[0]


def test_vale_findings_to_rows_skips_non_list_values() -> None:
    # Defensive: even handed the raw error object, the renderer must not crash.
    from eawf.surfaces.cli.commands.hook import _vale_findings_to_rows

    err = {"Code": "E100", "Text": "boom", "Span": 0, "Line": 0}
    assert _vale_findings_to_rows(err, rel_for={}) == []


# --- vale-prose: real binary parse (skipped when vale absent) -------------


@pytest.mark.skipif(_VALE_ABSENT, reason="vale binary not installed on host")
def test_vale_prose_parses_json_into_envelope(tmp_path) -> None:
    # A prose file with a weasel word + passive voice gives Vale something to
    # flag; the gate parses the JSON and stays non-blocking (exit 0).
    doc = tmp_path / "note.md"
    doc.write_text(
        "This was clearly implemented by the team and is very obviously correct.\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["--json", "hook", "vale-prose", str(doc)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    # Whether or not Vale's installed packages flag this specific prose, the
    # envelope must be well-formed and the gate non-blocking.
    assert payload["hook"] == "vale-prose"
    assert "clean" in payload


# --- vale-prose: --text-surface temp-file bridge --------------------------


@pytest.mark.skipif(_VALE_ABSENT, reason="vale binary not installed on host")
def test_vale_prose_text_surface_round_trips_commit_body(tmp_path) -> None:
    # The temp-file bridge: a commit-body string is written to a temp .md,
    # linted, then discarded — a non-crashing exit 0. Whether the host has
    # the Google package synced (scanned=1) or not (fail-open skip), the
    # round-trip itself must succeed and never crash.
    body = "Raise the runner budget after CI jitter flagged false failures.\n"
    result = runner.invoke(
        app,
        [
            "--json",
            "hook",
            "vale-prose",
            "--text-surface",
            body,
            "--surface-label",
            "commit body",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["hook"] == "vale-prose"
    assert payload["clean"] is True
    # Either it linted the temp file (scanned) or it failed open (skipped);
    # both are valid round-trips, neither leaks the raw temp path as a finding.
    assert payload.get("scanned") == 1 or payload.get("skipped") is True
    assert "/private/tmp" not in result.stdout and "/tmp/" not in result.stdout


def test_vale_prose_text_surface_remaps_temp_path_to_label(monkeypatch) -> None:
    # Deterministic temp-file bridge round-trip (no Google dependency): the
    # text is written to a temp .md, _run_vale_json returns an alert keyed by
    # that temp path, and the finding must report the surface label, never the
    # temp path. This pins the rel_for remap regardless of the host's Vale.
    from eawf.surfaces.cli.commands import hook as hook_mod

    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/vale")

    captured: dict[str, str] = {}

    def fake_run(targets, *, cwd):  # type: ignore[no-untyped-def]
        # The command wrote the text to exactly one temp file; key the alert
        # on that path so the rel_for remap is exercised end-to-end.
        temp_path = str(targets[0])
        captured["temp"] = temp_path
        return {
            temp_path: [
                {
                    "Line": 1,
                    "Span": [6, 12],
                    "Severity": "warning",
                    "Check": "Google.Weasel",
                    "Message": "Weasel word 'clearly'",
                }
            ]
        }

    monkeypatch.setattr(hook_mod, "_run_vale_json", fake_run)
    result = runner.invoke(
        app,
        [
            "hook",
            "vale-prose",
            "--text-surface",
            "This was clearly done by the team.\n",
            "--surface-label",
            "commit body",
        ],
    )
    assert result.exit_code == 0, result.stdout  # advisory, non-blocking
    assert "commit body" in result.stdout
    assert "Google.Weasel" in result.stdout
    # The raw temp path must never surface in the rendered finding.
    assert captured["temp"] not in result.stdout


def test_vale_prose_text_surface_fail_open_when_absent(monkeypatch) -> None:
    # The bridge also fails open: no binary -> advisory skip even for a text
    # surface, so a commit hook on a Vale-less host does not block.
    monkeypatch.setattr("shutil.which", lambda _name: None)
    result = runner.invoke(
        app,
        [
            "hook",
            "vale-prose",
            "--text-surface",
            "Some commit body.\n",
            "--surface-label",
            "commit body",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert "skip" in result.stdout.lower()


# --- eawf017-inline-refs --------------------------------------------------


def test_eawf017_inline_refs_blocks_on_bare_url(tmp_path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("Fixed it, see https://example.org/jitter for the trace.\n", encoding="utf-8")
    result = runner.invoke(app, ["hook", "eawf017-inline-refs", str(bad)])
    assert result.exit_code == 1, result.stdout
    assert "EAWF017" in result.stdout


def test_eawf017_inline_refs_blocks_on_three_path_refs(tmp_path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text(
        "Edited src/eawf/a.py:10, src/eawf/b.py:20, and src/eawf/c.py:30 to fix it.\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["hook", "eawf017-inline-refs", str(bad)])
    assert result.exit_code == 1, result.stdout
    assert "EAWF017" in result.stdout


def test_eawf017_inline_refs_clean_with_references_table(tmp_path) -> None:
    good = tmp_path / "good.md"
    good.write_text(
        "Raised the runner budget after CI jitter [a][b][c].\n"
        "\n"
        "## References\n"
        "\n"
        "[a] `src/eawf/a.py:10`\n"
        "[b] `src/eawf/b.py:20`\n"
        "[c] https://example.org/jitter\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["hook", "eawf017-inline-refs", str(good)])
    assert result.exit_code == 0, result.stdout
    assert "clean" in result.stdout.lower()


def test_eawf017_inline_refs_ignores_non_markdown_arg(tmp_path) -> None:
    txt = tmp_path / "note.txt"
    txt.write_text("See https://example.org/x inline.\n", encoding="utf-8")
    result = runner.invoke(app, ["hook", "eawf017-inline-refs", str(txt)])
    assert result.exit_code == 0, result.stdout


def test_eawf017_inline_refs_json_output(tmp_path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("See https://example.org/x for the trace.\n", encoding="utf-8")
    result = runner.invoke(app, ["--json", "hook", "eawf017-inline-refs", str(bad)])
    assert result.exit_code == 1, result.stdout
    payload = json.loads(result.stdout)
    assert payload["clean"] is False
    assert payload["violations"] >= 1


# --- registration smoke ----------------------------------------------------


@pytest.mark.parametrize("name", ["vale-prose", "eawf017-inline-refs"])
def test_hook_subcommands_registered(name: str) -> None:
    result = runner.invoke(app, ["hook", "--help"])
    assert result.exit_code == 0
    assert name in result.stdout
