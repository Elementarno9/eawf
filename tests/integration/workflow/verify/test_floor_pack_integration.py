"""The three fixture profiles compile distinct floor packs.

python, apps and robotics each declare their own ``verify.floor_checks``;
compiled, no two packs share the same set of check names.
"""

from __future__ import annotations

from eawf.platform.profiles import load_profile


def test_three_profiles_compile_distinct_floor_packs() -> None:
    """SC: python / apps / robotics yield byte-DIFFERENT compiled floor packs."""
    from eawf.workflow.verify.compile import compile_floor_pack

    packs: dict[str, list[str]] = {}
    for pid in ("python", "apps", "robotics"):
        body = load_profile(pid)
        assert body.verify is not None
        compiled = compile_floor_pack(
            body.verify.floor_checks,
            allowlist=list(body.verify.argv_allowlist),
        )
        packs[pid] = [spec.name for spec in compiled]

    # No two profile packs have the same set of check names.
    assert set(packs["python"]) != set(packs["apps"])
    assert set(packs["python"]) != set(packs["robotics"])
    assert set(packs["apps"]) != set(packs["robotics"])
