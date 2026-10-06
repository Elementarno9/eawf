"""Tests for the pure checks of the ledger history gates.

The append-only gate is proved on the in-place SHA remap a history rewrite once
applied to a committed batch ledger line: the fixture pair is that line before and
after the rewrite. The reachability gate is proved on rows carrying SHAs this
repository's ledgers bound to commits no longer reachable from the default branch.
The hook's exit policy is pinned here over those findings; the git-backed scan
is tested in the integration tier.
"""

from __future__ import annotations

from pathlib import Path

import click
import orjson
import pytest
from typer.main import get_command

from eawf.kernel.store.ledger import (
    LedgerRecord,
    LedgerTornTailError,
    line_digest,
    render_ledger_line,
    split_ledger_lines,
)
from eawf.platform.lint import ledger_history
from eawf.platform.lint.ledger_history import (
    SHA_RE,
    AppendOnlyFinding,
    LedgerHistoryError,
    LedgerHistoryReport,
    SealedRemoval,
    ShaMention,
    UnreachableFinding,
    append_only_finding,
    is_ledger_path,
    parse_range,
    sha_mentions,
    unreachable_findings,
)
from eawf.surfaces.cli.app import app

_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "ledger_history"
_LEDGER = ".ea/generations/gen-ca819f3245bf71a8/ledger/batch.jsonl"
_REMAPPED_FROM = "e2ed250ef4ab065391ecc7a895a8af22dc3d4d54"  # pragma: allowlist secret
_REMAPPED_TO = "b80533bebb6421ad4dd935aacd268171a08eefae"  # pragma: allowlist secret
#: Three of the stray SHAs, each on the fixture line of the same index.
_STRAYS = (
    ("P22-I01-W05", "131b469339b4354624e9c0a635aa5b851df096d7"),  # pragma: allowlist secret
    ("P28-I03-W03", "ecc5d99c0f4b59ed61e733223c69a1d9fdf7ff60"),  # pragma: allowlist secret
    ("P36-I01-W01", "d8bf262acf51a847d063f7d40b688a2684a21cf3"),  # pragma: allowlist secret
)
_TASK_LEDGER = ".ea/generations/gen-x/ledger/task.jsonl"


def _superseding(line: str, **payload_update: object) -> str:
    """Return a row superseding *line*, with *payload_update* merged into its payload."""
    record = LedgerRecord.model_validate_json(line)
    return render_ledger_line(
        record.model_copy(
            update={
                "supersedes": line_digest(line),
                "payload": {**record.payload, **payload_update},
            }
        )
    )


def _declaration(sha: str) -> list[dict[str, str]]:
    return [{"sha": sha, "archive_ref": "refs/tags/archive/x", "reason": "squashed away"}]


# --- append-only ------------------------------------------------------------


def test_append_only_reds_on_the_in_place_sha_remap() -> None:
    before = (_FIXTURES / "batch_before.jsonl").read_bytes()
    after = (_FIXTURES / "batch_remapped.jsonl").read_bytes()
    finding = append_only_finding(_LEDGER, before, after)
    assert finding is not None
    assert finding.path == _LEDGER
    assert finding.offset > len(split_ledger_lines(before)[0])
    assert before[finding.offset : finding.offset + 40] == _REMAPPED_FROM.encode()
    assert after[finding.offset : finding.offset + 40] == _REMAPPED_TO.encode()
    assert finding.detail == "rewrites a committed line"
    assert f"{_LEDGER}:byte {finding.offset}" in finding.render()


def test_append_only_accepts_an_appended_superseding_row() -> None:
    before = (_FIXTURES / "batch_before.jsonl").read_bytes()
    remapped = split_ledger_lines((_FIXTURES / "batch_remapped.jsonl").read_bytes())[1]
    original = split_ledger_lines(before)[1]
    correction = _superseding(original, **orjson.loads(remapped)["payload"])
    after = before + correction.encode() + b"\n"
    assert append_only_finding(_LEDGER, before, after) is None


@pytest.mark.parametrize(
    ("before", "after"),
    [(b"", b""), (b"", b"x\n"), (b"a\n", b"a\n"), (b"a\n", b"a\nb\n")],
)
def test_append_only_accepts_unchanged_and_extended_content(before: bytes, after: bytes) -> None:
    assert append_only_finding(_LEDGER, before, after) is None


def test_append_only_reds_on_a_change_to_the_last_byte() -> None:
    finding = append_only_finding(_LEDGER, b"ab\n", b"ab\r")
    assert finding is not None
    assert finding.offset == 2


