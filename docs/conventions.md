# Conventions

Eawf keeps long-lived rules in rendered `AGENTS.md` blocks and reserves source code comments for local implementation intent. The conventions below are the stable names and budgets shared by renderers, linters, and profile authors.

## Render Blocks

`RenderBlock.tier` classifies managed AGENTS content:

- `tier0`: short, always-visible operator rules. These blocks count toward the AGENTS.md tier-0 budget.
- `reference`: detailed rule text, expansions, examples, and profile-specific guidance. This is the default for backward compatibility.

The tier-0 budget gate scans bundled profile render blocks targeting `AGENTS.md`, sums only `tier0` block bodies, and rejects a profile set whose approximate token count exceeds `[tool.eawf.agents_md_budget].max-tier0-tokens`.

## Source Comments

Source comments explain why the code does something. They do not carry decision, audit, roundtable, or assistant provenance. Durable provenance belongs in state rows, reports, artifacts, commit bodies, and PR text.
