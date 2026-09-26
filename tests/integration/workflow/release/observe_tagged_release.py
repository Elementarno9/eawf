"""The version this source declares is published on the package index.

This is the ``release_tagged_observed`` proof of the ``product_canary``
profile, run by the gate runner at the pinned source of a checkpoint. The
tag's publish jobs upload the wheel and the source distribution before
the post-merge walk reaches its receipts step, so a read-back of the index
there is an observation of the tagged release rather than of the tree.

The module name carries no ``test_`` prefix on purpose. Pytest collects a
file outside its ``python_files`` pattern only when the file is named on
the command line, so the default suite never reaches the network, and the
one caller that names it is the gate binding. Before the tag is pushed the
version is absent from the index and this proof fails, which is the
receipt the checkpoint cannot yet have.
"""

from __future__ import annotations

import json

from eawf import __version__
from eawf.workflow.release.registry_readers import PACKAGE_INDEX_URL, UrllibOpener

#: The distribution kinds the ``pypi`` target of every rung publishes.
PUBLISHED_KINDS = frozenset({"bdist_wheel", "sdist"})


def test_the_declared_version_is_served_with_its_wheel_and_sdist() -> None:
    reply = UrllibOpener()(
        f"{PACKAGE_INDEX_URL}/pypi/eawf/{__version__}/json",
        headers={"Accept": "application/json"},
    )

    assert reply.status == 200, f"the index does not serve eawf {__version__}: {reply.status}"
    document = json.loads(reply.body)
    assert document["info"]["version"] == __version__
    served = {row["packagetype"] for row in document["urls"]}
    assert served >= PUBLISHED_KINDS, f"eawf {__version__} serves only {sorted(served)}"
