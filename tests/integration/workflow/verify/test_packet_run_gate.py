"""The ``packet_run`` assertion greens only when all four packet runs passed for the version.

The stable checkpoint needs a passing perfect-realization packet on
claude-code and codex, each on a born-native and an adopted repository, on
Linux, at the version being tagged, from a clean source commit the released
commit carries, on a runtime version the committed canary evidence export
certifies. These checks pin how the assertion reads the machine-local
receipts: which receipts count, how a missing, failed or uncertified run is
named, and how a malformed receipt or export is refused.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.release.signals import ReleaseSignalOutcome, ReleaseSignalStatus
from eawf.kernel.runtime.certification import CertifiedVersionRange
from eawf.workflow.evidence.provider_certification import canary_evidence_path
from eawf.workflow.release.packet_run import (
    PACKET_RECEIPTS_FILENAME,
    PacketOrigin,
    PacketReceiptError,
    PacketRunReceipt,
    PacketStep,
    append_packet_receipt,
    packet_coverage,
    packet_run_dir,
    read_packet_receipts,
)
from eawf.workflow.verify.realization_assertions import packet_run_outcome

pytestmark = pytest.mark.integration

VERSION: Final = "0.7.0"
RELEASE_KEY: Final = "REL-0.7.0"
STARTED: Final = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
PAIRS: Final = (
    ("claude-code", PacketOrigin.BORN_NATIVE),
    ("claude-code", PacketOrigin.ADOPTED),
    ("codex", PacketOrigin.BORN_NATIVE),
    ("codex", PacketOrigin.ADOPTED),
)

#: The committed stable export; it certifies claude-code 2.1.314..2.1.999
#: and codex 0.164.0..0.199.999 on Linux.
STABLE_EXPORT: Final = canary_evidence_path(Path(__file__).resolve().parents[4], RELEASE_KEY)

#: A runtime version inside each certified range.
CERTIFIED: Final = {"claude-code": "2.1.320", "codex": "0.170.0"}

_GIT_ENV: Final = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


def commit(repo: Path, message: str = "release") -> str:
    """Commit everything in *repo*, initialising it first if need be, and return HEAD."""
    if not (repo / ".git").exists():
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=_GIT_ENV)
    identity = ("-c", "user.name=t", "-c", "user.email=t@example.com")
    for argv in (("add", "-A"), (*identity, "commit", "-q", "--allow-empty", "-m", message)):
        subprocess.run(
            ["git", "-C", str(repo), *argv], check=True, capture_output=True, env=_GIT_ENV
        )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def release_repo(repo: Path, *, release_key: str = RELEASE_KEY) -> str:
    """Commit the stable canary export, filed for *release_key*, into *repo*; return HEAD."""
    document = json.loads(STABLE_EXPORT.read_text(encoding="utf-8"))
    target = canary_evidence_path(repo, release_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({**document, "release_key": release_key}), encoding="utf-8")
    return commit(repo)


def outcome_of(repo: Path, *, release_commit: str = "HEAD") -> ReleaseSignalOutcome:
    """Return the assertion's outcome for :data:`VERSION` released at *release_commit*."""
    return packet_run_outcome(
        repo, version=VERSION, release_key=RELEASE_KEY, release_commit=release_commit
    )


def packet_receipt(
    runtime: str,
    origin: PacketOrigin,
    /,
    *,
    version: str = VERSION,
    eawf_commit: str = "a" * 40,
    **overrides: Any,
) -> PacketRunReceipt:
    """Return a passing clean Linux receipt of *runtime* on *origin*, with *overrides* applied."""
    body: dict[str, Any] = {
        "runtime": runtime,
        "origin": origin,
        "os_class": "linux",
        "eawf_version": version,
        "eawf_commit": eawf_commit,
        "source_dirty": False,
        "runtime_version": CERTIFIED[runtime],
        "budget_tokens": 300_000,
        "outcome": "pass",
        "started_at": STARTED,
        "ended_at": STARTED + timedelta(minutes=5),
        "evidence_digest": f"sha256:{'b' * 64}",
        **overrides,
    }
    return PacketRunReceipt.model_validate(body)


