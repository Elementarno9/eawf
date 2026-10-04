"""The daemon certifies an installed runtime version through the conformance probe.

``runtime.certification.certify`` and the background probe a Run start queues both
walk the conformance runner's probe stage against the installed binary and record
what it concluded on this machine: a passed probe certifies the version and its
controls are admitted, a failed one quarantines it and a control is refused naming
what the probe did not find. While a background probe runs, a control on that
version is refused as in progress.

Every ``claude`` and ``codex`` here is a script on a private ``PATH`` that prints a
recorded ``--version`` and ``--help``, so no real binary and no model is reached.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import stat
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from eawf.kernel.state.epoch2.run import RunRuntimeTuple
from eawf.kernel.store.kinds.runtime_certification import MachineCertification
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods, runtime_certifier
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.conformance import runner_for
from eawf.runtime.daemon.methods.runtime_certification import RUNTIME_CERTIFY_METHOD
from eawf.runtime.daemon.runtime_certifier import (
    REPROBE_BACKOFF,
    RuntimeNotProbedError,
    certifier_for,
    certify_installed,
    installed_version,
)
from eawf.runtime.runtimes.probes import sdk_baseline
from eawf.workflow.evidence.machine_certification import (
    CERTIFICATION_LIFETIME,
    machine_certification_path,
    read_machine_certifications,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import method_context
from tests.integration.runtime.daemon.test_root_run_host_session import (
    _head as _host_head,
)
from tests.integration.runtime.daemon.test_root_run_host_session import (
    _hosted,
    _queued,
    _start,
    _stored_run,
    _transcript,
)
from tests.integration.runtime.daemon.test_root_run_host_session import (
    cli as cli,
)
from tests.integration.runtime.daemon.test_run_runtime_tuple import _ask, _controlled

pytestmark = pytest.mark.integration

#: Resolved before any test narrows ``PATH``, so a stub can still sleep.
SLEEP: Final = shutil.which("sleep") or "/bin/sleep"

CLAUDE_VERSION: Final = "2.1.288"
CODEX_VERSION: Final = "0.159.2"
INSTALLED: Final = {"harness": "claude-code", "harness_version": CLAUDE_VERSION}

#: A Claude Code ``--help`` advertising the flags the matrix rules on: tools and
#: streaming, plus the resume flags the matrix declares unsupported.
CLAUDE_HELP: Final = "Options: --allowedTools --output-format --resume --continue --session-id"

#: A Codex ``--help`` that advertises streaming but no MCP surface, so the
#: ``tool_use`` cell the matrix declares supported is not observed.
CODEX_HELP_WITHOUT_MCP: Final = "Commands: exec  Run Codex non-interactively"


def _script(
    directory: Path, name: str, version_line: str, help_text: str, *, wait: str = ""
) -> None:
    """Write an executable stand-in for one harness binary into *directory*."""
    body = (
        "#!/bin/sh\n"
        'case "$1" in\n'
        f"  --version) {wait}echo '{version_line}' ;;\n"
        f"  --help) echo '{help_text}' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _bin(tmp_path: Path, *, wait_for: Path | None = None) -> Path:
    """Return a ``PATH`` directory holding a passing ``claude`` and a failing ``codex``."""
    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    wait = "" if wait_for is None else f"while [ ! -f '{wait_for}' ]; do {SLEEP} 0.05; done; "
    _script(directory, "claude", f"{CLAUDE_VERSION} (Claude Code)", CLAUDE_HELP, wait=wait)
    _script(directory, "codex", f"codex-cli {CODEX_VERSION}", CODEX_HELP_WITHOUT_MCP)
    return directory


def _state_path(canary: CanaryProvision) -> Path:
    return canary.root / ".ea" / "state.json"


@pytest.fixture
def stubbed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put the stand-in harnesses on a private ``PATH`` holding nothing else."""
    directory = _bin(tmp_path)
    monkeypatch.setenv("PATH", str(directory))
    return directory


@pytest.fixture
def auto(
    monkeypatch: pytest.MonkeyPatch, runtime_certification_isolation: Callable[..., Any]
) -> None:
    """Restore the real background trigger the suite keeps off by default."""
    monkeypatch.setattr(
        runtime_certifier, "start_auto_certification", runtime_certification_isolation
    )


# ---------- the version a binary prints ----------


@pytest.mark.parametrize(
    ("printed", "version"),
    [
        ("2.1.288 (Claude Code)", "2.1.288"),
        ("codex-cli 0.159.2", "0.159.2"),
        ("tool 1.0.0-rc.1+build.7", "1.0.0-rc.1+build.7"),
        ("no version here", None),
        ("", None),
        (None, None),
    ],
)
def test_the_installed_version_is_the_token_the_binary_prints(
    printed: str | None, version: str | None
) -> None:
    assert installed_version(printed) == version


