"""The ``provider`` row needs a passing live dispatch smoke on every claimed os_class.

A certification proves a runtime honours its contract; it does not prove that
a dispatched agent carries a Task to a sealed candidate within the dispatch
scorecard. The live smoke (``tests/e2e/test_smoke_milestone.py`` under
``EAWF_SMOKE_PROVIDER=claude``) proves that, scores every Run with
``eawf bench dispatch --check --tier fixture`` and files the host's os_class
in ``dispatch-smoke-receipts.json`` beside the canary receipts. These checks
pin how the row reads that file, how the smoke writes it, and the
release-process step that tells an operator to run it.
"""

from __future__ import annotations

import inspect
import json
import subprocess
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from pydantic import ValidationError

import tests.e2e.test_smoke_milestone as smoke_module
from eawf.kernel.release.signals import ReleaseSignalName, ReleaseSignalStatus
from eawf.workflow.evidence.provider_certification import CANARY_EVIDENCE_DIRS, OsClass
from eawf.workflow.release.admission import checkpoint_release_config
from eawf.workflow.release.canary_receipts import (
    DISPATCH_SMOKE_RECEIPTS_FILENAME,
    DispatchSmokeGap,
    DispatchSmokeReceiptSet,
    dispatch_smoke_gaps,
    load_dispatch_smoke_receipts,
    read_dispatch_checks,
    read_dispatch_smoke_receipts,
)
from eawf.workflow.release.signal_probes import (
    build_receipt_probes,
    provider_probe,
    smoke_remediation,
)
from eawf.workflow.verify.release_readiness import ReleaseSignalContext
from tests.integration.workflow.verify.test_provider_os_class import (
    certified_on,
    check_document,
    committed,
    smoke_filed,
    staged,
)

pytestmark = pytest.mark.integration

REPO_ROOT: Final = Path(__file__).resolve().parents[4]
DEV4_VERSION: Final = "0.7.0.dev4"
DEV4_KEY: Final = f"REL-{DEV4_VERSION}"
RC3_VERSION: Final = "0.7.0rc3"
RC3_KEY: Final = f"REL-{RC3_VERSION}"

#: Where the dev4 smoke receipts sit, relative to a checkout.
RECEIPTS: Final = "/".join((*CANARY_EVIDENCE_DIRS[DEV4_KEY], DISPATCH_SMOKE_RECEIPTS_FILENAME))

#: The operator command the release-process step names.
SMOKE_COMMAND: Final = (
    "EAWF_SMOKE_PROVIDER=claude EAWF_SMOKE_EVIDENCE_DIR=<canary evidence directory> "
    "uv run pytest -m e2e tests/e2e/test_smoke_milestone.py"
)


def certified(tmp_path: Path, os_classes: list[str] | None = None) -> Path:
    """Stage a dev4 export certified on Linux and macOS, claiming *os_classes*."""
    document = certified_on(committed(os_classes=os_classes), "linux")
    return staged(tmp_path, document)


def probe_at(repo_root: Path) -> Any:
    """Return the dev4 ``provider`` outcome for a checkout at *repo_root*."""
    config = checkpoint_release_config(DEV4_VERSION, repo_root=REPO_ROOT)
    return provider_probe(
        ReleaseSignalContext(config, ReleaseSignalName.PROVIDER, None), repo_root=repo_root
    )


def receipts_file(root: Path, document: dict[str, Any] | str) -> Path:
    """Write *document* as the dev4 smoke receipt file of the checkout at *root*."""
    path = root / RECEIPTS
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document if isinstance(document, str) else json.dumps(document), "utf-8")
    return root


def documents_filed(root: Path, *documents: dict[str, Any]) -> Path:
    """Write each check document beside the dev4 receipt file, named by its Run."""
    directory = (root / RECEIPTS).parent
    directory.mkdir(parents=True, exist_ok=True)
    for document in documents:
        run_key = document["check"]["scorecard"]["run_key"]
        (directory / f"dispatch-check-{run_key}.json").write_text(json.dumps(document), "utf-8")
    return root


def receipt(os_class: str = "linux", **overrides: Any) -> dict[str, Any]:
    """Return one valid receipt row on *os_class* with *overrides* applied."""
    return {
        "os_class": os_class,
        "runtime_id": "claude-code",
        "run_refs": ["eawf://SMOKE/SMOKE/SMOKE/run/RUN-00000001"],
        "scorecard": "pass",
        "evidence_refs": ["dispatch-check-RUN-00000001.json"],
        **overrides,
    }


# --- CR-0507-1: the row passes on a passing receipt per claimed class --


def test_smoke_receipt_passes_when_every_claimed_class_has_a_passing_receipt(
    tmp_path: Path,
) -> None:
    outcome = probe_at(smoke_filed(certified(tmp_path), DEV4_KEY))

    assert outcome.status is ReleaseSignalStatus.PASS, outcome.remediation


def test_smoke_receipt_passes_for_a_single_claimed_class(tmp_path: Path) -> None:
    root = smoke_filed(certified(tmp_path, ["macos"]), DEV4_KEY, ("macos",))

    assert probe_at(root).status is ReleaseSignalStatus.PASS


