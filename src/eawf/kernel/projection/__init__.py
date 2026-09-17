"""Typed projections: the one shape the daemon hands the console.

:mod:`~eawf.kernel.projection.truth` holds the strict projection header and the
``TruthField`` every displayed value validates as,
:mod:`~eawf.kernel.projection.read_models` declares the read model each console route
renders, and :mod:`~eawf.kernel.projection.compute` builds one of those read models
from the epoch-2 document at a committed cursor, plus the keyed patches a commit
produces. The package re-exports nothing, so a caller that needs only the read-model
declarations does not pay for the validation models.
"""

from __future__ import annotations
