"""REL-024: the durable-effect boundary set is total.

Under test: every function in ``eawf.workflow.release`` that writes
durably carrying a boundary id; the AST scan refusing a seeded write
site that carries none; a marker naming something that is not a
:class:`~eawf.workflow.release.boundaries.PublicationBoundary` member;
and the reverse direction -- a registered in-package boundary that no
site claims, which is a boundary nobody is exercising.

The scan is the point rather than the list. A registry that is only a
list is a list someone has to remember to extend, and the write site
that gets forgotten is exactly the one that double-publishes; so the
seeded cases here are the proof that adding an unmarked durable write to
the package reds this module.

The subject is a library registry, but it is filed beside the
``release.*`` RPC modules whose crash boundaries it enumerates: EAWF025
gives one subject one owning kind, and
:mod:`tests.integration.runtime.daemon.methods.test_crash_boundaries`
consumes exactly this scan.
"""

from __future__ import annotations

from textwrap import dedent

import pytest

from eawf.workflow.release import (
    ledger,
    lifecycle,
    observe,
    preflight,
    publication,
    target_machine,
)
from eawf.workflow.release.boundaries import (
    EXTERNAL_BOUNDARIES,
    IN_PACKAGE_BOUNDARIES,
    PublicationBoundary,
    UnregisteredBoundaryError,
    assert_boundaries_total,
    boundaries_of,
    durable_boundary,
    durable_write_sites,
    package_sources,
)

pytestmark = pytest.mark.integration

#: Every durable write site the shipped package carries, as
#: ``module:function``. Pinned rather than derived so adding or removing
#: a durable write shows up as a diff on this list, not as a silently
#: different scan result.
EXPECTED_SITES = frozenset(
    {
        "ledger:record_operation",
        "lifecycle:advance_release",
        "observe:bake_release",
        "observe:observe_target",
        "observe:route_after_observation",
        "preflight:approve_release",
        "preflight:record_preflight_result",
        "publication:begin_publication",
        "publication:begin_verification",
        "publication:burn_release",
        "publication:reconcile_target",
        "publication:retry_publication",
        "records:record_release",
        "target_machine:advance_target_attempt",
        "target_machine:open_target_attempt",
    }
)

SEEDED_UNDECORATED = dedent(
    """
    def write_it(path, envelope):
        append_envelope(path, envelope)
    """
)

SEEDED_ATTRIBUTE_CALL = dedent(
    """
    def write_it(state_path, operation):
        ledger.record_operation(state_path, operation)
    """
)

SEEDED_UNKNOWN_BOUNDARY = dedent(
    """
    @durable_boundary(PublicationBoundary.NOT_A_BOUNDARY)
    def write_it(path, envelope):
        append_envelope(path, envelope)
    """
)

SEEDED_BARE_MARKER_ARG = dedent(
    """
    @durable_boundary("operation_open")
    def write_it(path, envelope):
        append_envelope(path, envelope)
    """
)

SEEDED_TOTAL = dedent(
    """
    @durable_boundary(
        PublicationBoundary.OPERATION_OPEN,
        PublicationBoundary.TARGET_DISPATCH,
        PublicationBoundary.EFFECT_RECEIPT_WRITE,
        PublicationBoundary.OBSERVATION_RECEIPT_WRITE,
        PublicationBoundary.TRANSITION_APPLY,
    )
    def write_it(path, envelope):
        append_envelope(path, envelope)
    """
)

SEEDED_PURE = dedent(
    """
    def read_it(operation, target_id):
        return latest_attempt(operation, target_id)
    """
)


# --- the shipped package -------------------------------------------------


def test_every_durable_write_site_in_the_package_carries_a_boundary_id() -> None:
    sites = assert_boundaries_total()
    assert {site.label for site in sites} == EXPECTED_SITES


def test_every_in_package_boundary_is_claimed_by_at_least_one_site() -> None:
    claimed = {boundary for site in assert_boundaries_total() for boundary in site.boundaries}
    assert claimed >= IN_PACKAGE_BOUNDARIES


def test_tag_push_is_the_only_boundary_the_package_scan_cannot_reach() -> None:
    assert {PublicationBoundary.TAG_PUSH} == EXTERNAL_BOUNDARIES
    assert set(PublicationBoundary) == IN_PACKAGE_BOUNDARIES | EXTERNAL_BOUNDARIES
    assert not IN_PACKAGE_BOUNDARIES & EXTERNAL_BOUNDARIES
    claimed = {boundary for site in assert_boundaries_total() for boundary in site.boundaries}
    assert PublicationBoundary.TAG_PUSH not in claimed


