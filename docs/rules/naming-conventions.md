<!-- Generated from the eawf profile render block `naming-conventions`. Do not hand-edit: re-run `eawf sync`. -->

# `naming-conventions`

Every cross-cutting concept has exactly one canonical name; rename an outlier to match the dominant form before merging instead of adding an adapter shim.

### Naming conventions

To prevent drift across state models, envelopes, parameters, and log keys, every cross-cutting concept has exactly one canonical name. Outliers MUST be renamed to match the dominant form before merging, not papered over with adapter shims.

**Agent role identifier** — ``agent_role`` (never ``role`` alone on a Wave / SubagentSpec field). Applies to :class:`~eawf.kernel.state.models.Wave.agent_role`, ``RoleSpec.role`` (the inner enum keeps the bare name because ``RoleSpec`` already namespaces it), CLI flags (``--agent-role``), and dispatch envelopes. The bare ``role`` remains valid inside ``RoleSpec`` because the surrounding type disambiguates.

**Effort bucket parameter** — ``effort_bucket`` (never ``size`` or ``effort_size``). Applies to :class:`~eawf.kernel.state.models.Wave.effort_bucket`, CLI flags (``--effort-bucket``), planner output, and EU-projection tables. Allowed values are the closed StrEnum ``XS|S|M|L|XL``.

**Evidence kind identifier** — ``evidence_kind`` (never ``kind`` alone on an EvidenceRecord field, and never ``evidence_type``). Applies to :class:`~eawf.kernel.state.models.EvidenceRecord.evidence_kind`, JSON keys, CLI flags (``--evidence-kind``), and gate-pack lookups. Bare ``kind`` remains valid on store envelopes where ``StoreKind`` already disambiguates.

**State scope identifier** — ``scope_id`` (never bare ``scope``). Applies to Pydantic field names on ``State`` models (e.g. ``PluginInstall``), :class:`~eawf.surfaces.render.envelope.EnvelopeHeader`, function kwargs (e.g. ``add_artifact(scope_id=...)``, ``artifact_urn(scope_id, ...)``), JSON keys on the wire, and ``state.json`` field names. Bare ``scope`` is reserved for CLI argument names (``--scope``) and skill-context attributes (``SkillContext.scope``) where the caller maps onto the URN.

**Output directory parameter** — ``output_dir`` (never ``out_dir`` or ``target_dir``). Applies to schema dumpers, plugin installers, and any helper that takes a write destination directory.

**Wave / iter / phase keys in logs and dict payloads** — ``wave=<id>``, ``iter=<id>``, ``phase=<id>``. Bare keys only, never ``wave_id=<id>`` in log lines (the trailing ``_id`` is reserved for typed-model field names where the type system benefits from explicit suffix). Inside structured envelopes (``EventPayload``, ``state.json``) keep the ``_id`` suffix so the schema is unambiguous.

**Log format inside library modules** — ``<funcname> key=value key=value`` form, space-separated, no leading ``:`` after the function name. f-strings only (project-wide rule 9). Example: ``logger.info(f"create_worktree wave={wave_id} branch={name!r}")``.

**Error message phrasing** — lowercase leading word, no trailing period, no class-name prefix. Use ``!r`` when interpolating user input so quoting is visible. Example: ``raise ValueError(f"unknown wave: {wave_id!r}")``.

**Docstring ``Raises:`` block** — Google-style ``Raises:`` block with one ``ExceptionType: explanation`` line per case. Do NOT use inline prose like ``Raises ValueError if ...`` in the summary; reserve the ``Raises:`` block for that.

**Mutator-path precision in wave success criteria** — when a wave's success criterion text references a "save through" or "persist via" path, name the **canonical writer** (the daemon, per rule 4) rather than the generic phrase ``state-CLI``. The authority map ``.ea/artifacts/research/long-term/2026-05-18-authority-map.md`` names the canonical writer per file. Conflating the operator-facing surface (``uv run eawf state ...``) with the daemon-internal subsystem in criterion prose makes audits flag false positives.
