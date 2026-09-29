"""The reflection surface: local reports of where a tree's Runs went.

:mod:`~eawf.observability.reflect.runs` reads the tree's Runs and their
event lines without a daemon, :mod:`~eawf.observability.reflect.titles`
fills each listed session's title, :mod:`~eawf.observability.reflect.report`
renders, stores, exports and prunes the reflection report,
:mod:`~eawf.observability.reflect.serve` serves an export on the loopback
interface, and :mod:`~eawf.observability.reflect.run_report` writes the
plain-text report of one Run. None of them writes a canonical record.
"""

from __future__ import annotations
