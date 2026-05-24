"""Kernel layer: typed state, storage, config, validation, specs, migrations.

The kernel super-package groups the load-bearing data-model packages that
the rest of the tree builds on: :mod:`~eawf.kernel.state` (typed entities,
IDs, URNs, atomic writes), :mod:`~eawf.kernel.store` (JSONL stores),
:mod:`~eawf.kernel.config` (layered configuration),
:mod:`~eawf.kernel.validate` (schema + invariant validation),
:mod:`~eawf.kernel.spec` (typed phase/iter/wave specs), and
:mod:`~eawf.kernel.migrations` (state-schema migration chain).
"""

from __future__ import annotations
