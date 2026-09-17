"""The ``0.7.0.dev3`` configuration: rendered, then resolved per record.

``dev3`` is the first rung whose configuration cannot be complete on its
own. Everything the train knows about it renders from the template --
the version, the channel, the epoch and the fifteen ``native_canary``
gates -- but which acceptance bundles it accepts is a fact about the
record being cut, and the train has nowhere to read that from. So the
rendered document carries no ``membership_refs`` and the rung requires
them, which makes the two halves meet at exactly one place: the
resolver.

These tests pin that seam from both sides. Resolved with the record's
refs, the configuration loads. Resolved without them, it is refused with
``invalid_membership_cardinality`` rather than loading a checkpoint that
claims a canary Milestone nobody named.

The last three tests are a regression guard rather than a claim about
``dev3``. A rendered document that shifted under an approval would
invalidate the sweep that read it, so the ``dev2`` text stays pinned --
but no longer byte-identical to the document the approval was swept
against, and that narrowing is deliberate. The manifest freeze reads
each target's registry identity off the configuration, and the ``dev2``
approval pins a manifest carrying all three identities, so the field has
to be present at this rung: a ``dev2`` configuration without it cannot
freeze its own approved manifest at all.

What replaces the byte pin is narrower and says more. The identity lines
are the *only* difference from the approved document, which the middle
test proves by removing them and recovering the approved digest exactly.
Everything the sweep read is therefore still frozen; what moved is one
additive field the sweep never looked at.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from eawf.kernel.release.checkpoint_template import (
    render_checkpoint_config,
    with_membership_refs,
)
from eawf.kernel.release.gate_binding import profile_gates
from eawf.kernel.spec.release import Release, ReleaseChannel, ReleaseGateProfile
from eawf.kernel.spec.release_config import parse_release_config
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.release_context import resolve_config
from eawf.workflow.release.train import (
    DEV2_RELEASE_CONFIG_YAML,
    DEV3_RELEASE_CONFIG_YAML,
    V07_CONFIG_TEMPLATE,
    V07_TRAIN,
    checkpoint_config_yaml,
)
from tests._release_helpers import release_record

#: The checkpoint under test.
DEV3_VERSION = "0.7.0.dev3"

#: The next rung, whose profile is still unauthored.
DEV4_VERSION = "0.7.0.dev4"

#: The acceptance bundle a ``dev3`` record names.
MEMBERSHIP_REF = "milestone://epoch2/native-canary"

#: sha256 of the rendered ``dev2`` configuration text as it stands now.
#: Pinned rather than recomputed, so a later edit to the train template
#: has to come through here and say what it moved.
DEV2_CONFIG_DIGEST = (
    "5e3d6dd2f93d7510e57624c6b1280dab43b02cdaca745d45a8ff1e0523651a38"  # pragma: allowlist secret
)

#: sha256 of that same text with the target ``identity`` lines removed,
#: which is the document the ``dev2`` record was approved against. The
#: two digests together localise any drift: a moved identity trips only
#: the first, anything else trips both.
DEV2_CONFIG_AS_APPROVED_DIGEST = (
    "d9a94643dd08786c92a99fa6a89d0ba8aab8bd29f8b773aeee2facfa09102f3f"  # pragma: allowlist secret
)

#: The registry identity each ``dev2`` target is observed against. These
#: three names are inputs to the frozen manifest the approved record
#: pins, so they are values rather than presence checks.
DEV2_TARGET_IDENTITIES = {
    "pypi": "eawf",
    "npm": "@elementarno/eawf",
    "github": "Elementarno9/eawf",
}


def dev3_record(**overrides: Any) -> Release:
    """Return a ``dev3`` candidate carrying one acceptance bundle."""
    payload: dict[str, Any] = {
        "key": "REL-0.7.0.dev3",
        "version": DEV3_VERSION,
        "channel": ReleaseChannel.DEV,
        "authority_epoch": 2,
        "membership_refs": (MEMBERSHIP_REF,),
    }
    payload.update(overrides)
    return release_record(**payload)


# --- what the template renders ---------------------------------------


def test_dev3_config_is_rendered_from_the_train_template() -> None:
    rendered = render_checkpoint_config(
        rung=V07_TRAIN.checkpoint_for_version(DEV3_VERSION),
        template=V07_CONFIG_TEMPLATE,
    )
    assert rendered == DEV3_RELEASE_CONFIG_YAML
    assert checkpoint_config_yaml(DEV3_VERSION) == DEV3_RELEASE_CONFIG_YAML


def test_dev3_config_declares_the_native_canary_profile() -> None:
    config = parse_release_config(DEV3_RELEASE_CONFIG_YAML)
    assert config.gates.profile is ReleaseGateProfile.NATIVE_CANARY
    assert config.authority_epoch == 2
    assert config.version == DEV3_VERSION


def test_dev3_config_requires_the_fifteen_native_canary_gates() -> None:
    config = parse_release_config(DEV3_RELEASE_CONFIG_YAML)
    assert config.gates.required == profile_gates(ReleaseGateProfile.NATIVE_CANARY)
    assert len(config.gates.required) == 15


def test_dev3_config_carries_no_membership_refs_of_its_own() -> None:
    """The template cannot know them, so the rendered document leaves them empty."""
    assert parse_release_config(DEV3_RELEASE_CONFIG_YAML).membership_refs == ()


# --- resolving it against a record -----------------------------------


def test_resolve_config_loads_dev3_with_the_records_membership_refs() -> None:
    record = dev3_record()
    config = resolve_config(record.version, membership_refs=record.membership_refs)
    assert config.membership_refs == (MEMBERSHIP_REF,)
    assert config.gates.profile is ReleaseGateProfile.NATIVE_CANARY


def test_resolve_config_carries_every_ref_the_record_names() -> None:
    record = dev3_record(membership_refs=(MEMBERSHIP_REF, "milestone://epoch2/native-canary-2"))
    config = resolve_config(record.version, membership_refs=record.membership_refs)
    assert config.membership_refs == record.membership_refs


def test_resolve_config_refuses_dev3_without_membership_refs() -> None:
    with pytest.raises(DaemonValidationError) as excinfo:
        resolve_config(DEV3_VERSION)
    assert "invalid_membership_cardinality" in str(excinfo.value)


def test_resolve_config_refuses_dev3_with_an_empty_membership_tuple() -> None:
    """An empty list is the same absence as no argument at all."""
    with pytest.raises(DaemonValidationError) as excinfo:
        resolve_config(DEV3_VERSION, membership_refs=())
    assert "invalid_membership_cardinality" in str(excinfo.value)


def test_resolve_config_still_loads_dev2_without_membership_refs() -> None:
    config = resolve_config("0.7.0.dev2")
    assert config.membership_refs == ()
    assert config.gates.profile is ReleaseGateProfile.DEV2


def test_resolve_config_refuses_dev2_carrying_membership_refs() -> None:
    """The epoch-1 rungs forbid bundles outright, so the same code fires."""
    with pytest.raises(DaemonValidationError) as excinfo:
        resolve_config("0.7.0.dev2", membership_refs=(MEMBERSHIP_REF,))
    assert "invalid_membership_cardinality" in str(excinfo.value)


def test_resolve_config_refuses_a_rung_with_no_authored_configuration() -> None:
    with pytest.raises(DaemonValidationError) as excinfo:
        resolve_config(DEV4_VERSION, membership_refs=(MEMBERSHIP_REF,))
    assert "no release configuration" in str(excinfo.value)


# --- the overlay itself ----------------------------------------------


def test_with_membership_refs_leaves_the_rest_of_the_document_alone() -> None:
    overlaid = with_membership_refs(DEV3_RELEASE_CONFIG_YAML, membership_refs=[MEMBERSHIP_REF])
    rendered = parse_release_config(DEV3_RELEASE_CONFIG_YAML)
    resolved = parse_release_config(overlaid)
    assert resolved.membership_refs == (MEMBERSHIP_REF,)
    assert resolved.model_dump(exclude={"membership_refs"}) == rendered.model_dump(
        exclude={"membership_refs"}
    )


def test_with_membership_refs_rejects_a_document_that_is_not_a_mapping() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        with_membership_refs("- a\n- b\n", membership_refs=[MEMBERSHIP_REF])


def test_with_membership_refs_rejects_a_document_with_no_release_block() -> None:
    with pytest.raises(ValueError, match="'release' mapping"):
        with_membership_refs("version: 0.7.0.dev3\n", membership_refs=[MEMBERSHIP_REF])


# --- the dev2 text the approved record was swept against -------------


def without_identity_lines(config_text: str) -> str:
    """Return *config_text* with every target ``identity`` line dropped.

    Args:
        config_text: Rendered checkpoint configuration YAML.

    Returns:
        The same text minus the identity rows, which is the document as
        it stood before the field existed. Any other added field stays,
        so the caller's equality still fails on it.
    """
    return "".join(
        line
        for line in config_text.splitlines(keepends=True)
        if not line.lstrip().startswith("identity:")
    )


def test_rendered_dev2_config_keeps_its_pinned_digest() -> None:
    digest = hashlib.sha256(DEV2_RELEASE_CONFIG_YAML.encode("utf-8")).hexdigest()
    assert digest == DEV2_CONFIG_DIGEST


def test_rendered_dev2_config_moved_only_by_its_target_identities() -> None:
    """Drop the identity lines and the approved document comes back."""
    as_approved = without_identity_lines(DEV2_RELEASE_CONFIG_YAML)
    digest = hashlib.sha256(as_approved.encode("utf-8")).hexdigest()
    assert digest == DEV2_CONFIG_AS_APPROVED_DIGEST


def test_rendered_dev2_config_declares_the_identity_of_every_target() -> None:
    """Without these the rung could not freeze the manifest it approved."""
    config = parse_release_config(DEV2_RELEASE_CONFIG_YAML)
    assert {target.target_id: target.identity for target in config.targets} == (
        DEV2_TARGET_IDENTITIES
    )
