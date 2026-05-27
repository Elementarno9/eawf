"""Deterministic rule-adherence checks for recorded wave evals.

The module scores recorded wave transcripts as advisory observations. A
failed rule lowers the rule-adherence report for that case, but callers
receive a typed report instead of an exception so experiments can compare
prompt variants before any rule becomes a blocking gate.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RuleId = Literal[
    "commands_use_rtk",
    "python_invocations_use_uv_run",
    "no_local_path_leaks",
    "no_direct_state_json_edits",
    "pydantic_models_forbid_extra",
    "no_destructive_git_reverts",
]

RULE_IDS: tuple[RuleId, ...] = (
    "commands_use_rtk",
    "python_invocations_use_uv_run",
    "no_local_path_leaks",
    "no_direct_state_json_edits",
    "pydantic_models_forbid_extra",
    "no_destructive_git_reverts",
)

_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_POSIX_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w:])/(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+")
_HOME_RELATIVE_PATH_RE = re.compile(r"(?<![\w])~/(?:[^\s`'\"\])]+)")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"\b[A-Za-z]:\\(?:[^\\/:*?\"<>|\s]+\\)+[^\\/:*?\"<>|\s]+")
_PYTHON_TOOL_NAMES = frozenset({"mypy", "pre-commit", "pytest", "python", "python3", "ruff"})
_PYTHON_VERSION_RE = re.compile(r"^python\d+(?:\.\d+)?$")
_MODEL_CLASS_RE = re.compile(r"^class\s+\w+\([^)]*BaseModel[^)]*\):", re.MULTILINE)
_TOP_LEVEL_BLOCK_RE = re.compile(r"^(?:class|def)\s+\w+", re.MULTILINE)
_STATE_PATH = ".ea/state.json"
_STATE_WRITE_TOOLS = frozenset({"cp", "mv", "perl", "rm", "sed", "tee", "truncate"})


class RecordedWaveCase(BaseModel):
    """Recorded wave transcript plus expected advisory rule failures."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    transcript: str = ""
    commands: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    verify_count: int = Field(ge=0)
    expected_failed_rules: tuple[RuleId, ...] = ()


class RuleAdherenceFinding(BaseModel):
    """One deterministic checker result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: RuleId
    passed: bool
    message: str
    severity: Literal["advisory"] = "advisory"
    evidence: tuple[str, ...] = ()


class RuleAdherenceReport(BaseModel):
    """Advisory rule-adherence report for one recorded wave case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    advisory_only: bool = True
    checker_count: int
    verify_count: int
    passed: bool
    score: float
    failed_rules: tuple[RuleId, ...]
    findings: tuple[RuleAdherenceFinding, ...]


class CorpusCaseScore(BaseModel):
    """Stable Exp-1 baseline row for one recorded wave case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    verify_count: int
    passed: bool
    score: float
    failed_rules: tuple[RuleId, ...]


class RuleAdherenceBaseline(BaseModel):
    """Exp-1 aggregate over a recorded wave corpus."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: str = "Exp-1"
    advisory_only: bool = True
    checker_count: int
    rule_ids: tuple[RuleId, ...]
    corpus_count: int
    verify_count: int
    pass_rate: float
    rule_pass_rates: dict[RuleId, float]
    cases: tuple[CorpusCaseScore, ...]


@dataclass(frozen=True)
class _RuleChecker:
    rule_id: RuleId
    check: Callable[[RecordedWaveCase], RuleAdherenceFinding]


def _pass(rule_id: RuleId, message: str) -> RuleAdherenceFinding:
    return RuleAdherenceFinding(rule_id=rule_id, passed=True, message=message)


def _fail(rule_id: RuleId, message: str, evidence: Iterable[str]) -> RuleAdherenceFinding:
    return RuleAdherenceFinding(
        rule_id=rule_id,
        passed=False,
        message=message,
        evidence=tuple(_safe_excerpt(item) for item in evidence),
    )


def _case_from_obj(case: RecordedWaveCase | Mapping[str, object]) -> RecordedWaveCase:
    if isinstance(case, RecordedWaveCase):
        return case
    return RecordedWaveCase.model_validate(case)


def _command_tokens(command: str) -> tuple[str, ...]:
    try:
        return tuple(shlex.split(command))
    except ValueError:
        return tuple(command.split())


def _first_executable(tokens: tuple[str, ...]) -> str | None:
    for token in tokens:
        if not _ENV_ASSIGN_RE.match(token):
            return token
    return None


