"""Typed projections: the one shape the daemon hands the console.

:mod:`~eawf.kernel.projection.truth` holds the strict projection header and the
``TruthField`` every displayed value validates as, and
:mod:`~eawf.kernel.projection.read_models` declares the read model each console route
renders. The package re-exports nothing, so a caller that needs only the read-model
declarations does not pay for the validation models.
"""

from __future__ import annotations
