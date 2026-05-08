from __future__ import annotations

import eawf


def test_import_and_version() -> None:
    assert eawf.__version__ == "0.1.0.dev0"