def file_all_four(repo: Path, *, version: str = VERSION, release_key: str = RELEASE_KEY) -> str:
    """Release *repo* under *release_key* and pass every pair of *version* at that commit.

    Returns:
        The released commit every receipt names.
    """
    head = release_repo(repo, release_key=release_key)
    for runtime, origin in PAIRS:
        append_packet_receipt(
            repo, packet_receipt(runtime, origin, version=version, eawf_commit=head)
        )
    return head


def file_three(repo: Path) -> str:
    """Release *repo* and pass every pair but codex/adopted at that commit; return it."""
    head = release_repo(repo)
    for runtime, origin in PAIRS[:3]:
        append_packet_receipt(repo, packet_receipt(runtime, origin, eawf_commit=head))
    return head


# ---- the assertion -----------------------------------------------------------

# DOM-005: the assertion holds only when the born-native and the adopted repository
# both passed the packet, on each runtime; a missing or failed origin is named.


def test_packet_run_holds_when_all_four_runs_passed(tmp_path: Path) -> None:
    file_all_four(tmp_path)

    outcome = outcome_of(tmp_path)

    assert outcome.status is ReleaseSignalStatus.PASS
    assert outcome.evidence_refs == (
        "packet_run:claude-code/born_native:passed",
        "packet_run:claude-code/adopted:passed",
        "packet_run:codex/born_native:passed",
        "packet_run:codex/adopted:passed",
    )


def test_packet_run_is_unavailable_naming_every_run_when_none_was_filed(tmp_path: Path) -> None:
    outcome = outcome_of(tmp_path)

    assert outcome.status is ReleaseSignalStatus.UNAVAILABLE
    assert outcome.remediation.startswith(
        "packet_run: no run for claude-code/born_native, claude-code/adopted, "
        "codex/born_native, codex/adopted;"
    )
    assert "eawf release packet-run" in outcome.remediation


def test_packet_run_names_the_one_missing_run(tmp_path: Path) -> None:
    file_three(tmp_path)

    outcome = outcome_of(tmp_path)

    assert outcome.status is ReleaseSignalStatus.UNAVAILABLE
    assert outcome.remediation.startswith("packet_run: no run for codex/adopted;")


def test_packet_run_breaks_naming_the_failed_run_and_its_step(tmp_path: Path) -> None:
    head = file_three(tmp_path)
    append_packet_receipt(
        tmp_path,
        packet_receipt(
            "codex",
            PacketOrigin.ADOPTED,
            eawf_commit=head,
            outcome="fail",
            failed_step=PacketStep.DISPATCH,
            failure="run RUN-00000002 ended FAILED",
        ),
    )

    outcome = outcome_of(tmp_path)

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert outcome.remediation.startswith("packet_run: codex/adopted failed at dispatch;")
    assert outcome.evidence_refs == ("packet_run:codex/adopted:failed:dispatch",)


def test_packet_run_names_a_missing_run_beside_a_failed_one(tmp_path: Path) -> None:
    head = release_repo(tmp_path)
    append_packet_receipt(tmp_path, packet_receipt(*PAIRS[0], eawf_commit=head))
    append_packet_receipt(tmp_path, packet_receipt(*PAIRS[1], eawf_commit=head))
    append_packet_receipt(
        tmp_path,
        packet_receipt(*PAIRS[2], eawf_commit=head, outcome="fail", failed_step=PacketStep.CERTIFY),
    )

    outcome = outcome_of(tmp_path)

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert outcome.remediation.startswith(
        "packet_run: codex/born_native failed at certify; no run for codex/adopted;"
    )