def _strip_runners(tokens: tuple[str, ...]) -> tuple[str, ...]:
    index = 0
    while index < len(tokens) and _ENV_ASSIGN_RE.match(tokens[index]):
        index += 1
    if index < len(tokens) and tokens[index] == "rtk":
        index += 1
    while index < len(tokens) and _ENV_ASSIGN_RE.match(tokens[index]):
        index += 1
    return tokens[index:]


def _all_text(case: RecordedWaveCase) -> tuple[str, ...]:
    return (case.transcript, *case.commands, *case.changed_files)


def _contains_local_path(value: str) -> bool:
    return bool(
        _POSIX_ABSOLUTE_PATH_RE.search(value)
        or _HOME_RELATIVE_PATH_RE.search(value)
        or _WINDOWS_ABSOLUTE_PATH_RE.search(value)
    )


def _safe_excerpt(value: str) -> str:
    if _contains_local_path(value):
        return "<redacted-local-path>"
    return value[:160]


def _check_commands_use_rtk(case: RecordedWaveCase) -> RuleAdherenceFinding:
    offenders: list[str] = []
    for command in case.commands:
        tokens = _command_tokens(command)
        executable = _first_executable(tokens)
        if executable is not None and executable != "rtk":
            offenders.append(command)
    if offenders:
        return _fail("commands_use_rtk", "shell commands should start with rtk", offenders)
    return _pass("commands_use_rtk", "all shell commands use rtk")


def _command_uses_uv_run(tokens: tuple[str, ...]) -> bool:
    body = _strip_runners(tokens)
    return len(body) >= 2 and body[0] == "uv" and body[1] == "run"


def _executable_needs_uv_run(tokens: tuple[str, ...]) -> bool:
    body = _strip_runners(tokens)
    if not body:
        return False
    executable = body[0].rsplit("/", 1)[-1]
    return executable in _PYTHON_TOOL_NAMES or bool(_PYTHON_VERSION_RE.match(executable))


def _check_python_invocations_use_uv_run(case: RecordedWaveCase) -> RuleAdherenceFinding:
    offenders: list[str] = []
    for command in case.commands:
        tokens = _command_tokens(command)
        if _executable_needs_uv_run(tokens) and not _command_uses_uv_run(tokens):
            offenders.append(command)
    if offenders:
        return _fail(
            "python_invocations_use_uv_run",
            "python tooling should be invoked through uv run",
            offenders,
        )
    return _pass("python_invocations_use_uv_run", "python tooling uses uv run")


def _check_no_local_path_leaks(case: RecordedWaveCase) -> RuleAdherenceFinding:
    offenders = [value for value in _all_text(case) if _contains_local_path(value)]
    if offenders:
        return _fail("no_local_path_leaks", "local paths should be redacted", offenders)
    return _pass("no_local_path_leaks", "no local path leaks found")


def _command_writes_state_json(command: str) -> bool:
    if _STATE_PATH not in command:
        return False
    body = _strip_runners(_command_tokens(command))
    if not body:
        return False
    if body[0] in _STATE_WRITE_TOOLS:
        return True
    return ">" in command


def _check_no_direct_state_json_edits(case: RecordedWaveCase) -> RuleAdherenceFinding:
    offenders = [path for path in case.changed_files if path == _STATE_PATH]
    offenders.extend(command for command in case.commands if _command_writes_state_json(command))
    if offenders:
        return _fail(
            "no_direct_state_json_edits",
            "state mutations should go through the canonical lifecycle writer",
            offenders,
        )
    return _pass("no_direct_state_json_edits", "no direct state.json edits found")


def _model_blocks_without_extra_forbid(text: str) -> tuple[str, ...]:
    offenders: list[str] = []
    for match in _MODEL_CLASS_RE.finditer(text):
        tail = text[match.end() :]
        next_block = _TOP_LEVEL_BLOCK_RE.search(tail)
        body = tail[: next_block.start()] if next_block else tail
        block = f"{match.group(0)}{body}"
        if 'extra="forbid"' not in block and "extra='forbid'" not in block:
            offenders.append(match.group(0))
    return tuple(offenders)


def _check_pydantic_models_forbid_extra(case: RecordedWaveCase) -> RuleAdherenceFinding:
    offenders = _model_blocks_without_extra_forbid(case.transcript)
    if offenders:
        return _fail(
            "pydantic_models_forbid_extra",
            "pydantic models should set extra='forbid'",
            offenders,
        )
    return _pass("pydantic_models_forbid_extra", "pydantic model snippets forbid extras")


def _is_destructive_git_command(command: str) -> bool:
    body = _strip_runners(_command_tokens(command))
    if len(body) < 2 or body[0] != "git":
        return False
    subcommand = body[1]
    if subcommand == "reset" and "--hard" in body[2:]:
        return True
    if subcommand == "checkout" and "--" in body[2:]:
        return True
    if subcommand == "restore" and "--staged" not in body[2:]:
        return True
    return subcommand == "clean" and any(
        token.startswith("-") and "f" in token for token in body[2:]
    )