def test_smoke_receipt_passes_beside_a_receipt_on_an_unclaimed_class(tmp_path: Path) -> None:
    root = smoke_filed(certified(tmp_path, ["macos"]), DEV4_KEY, ("macos",))
    receipts_file(
        root,
        {
            "release_key": DEV4_KEY,
            "receipts": [receipt("macos"), receipt("windows", scorecard="fail")],
        },
    )

    assert probe_at(root).status is ReleaseSignalStatus.PASS


def test_smoke_receipt_passes_through_the_bound_probe(tmp_path: Path) -> None:
    """The production registry reads the file, not only the bare probe."""
    root = smoke_filed(certified(tmp_path), DEV4_KEY)
    config = checkpoint_release_config(DEV4_VERSION, repo_root=REPO_ROOT)
    probe = build_receipt_probes(root)[ReleaseSignalName.PROVIDER]

    outcome = probe(ReleaseSignalContext(config, ReleaseSignalName.PROVIDER, None))

    assert outcome.status is ReleaseSignalStatus.PASS, outcome.remediation


def test_smoke_receipt_passes_loads_as_a_closed_receipt_set(tmp_path: Path) -> None:
    root = smoke_filed(tmp_path, DEV4_KEY)

    receipts = load_dispatch_smoke_receipts(root, DEV4_KEY)

    assert receipts is not None
    assert receipts.release_key == DEV4_KEY
    assert [(r.os_class, r.runtime_id, r.scorecard) for r in receipts.receipts] == [
        ("linux", "claude-code", "pass"),
        ("macos", "claude-code", "pass"),
    ]
    assert receipts.receipts[0].run_refs == ("eawf://SMOKE/SMOKE/SMOKE/run/RUN-00000001",)
    assert receipts.receipts[1].evidence_refs == ("dispatch-check-RUN-00000002.json",)


def test_smoke_receipt_passes_with_no_gap_for_passing_classes(tmp_path: Path) -> None:
    root = smoke_filed(tmp_path, DEV4_KEY)
    receipts = load_dispatch_smoke_receipts(root, DEV4_KEY)
    documents = read_dispatch_checks((root / RECEIPTS).parent, receipts)

    claims = (("claude-code", "linux"), ("claude-code", "macos"), ("claude-code", "linux"))

    assert dispatch_smoke_gaps(receipts, claims, documents) == ()
    assert dispatch_smoke_gaps(receipts, (), documents) == ()


# --- CR-0507-2: missing, failing and unreadable receipts ----------------


def test_smoke_receipt_refusals_without_a_receipt_file(tmp_path: Path) -> None:
    outcome = probe_at(certified(tmp_path))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert (
        "claude-code on linux: dispatch_smoke_missing; claude-code on macos: "
        "dispatch_smoke_missing" in outcome.remediation
    )
    assert "EAWF_SMOKE_PROVIDER=claude" in outcome.remediation


def test_smoke_receipt_refusals_name_only_the_class_lacking_a_receipt(tmp_path: Path) -> None:
    outcome = probe_at(smoke_filed(certified(tmp_path), DEV4_KEY, ("linux",)))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert "claude-code on macos: dispatch_smoke_missing" in outcome.remediation
    assert "linux:" not in outcome.remediation


def test_smoke_receipt_refusals_on_a_failing_scorecard(tmp_path: Path) -> None:
    outcome = probe_at(smoke_filed(certified(tmp_path), DEV4_KEY, scorecard="fail"))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert (
        "claude-code on linux: dispatch_smoke_failed; claude-code on macos: "
        "dispatch_smoke_failed" in outcome.remediation
    )


def test_smoke_receipt_refusals_when_another_runtime_smoked_the_class(tmp_path: Path) -> None:
    """A passing codex smoke does not back the claude-code claim on the same class."""
    root = receipts_file(
        certified(tmp_path, ["linux"]),
        {"release_key": DEV4_KEY, "receipts": [receipt(runtime_id="codex")]},
    )

    outcome = probe_at(root)

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert "claude-code on linux: dispatch_smoke_missing" in outcome.remediation


def test_smoke_receipt_passes_beside_a_failing_smoke_of_an_unclaimed_runtime(
    tmp_path: Path,
) -> None:
    """A failing codex smoke says nothing about the claude-code claim it does not back."""
    root = receipts_file(
        documents_filed(certified(tmp_path, ["linux"]), check_document("RUN-00000001")),
        {
            "release_key": DEV4_KEY,
            "receipts": [receipt(), receipt(runtime_id="codex", scorecard="fail")],
        },
    )

    assert probe_at(root).status is ReleaseSignalStatus.PASS


def test_smoke_receipt_refusals_keep_the_certification_refs(tmp_path: Path) -> None:
    outcome = probe_at(certified(tmp_path))

    assert outcome.evidence_refs == (
        "certification://claude-code/2026-09-18-linux",
        "certification://claude-code/2026-09-18",
    )


def test_smoke_receipt_refusals_report_a_certification_gap_first(tmp_path: Path) -> None:
    """An uncertified class is the certification's gap, not the smoke's."""
    outcome = probe_at(staged(tmp_path, committed()))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert "os_class_uncertified" in outcome.remediation
    assert "dispatch_smoke" not in outcome.remediation


def test_smoke_receipt_refusals_on_a_file_that_is_not_json(tmp_path: Path) -> None:
    outcome = probe_at(receipts_file(certified(tmp_path), "{not json"))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert f"the dispatch smoke receipt file at {RECEIPTS} does not read back" in (
        outcome.remediation
    )
    assert "canary evidence export" not in outcome.remediation
    assert "re-run the live dispatch smoke" in outcome.remediation


