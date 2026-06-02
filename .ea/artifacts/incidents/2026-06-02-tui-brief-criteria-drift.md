# Incident — TUI design brief silently thinned at the P29 propose (brief→criteria drift)

## Summary

The shipped P29 TUI diverges from the design it was built from: the canonical TUI chassis brief (`.ea/local/research/2026-05-30-tui-chassis.md`, the ratified `D-TUI-MODES` design) specifies a 6-mode digit map (1 Home / 2 Autopilot / 3 Research / 4 Trust / 5 Doctor / 6 Evidence), an autopilot cockpit with halt/skip/kill/pause/arm intervention keys, a 3-pane research orchestrator, and five named minor-defect fixes. The as-built TUI ships a 9-mode map (1 Home / 2 Trust / 3 Doctor / 4 Evidence / 5 Feed / 6 Config-stub / 7 Research / 8 Watch / 9 Autopilot), an autopilot pane with dispatch-only controls, a read-only research board, and none of the five defect fixes.

Verdict: **functional impact minor, process impact high.** The as-built TUI works and is internally consistent (the help overlay derives its key table from the live mode registry, so it advertises exactly what is bound). Nothing is broken. But the divergence was never a decision — it was the silent side-effect of a decomposition step, it shipped through every gate green, and none of the dropped scope is tracked anywhere. The same failure mode applies to any `/roadmap propose --from-briefs` run, which is why this is filed as a process incident rather than a TUI bug.

### What diverged (brief vs shipped, verified against source)

- **Mode digit map.** Brief pins 2=Autopilot, 3=Research, 4=Trust, 5=Doctor, 6=Evidence. As-built (`src/eawf/surfaces/tui/modes/registry.py` `MODE_REGISTRY`) is 2=Trust, 3=Doctor, 4=Evidence, 5=Feed, 6=Config(placeholder stub), 7=Research, 8=Watch, 9=Autopilot. Autopilot — the spawn cockpit — sits at digit 9 instead of 2. Feed and Config are net-new modes not in the brief; Config is a "coming soon" stub while config actually works via the `c` modal.
- **Autopilot intervention keys.** Brief: `H` halt / `S` skip / `K` kill / `space` pause / `a` arm. As-built (`modes/autopilot.py` BINDINGS): only `↑`/`↓` + `d` dispatch.
- **Research mode.** Brief: interactive 3-pane orchestrator (topic-tree | claims/evidence | progress/budget) + checkpoint drawer + `enter`/`a`/`p`/`r`/`s` keys. As-built (`modes/research_board.py`): a read-only scroll board over the campaign store.
- **Five minor defects (brief section K) — 0 of 5 fixed.** (a) roadmap tree resets cursor/scroll/expanded-set on `_rebuild` (`widgets/roadmap_tree.py`); (b) backlog sort is wired but has no key binding and no header glyph (`widgets/backlog_table.py`); (c) `priority` column label is 8 chars wide for 2-char values (`widgets/backlog_table.py`); (d) multichoice repeats the field+type prefix on every option row (`widgets/multichoice_checklist.py`); (e) breadcrumb stops at phase, is non-clickable, and the runtime cell is an idle/active stub (`widgets/header.py`).

### Timeline

- **2026-05-30** — `2026-05-30-tui-chassis.md` authored (~40 KB), carrying the full mode map, intervention keys, 3-pane research design, and the five section-K fixes. The brief states it promotes to `.ea/artifacts/` "in the same commit that lands the `D-TUI-MODES` Decision row."
- **2026-05-31 (digest pass)** — `/roadmap propose` synthesis dispatched parallel agents to digest the cluster briefs into wave-level detail. The TUI digest **preserved** the rich detail: digit map, `H/S/K/space/a` keys, the 3-pane orchestrator, and the five fixes as separate candidate waves with full criteria.
- **2026-06-01 ~03:22 (generator pass)** — the final plan was emitted by a JSON generator script that collapsed each rich candidate into a single one-line success criterion. Its planning narrative is entirely about structure (iter count, dep resolution, ID grammar, EU totals); the terms "intervention keys", "halt/skip/kill", and "3-pane" appear zero times. No dropped-detail log and no deferral target were produced.
- **2026-06-01 / 06-02 (build)** — W16 (chassis) assigned digits 1-6 to the spawn-free modes; W12/W13/W11 (I04) appended autopilot/research/watch at the "next free digit" 9/7/8. The `tui-chassis` brief was referenced zero times in the build sessions.
- **2026-06-02 (audit + close)** — the A47 flow-audit passed the TUI waves ("research_board / agent-watch / autopilot registered ... on digits 7/8/9 ... PASS"), then I04 + the audit closed (commit `24699431`). The audit validated criteria-vs-code; it had no lens on criteria-vs-brief.

### Root cause

1. **Two-pass decomposition with a lossy second pass.** The digest preserved the brief; the generator that wrote the executable criteria compressed each candidate to one line and dropped the rest with no deferral. The loss is at the brief→criteria boundary, not criteria→code. W16's criterion reads only "digit-key mode switch" — it never pins the mapping, so the digit assignment was left to the executor.
2. **Spawn-gating reshaped the digit map (this part was correct and brief-sourced).** Autopilot and Research are spawn-gated and could not exist in the spawn-free iter (I02) where digits 1-6 were handed out. I02 filled those six slots with the available spawn-free modes (adding Feed from the live-feed wave and a Config stub from the config-modal wave); the spawn-gated modes were appended at 7-9 in I04. The brief's 2=Autopilot/3=Research never had a slot at assignment time.
3. **No ratifying decision and no brief-fidelity audit lens.** The `D-TUI-MODES` Decision row was never landed (only D15 and D23 exist in `state.json`), so the brief stayed a gitignored local draft with no binding force, and nothing diffed the implementation against it. Audits check waves against their own criteria; A47 correctly passed digits 7/8/9 because they matched the (thin) criteria. The drift was structurally invisible to every gate.

