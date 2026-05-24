"""Platform layer: install, packaging, and durable-asset packages.

The platform super-package groups the supporting packages that install,
package, and persist eawf's durable assets beneath the kernel, workflow,
runtime, surfaces, and observability layers:
:mod:`~eawf.platform.profiles` (profile composition),
:mod:`~eawf.platform.registry` (read-only ``~/.eawf/registry.json``
helpers), :mod:`~eawf.platform.install` (init wizard, global install,
instrument probe), :mod:`~eawf.platform.templates` (bundled Jinja2
payloads), :mod:`~eawf.platform.artifacts` (typed artifact helpers),
:mod:`~eawf.platform.memory` (authoritative ``memory.jsonl`` + cache),
:mod:`~eawf.platform.scrub` (PII / path-hygiene scrubbing),
:mod:`~eawf.platform.lint` (custom static-analysis rules),
:mod:`~eawf.platform.backup` (manual snapshot backup), and
:mod:`~eawf.platform.docs` (documentation-generation surface).
"""

from __future__ import annotations
