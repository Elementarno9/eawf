"""Structured-logging support for eawf library modules.

This sub-package owns the emit-time sensitive-data filter
(:class:`~eawf.observability.logging.scrub.SensitiveScrubber`). It is named
``eawf.observability.logging`` and does NOT shadow the standard-library
``logging`` module: Python 3 resolves bare ``import logging`` via
absolute import, so library modules continue to bind the stdlib
module while ``eawf.observability.logging`` stays addressable by its fully
qualified name.
"""

from __future__ import annotations

from eawf.observability.logging.scrub import SensitiveScrubber

__all__ = ["SensitiveScrubber"]
