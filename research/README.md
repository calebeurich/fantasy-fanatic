# Research

Where the project's intuitions go to be tested. The product's rule (CLAUDE.md) is
that every heuristic ships with its reasoning in LOGIC.md; this directory is the
step before that - the studies that decide whether an intuition becomes a constant
at all. Everything here is deterministic, offline, and free to re-run: no LLM
anywhere in the loop.

## The method, which is the point

The product's aging logic was built from inspecting real league distributions and a
week of the owner's eye-tests ("a 23-year-old RB should read green", "any QB is done
by 40", "old rookies are still ascending"). Each of those is a claim about how value
behaves - so each one is testable, and the discipline for testing them without
fooling ourselves:

1. **Point-in-time everything.** Values come from the DynastyProcess archive
   (`sources/dp_values.py` - the git history of their weekly values file, ~330
   snapshots since 2020). Player archetypes are computed from data available *at the
   time*, never career hindsight.
2. **Survivorship counted, not dropped.** Players who crater vanish from the priced
   list; excluding them makes the surviving freaks look like the curve. Exits count
   at the destination snapshot's floor value, and exit rates ship beside the medians.
3. **Dilution normalized.** DP values are ECR-derived and roughly pool-conserved, so
   every rookie class dilutes every incumbent and raw year-over-year ratios sit
   below 1.0 almost everywhere. The honest age effect is each age cell relative to
   its own position's baseline.
4. **Hypotheses pre-registered, one at a time, confirmed on held-out years.** An
   effect that only exists in the years it was found in is noise wearing a pattern's
   clothes.

## Study: age curves vs. realized market value (2026-08-14)

`age_curve_study.py` - 8,328 player-year observations, 22 quarterly cohorts, each
player's value compared one year later, 2020-2026.

**Finding 1 - the market runs an earlier clock.** Relative to position baseline,
market value peaks 2-4 years *before* the production breakpoint everywhere: WR value
turns at ~26-27 (the production curve says 29), TE ~26 (vs 30), QB ~31 (vs 34-37),
and RB value never plateaus at all - it descends steadily from age 23, steepening
near the production breakpoint of 27. The product's `runway` is a production clock;
the market runs a value clock that leads it. The gap between the two clocks IS the
sell window, which validates the sell-early doctrine while sharpening its wording:
on average the market is already discounting from the mid-20s, so "the market has
not discounted him yet" is a claim about specific outliers - exactly the players the
`cornerstone-priced` note exists to flag.

**Finding 2 - QBs peak late, in value too.** QB value growth is strongest at ages
25-30 (1.14-1.26x position baseline) and 21-23-year-old QBs gain no faster than
baseline. Supports the prime-entry-at-27 tuning, and cautions that a very young QB's
"ascending" tag is a production claim, not a market-safety claim.

**Finding 3 - the pre-registered experience hypothesis is REJECTED.** The idea:
same-age rookies behave more "ascending" than experienced players (the old-rookie
intuition). In 2020-22 it looked spectacular - rookie RBs/WRs aged 22-23 at 1.07-1.25
vs 0.60-0.79 for 3+year veterans of the same age. In the held-out 2023-25 era the
effect vanished or inverted (rookie RBs 0.53 vs vets 0.64). Either rookie-mania was
an era artifact or the market learned to price entry hype at the draft - both
readings mean it does not graduate into the product. The overcook guard did its job
on the first swing. The specifically-older-rookie case (24+, the RJ Harvey shape)
never reached sample size and remains open.

**What changed in the product because of this study:** nothing, yet - by design.
The findings are recorded here and in ROADMAP.md; constants graduate only when a
finding survives a re-run after more data accumulates, and the first candidate is
replacing the UI's judgment-based decline tails with measured post-breakpoint decay
rates.