def _check_no_destructive_git_reverts(case: RecordedWaveCase) -> RuleAdherenceFinding:
    offenders = [command for command in case.commands if _is_destructive_git_command(command)]
    if offenders:
        return _fail(
            "no_destructive_git_reverts",
            "do not run destructive git revert commands",
            offenders,
        )
    return _pass("no_destructive_git_reverts", "no destructive git revert commands found")


_CHECKERS: tuple[_RuleChecker, ...] = (
    _RuleChecker("commands_use_rtk", _check_commands_use_rtk),
    _RuleChecker("python_invocations_use_uv_run", _check_python_invocations_use_uv_run),
    _RuleChecker("no_local_path_leaks", _check_no_local_path_leaks),
    _RuleChecker("no_direct_state_json_edits", _check_no_direct_state_json_edits),
    _RuleChecker("pydantic_models_forbid_extra", _check_pydantic_models_forbid_extra),
    _RuleChecker("no_destructive_git_reverts", _check_no_destructive_git_reverts),
)


def check_rule_adherence(case: RecordedWaveCase | Mapping[str, object]) -> RuleAdherenceReport:
    """Run all advisory rule-adherence checkers against one recorded case."""
    normalised = _case_from_obj(case)
    findings = tuple(checker.check(normalised) for checker in _CHECKERS)
    failed_rules = tuple(finding.rule_id for finding in findings if not finding.passed)
    passed_count = len(findings) - len(failed_rules)
    score = passed_count / len(findings) if findings else 1.0
    return RuleAdherenceReport(
        case_id=normalised.case_id,
        checker_count=len(findings),
        verify_count=normalised.verify_count,
        passed=not failed_rules,
        score=score,
        failed_rules=failed_rules,
        findings=findings,
    )


def load_recorded_wave_cases(input_dir: Path) -> tuple[RecordedWaveCase, ...]:
    """Load recorded wave cases from *input_dir*, excluding ``baseline.json``."""
    cases: list[RecordedWaveCase] = []
    for path in sorted(input_dir.glob("*.json")):
        if path.name == "baseline.json":
            continue
        cases.append(RecordedWaveCase.model_validate_json(path.read_text(encoding="utf-8")))
    return tuple(cases)


def build_rule_adherence_baseline(
    cases: Iterable[RecordedWaveCase | Mapping[str, object]],
    *,
    experiment_id: str = "Exp-1",
) -> RuleAdherenceBaseline:
    """Build the stable Exp-1 aggregate for a recorded wave corpus."""
    normalised = tuple(_case_from_obj(case) for case in cases)
    reports = tuple(check_rule_adherence(case) for case in normalised)
    total_checks = len(reports) * len(RULE_IDS)
    passed_checks = sum(len(report.findings) - len(report.failed_rules) for report in reports)
    rule_pass_rates = {rule_id: _rule_pass_rate(rule_id, reports) for rule_id in RULE_IDS}
    case_scores = tuple(
        CorpusCaseScore(
            case_id=report.case_id,
            verify_count=report.verify_count,
            passed=report.passed,
            score=report.score,
            failed_rules=report.failed_rules,
        )
        for report in reports
    )
    return RuleAdherenceBaseline(
        experiment_id=experiment_id,
        checker_count=len(RULE_IDS),
        rule_ids=RULE_IDS,
        corpus_count=len(normalised),
        verify_count=sum(case.verify_count for case in normalised),
        pass_rate=passed_checks / total_checks if total_checks else 1.0,
        rule_pass_rates=rule_pass_rates,
        cases=case_scores,
    )


def _rule_pass_rate(rule_id: RuleId, reports: tuple[RuleAdherenceReport, ...]) -> float:
    if not reports:
        return 1.0
    passed = sum(1 for report in reports if rule_id not in report.failed_rules)
    return passed / len(reports)


def dump_rule_adherence_baseline(baseline: RuleAdherenceBaseline) -> str:
    """Return deterministic JSON for a rule-adherence baseline."""
    return json.dumps(baseline.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


__all__ = [
    "RULE_IDS",
    "CorpusCaseScore",
    "RecordedWaveCase",
    "RuleAdherenceBaseline",
    "RuleAdherenceFinding",
    "RuleAdherenceReport",
    "build_rule_adherence_baseline",
    "check_rule_adherence",
    "dump_rule_adherence_baseline",
    "load_recorded_wave_cases",
]