# ---------- certify: pass and fail ----------


def test_a_passing_probe_certifies_the_installed_version(tmp_path: Path, stubbed: Path) -> None:
    canary = _controlled(tmp_path, INSTALLED)

    row = certify_installed(_state_path(canary), "claude-code")

    assert (row.outcome, row.runtime_id, row.harness_version) == (
        "certified",
        "claude-code",
        CLAUDE_VERSION,
    )
    assert {item.capability_id: item.status for item in row.capabilities} == {
        "tool_use": "verified",
        "streaming": "verified",
        "session_resume": "unsupported",
    }
    assert row.expires_at == row.verified_at + CERTIFICATION_LIFETIME
    assert read_machine_certifications(_state_path(canary)) == (row,)
    journal = runner_for(_state_path(canary)).journal.records(tuple_digest=row.tuple_digest)
    assert [(stage.stage, stage.outcome) for stage in journal] == [("probe", "passed")]


def test_a_certified_install_admits_cancel_and_refuses_resume(
    tmp_path: Path, stubbed: Path
) -> None:
    canary = _controlled(tmp_path, INSTALLED)
    certify_installed(_state_path(canary), "claude-code")

    with pytest.raises(DaemonValidationError, match="runtime_capability_uncertified"):
        _ask(canary, tmp_path, "resume")
    assert _ask(canary, tmp_path, "cancel")["warnings"] == []


def test_a_failing_probe_quarantines_and_the_control_names_the_findings(
    tmp_path: Path, stubbed: Path
) -> None:
    canary = _controlled(tmp_path, {"harness": "codex", "harness_version": CODEX_VERSION})

    row = certify_installed(_state_path(canary), "codex")

    assert (row.outcome, row.reason_code) == ("quarantined", "capability_not_observed")
    assert row.findings == (
        "tool_use: declared=supported but probe shows none of ['mcp'] in observed_flags",
    )
    with pytest.raises(DaemonValidationError) as refused:
        _ask(canary, tmp_path, "steer")
    assert str(refused.value) == (
        "validation_failed: runtime_quarantined: codex 0.159.2 is quarantined: codex 0.159.2 "
        "failed the conformance probe (capability_not_observed): tool_use"
    )


def test_an_absent_binary_records_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canary = _controlled(tmp_path, INSTALLED)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    with pytest.raises(RuntimeNotProbedError, match=r"^runtime_not_installed: claude-code"):
        certify_installed(_state_path(canary), "claude-code")
    assert read_machine_certifications(_state_path(canary)) == ()


def test_an_unknown_runtime_has_no_binary_to_probe(tmp_path: Path, stubbed: Path) -> None:
    canary = _controlled(tmp_path, INSTALLED)
    with pytest.raises(KeyError):
        certify_installed(_state_path(canary), "gemini")


# ---------- the verb ----------


def _verb(canary: CanaryProvision, tmp_path: Path, **params: Any) -> dict[str, Any]:
    ctx = method_context(tmp_path / "runtime")
    methods.ensure_all_methods_registered()
    return asyncio.run(
        methods.dispatch(RUNTIME_CERTIFY_METHOD, ctx, {"repo_root": str(canary.root), **params})
    )


def test_the_verb_answers_the_row_it_recorded(tmp_path: Path, stubbed: Path) -> None:
    canary = _controlled(tmp_path, INSTALLED)

    answer = _verb(canary, tmp_path, runtime="claude-code")

    row = MachineCertification.model_validate(answer)
    assert (row.outcome, row.harness_version) == ("certified", CLAUDE_VERSION)
    assert read_machine_certifications(_state_path(canary)) == (row,)


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"runtime": "gemini"}, "runtime_unknown"),
        ({}, "schema_validation_failed"),
        ({"runtime": "claude-code", "version": "2"}, "schema_validation_failed"),
    ],
)
def test_the_verb_refuses_a_request_it_cannot_probe(
    tmp_path: Path, stubbed: Path, params: dict[str, Any], code: str
) -> None:
    canary = _controlled(tmp_path, INSTALLED)
    with pytest.raises(DaemonValidationError, match=f"validation_failed: {code}"):
        _verb(canary, tmp_path, **params)


def test_the_verb_refuses_an_absent_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canary = _controlled(tmp_path, INSTALLED)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(DaemonValidationError, match="runtime_not_installed"):
        _verb(canary, tmp_path, runtime="claude-code")