@pytest.mark.parametrize(
    "document",
    [
        pytest.param([], id="not-a-mapping"),
        pytest.param({"release_key": DEV4_KEY}, id="missing-receipts"),
        pytest.param({"release_key": DEV4_KEY, "receipts": [], "extra": 1}, id="unknown-key"),
        pytest.param({"release_key": DEV4_KEY, "receipts": [receipt(run_refs=[])]}, id="no-run"),
        pytest.param(
            {"release_key": DEV4_KEY, "receipts": [receipt(scorecard="maybe")]}, id="scorecard"
        ),
        pytest.param({"release_key": DEV4_KEY, "receipts": [receipt("beos")]}, id="os-class"),
        pytest.param(
            {"release_key": DEV4_KEY, "receipts": [receipt(evidence_refs=["/abs/check.json"])]},
            id="absolute-ref",
        ),
        pytest.param(
            {"release_key": DEV4_KEY, "receipts": [receipt(runtime_id="x" * 65)]},
            id="runtime-too-long",
        ),
        pytest.param(
            {"release_key": DEV4_KEY, "receipts": [receipt(), receipt()]}, id="repeated-pair"
        ),
        pytest.param({"release_key": "REL-0.7.0.dev3", "receipts": []}, id="other-release"),
    ],
)
def test_smoke_receipt_refusals_on_a_file_that_does_not_validate(
    tmp_path: Path, document: Any
) -> None:
    outcome = probe_at(receipts_file(certified(tmp_path), json.dumps(document)))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert "does not read back" in outcome.remediation


def test_smoke_receipt_refusals_read_an_absent_file_as_none(tmp_path: Path) -> None:
    assert read_dispatch_smoke_receipts(tmp_path / DISPATCH_SMOKE_RECEIPTS_FILENAME) is None
    assert load_dispatch_smoke_receipts(tmp_path, DEV4_KEY) is None