def test_append_only_reds_on_a_truncation_and_a_deletion() -> None:
    truncated = append_only_finding(_LEDGER, b"a\nb\n", b"a\n")
    assert truncated is not None
    assert (truncated.offset, truncated.detail) == (2, "drops 2 committed bytes")
    deleted = append_only_finding(_LEDGER, b"a\n", b"")
    assert deleted is not None
    assert deleted.offset == 0


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (".ea/generations/gen-x/ledger/task.jsonl", True),
        (".ea/generations/gen-x/history/change.jsonl", True),
        (".ea/generations/gen-x/state.json", False),
        (".ea/generations/gen-x/local/runs.jsonl", False),
        ("src/eawf/ledger/task.jsonl", False),
        (".ea/no-such-family/x.jsonl", False),
    ],
)
def test_append_only_scope_is_the_committed_ledger_tier(path: str, expected: bool) -> None:
    assert is_ledger_path(path) is expected


# --- reachability: pure checks ----------------------------------------------


def _stray_lines() -> list[str]:
    return list(split_ledger_lines((_FIXTURES / "task_stray.jsonl").read_bytes()))


def _content(lines: list[str]) -> bytes:
    return "".join(f"{line}\n" for line in lines).encode()


def test_reachability_reports_each_stray_sha_of_this_repository() -> None:
    mentions, malformed = sha_mentions(_TASK_LEDGER, _content(_stray_lines()))
    assert malformed == ()
    strays = frozenset(sha for _key, sha in _STRAYS)
    findings = unreachable_findings(mentions, commits=strays, reachable=frozenset())
    assert [(f.line, f.record_key, f.sha) for f in findings] == [
        (index, key, sha) for index, (key, sha) in enumerate(_STRAYS, start=1)
    ]
    assert all(f.path == _TASK_LEDGER for f in findings)


def test_reachability_passes_strays_once_reachable() -> None:
    mentions, _ = sha_mentions(_TASK_LEDGER, _content(_stray_lines()))
    strays = frozenset(sha for _key, sha in _STRAYS)
    assert unreachable_findings(mentions, commits=strays, reachable=strays) == ()


def test_reachability_exempts_a_sha_declared_in_unreachable_shas() -> None:
    lines = _stray_lines()
    key, sha = _STRAYS[0]
    lines.append(_superseding(lines[0], unreachable_shas=_declaration(sha)))
    mentions, malformed = sha_mentions(_TASK_LEDGER, _content(lines))
    assert malformed == ()
    strays = frozenset(s for _k, s in _STRAYS)
    findings = unreachable_findings(mentions, commits=strays, reachable=frozenset())
    assert sha not in {f.sha for f in findings}
    assert key not in {f.record_key for f in findings}
    assert len(findings) == len(_STRAYS) - 1


def test_reachability_declaration_exempts_only_its_own_row() -> None:
    lines = _stray_lines()
    _key, sha = _STRAYS[0]
    # A declaration on another row leaves the stray row itself standing.
    lines.append(_superseding(lines[1], unreachable_shas=_declaration(sha)))
    mentions, _ = sha_mentions(_TASK_LEDGER, _content(lines))
    findings = unreachable_findings(mentions, commits=frozenset({sha}), reachable=frozenset())
    assert [f.line for f in findings] == [1]


def test_reachability_reports_a_malformed_declaration_and_exempts_nothing() -> None:
    lines = _stray_lines()
    _key, sha = _STRAYS[0]
    lines.append(_superseding(lines[0], unreachable_shas=[{"sha": sha}]))
    mentions, malformed = sha_mentions(_TASK_LEDGER, _content(lines))
    assert [m.line for m in malformed] == [len(lines)]
    assert "malformed unreachable_shas" in malformed[0].render()
    findings = unreachable_findings(mentions, commits=frozenset({sha}), reachable=frozenset())
    assert [f.line for f in findings] == [len(lines)]


def test_reachability_ignores_a_superseded_row() -> None:
    lines = _stray_lines()[:1]
    lines.append(_superseding(lines[0], legacy_refs={}))
    mentions, _ = sha_mentions(_TASK_LEDGER, _content(lines))
    assert all(m.line == 2 for m in mentions)


def test_reachability_reads_an_empty_and_a_single_row_ledger() -> None:
    assert sha_mentions(_TASK_LEDGER, b"") == ((), ())
    single = b'{"change_id": "c1", "sha": "' + b"a" * 40 + b'"}\n'
    mentions, _ = sha_mentions(_TASK_LEDGER, single)
    assert mentions == (ShaMention(path=_TASK_LEDGER, line=1, record_key="c1", sha="a" * 40),)


@pytest.mark.parametrize(
    ("text", "found"),
    [("a" * 40, ["a" * 40]), ("a" * 39, []), ("a" * 41, []), ("a" * 64, []), ("A" * 40, [])],
)
def test_reachability_matches_only_full_lowercase_shas(text: str, found: list[str]) -> None:
    assert SHA_RE.findall(f'"{text}"') == found


def test_reachability_refuses_a_torn_or_non_json_ledger() -> None:
    with pytest.raises(LedgerTornTailError):
        sha_mentions(_TASK_LEDGER, b'{"a": 1}')
    with pytest.raises(orjson.JSONDecodeError):
        sha_mentions(_TASK_LEDGER, b"not json\n")


