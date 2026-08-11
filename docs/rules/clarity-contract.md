<!-- Generated from the eawf profile render block `clarity-contract`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=clarity-contract version=1.1 hash=a6fa5d567455439a -->
# `clarity-contract`

Every newcomer-facing artifact must be understandable without opening ``state.json``: right audience, jargon glossed on first use, motivation stated, scannable, references tabulated.

### Doc-clarity contract (the newcomer test)

Every newcomer-facing artifact — commit subject + body, PR body, research / audit / decision brief, and every entity ``title`` / ``description`` — must pass one gate: *would someone who joined today understand this without opening ``state.json``?* The five checks behind that gate:

- **Audience-fit** — write for a newcomer, not an insider.
- **Jargon defined on first use** — internal codes are glossed the first time they appear in prose: lifecycle ids (``P<NN>`` / ``I<NN>`` / ``W<NN>``), cluster / decision codes (``C0<N>`` / ``D<NN>`` / ``D-SUP-<NN>``), hypothesis ids (``H<NN>-<NN>``), and screaming-snake flags (``SWITCH_*`` / ``EAWF_*``). Commit-subject type prefixes are EXEMPT — the commit-prefix rule requires them.
- **Why-present** — say the motivation, not only the what; a ``description`` that merely restates its ``title`` fails.
- **Scannable** — short paragraphs, headings, lists; no wall of text.
- **Reference-hygiene** — dense ``[N]`` markers backed by a ``## References`` table; no inline ``path:line`` soup or bare URLs mid-prose.

The approved-term glossary, the internal-code blocklist, and the six scored dimensions are the typed source the prose lints read: :mod:`eawf.platform.profiles.clarity`.
<!-- END EAWF:managed id=clarity-contract -->
