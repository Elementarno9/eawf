"""Structured-logging support for eawf library modules.

This sub-package owns the emit-time sensitive-data filter
(:class:`~eawf.logging.scrub.SensitiveScrubber`). It is named
``eawf.logging`` and does NOT shadow the standard-library
``logging`` module: Python 3 resolves bare ``import logging`` via
absolute import, so library modules continue to bind the stdlib
module while ``eawf.logging`` stays addressable by its fully
qualified name.
"""

from __future__ import annotations

from eawf.logging.scrub import SensitiveScrubber

__all__ = ["SensitiveScrubber"]
