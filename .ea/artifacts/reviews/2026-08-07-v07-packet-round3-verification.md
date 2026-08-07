# v0.7 proposal packet — round-3 structural verification

## Summary

This is a **mechanical** verification of the Eä Workflow (`eawf`) v0.7.0 proposal packet [1], run on 2026-08-07 to discharge the backlog-disposition claims the packet makes. It is the evidence behind closing fifteen open backlog rows: the twelve the packet abandons and the three it closes as duplicates [2].

Scope limit, stated up front so no later reader over-reads this record: **no prose coherence review backs it.** A third-round coherence pass over the seventeen specification files was dispatched and did not deliver its findings, so the packet's prose is unchanged since its round-two revision and is unreviewed at round three. What follows attests structure and set arithmetic only. A reader who needs assurance that the specification text is internally consistent will not find it here.

Two checks ran. Both pass.

**Structural integrity of the packet.** Over the seventeen numbered specification files plus `README.md` [1]: 604 requirement identifiers defined; zero identifiers defined twice; zero identifiers referenced without a definition; zero identifiers defined outside their owning file, where the owning file is fixed by the family-to-file map (`AUTH`→`00`, `REL`→`10`, `DOM`→`20`, `PLAN`→`30`, `RUN`→`40`, `DEL`→`50`, `UI`→`60`, `RULE`→`64`, `SURF`→`65`, `SKILL`→`66`, `MEAS`→`67`, `NTFY`→`68`, `QUAL`→`70`, `LINT`→`72`, `ECON`→`75`, `PRX`→`80`). Every numbered file declares a Spec ID and no two collide. The `README.md` read-order table carries a row per file with no missing and no phantom rows. The dependency graph in `00` [3] has seventeen nodes and thirty-eight edges, is acyclic, has no node without a packet file and no packet file absent from it, and builds every edge the packet update plan [4] prescribed in the prescribed direction. Every internal link resolves on disk.

**Backlog partition.** The packet claims the seventy-five open backlog rows partition exactly into sixty routed to a destination file, twelve abandoned, and three closed as duplicates [2]. Verified by extracting the identifiers from all three tables and comparing against live state: seventy-five identifiers, each appearing exactly once, set-identical to the live open set. No identifier is unknown to state. The deliberate trap the packet records — that `B070`/`B70` and `B071`/`B71` are genuinely distinct rows rather than zero-padding variants — holds: all four exist in state as separate items, and normalising the padding would have merged two pairs and broken the partition.

The fifteen rows closed against this record are the twelve abandoned (`B045`, `B046`, `B076`, `B114`, `B077`, `B078`, `B079`, `B083`, `B101`, `B106`, `B063`, `B098`) and the three duplicates (`B128`, `B129`, `B131`). Each carries the packet's own recorded reason as its resolution text. The three survivors those duplicates restate (`B124`, `B125`, `B127`) stay open and carry the work forward.

## References

[1] `.ea/local/research/v0.7-proposal/README.md` — packet index, read order, and the specification completeness contract.

[2] `.ea/local/research/v0.7-proposal/90-source-map-and-archive.md` — source disposition, the backlog destination table, the abandoned table, and the duplicates table.

[3] `.ea/local/research/v0.7-proposal/00-authority-scope-and-decisions.md` — proposal authority, scope rows, and the inter-file dependency graph.

[4] `.ea/local/research/v0.7-proposal/2026-08-06-v07-packet-update-plan.md` — the plan brief whose prescribed files, requirements, and graph edges the packet claims to have landed.

## Provenance

- verification date: 2026-08-07
- packet state: unchanged since its 2026-08-06 revision; this record adds no packet edits
- checks run: identifier integrity, family-to-file ownership, Spec ID declaration, README read-order coverage, dependency-graph shape and acyclicity, prescribed-edge presence, internal link resolution, backlog partition arithmetic
- checks NOT run: prose coherence over the specification text, requirement-text quality, cross-file contradiction sweep, fixture-bundle execution
- state mutations: fifteen backlog closes cite this record
- roadmap mutations: none

## Scrub

- status: clean
- references: repo-relative
- credentials, PII, machine-specific paths: none
