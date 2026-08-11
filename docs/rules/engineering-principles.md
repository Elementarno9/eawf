<!-- Generated from the eawf profile render block `engineering-principles`. Do not hand-edit: re-run `eawf sync`. -->

<!-- BEGIN EAWF:managed id=engineering-principles version=1.1 hash=fb6567b814e4a58a -->
# `engineering-principles`

Reach for the simplest design that solves the immediate need: no helper, parameter, or config knob without a present-day caller, and no handling for states that cannot happen.

### Rationale

**Engineering principles (DRY/KISS/YAGNI).** Speculative flexibility is the dominant source of accidental complexity: an abstraction added for a caller that never arrives costs reading effort on every later edit while paying back nothing. DRY (don't repeat yourself) keeps one canonical home per behaviour; KISS (keep it simple, stupid) keeps the design no larger than the immediate need; YAGNI (you aren't gonna need it) defers anything the current change does not require.


### Mechanism

Reach for the simplest design that solves the immediate need. Three similar lines are better than a half-fitted helper — do not extract until a third caller actually appears. Do not add error handling, fallbacks, or validation for scenarios that cannot happen on the real call paths.


### Verification

A reviewer checks that each new helper, parameter, or config knob has a present-day caller; a helper introduced for one or two call sites, or for a use site that does not yet exist, is rejected. Defensive branches for impossible states are removed before merge.
<!-- END EAWF:managed id=engineering-principles -->