# REL-022: support is claimed only from evidence on the exact version and platform the
# release covers, so another version's receipt or a macOS receipt counts for nothing.
def test_packet_run_does_not_count_a_receipt_of_another_version(tmp_path: Path) -> None:
    head = file_three(tmp_path)
    append_packet_receipt(tmp_path, packet_receipt(*PAIRS[3], version="0.7.0rc4", eawf_commit=head))

    outcome = outcome_of(tmp_path)

    assert outcome.status is ReleaseSignalStatus.UNAVAILABLE
    assert outcome.remediation.startswith("packet_run: no run for codex/adopted;")


def test_packet_run_does_not_count_a_receipt_off_linux(tmp_path: Path) -> None:
    head = file_three(tmp_path)
    append_packet_receipt(tmp_path, packet_receipt(*PAIRS[3], eawf_commit=head, os_class="macos"))

    outcome = outcome_of(tmp_path)

    assert outcome.status is ReleaseSignalStatus.UNAVAILABLE
    assert "codex/adopted" in outcome.remediation


def test_packet_run_lets_a_later_pass_supersede_an_earlier_failure(tmp_path: Path) -> None:
    head = release_repo(tmp_path)
    append_packet_receipt(
        tmp_path,
        packet_receipt(*PAIRS[0], eawf_commit=head, outcome="fail", failed_step=PacketStep.LAND),
    )
    for runtime, origin in PAIRS:
        append_packet_receipt(tmp_path, packet_receipt(runtime, origin, eawf_commit=head))

    assert outcome_of(tmp_path).status is ReleaseSignalStatus.PASS


def test_packet_run_refuses_a_malformed_receipt_by_line(tmp_path: Path) -> None:
    file_all_four(tmp_path)
    path = packet_run_dir(tmp_path) / PACKET_RECEIPTS_FILENAME
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"runtime": "codex", "outcome": "pass"}\n')

    outcome = outcome_of(tmp_path)

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert outcome.remediation.startswith(
        f"packet_run: {PACKET_RECEIPTS_FILENAME} line 5 is not a packet-run receipt"
    )
    assert outcome.evidence_refs == ("packet_run:malformed-receipt",)


# ---- which source a receipt counts from -------------------------------------------


def test_packet_run_does_not_count_a_receipt_from_a_dirty_source(tmp_path: Path) -> None:
    head = file_three(tmp_path)
    append_packet_receipt(tmp_path, packet_receipt(*PAIRS[3], eawf_commit=head, source_dirty=True))

    outcome = outcome_of(tmp_path)

    assert outcome.status is ReleaseSignalStatus.UNAVAILABLE
    assert outcome.remediation.startswith(
        f"packet_run: no run for codex/adopted; set aside: codex/adopted at {head[:12]} "
        "ran from a dirty source tree;"
    )


def test_packet_run_keeps_a_clean_failure_over_a_later_dirty_pass(tmp_path: Path) -> None:
    head = file_three(tmp_path)
    append_packet_receipt(
        tmp_path,
        packet_receipt(*PAIRS[3], eawf_commit=head, outcome="fail", failed_step=PacketStep.LAND),
    )
    append_packet_receipt(tmp_path, packet_receipt(*PAIRS[3], eawf_commit=head, source_dirty=True))

    outcome = outcome_of(tmp_path)

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert outcome.evidence_refs == ("packet_run:codex/adopted:failed:land",)


@pytest.mark.parametrize("where", ["later-commit", "unknown-commit"])
def test_packet_run_does_not_count_a_commit_the_release_does_not_carry(
    tmp_path: Path, where: str
) -> None:
    released = file_three(tmp_path)
    later = commit(tmp_path, "fix: after the release") if where == "later-commit" else "c" * 40
    append_packet_receipt(tmp_path, packet_receipt(*PAIRS[3], eawf_commit=later))

    outcome = outcome_of(tmp_path, release_commit=released)

    assert outcome.status is ReleaseSignalStatus.UNAVAILABLE
    assert (
        f"set aside: codex/adopted at {later[:12]} is not an ancestor of the released commit"
        in outcome.remediation
    )


