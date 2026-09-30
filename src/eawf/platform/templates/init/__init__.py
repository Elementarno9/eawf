"""Init bootstrap templates (C08 — P25-W16).

Three YAML templates ship in v0.3 per C08 D7 (revised 2026-05-18 per
operator Q24): ``research.yaml``, ``engineering.yaml``,
``reverse-engineering.yaml``. Each template is a ``.ea/config.yaml``
seed that ``eawf init --template <name>`` merges with operator answers
(project_code, project_title) to bootstrap a workspace.

The templates differ in the profiles they enable, the wave parallelism
they allow and, for engineering, the acceptance commands ship runs.

``spike.yaml`` and ``hybrid.yaml`` are deferred to v0.4+ (Q24 — YAGNI
trim; demand-signal unclear). Discovery + load lives in
:mod:`eawf.platform.profiles.discovery` (``load_init_template``).
"""

from __future__ import annotations
