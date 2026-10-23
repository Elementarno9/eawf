"""The STE prose lint reports long sentences and semicolons and never blocks."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from eawf.platform.lint.ste_prose import (
    DESCRIPTION_MAX_WORDS,
    INSTRUCTION_MAX_WORDS,
    SentenceKind,
    SteFinding,
    file_findings,
    main,
    markdown_findings,
    prose_findings,
    yaml_findings,
)


def _sentence(words: int) -> str:
    return " ".join(["Run"] * words) + "."


def _kinds(findings: list[SteFinding]) -> list[str]:
    return [finding.kind for finding in findings]


@pytest.mark.parametrize(
    "body",
    [
        f"instruction: {_sentence(21)}\n",
        f"rationale: {_sentence(26)}\n",
        "instruction: Run the gate; then stop.\n",
    ],
)
def test_ste_lint_reports_and_never_blocks(
    tmp_path: Path, body: str, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "rule.yaml"
    source.write_text(body, encoding="utf-8")
    assert main([str(source)]) == 0
    out = capsys.readouterr().out
    assert out.count(" ste-") == 1
    assert out.endswith("ste-prose: 1 advisory finding(s)\n")


@pytest.mark.parametrize(
    ("kind", "limit"),
    [("instruction", INSTRUCTION_MAX_WORDS), ("description", DESCRIPTION_MAX_WORDS)],
)
def test_prose_findings_flags_one_word_past_the_limit(kind: SentenceKind, limit: int) -> None:
    assert prose_findings(_sentence(limit), kind=kind, path="p", line=1) == []
    (finding,) = prose_findings(_sentence(limit + 1), kind=kind, path="p", line=3)
    assert (finding.kind, finding.line) == (f"{kind}-length", 3)
    assert finding.detail == f"{limit + 1} words > {limit}"


@pytest.mark.parametrize("text", ["", "   ", "Run."])
def test_prose_findings_passes_empty_and_single_word_text(text: str) -> None:
    assert prose_findings(text, kind="instruction", path="p", line=1) == []


def test_prose_findings_splits_sentences_and_ignores_code_semicolons() -> None:
    text = f"{_sentence(20)} {_sentence(20)} Run `a; b` now."
    assert prose_findings(text, kind="instruction", path="p", line=1) == []
    assert _kinds(prose_findings("Run a; run b.", kind="description", path="p", line=1)) == [
        "semicolon"
    ]


def test_markdown_findings_classifies_items_and_skips_code_tables_headings() -> None:
    text = "\n".join(
        [
            f"# {_sentence(30)}",
            f"- {_sentence(21)}",
            "",
            " ".join(["Run"] * 13),
            _sentence(13),
            "```",
            "a; b",
            "```",
            f"| {_sentence(30)} |",
        ]
    )
    findings = markdown_findings(text, path="skill:x", line=10)
    assert [(f.kind, f.line) for f in findings] == [
        ("instruction-length", 11),
        ("description-length", 13),
    ]


def test_yaml_findings_reads_prose_fields_and_body_templates() -> None:
    text = "\n".join(
        [
            "rules:",
            f"  - title: {_sentence(30)}",
            f"    instruction: {_sentence(21)}",
            "    body_template: |",
            "      Run a; run b.",
        ]
    )
    findings = yaml_findings(text, path="m.yaml")
    assert [(f.kind, f.line) for f in findings] == [("instruction-length", 3), ("semicolon", 5)]
    assert yaml_findings("", path="m.yaml") == []


def test_yaml_findings_refuses_malformed_yaml() -> None:
    with pytest.raises(yaml.YAMLError):
        yaml_findings("a: [b\n", path="m.yaml")


def test_file_findings_reads_markdown_and_refuses_missing(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text("Run a; run b.\n", encoding="utf-8")
    assert _kinds(file_findings(page)) == ["semicolon"]
    with pytest.raises(FileNotFoundError):
        file_findings(tmp_path / "absent.md")


def test_prose_findings_refuses_non_text() -> None:
    with pytest.raises(AttributeError):
        prose_findings(None, kind="instruction", path="p", line=1)  # type: ignore[arg-type]


def test_main_lints_default_sources_and_returns_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(Path(__file__).resolve().parents[4])
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "skill:" in out
    assert "src/eawf/platform/rules/data/modules/" in out