def test_package_sources_reads_every_module_but_the_package_init() -> None:
    sources = package_sources()
    assert "__init__" not in sources
    assert {"ledger", "lifecycle", "observe", "publication", "target_machine"} <= set(sources)


@pytest.mark.parametrize(
    ("marked", "expected"),
    [
        (lifecycle.advance_release, {PublicationBoundary.TRANSITION_APPLY}),
        (preflight.approve_release, {PublicationBoundary.TRANSITION_APPLY}),
        (target_machine.open_target_attempt, {PublicationBoundary.TARGET_DISPATCH}),
        (
            target_machine.advance_target_attempt,
            {
                PublicationBoundary.EFFECT_RECEIPT_WRITE,
                PublicationBoundary.OBSERVATION_RECEIPT_WRITE,
            },
        ),
        (publication.reconcile_target, {PublicationBoundary.EFFECT_RECEIPT_WRITE}),
        (observe.observe_target, {PublicationBoundary.OBSERVATION_RECEIPT_WRITE}),
        (
            publication.begin_publication,
            {
                PublicationBoundary.OPERATION_OPEN,
                PublicationBoundary.TARGET_DISPATCH,
                PublicationBoundary.TRANSITION_APPLY,
            },
        ),
    ],
)
def test_the_runtime_marker_agrees_with_the_scan(
    marked: object, expected: set[PublicationBoundary]
) -> None:
    assert boundaries_of(marked) == expected  # type: ignore[arg-type]


def test_the_ledger_append_is_the_shared_write_of_four_boundaries() -> None:
    assert boundaries_of(ledger.record_operation) == {
        PublicationBoundary.OPERATION_OPEN,
        PublicationBoundary.TARGET_DISPATCH,
        PublicationBoundary.EFFECT_RECEIPT_WRITE,
        PublicationBoundary.OBSERVATION_RECEIPT_WRITE,
    }


# --- the scan ------------------------------------------------------------


def test_a_seeded_undecorated_write_site_is_refused() -> None:
    with pytest.raises(UnregisteredBoundaryError, match="unregistered_publication_boundary") as err:
        assert_boundaries_total({"seeded": SEEDED_UNDECORATED})
    assert err.value.sites == ("seeded:write_it",)


def test_a_seeded_write_site_reached_through_an_attribute_call_is_found() -> None:
    with pytest.raises(UnregisteredBoundaryError) as err:
        assert_boundaries_total({"seeded": SEEDED_ATTRIBUTE_CALL})
    assert err.value.sites == ("seeded:write_it",)


def test_a_marker_naming_an_unknown_boundary_is_refused() -> None:
    with pytest.raises(UnregisteredBoundaryError, match="unknown_publication_boundary"):
        durable_write_sites({"seeded": SEEDED_UNKNOWN_BOUNDARY})


def test_a_marker_argument_the_scan_cannot_read_is_refused() -> None:
    with pytest.raises(UnregisteredBoundaryError, match="unknown_publication_boundary"):
        durable_write_sites({"seeded": SEEDED_BARE_MARKER_ARG})


def test_a_registered_boundary_no_site_claims_is_refused() -> None:
    with pytest.raises(UnregisteredBoundaryError, match="unclaimed_publication_boundary") as err:
        assert_boundaries_total({})
    assert err.value.missing == IN_PACKAGE_BOUNDARIES


def test_one_seeded_site_claiming_every_in_package_boundary_passes() -> None:
    sites = assert_boundaries_total({"seeded": SEEDED_TOTAL})
    assert len(sites) == 1
    assert sites[0].boundaries == IN_PACKAGE_BOUNDARIES


def test_a_function_that_writes_nothing_is_not_a_site() -> None:
    assert durable_write_sites({"seeded": SEEDED_PURE + SEEDED_TOTAL}) == (
        durable_write_sites({"seeded": SEEDED_TOTAL})
    )


def test_an_unparseable_module_is_refused_rather_than_skipped() -> None:
    with pytest.raises(SyntaxError):
        durable_write_sites({"seeded": "def broken(:\n"})


# --- the marker ----------------------------------------------------------


def test_the_marker_returns_the_same_function_object() -> None:
    def leg() -> int:
        return 1

    assert durable_boundary(PublicationBoundary.TAG_PUSH)(leg) is leg
    assert leg() == 1


def test_the_marker_refuses_to_name_no_boundary_at_all() -> None:
    with pytest.raises(ValueError, match="at least one boundary id"):
        durable_boundary()


def test_boundaries_of_raises_key_error_for_an_unmarked_function() -> None:
    def leg() -> None:
        return None

    with pytest.raises(KeyError, match="carries no publication boundary id"):
        boundaries_of(leg)