def test_packet_run_counts_a_receipt_from_an_ancestor_of_the_release(tmp_path: Path) -> None:
    earlier = file_all_four(tmp_path)
    released = commit(tmp_path, "state: accept the Milestone")

    outcome = outcome_of(tmp_path, release_commit=released)

    assert earlier != released
    assert outcome.status is ReleaseSignalStatus.PASS


# ---- which runtime versions a receipt counts on -----------------------------------


@pytest.mark.parametrize("runtime_version", ["0.164.0", "0.199.999"], ids=["min", "max"])
def test_packet_run_holds_on_either_bound_of_the_certified_range(
    tmp_path: Path, runtime_version: str
) -> None:
    head = file_three(tmp_path)
    append_packet_receipt(
        tmp_path, packet_receipt(*PAIRS[3], eawf_commit=head, runtime_version=runtime_version)
    )

    assert outcome_of(tmp_path).status is ReleaseSignalStatus.PASS


@pytest.mark.parametrize(
    ("runtime_version", "reason"),
    [
        ("0.163.9", "codex 0.163.9 is outside the certified range 0.164.0..0.199.999"),
        ("0.200.0", "codex 0.200.0 is outside the certified range 0.164.0..0.199.999"),
        ("0.170", "codex 0.170 is not a SemVer version inside 0.164.0..0.199.999"),
    ],
    ids=["below-min", "above-max", "not-semver"],
)
def test_packet_run_refuses_a_runtime_version_outside_the_certified_range(
    tmp_path: Path, runtime_version: str, reason: str
) -> None:
    head = file_three(tmp_path)
    append_packet_receipt(
        tmp_path, packet_receipt(*PAIRS[3], eawf_commit=head, runtime_version=runtime_version)
    )

    outcome = outcome_of(tmp_path)

    assert outcome.status is ReleaseSignalStatus.FAIL
    export = canary_evidence_path(tmp_path, RELEASE_KEY).relative_to(tmp_path).as_posix()
    assert outcome.remediation.startswith(
        f"packet_run: codex/adopted refused: {reason} ({export});"
    )
    assert outcome.evidence_refs == ("packet_run:codex/adopted:refused:runtime-version",)


def test_packet_run_refuses_a_pass_when_no_export_certifies_the_runtime(tmp_path: Path) -> None:
    head = commit(tmp_path)
    for runtime, origin in PAIRS:
        append_packet_receipt(tmp_path, packet_receipt(runtime, origin, eawf_commit=head))

    outcome = outcome_of(tmp_path)

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert (
        "codex/adopted refused: codex 0.170.0 ran, but no certified version range is recorded "
        f"(no committed {RELEASE_KEY} canary evidence export)"
    ) in outcome.remediation
    assert len(outcome.evidence_refs) == 4


def test_packet_run_breaks_on_an_unreadable_export(tmp_path: Path) -> None:
    file_all_four(tmp_path)
    canary_evidence_path(tmp_path, RELEASE_KEY).write_text("{", encoding="utf-8")

    outcome = outcome_of(tmp_path)

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert "is not valid JSON" in outcome.remediation
    assert outcome.evidence_refs == ("packet_run:canary-evidence",)


# ---- the receipt store -----------------------------------------------------------


def test_read_packet_receipts_returns_none_from_an_empty_file(tmp_path: Path) -> None:
    path = packet_run_dir(tmp_path) / PACKET_RECEIPTS_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text("\n", encoding="utf-8")

    assert read_packet_receipts(tmp_path) == ()


def test_read_packet_receipts_round_trips_one_receipt(tmp_path: Path) -> None:
    receipt = packet_receipt(*PAIRS[0])
    append_packet_receipt(tmp_path, receipt)

    assert read_packet_receipts(tmp_path) == (receipt,)