The operator was never asked. The only propose-time directive was to add all iterations and waves "with clear fields" — a completeness-of-structure bar, not a richness bar — and the two planning AskUserQuestion prompts were about spawn baseline and doc-clarity placement, not TUI scope.

### What went right

The spawn-free vs spawn-gated split is genuine and brief-sourced, not a regression. Honest-empty states are implemented across every mode. The help overlay is self-consistent because it derives from the registry rather than a hardcoded list. The as-built layout also added two useful modes the brief did not have (Feed, Watch). The failure is one of fidelity and tracking, not of build quality.

## Impact

The dropped scope is real and entirely untracked: no backlog item and no pending wave covers the digit map, the autopilot intervention keys, the research 3-pane interaction, or the five section-K defects. The remaining P29 iters (I05 trust/reputation, I06 release, I07 doc-clarity) contain no TUI work, so absent a deliberate re-filing the divergence is permanent. The prerequisites that originally gated the rich cuts now exist (pgid-kill `I01-W25`, budget HALT `I03-W05`, live spawn `I04-W01`, the research campaign engine `I04-W09`), so the missing work is buildable today.

## Proposed mitigations

1. **Decomposition-coverage check at propose.** When `/roadmap propose --from-briefs` compresses a brief into wave criteria, diff the brief's enumerated deliverables and fixes against the union of generated criteria. Every enumerated item must map to a criterion or to an explicit deferral (a backlog id or a named future wave). Unmapped items halt the propose for an explicit drop/defer decision rather than vanishing.
2. **No silent truncation in propose-time generators.** Any generator that condenses candidates must emit a dropped-detail report (the "no silent caps" principle): for each wave, what source detail did not survive into the criterion. A reviewer reads the report before apply.
3. **Land the ratifying Decision row at the first build wave, not "someday".** A design brief that drives a multi-wave build must land its `Decision` row as the first wave's deliverable (or as a staged PENDING ratification at propose), so there is a binding contract to audit against. A brief that is referenced by wave provenance tags but never promoted is a standing drift risk.
4. **Add a provenance-fidelity lens to `/audit`.** When a wave carries a brief provenance tag (e.g. `[TUI-1 | tui-chassis | ...]`), the auditor spot-checks the wave's criteria against the cited brief section for dropped scope, not only the code against the criteria. This is the lens that would have caught the digit-map and intervention-key loss.
5. **Pin stable contracts inside criteria.** A wave that establishes a durable contract — a digit/key map, a public API shape, an enum — must enumerate the concrete contract in its success criteria. "Adopt MODES chassis with digit-key mode switch" is unfalsifiable about the mapping; "digits bind 1=home 2=autopilot 3=research ..." is verifiable.
6. **Use the typed IntentBrief that already exists.** `eawf roadmap revise --add-wave --intent-problem ... --intent-desired-outcome ...` attaches a typed `IntentBrief` to a wave that survives propose intact. Design-derived waves should carry an IntentBrief rather than a lone one-line criterion, so the "why" and the desired outcome travel with the wave to the executor.

## Remediation (this incident)

Five waves are drafted to restore brief parity and were intended for the current iter: W16 renumber `MODE_REGISTRY` to the brief map + land `D-TUI-MODES` + retire the Config stub; W17 autopilot intervention keys; W18 research 3-pane orchestrator; W19 section-K defects a-d; W20 breadcrumb full-location + clickable + real runtime cell. Placement is pending an operator decision: I04 closed (commit `24699431`) during the investigation and there is no iter-reopen, so the waves will land in a new iter under the still-active P29, fold into the next planned iter, or open a successor phase.

## References

- Design source: `.ea/local/research/2026-05-30-tui-chassis.md` (the `D-TUI-MODES` brief, never promoted).
- Build order / catalog: `.ea/local/research/2026-05-31-p29-build-order.md`, `.ea/local/research/2026-05-31-p29-detailed-plan.md`.
- As-built: `src/eawf/surfaces/tui/modes/registry.py` (`MODE_REGISTRY`), `modes/autopilot.py`, `modes/research_board.py`, `screens/help.py` (`mode_key_rows`).
- Unfixed defects: `widgets/roadmap_tree.py`, `widgets/backlog_table.py`, `widgets/multichoice_checklist.py`, `widgets/header.py`.
- Thin criteria: `eawf wave show P29-I02-W16 --dispatch-prompt` (and W17/W22/W26, I04-W12/W13); the criteria live in `.ea/state.json`.
- Prior audit that passed the divergence: `.ea/artifacts/audits/2026-06-02-A47-P29-i04-flow-audit.md`.
- I04 close: commit `24699431` `[P29-I04] state: close iter (audit=A47-P29-i04)`.

## Provenance

Produced by a read-only `/research` investigation on 2026-06-02 (TUI brief-vs-as-built review + session-history root-cause dig requested by the operator). Findings were assembled by fresh-context survey subagents reading the briefs, the live TUI source, the `.ea/state.json` wave criteria, and the build/planning session transcripts, cross-checked against `git log` and the A47 audit. The digit-map, intervention-key, 3-pane, and five-defect divergences were each verified against committed source before assertion. The operator dispositioned the finding ("restore brief design") via `AskUserQuestion`; this brief records the incident and the proposed process mitigations. It can be registered as a state-resident `Incident` row and promote alongside `D-TUI-MODES` when the remediation waves land.

## Scrub

- status: clean

No absolute local paths, hostnames, credentials, or PII. References are repo-relative or eawf-internal ids; session transcripts are referred to by role and date, not by machine path.
