"""The detect-secrets hook runs pattern detectors only, over an empty baseline.

Both entropy detectors are off: their hits in this repository were git
object ids and sha256 digests, never a credential, and the baseline that
acknowledged them churned with every state commit. These tests pin the hook
arguments, the empty baseline, that every remaining detector still fires on
a real-format token, and that hex ids no longer do. Every token is built by
concatenation at test time so no committed line reads as a credential.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import string
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from detect_secrets.core.plugins.util import get_mapping_from_secret_type_to_class
from detect_secrets.main import main as detect_secrets_main
from detect_secrets.settings import transient_settings

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOK_REPO = "https://github.com/Yelp/detect-secrets"
_ENTROPY_PLUGINS = ("HexHighEntropyString", "Base64HighEntropyString")
_EXPECTED_ARGS = [
    "--baseline",
    ".secrets.baseline",
    "--disable-plugin",
    "HexHighEntropyString",
    "--disable-plugin",
    "Base64HighEntropyString",
]
#: The 0.7.1 hook passed only the baseline, so every built-in plugin ran.
_V071_SCAN_ARGS: list[str] = []
#: The stock filter that drops a hit without a letter in it, which every
#: dotted-quad address is, so the public-IP detector only reports with it off.
_LETTERLESS_FILTER = "detect_secrets.filters.heuristic.is_not_alphanumeric_string"
_LETTERLESS_DETECTORS = frozenset({"IPPublicDetector"})
_HEX = "0123456789abcdef"
_LOWER_ALNUM = string.ascii_lowercase + string.digits
_ALNUM = string.ascii_letters + string.digits
_UPPER_ALNUM = string.ascii_uppercase + string.digits


def _detect_secrets_hook() -> dict[str, Any]:
    """Return the repository's detect-secrets repo entry from the pre-commit config."""
    config = yaml.safe_load((_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    (entry,) = [repo for repo in config["repos"] if repo["repo"] == _HOOK_REPO]
    return entry


def _hook_comment_lines() -> list[str]:
    """Return the comment lines inside the detect-secrets block of the config."""
    lines = (_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8").splitlines()
    start = lines.index(f"  - repo: {_HOOK_REPO}")
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("  - repo:"):
            break
        if line.strip().startswith("#"):
            block.append(line.strip())
    return block


def _scan_args() -> list[str]:
    """Return the hook arguments minus ``--baseline <path>``, as ``scan`` takes them."""
    args = list(_detect_secrets_hook()["hooks"][0]["args"])
    at = args.index("--baseline")
    del args[at : at + 2]
    return args


def _scan(root: Path, name: str, args: list[str]) -> dict[str, Any]:
    """Run ``detect-secrets scan`` in-process over ``root/name`` and return its JSON.

    The scan resolves paths against the working directory and keeps its plugin
    set in a module global, so each call starts from fresh settings and the
    caller has already changed into ``root``. ``--no-verify`` keeps the scan
    offline: by default several detectors send each hit to its provider's API
    and drop it when the provider rejects it, which a fixture token always is.
    """
    assert Path.cwd() == root
    out = io.StringIO()
    with transient_settings({"plugins_used": []}), contextlib.redirect_stdout(out):
        detect_secrets_main(["scan", "--no-verify", *args, name])
    result: dict[str, Any] = json.loads(out.getvalue())
    return result


def _chars(seed: str, length: int, alphabet: str) -> str:
    """Return *length* deterministic, non-sequential characters drawn from *alphabet*."""
    stream = b""
    counter = 0
    while len(stream) < length:
        stream += hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        counter += 1
    return "".join(alphabet[byte % len(alphabet)] for byte in stream[:length])


def _b64url(payload: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def _tokens() -> dict[str, str]:
    """Return one real-format line per remaining detector, keyed by plugin name."""
    jwt = ".".join(
        [
            _b64url({"alg": "HS256", "typ": "JWT"}),
            _b64url({"sub": "fixture", "iat": 1700000000}),
            _chars("jwt", 43, _ALNUM),
        ]
    )
    return {
        "ArtifactoryDetector": "artifactory: " + "AK" + "C" + _chars("art", 24, _ALNUM),
        "AWSKeyDetector": "aws: " + "AK" + "IA" + _chars("aws", 16, _UPPER_ALNUM),
        "AzureStorageKeyDetector": "Account" + "Key=" + _chars("az", 88, _ALNUM),
        "BasicAuthDetector": "https://fixture:" + _chars("ba", 16, _ALNUM) + "@example.com/",
        "CloudantDetector": "https://acct:" + _chars("cl", 64, _HEX) + "@acct.cloudant.com",
        "DiscordBotTokenDetector": "discord: "
        + "M"
        + _chars("dc1", 23, _ALNUM)
        + "."
        + _chars("dc2", 6, _ALNUM)
        + "."
        + _chars("dc3", 27, _ALNUM),
        "GitHubTokenDetector": "github: " + "gh" + "p_" + _chars("gh", 36, _ALNUM),
        "GitLabTokenDetector": "gitlab: " + "gl" + "pat-" + _chars("gl", 20, _ALNUM),
        "IbmCloudIamDetector": 'ibm_cloud_iam_api_key = "' + _chars("iam", 44, _ALNUM) + '"',
        "IbmCosHmacDetector": 'ibm_cos_hmac_secret_access_key = "' + _chars("cos", 48, _HEX) + '"',
        "IPPublicDetector": "host: " + ".".join(["203", "0", "113", "47"]),
        "JwtTokenDetector": "jwt: " + jwt,
        "KeywordDetector": 'password = "' + _chars("kw", 20, _ALNUM) + '"',
        "MailchimpDetector": "mailchimp: " + _chars("mc", 32, _LOWER_ALNUM) + "-us" + "12",
        "NpmDetector": "//registry.npmjs.org/:_auth"
        + "Token="
        + "np"
        + "m_"
        + _chars("npm", 36, _ALNUM),
        "OpenAIDetector": "openai: "
        + "sk-"
        + _chars("oa1", 20, _ALNUM)
        + "T3Blbk"
        + "FJ"
        + _chars("oa2", 20, _ALNUM),
        "PrivateKeyDetector": "-----BEGIN RSA " + "PRIVATE KEY-----",
        "PypiTokenDetector": "pypi: " + "py" + "pi-AgEIcHlwaS5vcmc" + _chars("pypi", 72, _ALNUM),
        "SendGridDetector": "sendgrid: "
        + "S"
        + "G."
        + _chars("sg1", 22, _ALNUM)
        + "."
        + _chars("sg2", 43, _ALNUM),
        "SlackDetector": "slack: "
        + "xo"
        + "xb-"
        + "1234567890"
        + "-"
        + _chars("sl", 24, _LOWER_ALNUM),
        "SoftlayerDetector": 'softlayer_api_key = "' + _chars("slr", 64, _LOWER_ALNUM) + '"',
        "SquareOAuthDetector": "square: " + "sq0" + "csp-" + _chars("sq", 43, _ALNUM),
        "StripeDetector": "stripe: " + "sk" + "_live_" + _chars("st", 24, _ALNUM),
        "TelegramBotTokenDetector": "telegram: " + "583920174" + ":" + _chars("tg", 35, _ALNUM),
        "TwilioKeyDetector": "twilio: " + "A" + "C" + _chars("tw", 32, _LOWER_ALNUM),
    }


def _hex_document() -> str:
    """Return ten 40-hex and ten 64-hex values under the keys state rows use."""
    rows: list[dict[str, str]] = []
    for n in range(10):
        tree = hashlib.sha1(f"tree-{n}".encode()).hexdigest()
        digest = hashlib.sha256(f"digest-{n}".encode()).hexdigest()
        rows.append({"head_sha": tree, "digest": f"sha256:{digest}"})
        rows.append({"value": tree if n % 2 else digest})
    return json.dumps(rows, indent=2) + "\n"


def _found_types(result: dict[str, Any]) -> set[str]:
    return {row["type"] for rows in result["results"].values() for row in rows}


def test_hook_args_and_no_exclude() -> None:
    entry = _detect_secrets_hook()
    (hook,) = entry["hooks"]

    assert entry["rev"] == "v1.5.0"
    assert hook["id"] == "detect-secrets"
    assert hook["args"] == _EXPECTED_ARGS
    assert "exclude" not in hook
    text = (_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert [line for line in text.splitlines() if "Baseline-hash" in line] == []
    assert [line for line in text.splitlines() if "epoch1-snapshot" in line] == []


def test_baseline_empty_and_25_plugins() -> None:
    baseline = json.loads((_REPO_ROOT / ".secrets.baseline").read_text(encoding="utf-8"))
    names = [plugin["name"] for plugin in baseline["plugins_used"]]

    assert baseline["results"] == {}
    assert len(names) == 25
    assert len(set(names)) == 25
    assert not set(names) & set(_ENTROPY_PLUGINS)
    comments = "\n".join(_hook_comment_lines())
    for path, rows in baseline["results"].items():
        for row in rows:
            assert f"{path}:{row['line_number']}" in comments


def test_detectors_and_hex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    tokens = _tokens()
    (tmp_path / "tokens.txt").write_text("\n".join(tokens.values()) + "\n", encoding="utf-8")
    (tmp_path / "hex.json").write_text(_hex_document(), encoding="utf-8")
    args = _scan_args()
    by_type = get_mapping_from_secret_type_to_class()

    scanned = _scan(tmp_path, "tokens.txt", args)
    plugins = {plugin["name"] for plugin in scanned["plugins_used"]}
    found = {by_type[secret_type].__name__ for secret_type in _found_types(scanned)}
    unfiltered = _scan(tmp_path, "tokens.txt", [*args, "--disable-filter", _LETTERLESS_FILTER])
    found_unfiltered = {by_type[kind].__name__ for kind in _found_types(unfiltered)}

    assert plugins == set(tokens)
    assert len(plugins) == 25
    assert found == plugins - _LETTERLESS_DETECTORS
    assert found_unfiltered == plugins
    assert _scan(tmp_path, "hex.json", args)["results"] == {}
    assert _found_types(_scan(tmp_path, "hex.json", _V071_SCAN_ARGS)) == {"Hex High Entropy String"}


def test_no_baseline_special_cases() -> None:
    for tool in ("tools/ci_changes.py", "tools/commit_prefix_lint.py"):
        assert "secrets.baseline" not in (_REPO_ROOT / tool).read_text(encoding="utf-8"), tool
    workflow = (_REPO_ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8")
    assert "secrets.baseline" not in workflow
    pyproject_text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    pyproject = tomllib.loads(pyproject_text)
    assert "detect_secrets" not in pyproject["tool"]["deptry"]["per_rule_ignores"]["DEP004"]
    assert "detect_secrets" not in pyproject_text