@pytest.mark.parametrize(
    "line",
    ["not json", "[]", '{"schema_version": "packet-run/v2"}'],
    ids=["not-json", "not-an-object", "missing-fields"],
)
def test_read_packet_receipts_refuses_a_malformed_line(tmp_path: Path, line: str) -> None:
    path = packet_run_dir(tmp_path) / PACKET_RECEIPTS_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text(f"{line}\n", encoding="utf-8")

    with pytest.raises(PacketReceiptError, match="line 1 is not a packet-run receipt"):
        read_packet_receipts(tmp_path)


def test_packet_coverage_puts_every_pair_in_exactly_one_bucket() -> None:
    receipts = (
        packet_receipt(*PAIRS[0]),
        packet_receipt(*PAIRS[1], outcome="fail", failed_step=PacketStep.TRANSCRIPT),
        packet_receipt(*PAIRS[2], runtime_version="0.1.0"),
        packet_receipt(*PAIRS[3], source_dirty=True),
    )
    ranges = {
        "claude-code": (CertifiedVersionRange(min_version="2.1.0", max_version="2.1.999"),),
        "codex": (CertifiedVersionRange(min_version="0.164.0", max_version="0.199.999"),),
    }

    coverage = packet_coverage(receipts, version=VERSION, released=lambda _: True, ranges=ranges)

    assert coverage.passed == ("claude-code/born_native",)
    assert coverage.failed == (("claude-code/adopted", PacketStep.TRANSCRIPT),)
    assert coverage.refused == (
        ("codex/born_native", "codex 0.1.0 is outside the certified range 0.164.0..0.199.999"),
    )
    assert coverage.missing == ("codex/adopted",)
    assert coverage.ignored == (f"codex/adopted at {'a' * 12} ran from a dirty source tree",)


def test_packet_coverage_of_no_receipts_names_every_pair_missing() -> None:
    coverage = packet_coverage((), version=VERSION, released=lambda _: True, ranges={})

    assert coverage.missing == tuple(f"{runtime}/{origin.value}" for runtime, origin in PAIRS)
    assert (coverage.passed, coverage.failed, coverage.refused, coverage.ignored) == (
        (),
        (),
        (),
        (),
    )


# ---- the receipt model ------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"outcome": "fail"}, "failed_step is set exactly when the outcome is fail"),
        ({"failed_step": PacketStep.INIT}, "failed_step is set exactly when the outcome is fail"),
        ({"failure": "boom"}, "a passed packet run carries no failure"),
        ({"ended_at": STARTED - timedelta(seconds=1)}, "ended_at precedes started_at"),
        ({"runtime": "opencode"}, "runtime"),
        ({"origin": "forked"}, "origin"),
        ({"eawf_commit": "abc"}, "eawf_commit"),
        ({"source_dirty": "maybe"}, "source_dirty"),
        ({"budget_tokens": 0}, "budget_tokens"),
        ({"evidence_digest": "sha256:xyz"}, "evidence_digest"),
        ({"started_at": datetime(2026, 10, 30, 12, 0)}, "timezone-aware"),
        ({"schema_version": "packet-run/v1"}, "schema_version"),
        ({"scratch": "/tmp/packet"}, "Extra inputs are not permitted"),
    ],
    ids=[
        "fail-without-step",
        "pass-with-step",
        "pass-with-failure",
        "ended-before-started",
        "unknown-runtime",
        "unknown-origin",
        "short-commit",
        "dirty-not-bool",
        "zero-budget",
        "bad-digest",
        "naive-time",
        "old-schema",
        "extra-key",
    ],
)
def test_packet_run_receipt_refuses_an_inconsistent_receipt(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        packet_receipt(*PAIRS[0], **overrides)


def test_packet_run_receipt_requires_the_dirty_flag() -> None:
    body = packet_receipt(*PAIRS[0]).model_dump(mode="json")
    del body["source_dirty"]

    with pytest.raises(ValidationError, match="source_dirty"):
        PacketRunReceipt.model_validate(body)