# ---------- automatic certification ----------


def _reported(version: str = CLAUDE_VERSION) -> RunRuntimeTuple:
    return RunRuntimeTuple(harness="claude-code", harness_version=version)


def test_the_background_probe_holds_controls_in_progress_then_certifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auto: None
) -> None:
    release = tmp_path / "release"
    monkeypatch.setenv("PATH", str(_bin(tmp_path, wait_for=release)))
    canary = _controlled(tmp_path, INSTALLED)
    tree = canary.root / ".ea"

    queued = runtime_certifier.start_auto_certification(tree, _reported(), now=datetime.now(UTC))
    assert queued is not None
    try:
        with pytest.raises(DaemonValidationError) as held:
            _ask(canary, tmp_path, "steer")
        assert "runtime_certification_in_progress: claude-code 2.1.288 is being certified" in str(
            held.value
        )
        again = runtime_certifier.start_auto_certification(tree, _reported(), now=datetime.now(UTC))
        assert again is None
    finally:
        release.touch()
    row = queued.result(timeout=30)

    assert row is not None and row.outcome == "certified"
    assert not certifier_for(tree).certifying("claude-code", CLAUDE_VERSION)
    assert _ask(canary, tmp_path, "cancel")["warnings"] == []
    assert len(read_machine_certifications(tree / "state.json")) == 1


def test_a_version_probed_once_is_never_queued_twice(
    tmp_path: Path, stubbed: Path, auto: None
) -> None:
    canary = _controlled(tmp_path, {"harness": "codex", "harness_version": CODEX_VERSION})
    tree = canary.root / ".ea"
    reported = RunRuntimeTuple(harness="codex", harness_version=CODEX_VERSION)

    queued = runtime_certifier.start_auto_certification(tree, reported, now=datetime.now(UTC))
    assert queued is not None
    row = queued.result(timeout=30)

    assert row is not None and row.outcome == "quarantined"
    assert runtime_certifier.start_auto_certification(tree, reported, now=datetime.now(UTC)) is None
    assert len(read_machine_certifications(tree / "state.json")) == 1


def test_a_certified_or_unversioned_or_unknown_runtime_queues_nothing(
    tmp_path: Path, stubbed: Path, auto: None
) -> None:
    canary = _controlled(tmp_path, INSTALLED)
    tree = canary.root / ".ea"
    certify_installed(tree / "state.json", "claude-code")
    now = datetime.now(UTC)

    assert runtime_certifier.start_auto_certification(tree, _reported(), now=now) is None
    unversioned = RunRuntimeTuple(harness="claude-code")
    assert runtime_certifier.start_auto_certification(tree, unversioned, now=now) is None
    unknown = RunRuntimeTuple(harness="gemini", harness_version="1.0.0")
    assert runtime_certifier.start_auto_certification(tree, unknown, now=now) is None


def test_an_expired_certification_is_probed_again(
    tmp_path: Path, stubbed: Path, auto: None
) -> None:
    canary = _controlled(tmp_path, INSTALLED)
    tree = canary.root / ".ea"
    first = certify_installed(tree / "state.json", "claude-code")
    later = first.verified_at + CERTIFICATION_LIFETIME + timedelta(seconds=1)

    queued = runtime_certifier.start_auto_certification(tree, _reported(), now=later)

    assert queued is not None
    assert queued.result(timeout=30) is not None
    assert len(read_machine_certifications(tree / "state.json")) == 2


def test_auto_certification_off_in_config_queues_nothing(
    tmp_path: Path, stubbed: Path, auto: None
) -> None:
    canary = _controlled(tmp_path, INSTALLED)
    config = canary.root / ".ea" / "config.yaml"
    document = yaml.safe_load(config.read_text(encoding="utf-8")) if config.is_file() else {}
    document.setdefault("runtime", {})["auto_certify"] = False
    config.write_text(yaml.safe_dump(document), encoding="utf-8")

    queued = runtime_certifier.start_auto_certification(
        canary.root / ".ea", _reported(), now=datetime.now(UTC)
    )

    assert queued is None


def test_a_run_start_on_an_uncertified_version_certifies_it_in_the_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: Path, auto: None
) -> None:
    monkeypatch.setenv("PATH", f"{_bin(tmp_path)}:/usr/bin:/bin")
    _hosted(monkeypatch)
    _transcript(cli, [{**row, "version": CLAUDE_VERSION} for row in _host_head()])
    canary = _queued(tmp_path)

    _start(canary, tmp_path)

    assert _stored_run(canary).runtime_tuple is not None
    rows = _await_rows(canary.root / ".ea" / "state.json")
    assert [(row.harness_version, row.outcome) for row in rows] == [(CLAUDE_VERSION, "certified")]


