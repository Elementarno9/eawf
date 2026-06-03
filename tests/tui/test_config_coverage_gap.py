"""Coverage-gap accounting for the config-modal curated surface (P29-I08-W26).

The TUI config modal surfaces the curated :data:`CONFIG_REGISTRY`, a small
subset of the full :data:`LEAF_KEY_REGISTRY` leaf catalog. The difference
between the two -- the *coverage gap* -- is every leaf key the daemon will
persist but the modal does NOT offer for editing. A gap key is fine, but it
must be fine for a *principled* reason, not by accident: a key silently
falling out of the curated set is a coverage regression the modal cannot
otherwise catch.

This module buckets every gap key into exactly one of three reasons and
pins the per-bucket counts + the total to a snapshot, so adding a leaf key
without curating it (or accounting for why it stays hidden) fails here:

* **locked** -- ``writable_layers == ()``: a code-only / read-only leaf
  (e.g. ``schema_version``) that no layer may write, so the editable modal
  must not offer it.
* **structural** -- a writable but non-scalar leaf (``list_str`` /
  ``list_any`` / ``mapping`` / ``any``): a list / map / free-form shape the
  modal's single-row scalar editor cannot cleanly stage; these are edited
  from the CLI / YAML directly.
* **intentionally-CLI-only** -- a writable scalar leaf (``bool`` / ``int``
  / ``float`` / ``str`` / ``literal``) that is editable in principle but
  deliberately not curated into the menu (kept lean per the curated-subset
  contract).

The bucketing predicate is mutually exclusive by construction: ``locked``
is tested first (empty ``writable_layers``), then ``structural`` (writable
+ non-scalar), and every remaining writable scalar leaf is CLI-only. The
test asserts each gap key lands in exactly one bucket, the buckets
partition the gap with no overlap, and the counts match the pinned
snapshot.
"""

from __future__ import annotations

from collections.abc import Mapping

from eawf.kernel.config.registry import (
    CONFIG_REGISTRY,
    LEAF_KEY_REGISTRY,
    LeafKey,
)

#: Leaf-key value shapes the modal's single-row scalar editor cannot stage
#: (list / map / free-form). A writable leaf of one of these shapes is
#: bucketed ``structural`` -- edited from the CLI / YAML, not the modal.
_NON_SCALAR_TYPES: frozenset[str] = frozenset({"list_str", "list_any", "mapping", "any"})

#: Pinned coverage-gap snapshot. The total is the size of
#: ``LEAF_KEY_REGISTRY - CONFIG_REGISTRY``; the per-bucket counts partition
#: it. Bumping these is the *intended* signal when a leaf key is added or
#: curated -- a silent drift fails the snapshot assertions below.
_GAP_SNAPSHOT: Mapping[str, int] = {
    "total": 133,
    "locked": 4,
    "structural": 31,
    "cli_only": 98,
}


def _gap_keys() -> frozenset[str]:
    """Return the leaf keys present in the catalog but absent from the menu."""
    curated = {entry.key for entry in CONFIG_REGISTRY}
    return frozenset(LEAF_KEY_REGISTRY.keys() - curated)


def _bucket_for(leaf: LeafKey) -> str:
    """Return the single coverage-gap bucket *leaf* belongs to.

    The predicate is ordered so the buckets are mutually exclusive: a locked
    leaf (no writable layer) is ``"locked"`` regardless of type; a writable
    leaf of a non-scalar shape is ``"structural"``; every other (writable
    scalar) leaf is ``"cli_only"``.

    Args:
        leaf: The leaf-catalog row to classify.

    Returns:
        One of ``"locked"`` / ``"structural"`` / ``"cli_only"``.
    """
    if leaf.writable_layers == ():
        return "locked"
    if leaf.type in _NON_SCALAR_TYPES:
        return "structural"
    return "cli_only"


def _bucketed_gap() -> dict[str, list[str]]:
    """Bucket every gap key by :func:`_bucket_for`, sorted within each bucket."""
    buckets: dict[str, list[str]] = {"locked": [], "structural": [], "cli_only": []}
    for key in _gap_keys():
        buckets[_bucket_for(LEAF_KEY_REGISTRY[key])].append(key)
    for members in buckets.values():
        members.sort()
    return buckets


def test_curated_registry_is_a_strict_subset_of_the_leaf_catalog() -> None:
    """The menu curates a subset -- a non-empty gap is the premise of this module."""
    curated = {entry.key for entry in CONFIG_REGISTRY}
    leaf = set(LEAF_KEY_REGISTRY.keys())
    assert curated <= leaf, sorted(curated - leaf)
    assert _gap_keys(), "expected a non-empty coverage gap to account for"


def test_every_gap_key_lands_in_exactly_one_bucket() -> None:
    """Each gap key is classified into exactly one of the three buckets."""
    buckets = _bucketed_gap()
    flat = [key for members in buckets.values() for key in members]
    # No key is double-counted across buckets.
    assert len(flat) == len(set(flat)), "a gap key landed in more than one bucket"
    # The buckets partition the gap exactly -- every key in, nothing extra.
    assert set(flat) == _gap_keys()


def test_buckets_do_not_overlap() -> None:
    """The three bucket key-sets are pairwise disjoint."""
    buckets = _bucketed_gap()
    locked = set(buckets["locked"])
    structural = set(buckets["structural"])
    cli_only = set(buckets["cli_only"])
    assert locked & structural == set()
    assert locked & cli_only == set()
    assert structural & cli_only == set()


def test_locked_bucket_predicate_holds() -> None:
    """Every ``locked`` member declares no writable layer (the locking signal)."""
    for key in _bucketed_gap()["locked"]:
        assert LEAF_KEY_REGISTRY[key].writable_layers == (), key


def test_structural_bucket_predicate_holds() -> None:
    """Every ``structural`` member is writable but a non-scalar (list/map/any) shape."""
    for key in _bucketed_gap()["structural"]:
        leaf = LEAF_KEY_REGISTRY[key]
        assert leaf.writable_layers != (), key
        assert leaf.type in _NON_SCALAR_TYPES, (key, leaf.type)


def test_cli_only_bucket_predicate_holds() -> None:
    """Every ``cli_only`` member is a writable scalar leaf left uncurated."""
    for key in _bucketed_gap()["cli_only"]:
        leaf = LEAF_KEY_REGISTRY[key]
        assert leaf.writable_layers != (), key
        assert leaf.type not in _NON_SCALAR_TYPES, (key, leaf.type)


def test_gap_counts_match_pinned_snapshot() -> None:
    """The bucket counts + total match the snapshot, catching silent growth.

    When this fails after adding / curating a leaf key, update
    :data:`_GAP_SNAPSHOT` deliberately -- the snapshot exists so a coverage
    change is an explicit edit, not an accident.
    """
    buckets = _bucketed_gap()
    actual = {
        "total": len(_gap_keys()),
        "locked": len(buckets["locked"]),
        "structural": len(buckets["structural"]),
        "cli_only": len(buckets["cli_only"]),
    }
    assert actual == dict(_GAP_SNAPSHOT), actual


def test_snapshot_buckets_sum_to_total() -> None:
    """Internal consistency: the pinned per-bucket counts add up to the total."""
    bucket_sum = _GAP_SNAPSHOT["locked"] + _GAP_SNAPSHOT["structural"] + _GAP_SNAPSHOT["cli_only"]
    assert bucket_sum == _GAP_SNAPSHOT["total"]
