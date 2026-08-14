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

## Study: the two clocks, production vs value (2026-08-14)

`redraft_curves.py` - the dynasty cohorts joined to historical POSITIONAL redraft
ECR (FantasyPros via db_fpecr, weekly since 2020) by FantasyPros id. 3,301
dynasty-relevant observations, 2,951 with a production rank.

**Finding 4 - the clocks cross, and that IS the market's age discount.** Among
players still dynasty-relevant, positional production rank IMPROVES with age while
dynasty value falls: an RB at 23 ranks ~RB30 as a producer at 1.27x baseline value;
at 27-29 he ranks ~RB17 while priced at 0.37-0.66x. WRs: rank ~45 at 22 (1.26x) vs
~20 at 29 (0.72x). Selection is doing honest work here - the aged players still on
the list are precisely the survivors - but that is the point: the market's discount
on them is NOT about current production, which is the best on the board; it prices
the missing future seasons. This puts numbers behind the shipped persuasion-tier
sentence "the market discounts age the buyer isn't paying for", and behind green's
mirror: young players carry the highest values while ranking WORST as producers -
green is a price about the future, not the present.

**Finding 5 - "how chopped is red, really": the decline tails roughly measure out.**
Among players PAST the production breakpoint who still rank startable: RBs stay
57-66% startable with 62-83% one-year survival through +2 years past the breakpoint,
then 0% survival at 3+ - the display tail of 2 is about right. WRs hold ~70%
survival through +1, drop to 20% at +2 and 0% at 3+ - the tail of 3 is, if
anything, generous. QB numbers are small and scattered (the survivors keep
surviving); elite TEs that remain relevant at 3+ past the break are the Kelce
pattern - rank ~TE2, 89% survival, pure survivor selection at n=13. First-pass
support for graduating the RB/WR tails from eye-test to measured constants after
one more year of data.

## Study: archetypes and tiers (2026-08-14)

`archetype_tests.py` - role tags reconstructed from the season COMPLETED before each
cohort (no hindsight), contract state from the deal actually covering year t, joined
via the DP id crosswalk. Plus a within-position value-tier split (top-12 at time t).

**Finding 6 - pass-catching RB: confirmed, strongly, in both eras.** RBs at >=4
targets/game the prior season hold roughly DOUBLE the value of other RBs at every
age (25+: 0.52 vs 0.33 in 2020-22, 0.66 vs 0.38 in 2023-25). The +2 curve shift is
directionally right and possibly understated.

**Finding 7 - the rushing-QB discount is questioned, not yet overturned.** High-rush
QBs as a pool hold BETTER than other QBs in both eras (about 1.0 vs 0.5-0.8) - but
this test pools true rushing_qbs with dual threats (the Lamar shape our override
already exempts). Overturning the discount needs the elite-passer EPA split
reconstructed per season; until then the override stands with a flag on it.

**Finding 8 - contract year is a real value signal, era-stable.** Final-contract-year
players shed far more value than mid-contract players of the same position and age
band (RB 23-28: ~0.35 vs ~0.61 in BOTH eras; TE similar; WR strong in 2020-22,
weak in 2023-25; QB present at 29+). Mechanism is partly selection - teams extend
the players they believe in, so final-year status carries the team's own quality
verdict - but it is knowable at decision time either way. This REOPENS the parked
contract question with the polarity clarified: contract state earns its way in as a
VALUE-drift signal, not as an age-curve modifier.

**Finding 9 - tier conditions everything (the CeeDee Lamb objection).** Top-12
players at time t hold dramatically better than the rest of their position in every
age band, era-stable: elite QBs ~0.9-1.0 vs ~0.5, elite RBs ~2x their cohort, elite
WRs 28+ 0.68 vs 0.35. The all-timer-TE pattern (finding: top-3 TEs 29+ hold 0.72 vs
0.34) is one instance of this. Consequence already applied: the measured holding-
cost table (analysis/market_drift.py) is tier-conditioned, because a cohort median
without tier tells an elite player's owner a fate that mostly belongs to the
non-elite - the exact bleed into projections-arbitrage territory the owner flagged.

**What changed in the product so far:** one artifact - analysis/market_drift.py, the
tier-conditioned holding-cost table (measured constants with provenance; note-only
by doctrine, not yet wired into payloads). Everything else is recorded and waiting:
constants graduate when findings survive a re-run after more data accumulates.
Graduation queue, in order of evidence strength: the tier-conditioned drift note
into sell-window payloads; the contract-year signal as a second note; the decline
tails from eye-test to measured; the rushing-QB override review once the EPA split
is reconstructed.
