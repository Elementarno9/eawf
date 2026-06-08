from __future__ import annotations

import re

import eawf


def test_import_and_version() -> None:
    # Assert the package imports and exposes a well-formed version rather than
    # a pinned literal, so a release bump does not red this smoke. The
    # single-source coupling (runtime == _version.py) is pinned separately by
    # tests/unit/test_version_coupling.py.
    assert re.match(r"^\d+\.\d+\.\d+", eawf.__version__)
