"""REL-018: the three publication observation adapters.

Under test: every adapter queried against a committed recorded response
and answering with a :class:`PublicationObservation` that names the
externally queried identity, the digests it read back, the digest
pinning the response it read them out of, and an evidence reference
that reconstructs the read-back. The match / missing / mismatch /
unknown quartet is driven for all three adapters off the same fixture
set, so a new adapter cannot ship with only its happy path recorded.

Also under test: the adapter is resolved from the target's declared
``observe_adapter`` and nothing else -- an undeclared adapter name is
refused by the configuration loader, and the implementation table is
total over the adapter enum, so a runtime fallback has nowhere to hide.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.release_config import (
    ObservationAdapter,
    ReleaseConfigError,
    ReleaseConfigRejection,
)
from eawf.workflow.release.adapters import (
    DEFAULT_REGISTRY_READERS,
    OBSERVATION_ADAPTERS,
    UndeclaredObservationAdapterError,
    observe_publication,
    resolve_observe_adapter,
)
from eawf.workflow.release.observation import (
    CODE_RESULTS,
    FrozenArtifact,
    FrozenManifest,
    FrozenTarget,
    ObservationCode,
    ObservationResult,
    PublicationObservation,
    RecordedResponse,
    assert_manifest_binds,
    evidence_reference,
    observation_request,
)
from tests.unit.kernel.release.conftest import (
    ADAPTER_STEMS,
    NOW,
    dev1_config,
    frozen_manifest,
    read_back_request,
    recorded_response,
    release_record,
)

pytestmark = pytest.mark.unit

#: Every configured leg of the ``0.7.0.dev1`` checkpoint.
TARGET_IDS = tuple(ADAPTER_STEMS)

#: The result each recorded case is expected to reach, for every adapter.
CASE_RESULTS = {
    "match": ObservationResult.MATCH,
    "missing": ObservationResult.MISSING,
    "mismatch": ObservationResult.MISMATCH,
    "unknown": ObservationResult.UNKNOWN,
}


def observe(target_id: str, case: str) -> PublicationObservation:
    """Return the observation the recorded *case* supports for *target_id*."""
    return observe_publication(
        read_back_request(target_id),
        response=recorded_response(target_id, case),
        observed_at=NOW,
    )


# --- the four recorded verdicts, for every adapter ------------------------


@pytest.mark.parametrize("target_id", TARGET_IDS)
@pytest.mark.parametrize("case", sorted(CASE_RESULTS))
def test_every_adapter_records_every_verdict(target_id: str, case: str) -> None:
    observation = observe(target_id, case)
    assert observation.result is CASE_RESULTS[case]
    assert CODE_RESULTS[observation.code] is observation.result
    assert observation.detail.strip()


@pytest.mark.parametrize("target_id", TARGET_IDS)
def test_a_match_reports_every_frozen_digest_it_read_back(target_id: str) -> None:
    request = read_back_request(target_id)
    observation = observe(target_id, "match")
    assert observation.code is ObservationCode.MATCHED
    assert dict(observation.observed_digests) == {
        artifact.filename: artifact.digest for artifact in request.artifacts
    }


@pytest.mark.parametrize("target_id", TARGET_IDS)
def test_a_mismatch_names_the_digest_that_disagreed(target_id: str) -> None:
    observation = observe(target_id, "mismatch")
    assert observation.code is ObservationCode.DIGEST_MISMATCH
    assert f"sha256:{'9' * 64}" in observation.detail


@pytest.mark.parametrize("target_id", TARGET_IDS)
def test_a_missing_version_reads_back_no_digests(target_id: str) -> None:
    observation = observe(target_id, "missing")
    assert observation.code is ObservationCode.VERSION_ABSENT
    assert dict(observation.observed_digests) == {}


@pytest.mark.parametrize("target_id", TARGET_IDS)
def test_an_unreachable_registry_settles_nothing(target_id: str) -> None:
    observation = observe(target_id, "unknown")
    assert observation.code is ObservationCode.REGISTRY_UNREACHABLE
    assert observation.conclusive is False
    assert observation.matched is False


# --- the identity and evidence an observation carries --------------------


@pytest.mark.parametrize("target_id", TARGET_IDS)
def test_the_observation_carries_the_externally_queried_identity(target_id: str) -> None:
    request = read_back_request(target_id)
    observation = observe(target_id, "match")
    assert observation.queried_identity == request.identity
    assert observation.target_id == target_id
    assert observation.adapter is request.target.observe_adapter


@pytest.mark.parametrize("target_id", TARGET_IDS)
def test_the_adapter_digest_pins_the_exact_response_read(target_id: str) -> None:
    response = recorded_response(target_id, "match")
    observation = observe(target_id, "match")
    assert observation.adapter_digest == response.digest
    assert observation.evidence_ref == evidence_reference(
        adapter=observation.adapter,
        target_id=target_id,
        identity=observation.queried_identity,
        version=observation.version,
        adapter_digest=response.digest,
    )


@pytest.mark.parametrize("target_id", TARGET_IDS)
def test_the_observation_names_the_manifest_it_compared_against(target_id: str) -> None:
    observation = observe(target_id, "match")
    assert observation.manifest_digest == frozen_manifest().digest


def test_two_responses_that_differ_produce_different_adapter_digests() -> None:
    assert recorded_response("pypi", "match").digest != recorded_response("pypi", "mismatch").digest


# --- the shapes each adapter reads ---------------------------------------


def test_package_index_answering_about_another_project_is_an_identity_mismatch() -> None:
    payload = json.loads(json.dumps(recorded_response("pypi", "match").payload))
    payload["info"]["name"] = "eawf-fork"
    observation = observe_publication(
        read_back_request("pypi"),
        response=RecordedResponse(status=200, payload=payload),
        observed_at=NOW,
    )
    assert observation.code is ObservationCode.IDENTITY_MISMATCH
    assert "eawf-fork" in observation.detail


def test_package_index_exposing_another_version_is_version_absent() -> None:
    payload = json.loads(json.dumps(recorded_response("pypi", "match").payload))
    payload["info"]["version"] = "0.7.0.dev2"
    observation = observe_publication(
        read_back_request("pypi"),
        response=RecordedResponse(status=200, payload=payload),
        observed_at=NOW,
    )
    assert observation.code is ObservationCode.VERSION_ABSENT


def test_package_index_dropping_a_frozen_artifact_is_artifact_absent() -> None:
    payload = json.loads(json.dumps(recorded_response("pypi", "match").payload))
    payload["urls"] = payload["urls"][:1]
    observation = observe_publication(
        read_back_request("pypi"),
        response=RecordedResponse(status=200, payload=payload),
        observed_at=NOW,
    )
    assert observation.code is ObservationCode.ARTIFACT_ABSENT
    assert ".tar.gz" in observation.detail


def test_npm_registry_answering_about_another_package_is_an_identity_mismatch() -> None:
    payload = json.loads(json.dumps(recorded_response("npm", "match").payload))
    payload["name"] = "@other/eawf"
    observation = observe_publication(
        read_back_request("npm"),
        response=RecordedResponse(status=200, payload=payload),
        observed_at=NOW,
    )
    assert observation.code is ObservationCode.IDENTITY_MISMATCH


def test_npm_registry_reads_the_semver_spelling_of_the_checkpoint() -> None:
    payload = json.loads(json.dumps(recorded_response("npm", "match").payload))
    payload["versions"] = {"0.7.0.dev1": payload["versions"]["0.7.0-dev.1"]}
    observation = observe_publication(
        read_back_request("npm"),
        response=RecordedResponse(status=200, payload=payload),
        observed_at=NOW,
    )
    assert observation.code is ObservationCode.VERSION_ABSENT


def test_source_host_release_hanging_off_another_tag_is_an_identity_mismatch() -> None:
    payload = json.loads(json.dumps(recorded_response("github", "match").payload))
    payload["tag_name"] = "v0.7.0.dev2"
    observation = observe_publication(
        read_back_request("github"),
        response=RecordedResponse(status=200, payload=payload),
        observed_at=NOW,
    )
    assert observation.code is ObservationCode.IDENTITY_MISMATCH
    assert "v0.7.0.dev1" in observation.detail


@pytest.mark.parametrize("target_id", TARGET_IDS)
def test_an_unreadable_body_is_unknown_rather_than_a_verdict(target_id: str) -> None:
    observation = observe_publication(
        read_back_request(target_id),
        response=RecordedResponse(status=200, payload=None),
        observed_at=NOW,
    )
    assert observation.code is ObservationCode.RESPONSE_UNREADABLE
    assert observation.result is ObservationResult.UNKNOWN


@pytest.mark.parametrize("target_id", TARGET_IDS)
def test_a_body_with_no_readable_rows_reads_back_no_digests(target_id: str) -> None:
    observation = observe_publication(
        read_back_request(target_id),
        response=RecordedResponse(status=200, payload={"name": "x", "info": None}),
        observed_at=NOW,
    )
    assert observation.result is ObservationResult.MISMATCH


# --- adapter resolution --------------------------------------------------


def test_the_adapter_table_is_total_over_the_declared_adapters() -> None:
    assert set(OBSERVATION_ADAPTERS) == set(ObservationAdapter)
    assert set(DEFAULT_REGISTRY_READERS) == set(ObservationAdapter)


@pytest.mark.parametrize("target_id", TARGET_IDS)
def test_the_adapter_comes_from_the_targets_declaration(target_id: str) -> None:
    target = read_back_request(target_id).target
    assert resolve_observe_adapter(target) is OBSERVATION_ADAPTERS[target.observe_adapter]


def test_an_unimplemented_adapter_raises_rather_than_falling_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = read_back_request("pypi").target
    monkeypatch.setattr(
        "eawf.workflow.release.adapters.OBSERVATION_ADAPTERS",
        {
            adapter: fn
            for adapter, fn in OBSERVATION_ADAPTERS.items()
            if adapter is not ObservationAdapter.PACKAGE_INDEX
        },
    )
    with pytest.raises(UndeclaredObservationAdapterError) as excinfo:
        resolve_observe_adapter(target)
    assert excinfo.value.target_id == "pypi"
    assert excinfo.value.adapter is ObservationAdapter.PACKAGE_INDEX
    assert "undeclared_observation_adapter" in str(excinfo.value)


def test_an_undeclared_adapter_is_refused_at_configuration_load() -> None:
    with pytest.raises(ReleaseConfigError) as excinfo:
        dev1_config(
            targets=[
                {
                    "target_id": "pypi",
                    "artifact_kinds": ["wheel"],
                    "observe_adapter": "crates_registry",
                    "timeout_seconds": 60,
                    "retry_limit": 0,
                }
            ]
        )
    assert excinfo.value.code is ReleaseConfigRejection.MISSING_OBSERVATION_ADAPTER


def test_the_default_reader_reports_an_unreachable_registry() -> None:
    observation = observe_publication(read_back_request("pypi"), observed_at=NOW)
    assert observation.code is ObservationCode.REGISTRY_UNREACHABLE


def test_a_naive_observation_instant_is_refused() -> None:
    with pytest.raises(ValueError, match="observed_at must be timezone-aware"):
        observe_publication(
            read_back_request("pypi"),
            response=recorded_response("pypi", "match"),
            observed_at=datetime(2026, 9, 4, 12, 0),
        )


# --- the frozen manifest binding -----------------------------------------


def test_the_manifest_binds_only_the_release_that_approved_its_digest() -> None:
    manifest = frozen_manifest()
    release = release_record(manifest_digest=manifest.digest)
    assert assert_manifest_binds(release, manifest) is None


def test_a_reforged_manifest_cannot_be_observed_against() -> None:
    manifest = frozen_manifest()
    release = release_record(manifest_digest=f"sha256:{'c' * 64}")
    with pytest.raises(ValueError, match="is not the digest release"):
        assert_manifest_binds(release, manifest)


def test_a_manifest_for_another_release_is_refused() -> None:
    manifest = frozen_manifest()
    other = manifest.model_copy(update={"release_key": "REL-0.7.0.dev2", "version": "0.7.0.dev2"})
    release = release_record(manifest_digest=manifest.digest)
    with pytest.raises(ValueError, match="cannot observe release"):
        assert_manifest_binds(release, other)


def test_a_release_with_no_pinned_manifest_cannot_be_observed() -> None:
    manifest = frozen_manifest()
    draft = release_record(status="draft", manifest_digest=None)
    with pytest.raises(ValueError, match="pins no manifest_digest"):
        assert_manifest_binds(draft, manifest)


def test_the_manifest_digest_changes_when_a_frozen_digest_changes() -> None:
    manifest = frozen_manifest()
    repinned = FrozenManifest.model_validate(
        {
            **manifest.model_dump(mode="json"),
            "targets": {
                **manifest.model_dump(mode="json")["targets"],
                "npm": {
                    "identity": "@elementarno/eawf",
                    "artifacts": [
                        {
                            "kind": "codex_plugin",
                            "filename": "eawf-0.7.0-dev.1.tgz",
                            "digest": f"sha256:{'7' * 64}",
                        }
                    ],
                },
            },
        }
    )
    assert repinned.digest != manifest.digest


def test_a_manifest_key_that_contradicts_its_version_is_refused() -> None:
    with pytest.raises(ValidationError, match=re.escape("must be 'REL-0.7.0.dev1'")):
        FrozenManifest(
            release_key="REL-0.7.0.dev2",
            version="0.7.0.dev1",
            targets={
                "pypi": FrozenTarget(
                    identity="eawf",
                    artifacts=(
                        FrozenArtifact(kind="wheel", filename="w.whl", digest=f"sha256:{'1' * 64}"),
                    ),
                )
            },
        )


def test_a_manifest_target_with_no_artifacts_is_refused() -> None:
    with pytest.raises(ValidationError):
        FrozenTarget(identity="eawf", artifacts=())


def test_a_manifest_target_pinning_one_filename_twice_is_refused() -> None:
    artifact = FrozenArtifact(kind="wheel", filename="w.whl", digest=f"sha256:{'1' * 64}")
    with pytest.raises(ValidationError, match="more than once"):
        FrozenTarget(identity="eawf", artifacts=(artifact, artifact))


def test_a_manifest_with_no_targets_is_refused() -> None:
    with pytest.raises(ValidationError):
        FrozenManifest(release_key="REL-0.7.0.dev1", version="0.7.0.dev1", targets={})


def test_a_request_for_an_unconfigured_target_raises() -> None:
    with pytest.raises(KeyError, match="configures no target"):
        observation_request(dev1_config(), frozen_manifest(), target_id="crates")


def test_a_request_for_an_unfrozen_target_raises() -> None:
    manifest = frozen_manifest()
    payload = manifest.model_dump(mode="json")
    payload["targets"].pop("npm")
    with pytest.raises(KeyError, match="freezes no target"):
        observation_request(dev1_config(), FrozenManifest.model_validate(payload), target_id="npm")


def test_a_manifest_for_another_version_is_refused_at_request_time() -> None:
    manifest = frozen_manifest()
    other = manifest.model_copy(update={"release_key": "REL-0.7.0.dev2", "version": "0.7.0.dev2"})
    with pytest.raises(ValueError, match="manifest freezes version"):
        observation_request(dev1_config(), other, target_id="pypi")


def test_an_observation_whose_result_contradicts_its_code_is_refused() -> None:
    payload = observe("pypi", "match").model_dump(mode="json")
    payload["result"] = ObservationResult.MISMATCH.value
    with pytest.raises(ValidationError, match="disagrees with code"):
        PublicationObservation.model_validate(payload)


def test_an_observation_with_a_blank_detail_is_refused() -> None:
    payload = observe("pypi", "match").model_dump(mode="json")
    payload["detail"] = ""
    with pytest.raises(ValidationError):
        PublicationObservation.model_validate(payload)


def test_an_observation_rejects_an_unknown_key() -> None:
    payload = observe("pypi", "match").model_dump(mode="json")
    payload["confidence"] = 1.0
    with pytest.raises(ValidationError):
        PublicationObservation.model_validate(payload)


def test_a_recorded_response_rejects_an_out_of_range_status() -> None:
    with pytest.raises(ValidationError):
        RecordedResponse(status=999)