# --- range parsing ------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("origin/main..HEAD", ("origin/main", "HEAD")), ("HEAD", ("HEAD", None))],
)
def test_reachability_range_parses(spec: str, expected: tuple[str, str | None]) -> None:
    assert parse_range(spec) == expected


@pytest.mark.parametrize("spec", ["", "..HEAD", "HEAD..", "a...b"])
def test_reachability_range_refuses_a_malformed_spec(spec: str) -> None:
    with pytest.raises(LedgerHistoryError):
        parse_range(spec)


# --- the hook blocks ------------------------------------------------------------


def _report(**findings: object) -> LedgerHistoryReport:
    return LedgerHistoryReport.model_validate(
        {
            "range": "base..HEAD",
            "base": "a" * 40,
            "tip": "b" * 40,
            "ledgers_scanned": 1,
            "append_only": (),
            "unreachable": (),
            "malformed": (),
            **findings,
        }
    )


def _run_hook(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    report: LedgerHistoryReport,
    *args: str,
) -> tuple[int, str]:
    """Run the hook over *report* in place of a scan; return its exit code and output."""
    # The scan reads the repository; the hook's exit policy is what this tier pins.
    monkeypatch.setattr(ledger_history, "scan_ledger_history", lambda _repo, _spec: report)
    argv = ["--json", "hook", "ledger-history", *args]
    try:
        code = get_command(app).main(argv, prog_name="eawf", standalone_mode=False)
    except click.UsageError as exc:
        return exc.exit_code, exc.format_message()
    return (code or 0), capsys.readouterr().out


def _remap_finding() -> AppendOnlyFinding:
    before = (_FIXTURES / "batch_before.jsonl").read_bytes()
    after = (_FIXTURES / "batch_remapped.jsonl").read_bytes()
    finding = append_only_finding(_LEDGER, before, after)
    assert finding is not None
    return finding


def _stray_findings() -> tuple[UnreachableFinding, ...]:
    mentions, _ = sha_mentions(_TASK_LEDGER, _content(_stray_lines()))
    strays = frozenset(sha for _key, sha in _STRAYS)
    return unreachable_findings(mentions, commits=strays, reachable=frozenset())


def test_hook_blocks_on_the_in_place_sha_remap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    finding = _remap_finding()
    code, output = _run_hook(
        monkeypatch, capsys, _report(append_only=(finding,)), "--range", "base..HEAD"
    )
    assert code == 1
    payload = orjson.loads(output)
    assert payload["clean"] is False
    assert payload["append_only"] == [finding.model_dump(mode="json")]
    assert "report_only" not in payload


def test_hook_blocks_on_the_stray_shas(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    findings = _stray_findings()
    assert len(findings) == len(_STRAYS)
    code, output = _run_hook(
        monkeypatch, capsys, _report(unreachable=findings), "--range", "base..HEAD"
    )
    assert code == 1
    assert [f["sha"] for f in orjson.loads(output)["unreachable"]] == [sha for _key, sha in _STRAYS]


def test_hook_blocks_on_a_single_malformed_declaration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = _stray_lines()
    lines.append(_superseding(lines[0], unreachable_shas=[{"sha": _STRAYS[0][1]}]))
    _, malformed = sha_mentions(_TASK_LEDGER, _content(lines))
    assert len(malformed) == 1
    code, output = _run_hook(
        monkeypatch, capsys, _report(malformed=malformed), "--range", "base..HEAD"
    )
    assert code == 1
    assert len(orjson.loads(output)["malformed"]) == 1


def test_hook_passes_declared_strays_and_a_sealed_removal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = _stray_lines()
    for index, (_key, sha) in enumerate(_STRAYS):
        lines.append(_superseding(lines[index], unreachable_shas=_declaration(sha)))
    mentions, malformed = sha_mentions(_TASK_LEDGER, _content(lines))
    strays = frozenset(sha for _key, sha in _STRAYS)
    assert unreachable_findings(mentions, commits=strays, reachable=frozenset()) == ()
    assert malformed == ()
    sealed = SealedRemoval(path=_TASK_LEDGER, snapshot=f"{_TASK_LEDGER}.snap", sealed_bytes=1)
    code, output = _run_hook(
        monkeypatch, capsys, _report(sealed=(sealed,)), "--range", "base..HEAD"
    )
    assert code == 0
    payload = orjson.loads(output)
    assert payload["clean"] is True
    assert payload["sealed"] == [sealed.model_dump(mode="json")]


def test_hook_no_longer_accepts_report_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    finding = _remap_finding()
    code, output = _run_hook(
        monkeypatch,
        capsys,
        _report(append_only=(finding,)),
        "--range",
        "base..HEAD",
        "--report-only",
    )
    assert code == 2
    assert "No such option: --report-only" in output


def test_hook_requires_a_range(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, output = _run_hook(monkeypatch, capsys, _report())
    assert code == 2
    assert "--range" in output