def _await_rows(state_path: Path, *, within: float = 30.0) -> tuple[MachineCertification, ...]:
    """Return the probe rows once the background probe has written one."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        rows = read_machine_certifications(state_path)
        if rows:
            return rows
        time.sleep(0.05)
    raise AssertionError(f"no probe row was recorded within {within}s")


# ---------- a probe that could not read the binary records nothing ----------


def _help_times_out(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Make every ``--help`` call time out, as a loaded machine's would, until the event is set."""
    real = sdk_baseline._run
    recovered = threading.Event()

    def run(argv: list[str], **kwargs: Any) -> tuple[int, str, str]:
        if argv[-1] == "--help" and not recovered.is_set():
            return -1, "", "timeout after 10.0s"
        return real(argv, **kwargs)

    monkeypatch.setattr(sdk_baseline, "_run", run)
    return recovered


def test_a_help_timeout_records_no_quarantine(
    tmp_path: Path, stubbed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canary = _controlled(tmp_path, INSTALLED)
    _help_times_out(monkeypatch)

    with pytest.raises(
        RuntimeNotProbedError, match=r"^runtime_probe_incomplete: claude-code 2\.1\.288"
    ):
        certify_installed(_state_path(canary), "claude-code")

    assert read_machine_certifications(_state_path(canary)) == ()


def test_a_help_that_fails_silently_records_no_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "silent"
    directory.mkdir()
    binary = directory / "claude"
    binary.write_text(
        "#!/bin/sh\n"
        f'case "$1" in --version) echo "{CLAUDE_VERSION} (Claude Code)" ;; *) exit 3 ;; esac\n',
        encoding="utf-8",
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(directory))
    canary = _controlled(tmp_path, INSTALLED)

    with pytest.raises(RuntimeNotProbedError, match="runtime_probe_incomplete"):
        certify_installed(_state_path(canary), "claude-code")

    assert read_machine_certifications(_state_path(canary)) == ()


def test_a_probe_that_recorded_nothing_is_queued_again_by_the_next_start(
    tmp_path: Path, stubbed: Path, monkeypatch: pytest.MonkeyPatch, auto: None
) -> None:
    canary = _controlled(tmp_path, INSTALLED)
    tree = canary.root / ".ea"
    recovered = _help_times_out(monkeypatch)
    now = datetime.now(UTC)

    first = runtime_certifier.start_auto_certification(tree, _reported(), now=now)
    assert first is not None and first.result(timeout=30) is None
    assert read_machine_certifications(tree / "state.json") == ()
    recovered.set()
    again = runtime_certifier.start_auto_certification(tree, _reported(), now=now)

    assert again is not None
    row = again.result(timeout=30)
    assert row is not None and row.outcome == "certified"


# ---------- a quarantine is probed again after the back-off ----------


def test_a_quarantine_is_probed_again_only_after_the_back_off(
    tmp_path: Path, stubbed: Path, auto: None
) -> None:
    canary = _controlled(tmp_path, {"harness": "codex", "harness_version": CODEX_VERSION})
    tree = canary.root / ".ea"
    reported = RunRuntimeTuple(harness="codex", harness_version=CODEX_VERSION)
    first = certify_installed(tree / "state.json", "codex")
    assert first.outcome == "quarantined"

    within = first.verified_at + REPROBE_BACKOFF - timedelta(seconds=1)
    after = first.verified_at + REPROBE_BACKOFF

    assert runtime_certifier.start_auto_certification(tree, reported, now=within) is None
    queued = runtime_certifier.start_auto_certification(tree, reported, now=after)
    assert queued is not None
    assert queued.result(timeout=30) is not None
    assert len(read_machine_certifications(tree / "state.json")) == 2
    assert runtime_certifier.start_auto_certification(tree, reported, now=after) is None


# ---------- an unexpected probe failure is logged and retried ----------


def test_an_unexpected_probe_failure_is_logged_and_frees_the_version(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    certifier = certifier_for(tmp_path / "tree" / ".ea")
    now = datetime.now(UTC)

    def broken(_runtime_id: str) -> MachineCertification:
        raise OSError("journal unreadable")

    monkeypatch.setattr(certifier, "certify", broken)
    with caplog.at_level(logging.ERROR, logger=runtime_certifier.__name__):
        queued = certifier.ensure("claude-code", CLAUDE_VERSION, now=now)
        assert queued is not None and queued.result(timeout=30) is None

    (record,) = [r for r in caplog.records if r.message.startswith("_probe failed")]
    assert record.exc_info is not None and "journal unreadable" in str(record.exc_info[1])
    assert not certifier.certifying("claude-code", CLAUDE_VERSION)
    assert certifier.ensure("claude-code", CLAUDE_VERSION, now=now) is not None


def test_a_queued_version_is_not_queued_again_within_the_back_off(tmp_path: Path) -> None:
    certifier = certifier_for(tmp_path / "tree" / ".ea")
    now = datetime.now(UTC)
    release = threading.Event()
    certifier._executor.submit(release.wait)
    try:
        assert certifier.ensure("claude-code", CLAUDE_VERSION, now=now) is not None
        later = now + REPROBE_BACKOFF - timedelta(seconds=1)
        assert certifier.ensure("claude-code", CLAUDE_VERSION, now=later) is None
        assert certifier.ensure("claude-code", CLAUDE_VERSION, now=now + REPROBE_BACKOFF)
    finally:
        release.set()


# ---------- a Run start survives a broken probe trigger ----------


def test_a_run_starts_when_the_auto_certification_trigger_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cli: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _hosted(monkeypatch)
    _transcript(cli, [{**row, "version": CLAUDE_VERSION} for row in _host_head()])
    canary = _queued(tmp_path)

    def torn(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("torn runtime_certification.jsonl line")

    monkeypatch.setattr(runtime_certifier, "start_auto_certification", torn)
    with caplog.at_level(logging.ERROR):
        _start(canary, tmp_path)

    run = _stored_run(canary)
    assert run.status == "RUNNING" and run.runtime_tuple is not None
    assert any(r.message.startswith("auto_certification_skipped") for r in caplog.records)


# ---------- the auto_certify switch is read from every layer, every time ----------


def _switch_off(path: Path) -> None:
    """Write ``runtime.auto_certify: false`` into the layer file at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("runtime:\n  auto_certify: false\n", encoding="utf-8")


@pytest.mark.parametrize("layer", ["local", "global"])
def test_turning_the_switch_off_in_any_layer_holds_without_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auto: None, layer: str
) -> None:
    from eawf.kernel.config import layered

    global_file = tmp_path / "home" / "config.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: global_file)
    canary = _controlled(tmp_path, INSTALLED)
    tree = canary.root / ".ea"
    queued: list[tuple[str, str]] = []

    def ensure(runtime_id: str, version: str, *, now: datetime) -> None:
        queued.append((runtime_id, version))

    monkeypatch.setattr(certifier_for(tree), "ensure", ensure)
    now = datetime.now(UTC)

    runtime_certifier.start_auto_certification(tree, _reported(), now=now)
    assert queued == [("claude-code", CLAUDE_VERSION)]
    _switch_off(layered.local_config_path(canary.root) if layer == "local" else global_file)

    assert runtime_certifier.start_auto_certification(tree, _reported(), now=now) is None
    assert len(queued) == 1


def test_a_certified_version_composes_no_config(
    tmp_path: Path, stubbed: Path, monkeypatch: pytest.MonkeyPatch, auto: None
) -> None:
    """Boundary: a Run on a version already certified never pays for the config read."""
    canary = _controlled(tmp_path, INSTALLED)
    tree = canary.root / ".ea"
    certify_installed(tree / "state.json", "claude-code")
    calls: list[Path] = []
    monkeypatch.setattr(runtime_certifier, "resolve_auto_certify", calls.append)
    now = datetime.now(UTC)

    assert runtime_certifier.start_auto_certification(tree, _reported(), now=now) is None
    assert calls == []


# ---------- a torn certification row never keeps a Run running ----------


def _tear(canary: CanaryProvision) -> None:
    """Append a line to this machine's probe rows that is not an envelope."""
    path = machine_certification_path(_state_path(canary))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"torn": \n', encoding="utf-8")


@pytest.mark.parametrize("control", ["cancel", "interrupt"])
def test_a_stop_is_admitted_over_a_torn_certification_row(tmp_path: Path, control: str) -> None:
    canary = _controlled(tmp_path, INSTALLED)
    _tear(canary)

    answer = _ask(canary, tmp_path, control)

    (warning,) = answer["warnings"]
    assert warning.startswith(f"{control} was admitted uncertified, as a stop always is: ")
    assert "certification records could not be read" in warning


def test_a_steer_over_a_torn_certification_row_is_still_refused(tmp_path: Path) -> None:
    """The error path: only a stop is let through an unreadable record."""
    canary = _controlled(tmp_path, INSTALLED)
    _tear(canary)

    with pytest.raises(ValueError, match="validation error"):
        _ask(canary, tmp_path, "steer")