def test_smoke_receipt_refusals_raise_on_an_unmapped_release(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        load_dispatch_smoke_receipts(tmp_path, "REL-0.7.0.dev2")


def test_smoke_receipt_refusals_raise_value_error_on_a_bad_file(tmp_path: Path) -> None:
    receipts_file(tmp_path, "{")

    with pytest.raises(ValueError, match=r"dispatch-smoke-receipts\.json is invalid"):
        load_dispatch_smoke_receipts(tmp_path, DEV4_KEY)


def test_smoke_receipt_refusals_name_each_gap_once_in_claim_order() -> None:
    receipts = DispatchSmokeReceiptSet.model_validate(
        {"release_key": DEV4_KEY, "receipts": [receipt("macos", scorecard="fail")]}
    )

    claims = (("claude-code", "windows"), ("claude-code", "macos"), ("claude-code", "windows"))

    assert dispatch_smoke_gaps(receipts, claims, {}) == (
        ("claude-code", "windows", DispatchSmokeGap.MISSING),
        ("claude-code", "macos", DispatchSmokeGap.FAILED),
    )
    assert dispatch_smoke_gaps(None, (("claude-code", "linux"),), {}) == (
        ("claude-code", "linux", DispatchSmokeGap.MISSING),
    )


def test_smoke_receipt_refusals_match_the_runtime_as_well_as_the_class(tmp_path: Path) -> None:
    receipts = DispatchSmokeReceiptSet.model_validate(
        {"release_key": DEV4_KEY, "receipts": [receipt("linux")]}
    )

    documents = read_dispatch_checks(
        (documents_filed(tmp_path, check_document("RUN-00000001")) / RECEIPTS).parent, receipts
    )

    assert dispatch_smoke_gaps(
        receipts, (("codex", "linux"), ("claude-code", "linux")), documents
    ) == (("codex", "linux", DispatchSmokeGap.MISSING),)


def test_smoke_receipt_refusals_reject_a_repeated_pair_in_the_model() -> None:
    with pytest.raises(ValidationError, match="carry more than one receipt"):
        DispatchSmokeReceiptSet.model_validate(
            {"release_key": DEV4_KEY, "receipts": [receipt(), receipt(scorecard="fail")]}
        )


# --- CR-0507-3: the smoke's budget and its receipt writer ---------------


class _BenchStub(smoke_module.Smoke):
    """A smoke world whose CLI answers each bench check with a canned exit code."""

    def __init__(self, root: Path, exits: dict[str, int]) -> None:
        super().__init__(root=root, repo=root, runtime_dir=root, env={})
        self.exits = exits
        self.argvs: list[tuple[str, ...]] = []
        self.dispatched = list(exits)

    def run_eawf(self, *args: str) -> subprocess.CompletedProcess[str]:
        self.argvs.append(args)
        self.log.append({"argv": ["eawf", *args]})
        code = self.exits[args[2]]
        return subprocess.CompletedProcess(args, code, json.dumps({"check": args[2]}), "")


def checks(passed: dict[str, bool]) -> dict[str, tuple[bool, str]]:
    """Return a stub scorecard per Run key: its verdict beside a check document."""
    return {key: (ok, json.dumps(check_document(key))) for key, ok in passed.items()}


def test_smoke_budget_and_receipt_writer_budget_is_600000() -> None:
    assert smoke_module.PROVIDER_ARGS == ("--provider", "claude-code", "--budget", "600000")
    assert smoke_module.SMOKE_RUNTIME.runtime_id == "claude-code"


@pytest.mark.parametrize(
    ("provider", "runtime_id", "profile_id", "model", "prefix"),
    [
        ("stub", "claude-code", "claude-default", "claude-haiku-4-5", "dispatch-check-"),
        ("claude", "claude-code", "claude-default", "claude-haiku-4-5", "dispatch-check-"),
        ("codex", "codex", "codex-default", "gpt-6.1-sol", "dispatch-check-codex-"),
    ],
)
def test_smoke_runtime_maps_each_provider(
    provider: str, runtime_id: str, profile_id: str, model: str, prefix: str
) -> None:
    row = smoke_module.smoke_runtime(provider)

    assert (row.runtime_id, row.profile_id, row.model, row.check_prefix) == (
        runtime_id,
        profile_id,
        model,
        prefix,
    )


def test_smoke_runtime_models_are_ones_the_profile_lists() -> None:
    """A narrowed profile may only keep a model the built-in profile already allows."""
    from eawf.kernel.config.defaults import built_in_defaults

    listed = {
        profile["profile_id"]: profile["model_policy"]["allowed"]
        for profile in built_in_defaults()["runtime"]["profiles"]
    }
    for row in smoke_module.SMOKE_RUNTIMES.values():
        assert row.model in listed[row.profile_id], row


def test_smoke_runtime_names_each_signal_provider() -> None:
    """Every runtime the provider row tells an operator to smoke has a smoke runtime."""
    from eawf.workflow.release.signal_probes import _SMOKE_PROVIDERS

    for runtime_id, provider in _SMOKE_PROVIDERS.items():
        assert smoke_module.smoke_runtime(provider).runtime_id == runtime_id


@pytest.mark.parametrize("provider", ["", "Claude", "codex ", "claude-code", "opencode"])
def test_smoke_runtime_refuses_an_unknown_provider(provider: str) -> None:
    with pytest.raises(ValueError, match="names no smoke runtime; name one of claude, codex, stub"):
        smoke_module.smoke_runtime(provider)


def test_smoke_budget_and_receipt_writer_files_codex_beside_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both runtimes' scratch trees key Runs alike, so neither's documents overwrite the other's."""
    smoke_module._file_smoke_receipt(
        tmp_path, checks({"RUN-00000002": True}), os_class="linux", release_key=DEV4_KEY
    )
    monkeypatch.setattr(smoke_module, "SMOKE_RUNTIME", smoke_module.smoke_runtime("codex"))

    written = smoke_module._file_smoke_receipt(
        tmp_path, checks({"RUN-00000002": True}), os_class="linux", release_key=DEV4_KEY
    )

    assert [(r.runtime_id, r.evidence_refs) for r in written.receipts] == [
        ("claude-code", ("dispatch-check-RUN-00000002.json",)),
        ("codex", ("dispatch-check-codex-RUN-00000002.json",)),
    ]
    assert sorted(path.name for path in tmp_path.glob("dispatch-check-*.json")) == [
        "dispatch-check-RUN-00000002.json",
        "dispatch-check-codex-RUN-00000002.json",
    ]
    documents = read_dispatch_checks(tmp_path, written)
    claims = (("claude-code", "linux"), ("codex", "linux"))
    assert dispatch_smoke_gaps(written, claims, documents) == ()


def test_smoke_budget_and_receipt_writer_maps_each_host_platform() -> None:
    assert smoke_module.OS_CLASSES == {"linux": "linux", "darwin": "macos", "win32": "windows"}


def test_smoke_budget_and_receipt_writer_runs_the_fixture_check_per_run(tmp_path: Path) -> None:
    world = _BenchStub(tmp_path, {"RUN-00000001": 0, "RUN-00000002": 2})

    result = smoke_module._dispatch_checks(world)

    assert world.argvs == [
        ("bench", "dispatch", "RUN-00000001", "--check", "--tier", "fixture"),
        ("bench", "dispatch", "RUN-00000002", "--check", "--tier", "fixture"),
    ]
    assert {key: ok for key, (ok, _doc) in result.items()} == {
        "RUN-00000001": True,
        "RUN-00000002": False,
    }
    assert json.loads(result["RUN-00000002"][1]) == {"check": "RUN-00000002"}


def test_smoke_budget_and_receipt_writer_refuses_an_unreadable_run(tmp_path: Path) -> None:
    """Exit 1 means the Run could not be scored, which is neither pass nor fail."""
    with pytest.raises(AssertionError):
        smoke_module._dispatch_checks(_BenchStub(tmp_path, {"RUN-00000001": 1}))


def test_smoke_budget_and_receipt_writer_files_a_passing_receipt(tmp_path: Path) -> None:
    out = tmp_path / "evidence"

    written = smoke_module._file_smoke_receipt(
        out,
        checks({"RUN-00000001": True, "RUN-00000002": True}),
        os_class="linux",
        release_key=DEV4_KEY,
    )

    assert read_dispatch_smoke_receipts(out / DISPATCH_SMOKE_RECEIPTS_FILENAME) == written
    (row,) = written.receipts
    assert (row.os_class, row.runtime_id, row.scorecard) == ("linux", "claude-code", "pass")
    assert row.run_refs == (
        "eawf://SMOKE/SMOKE/SMOKE/run/RUN-00000001",
        "eawf://SMOKE/SMOKE/SMOKE/run/RUN-00000002",
    )
    assert row.evidence_refs == (
        "dispatch-check-RUN-00000001.json",
        "dispatch-check-RUN-00000002.json",
    )
    check = json.loads((out / "dispatch-check-RUN-00000002.json").read_text(encoding="utf-8"))
    assert check == check_document("RUN-00000002")


def test_smoke_budget_and_receipt_writer_fails_on_one_breached_run(tmp_path: Path) -> None:
    written = smoke_module._file_smoke_receipt(
        tmp_path,
        checks({"RUN-00000001": True, "RUN-00000002": False}),
        os_class="macos",
        release_key=DEV4_KEY,
    )

    assert [r.scorecard for r in written.receipts] == ["fail"]


def test_smoke_budget_and_receipt_writer_keeps_the_other_class(tmp_path: Path) -> None:
    """The Linux and macOS smokes file into one set; a rerun replaces its own row."""
    smoke_module._file_smoke_receipt(
        tmp_path, checks({"RUN-00000001": True}), os_class="macos", release_key=DEV4_KEY
    )
    smoke_module._file_smoke_receipt(
        tmp_path, checks({"RUN-00000002": False}), os_class="linux", release_key=DEV4_KEY
    )

    written = smoke_module._file_smoke_receipt(
        tmp_path, checks({"RUN-00000003": True}), os_class="linux", release_key=DEV4_KEY
    )

    assert [(r.os_class, r.scorecard, r.run_refs[0][-12:]) for r in written.receipts] == [
        ("macos", "pass", "RUN-00000001"),
        ("linux", "pass", "RUN-00000003"),
    ]


def test_smoke_budget_and_receipt_writer_written_set_passes_the_row(tmp_path: Path) -> None:
    root = certified(tmp_path)
    directory = root.joinpath(*CANARY_EVIDENCE_DIRS[DEV4_KEY])
    for os_class in ("linux", "macos"):
        smoke_module._file_smoke_receipt(
            directory, checks({"RUN-00000001": True}), os_class=os_class, release_key=DEV4_KEY
        )

    assert probe_at(root).status is ReleaseSignalStatus.PASS


def test_smoke_budget_and_receipt_writer_refuses_another_release(tmp_path: Path) -> None:
    smoke_module._file_smoke_receipt(
        tmp_path, checks({"RUN-00000001": True}), os_class="linux", release_key=DEV4_KEY
    )

    with pytest.raises(ValueError, match=f"was filed for {DEV4_KEY}"):
        smoke_module._file_smoke_receipt(
            tmp_path,
            checks({"RUN-00000001": True}),
            os_class="linux",
            release_key="REL-0.7.0.dev3",
        )


def test_smoke_budget_and_receipt_writer_refuses_an_unknown_class(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        smoke_module._file_smoke_receipt(
            tmp_path, checks({"RUN-00000001": True}), os_class="beos", release_key=DEV4_KEY
        )


def test_smoke_budget_and_receipt_writer_refuses_no_runs(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        smoke_module._file_smoke_receipt(tmp_path, {}, os_class="linux", release_key=DEV4_KEY)


def test_smoke_budget_and_receipt_writer_is_wired_into_the_live_smoke() -> None:
    source = inspect.getsource(smoke_module.test_smoke_milestone)

    assert 'if PROVIDER != "stub":' in source
    assert "_file_smoke_receipt(" in source
    assert "_dispatch_checks(world)" in source
    assert "os_class=OS_CLASSES[sys.platform]" in source


# --- CR-0507-4: the release-process preflight step ----------------------


def release_process_block() -> dict[str, Any]:
    """Return the ``release-process`` render block of the core profile."""
    core = REPO_ROOT / "src" / "eawf" / "platform" / "profiles" / "data" / "core.yaml"
    document: dict[str, Any] = yaml.safe_load(core.read_text(encoding="utf-8"))
    (block,) = [b for b in document["render_blocks"] if b["id"] == "release-process"]
    found: dict[str, Any] = block
    return found


def test_release_process_bullet_is_in_the_profile_block() -> None:
    block = release_process_block()

    assert block["version"] == "2.1"
    bullets = [line for line in block["body_template"].splitlines() if line.startswith("- ")]
    (smoke,) = [line for line in bullets if "dispatch smoke" in line]
    assert "Before cutting a release candidate, run the live dispatch smoke on Linux (bwrap) " in (
        smoke
    )
    assert "and on macOS (seatbelt)" in smoke
    assert f"``{SMOKE_COMMAND}``" in smoke
    assert smoke.endswith("and commit its ``dispatch-smoke-receipts.json``.")


def test_release_process_bullet_is_in_the_rendered_page() -> None:
    page = (REPO_ROOT / "docs" / "rules" / "release-process.md").read_text(encoding="utf-8")
    (smoke,) = [
        line
        for line in release_process_block()["body_template"].splitlines()
        if "dispatch smoke" in line
    ]

    assert "id=release-process version=2.1 " in page
    assert smoke in page.splitlines()


# --- each pass receipt is re-checked against its check documents --------


def test_a_receipt_whose_check_documents_are_missing_fails_the_gate(tmp_path: Path) -> None:
    """A hand-written pass receipt with no document beside it backs nothing."""
    two_runs = [
        "eawf://SMOKE/SMOKE/SMOKE/run/RUN-00000001",
        "eawf://SMOKE/SMOKE/SMOKE/run/RUN-00000002",
    ]
    cases = {
        "no-document": ((), receipt()),
        "no-ref": ((check_document("RUN-00000001"),), receipt(evidence_refs=[])),
        "one-of-two": (
            (check_document("RUN-00000001"), check_document("RUN-00000002")),
            receipt(run_refs=two_runs),
        ),
        "outside-the-directory": (
            (check_document("RUN-00000001"),),
            receipt(evidence_refs=["nested/dispatch-check-RUN-00000001.json"]),
        ),
    }
    for name, (documents, row) in cases.items():
        root = certified(tmp_path / name, ["linux"])
        receipts_file(
            documents_filed(root, *documents), {"release_key": DEV4_KEY, "receipts": [row]}
        )

        outcome = probe_at(root)

        assert outcome.status is ReleaseSignalStatus.FAIL, name
        assert "claude-code on linux: dispatch_smoke_check_missing" in outcome.remediation, name
    assert DispatchSmokeGap.CHECK_MISSING.value == "dispatch_smoke_check_missing"


def test_a_check_document_with_a_breach_fails_the_gate(tmp_path: Path) -> None:
    breached = probe_at(
        receipts_file(
            documents_filed(
                certified(tmp_path / "breach", ["linux"]),
                check_document("RUN-00000001", turns=26),
            ),
            {"release_key": DEV4_KEY, "receipts": [receipt()]},
        )
    )
    other_run = certified(tmp_path / "other-run", ["linux"])
    (other_run / RECEIPTS).with_name("dispatch-check-RUN-00000001.json").write_text(
        json.dumps(check_document("RUN-00000009")), encoding="utf-8"
    )
    mismatched = probe_at(
        receipts_file(other_run, {"release_key": DEV4_KEY, "receipts": [receipt()]})
    )

    assert breached.status is ReleaseSignalStatus.FAIL
    assert "claude-code on linux: dispatch_smoke_check_breached" in breached.remediation
    assert mismatched.status is ReleaseSignalStatus.FAIL
    assert "claude-code on linux: dispatch_smoke_check_mismatched" in mismatched.remediation


def test_a_receipt_naming_one_document_twice_is_mismatched(tmp_path: Path) -> None:
    root = documents_filed(certified(tmp_path, ["linux"]), check_document("RUN-00000001"))
    row = receipt(
        run_refs=[
            "eawf://SMOKE/SMOKE/SMOKE/run/RUN-00000001",
            "eawf://SMOKE/SMOKE/SMOKE/run/RUN-00000002",
        ],
        evidence_refs=["dispatch-check-RUN-00000001.json"] * 2,
    )

    outcome = probe_at(receipts_file(root, {"release_key": DEV4_KEY, "receipts": [row]}))

    assert "claude-code on linux: dispatch_smoke_check_mismatched" in outcome.remediation


def _invalid(document: dict[str, Any], change: str) -> dict[str, Any]:
    """Return *document* broken in the way *change* names."""
    body = document["check"]
    if change == "extra-key":
        document["extra"] = 1
    elif change == "row-extra-key":
        body["checks"][0]["note"] = "x"
    elif change == "scorecard-extra-key":
        body["scorecard"]["host"] = "x"
    elif change == "bad-run-key":
        body["scorecard"]["run_key"] = "RUN-1"
    elif change == "unlisted-breach":
        body["checks"][0]["breached"] = True
    elif change == "missing-check":
        del document["check"]
    return document


@pytest.mark.parametrize(
    "change",
    [
        "extra-key",
        "row-extra-key",
        "scorecard-extra-key",
        "bad-run-key",
        "unlisted-breach",
        "missing-check",
        "not-json",
    ],
)
def test_a_check_document_that_does_not_validate_fails_the_gate(
    tmp_path: Path, change: str
) -> None:
    root = receipts_file(
        certified(tmp_path, ["linux"]), {"release_key": DEV4_KEY, "receipts": [receipt()]}
    )
    path = (root / RECEIPTS).with_name("dispatch-check-RUN-00000001.json")
    text = (
        "{"
        if change == "not-json"
        else json.dumps(_invalid(check_document("RUN-00000001"), change))
    )
    path.write_text(text, encoding="utf-8")

    outcome = probe_at(root)

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert "does not read back" in outcome.remediation
    assert "dispatch-check-RUN-00000001.json is invalid" in outcome.remediation
    with pytest.raises(ValueError, match=r"dispatch-check-RUN-00000001\.json is invalid"):
        read_dispatch_checks(path.parent, load_dispatch_smoke_receipts(root, DEV4_KEY))


def test_check_documents_are_read_only_for_passing_receipts(tmp_path: Path) -> None:
    """A failing receipt is already a gap, so its documents are not opened."""
    root = receipts_file(
        tmp_path, {"release_key": DEV4_KEY, "receipts": [receipt(scorecard="fail")]}
    )
    (root / RECEIPTS).with_name("dispatch-check-RUN-00000001.json").write_text("{", "utf-8")

    assert read_dispatch_checks((root / RECEIPTS).parent, None) == {}
    assert (
        read_dispatch_checks((root / RECEIPTS).parent, load_dispatch_smoke_receipts(root, DEV4_KEY))
        == {}
    )


def test_the_remediation_names_the_evidence_dir_and_provider(tmp_path: Path) -> None:
    directory = "/".join(CANARY_EVIDENCE_DIRS[DEV4_KEY])
    outcome = probe_at(certified(tmp_path))

    assert outcome.status is ReleaseSignalStatus.FAIL
    assert (
        f"EAWF_SMOKE_PROVIDER=claude EAWF_SMOKE_EVIDENCE_DIR={directory} "
        "uv run pytest -m e2e tests/e2e/test_smoke_milestone.py" in outcome.remediation
    )
    assert outcome.remediation.count("EAWF_SMOKE_EVIDENCE_DIR=") == 1
    codex = smoke_remediation((("codex", "linux"), ("claude-code", "macos")), DEV4_KEY)
    assert f"EAWF_SMOKE_PROVIDER=codex EAWF_SMOKE_EVIDENCE_DIR={directory} " in codex
    assert f"EAWF_SMOKE_PROVIDER=claude EAWF_SMOKE_EVIDENCE_DIR={directory} " in codex
    unsmoked = smoke_remediation((("opencode", "linux"),), DEV4_KEY)
    assert "no live smoke dispatches to opencode" in unsmoked
    assert "EAWF_SMOKE_PROVIDER=" not in unsmoked


@pytest.mark.parametrize("version", ("0.7.0rc3", "0.7.0rc4"))
def test_the_committed_candidate_receipts_verify(version: str) -> None:
    """Each candidate's own receipt backs its Linux Claude Code claim."""
    key = f"REL-{version}"
    directory = REPO_ROOT.joinpath(*CANARY_EVIDENCE_DIRS[key])
    receipts = load_dispatch_smoke_receipts(REPO_ROOT, key)
    assert receipts is not None
    documents = read_dispatch_checks(directory, receipts)
    (row,) = receipts.receipts
    config = checkpoint_release_config(version, repo_root=REPO_ROOT)

    outcome = provider_probe(
        ReleaseSignalContext(config, ReleaseSignalName.PROVIDER, None), repo_root=REPO_ROOT
    )

    assert (row.os_class, row.runtime_id, row.scorecard) == ("linux", "claude-code", "pass")
    assert sorted(documents) == [f"dispatch-check-RUN-0000000{n}.json" for n in range(2, 8)]
    assert all(not document.check.breaches for document in documents.values())
    assert dispatch_smoke_gaps(receipts, (("claude-code", "linux"),), documents) == ()
    assert outcome.status is ReleaseSignalStatus.PASS, outcome.remediation


def test_the_committed_stable_receipts_verify() -> None:
    """The stable receipt set backs both Linux claims, claude-code and codex."""
    key = "REL-0.7.0"
    directory = REPO_ROOT.joinpath(*CANARY_EVIDENCE_DIRS[key])
    receipts = load_dispatch_smoke_receipts(REPO_ROOT, key)
    assert receipts is not None
    documents = read_dispatch_checks(directory, receipts)
    config = checkpoint_release_config("0.7.0", repo_root=REPO_ROOT)

    outcome = provider_probe(
        ReleaseSignalContext(config, ReleaseSignalName.PROVIDER, None), repo_root=REPO_ROOT
    )

    assert sorted((r.os_class, r.runtime_id, r.scorecard) for r in receipts.receipts) == [
        ("linux", "claude-code", "pass"),
        ("linux", "codex", "pass"),
    ]
    assert len(documents) == 2 * smoke_module.BATCHES * smoke_module.TASKS_PER_BATCH
    claims = (("claude-code", "linux"), ("codex", "linux"))
    assert dispatch_smoke_gaps(receipts, claims, documents) == ()
    assert outcome.status is ReleaseSignalStatus.PASS, outcome.remediation


def _forged(change: str) -> dict[str, Any]:
    """Return the committed rc3 RUN-00000002 document altered as *change* names.

    Each alteration keeps the breach list in step with the rows, so the
    document validates and only recomputing its checks can refuse it.
    """
    directory = REPO_ROOT.joinpath(*CANARY_EVIDENCE_DIRS[RC3_KEY])
    document: dict[str, Any] = json.loads(
        (directory / "dispatch-check-RUN-00000002.json").read_text(encoding="utf-8")
    )
    body = document["check"]
    rows = {row["metric"]: row for row in body["checks"]}
    if change == "hidden-breach":
        body["scorecard"]["turns"] = 999
        rows["turns"]["observed"] = 999
    elif change == "rows-disagree-with-scorecard":
        body["scorecard"]["turns"] = 999
    elif change == "reviewer-forge":
        body["tier"] = "real"
        body["scorecard"]["status"] = "FAILED"
        body["scorecard"]["turns"] = 999
        body["checks"] = [{"metric": "turns", "observed": 999, "limit": 25, "breached": False}]
    elif change == "real-tier":
        body["tier"] = "real"
    elif change == "status-failed":
        body["scorecard"]["status"] = "FAILED"
        rows["status"].update(observed="FAILED", breached=True)
        body["breaches"] = ["status"]
    elif change == "status-failed-row-says-completed":
        body["scorecard"]["status"] = "FAILED"
    elif change == "missing-metric":
        body["checks"] = [row for row in body["checks"] if row["metric"] != "wall_seconds"]
    elif change == "loosened-limit":
        body["scorecard"]["turns"] = 30
        rows["turns"].update(observed=30, limit=1000)
    elif change == "loosened-rate-limit":
        body["scorecard"]["read_truncation_rate"] = "0.5"
        rows["read_truncation_rate"].update(observed="0.5", limit="0.90")
    return document


@pytest.mark.parametrize(
    ("change", "gap"),
    [
        pytest.param("hidden-breach", DispatchSmokeGap.CHECK_FORGED, id="hidden-breach"),
        pytest.param(
            "rows-disagree-with-scorecard", DispatchSmokeGap.CHECK_FORGED, id="observed-mismatch"
        ),
        pytest.param("reviewer-forge", DispatchSmokeGap.CHECK_FORGED, id="reviewer-forge"),
        pytest.param("real-tier", DispatchSmokeGap.CHECK_FORGED, id="non-fixture-tier"),
        pytest.param("status-failed", DispatchSmokeGap.CHECK_BREACHED, id="not-completed"),
        pytest.param(
            "status-failed-row-says-completed",
            DispatchSmokeGap.CHECK_FORGED,
            id="not-completed-row-hidden",
        ),
        pytest.param("missing-metric", DispatchSmokeGap.CHECK_FORGED, id="missing-metric"),
        pytest.param("loosened-limit", DispatchSmokeGap.CHECK_FORGED, id="loosened-limit"),
        pytest.param(
            "loosened-rate-limit", DispatchSmokeGap.CHECK_FORGED, id="loosened-decimal-limit"
        ),
    ],
)
def test_a_document_whose_rows_hide_a_breach_fails_the_gate(
    tmp_path: Path, change: str, gap: DispatchSmokeGap
) -> None:
    """A pass receipt counts only when its checks recompute clean on the fixture tier."""
    row = receipt(
        run_refs=["eawf://EAWF/EAWF/EAWF/run/RUN-00000002"],
        evidence_refs=["dispatch-check-RUN-00000002.json"],
    )
    receipts = DispatchSmokeReceiptSet.model_validate({"release_key": RC3_KEY, "receipts": [row]})
    claims: tuple[tuple[str, OsClass], ...] = (("claude-code", "linux"),)
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "dispatch-check-RUN-00000002.json").write_text(
        json.dumps(_forged("none")), encoding="utf-8"
    )
    (tmp_path / "dispatch-check-RUN-00000002.json").write_text(
        json.dumps(_forged(change)), encoding="utf-8"
    )

    clean_gaps = dispatch_smoke_gaps(receipts, claims, read_dispatch_checks(clean, receipts))
    gaps = dispatch_smoke_gaps(receipts, claims, read_dispatch_checks(tmp_path, receipts))

    assert clean_gaps == ()
    assert gaps == (("claude-code", "linux", gap),)
    assert DispatchSmokeGap.CHECK_FORGED.value == "dispatch_smoke_check_forged"


@pytest.mark.parametrize(
    ("scorecard_key", "metric", "nullable"),
    [
        pytest.param("turns", "turns", False, id="turns"),
        pytest.param("read_truncation_rate", "read_truncation_rate", False, id="truncation"),
        pytest.param("identical_reread_rate", "identical_reread_rate", False, id="reread"),
        pytest.param("billable_tokens", "billable_tokens", True, id="total-tokens"),
        pytest.param("first_patch_seconds", "first_patch_seconds", True, id="first-patch"),
        pytest.param("wall_seconds", "wall_seconds", True, id="wall-seconds"),
    ],
)
def test_a_document_with_a_null_fixture_metric_fails_the_gate(
    tmp_path: Path, scorecard_key: str, metric: str, nullable: bool
) -> None:
    """A document that leaves a fixture-limited metric null backs no Run.

    The row is nulled with the scorecard, so the rows still recompute and
    no breach shows; only the completeness check can refuse it. A metric
    the scorecard model never allows null is refused when it is read.
    """
    row = receipt(
        run_refs=["eawf://EAWF/EAWF/EAWF/run/RUN-00000002"],
        evidence_refs=["dispatch-check-RUN-00000002.json"],
    )
    receipts = DispatchSmokeReceiptSet.model_validate({"release_key": RC3_KEY, "receipts": [row]})
    document = _forged("none")
    body = document["check"]
    body["scorecard"][scorecard_key] = None
    (filed,) = [check for check in body["checks"] if check["metric"] == metric]
    filed["observed"] = None
    (tmp_path / "dispatch-check-RUN-00000002.json").write_text(
        json.dumps(document), encoding="utf-8"
    )

    if not nullable:
        with pytest.raises(ValueError, match=r"dispatch-check-RUN-00000002\.json is invalid"):
            read_dispatch_checks(tmp_path, receipts)
        return
    gaps = dispatch_smoke_gaps(
        receipts, (("claude-code", "linux"),), read_dispatch_checks(tmp_path, receipts)
    )

    assert gaps == (("claude-code", "linux", DispatchSmokeGap.CHECK_INCOMPLETE),)
    assert DispatchSmokeGap.CHECK_INCOMPLETE.value == "dispatch_smoke_check_incomplete"
