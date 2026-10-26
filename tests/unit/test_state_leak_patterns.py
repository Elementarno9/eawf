"""Unit tests for the shared state leak pattern set.

Every leak fixture below is assembled at runtime from split pieces, so this
file carries no literal the leak lints or detect-secrets would flag.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from eawf.observability.logging.state_leak import (
    HOME_PATH_PATTERNS,
    TOKEN_PATTERNS,
    StateLeakHit,
    StateLeakKind,
    default_allowed_emails,
    is_placeholder_or_nonemail,
    is_placeholder_path,
    scan_state_leaks,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

MACOS_HOME = "/" + "Users" + "/" + "alice"
WINDOWS_HOME = "C:" + "\\" + "Users" + "\\" + "alice"
LINUX_HOME = "/" + "home" + "/" + "alice"
SERVER_ROOT = "/" + "srv" + "/" + "work"
RUNTIME_DIR = "/" + "run" + "/" + "user" + "/" + "1001"
PYTEST_ROOT = "pytest" + "-of-" + "alice"
LEAKED_EMAIL = "alice" + "@" + "acmecorp.io"

# A key body long enough for every token shape; repeated characters keep the
# entropy far below what a secret scanner treats as a credential.
_BODY = "A1" * 20

TOKENS = {
    "sk-ant": "sk-" + "ant-" + _BODY,
    "sk": "sk-" + "proj" + _BODY,
    "ghp_": "gh" + "p_" + _BODY,
    "github_pat_": "github" + "_pat_" + _BODY,
    "AKIA": "AK" + "IA" + "Z" * 16,
}


def _scan(text: str) -> list[StateLeakHit]:
    return scan_state_leaks(text, allowed_emails=default_allowed_emails())


@pytest.mark.parametrize(
    "home", [MACOS_HOME, WINDOWS_HOME, LINUX_HOME, SERVER_ROOT, RUNTIME_DIR, PYTEST_ROOT]
)
def test_scan_state_leaks_matches_each_home_anchor(home: str) -> None:
    hits = _scan(f"outcome: worked in {home} today")

    assert hits == [StateLeakHit(kind=StateLeakKind.HOME_PATH, snippet=home)]


@pytest.mark.parametrize(
    "placeholder",
    ["/Users/<name>", "/home/<user>/x", "C:\\Users\\...", "/Users/.../repo", "/srv/<site>/x"],
)
def test_scan_state_leaks_skips_home_placeholder(placeholder: str) -> None:
    assert _scan(f"never commit {placeholder}") == []


def test_scan_state_leaks_matches_non_allowlisted_email() -> None:
    hits = _scan(f"contact {LEAKED_EMAIL} for access")

    assert hits == [StateLeakHit(kind=StateLeakKind.EMAIL, snippet=LEAKED_EMAIL)]


def test_scan_state_leaks_skips_allowlisted_email() -> None:
    assert _scan("Co-Authored-By: Claude <noreply@anthropic.com>") == []


def test_scan_state_leaks_casefolds_match_against_allowlist() -> None:
    allowed = frozenset({LEAKED_EMAIL})

    assert scan_state_leaks(f"mail {LEAKED_EMAIL.upper()}", allowed_emails=allowed) == []
    assert scan_state_leaks(f"mail {LEAKED_EMAIL}", allowed_emails=frozenset()) == [
        StateLeakHit(kind=StateLeakKind.EMAIL, snippet=LEAKED_EMAIL)
    ]


@pytest.mark.parametrize("addr", ["test@example.com", "ops@build.test", "setup-uv@v8.1.0"])
def test_scan_state_leaks_skips_placeholder_or_nonemail(addr: str) -> None:
    assert _scan(f"uses {addr}") == []


@pytest.mark.parametrize("shape", sorted(TOKENS))
def test_scan_state_leaks_matches_each_token_shape_once(shape: str) -> None:
    token = TOKENS[shape]

    hits = _scan(f'"note": "key {token} pasted"')

    assert hits == [StateLeakHit(kind=StateLeakKind.TOKEN, snippet=token)]


@pytest.mark.parametrize(
    "word",
    [
        "task-" + "abcdefghijklmnopqrstuvwxyz",
        "risk-" + "assessment-for-the-quarterly-review",
        "desk-" + "0123456789abcdefghij",
    ],
)
def test_scan_state_leaks_ignores_sk_run_inside_word(word: str) -> None:
    assert _scan(f"see {word}") == []


def test_scan_state_leaks_sk_body_length_boundary() -> None:
    short = "sk-" + "a" * 19
    exact = "sk-" + "a" * 20

    assert _scan(short) == []
    assert _scan(exact) == [StateLeakHit(kind=StateLeakKind.TOKEN, snippet=exact)]


def test_scan_state_leaks_ignores_akia_inside_longer_run() -> None:
    assert _scan("XAK" + "IA" + "Z" * 16) == []
    assert _scan("AK" + "IA" + "Z" * 17) == []


def test_scan_state_leaks_empty_text_returns_nothing() -> None:
    assert _scan("") == []


def test_scan_state_leaks_reports_families_in_order() -> None:
    text = f"{TOKENS['ghp_']} {LEAKED_EMAIL} {MACOS_HOME}"

    kinds = [hit.kind for hit in _scan(text)]

    assert kinds == [StateLeakKind.HOME_PATH, StateLeakKind.EMAIL, StateLeakKind.TOKEN]


def test_scan_state_leaks_rejects_bytes() -> None:
    with pytest.raises(TypeError):
        scan_state_leaks(b"payload", allowed_emails=frozenset())  # type: ignore[arg-type]


def test_scan_state_leaks_ignores_tilde_home() -> None:
    assert _scan("config lives in ~/.eawf/registry.json") == []


@pytest.mark.parametrize("lead", ["513-", "513:", "=", "'", "(", " "])
def test_scan_state_leaks_matches_a_server_root_after_a_separator(lead: str) -> None:
    hits = _scan(f"{lead}{SERVER_ROOT}/eawf/tests")

    assert hits == [StateLeakHit(kind=StateLeakKind.HOME_PATH, snippet=SERVER_ROOT)]


def test_scan_state_leaks_matches_a_pytest_root_under_any_temp_dir() -> None:
    path = f"/var/tmp/{PYTEST_ROOT}/pytest-70/test_x0"

    assert _scan(f"layer {path}") == [
        StateLeakHit(kind=StateLeakKind.HOME_PATH, snippet=PYTEST_ROOT)
    ]


@pytest.mark.parametrize(
    "text",
    ["see /run/user/<uid>/eawfd", "the /run/users/ dir", "mypytest-of-x", "pytest-of-<user>"],
)
def test_scan_state_leaks_skips_runtime_and_pytest_lookalikes(text: str) -> None:
    assert _scan(text) == []


def test_scan_state_leaks_ignores_a_root_word_that_is_not_the_server_root() -> None:
    assert _scan("served /srvx/a and /usr/srv/b; see the srv/ docs") == []


_ESCAPED_BREAK = "\\" + "n"
_SERVER_SHORT = "/" + "srv" + "/" + "x"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"{_ESCAPED_BREAK}{SERVER_ROOT}/repo", [SERVER_ROOT]),
        (f"foo_{_SERVER_SHORT}", [_SERVER_SHORT]),
        (f"{_ESCAPED_BREAK}{RUNTIME_DIR}", [RUNTIME_DIR]),
        (f"{_ESCAPED_BREAK}{PYTEST_ROOT}", [PYTEST_ROOT]),
        (f"{'\\' + 't'}{_SERVER_SHORT}", [_SERVER_SHORT]),
        (f"{'\\' + 'r'}{_SERVER_SHORT}", [_SERVER_SHORT]),
        ("/usr/srv/x", []),
        ("src/srv/x", []),
        ("https://host/srv/x", []),
        ("./srv/x", []),
        ("docs/rules/srv-layout.md", []),
        (f"{_ESCAPED_BREAK}/srv/<project>/", []),
        (f"{_ESCAPED_BREAK}pytest-of-<user>", []),
    ],
)
def test_a_path_after_an_escaped_break_or_underscore_is_flagged(
    text: str, expected: list[str]
) -> None:
    assert _scan(text) == [
        StateLeakHit(kind=StateLeakKind.HOME_PATH, snippet=snippet) for snippet in expected
    ]


@pytest.mark.parametrize(
    "value", [f"a\n{SERVER_ROOT}/repo", f"a{_ESCAPED_BREAK}{SERVER_ROOT}/repo"]
)
def test_path_leak_lint_flags_a_path_after_a_json_escaped_break(tmp_path: Path, value: str) -> None:
    from eawf.surfaces.cli.commands.hook import _scan_path_leaks

    (tmp_path / "rows.jsonl").write_text(json.dumps({"note": value}) + "\n", encoding="utf-8")

    findings = _scan_path_leaks(["rows.jsonl"], cwd=tmp_path, diff_scoped=False)

    assert [(finding.lineno, finding.snippet) for finding in findings] == [(1, SERVER_ROOT)]


def test_home_path_patterns_are_the_scrubbers_six_anchors() -> None:
    from eawf.observability.logging.scrub import SensitiveScrubber

    assert len(HOME_PATH_PATTERNS) == 6
    assert all(pattern in SensitiveScrubber.PATTERNS for pattern in HOME_PATH_PATTERNS)


def test_token_patterns_cover_every_shape() -> None:
    for token in TOKENS.values():
        assert sum(1 for pattern in TOKEN_PATTERNS if pattern.search(token)) == 1


def test_is_placeholder_path_discriminates_placeholder_from_leak() -> None:
    assert is_placeholder_path("/Users/<name>")
    assert is_placeholder_path("C:\\Users\\...")
    assert not is_placeholder_path(MACOS_HOME)
    assert not is_placeholder_path("")


def test_is_placeholder_or_nonemail_boundaries() -> None:
    assert is_placeholder_or_nonemail("nobody@")
    assert is_placeholder_or_nonemail("pin@v1.x1")
    assert is_placeholder_or_nonemail("a@b.c")
    assert not is_placeholder_or_nonemail("a" + "@" + "b.io")
    assert not is_placeholder_or_nonemail(LEAKED_EMAIL)


def test_default_allowed_emails_includes_noreply_casefolded() -> None:
    allowed = default_allowed_emails()

    assert "noreply@anthropic.com" in allowed
    assert all(email == email.casefold() for email in allowed)


def test_pre_commit_config_excludes_only_the_snapshot_document_from_detect_secrets() -> None:
    config = yaml.safe_load((_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hooks = [hook for repo in config["repos"] for hook in repo["hooks"]]

    detect_secrets = next(hook for hook in hooks if hook["id"] == "detect-secrets")

    snapshot = "tests/fixtures/epoch1-snapshot"
    assert re.search(detect_secrets["exclude"], f"{snapshot}/state.json")
    assert not re.search(detect_secrets["exclude"], f"{snapshot}/store/evidence.jsonl")
    assert not re.search(detect_secrets["exclude"], ".ea/store/audit.jsonl")
